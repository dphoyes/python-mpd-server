import anyio
import re

from .errors import Ack, MpdCommandError, parse_ack_line
from .utils import WithDaemonTasks, StreamBuffer
from .logging import Logger

logger = Logger(__name__)


def quote(text):
    if isinstance(text, str):
        return "".join(('"', text.replace("\\", "\\\\").replace('"', '\\"'), '"'))
    elif isinstance(text, bytes):
        return b"".join((b'"', text.replace(b"\\", b"\\\\").replace(b'"', b'\\"'), b'"'))
    else:
        raise TypeError


class IdleConsumer:
    def __init__(self, x):
        self.subsystems = x
        self.result_q = anyio.streams.stapled.StapledObjectStream(*anyio.create_memory_object_stream(1))

class CommandQueueItem:
    def __init__(self, x, forwarding_mode):
        self.command = x
        self.forwarding_mode = forwarding_mode
        self.result_q = anyio.streams.stapled.StapledObjectStream(*anyio.create_memory_object_stream(1))


async def parse_raw_key_value_pairs(response_lines):
    async for line in response_lines:
        k, v = line.split(b':', maxsplit=1)
        yield bytes(k), bytes(v.strip())


async def parse_raw_objects(response_lines, delimiter):
    if isinstance(delimiter, bytes):
        delimiter = delimiter,
    obj = {}
    async for k, v in parse_raw_key_value_pairs(response_lines):
        if k in delimiter:
            if obj:
                yield obj
            obj = {k: v}
        elif k in obj:
            obj_k = obj[k]
            if not isinstance(obj_k, list):
                obj[k] = obj_k = [obj_k]
            obj_k.append(v)
        else:
            obj[k] = v
    if obj:
        yield obj


async def parse_raw_object(response_lines):
    ret = [x async for x in parse_raw_objects(response_lines, delimiter=())]
    len_ret = len(ret)
    if len_ret == 0:
        return {}
    elif len_ret > 1:
        raise AssertionError("Multiple objects returned")
    else:
        return ret[0]


async def parse_list(response_lines, key, ignore_other_keys=False):
    async for k, v in parse_raw_key_value_pairs(response_lines):
        if k == key:
            yield v.decode('utf-8')
        elif not ignore_other_keys:
            raise AssertionError(f"Unexpected key: {k}")


class MpdClient(WithDaemonTasks):
    def __init__(self, host, port=6600, default_partition=None):
        super().__init__()
        self.host, self.port, self.default_partition = host, port, default_partition
        self.command_queue = anyio.streams.stapled.StapledObjectStream(
            *anyio.create_memory_object_stream(1, item_type=CommandQueueItem)
        )
        self.wake = anyio.Event()
        self.idle_consumers = set()

    async def _start_daemon_tasks(self, tasks):
        await tasks.start(self._run)

    def _connect(self):
        if self.host.startswith('/'):
            return anyio.connect_unix(self.host)
        else:
            return anyio.connect_tcp(self.host, self.port)

    async def _run(self, *, task_status=anyio.TASK_STATUS_IGNORED):
        async with await self._connect() as stream:
            stream = StreamBuffer(stream)
            welcome = (await stream.extract_until(b'\n')).decode('utf-8').strip()
            logger.debug("Connected to MPD server at {}:{}: {}", self.host, self.port, welcome)

            if self.default_partition is not None:
                for _ in range(3):
                    await stream.send_all(f"partition {quote(self.default_partition)}\n".encode('utf8'))
                    try:
                        async for line in self._iter_response(stream):
                            raise RuntimeError("Unexpected response")
                        logger.debug("Switched to partition {}", self.default_partition)
                        break
                    except MpdCommandError as e:
                        if e.error != Ack.ERROR_NO_EXIST:
                            raise
                    await stream.send_all(f"newpartition {quote(self.default_partition)}\n".encode('utf8'))
                    try:
                        async for line in self._iter_response(stream):
                            raise RuntimeError("Unexpected response")
                    except MpdCommandError as e:
                        if e.error != Ack.ERROR_EXIST:
                            raise
                else:
                    raise RuntimeError(f"Unable to change partition to {self.default_partition}")

            task_status.started()

            while True:
                cmd = None
                with anyio.move_on_after(0.1):
                    cmd = await self.command_queue.receive()
                if cmd is not None:
                    logger.debug("Writing command: {}", cmd.command)
                    await stream.send_all(cmd.command)
                    if not cmd.command.endswith(b'\n'):
                        await stream.send_all(b'\n')
                    try:
                        if cmd.forwarding_mode:
                            async for chunk in stream.forward_mpd_response():
                                await cmd.result_q.send(chunk)
                        else:
                            async for line in self._iter_response(stream):
                                await cmd.result_q.send(line)
                    except MpdCommandError as e:
                        await cmd.result_q.send(e)
                    await cmd.result_q.send(None)
                else:
                    if not self.idle_consumers:
                        subsystems = "database",
                    elif any(not c.subsystems for c in self.idle_consumers):
                        subsystems = ()
                    else:
                        subsystems = {s for c in self.idle_consumers for s in c.subsystems}
                    self.wake = anyio.Event()
                    logger.debug("Entering idle: {}", subsystems)
                    await stream.send_all("idle {}\n".format(' '.join(subsystems)).encode('utf-8'))
                    async with anyio.create_task_group() as tasks:
                        async def handle_wake_from_idle():
                            await self.wake.wait()
                            await stream.send_all(b"noidle\n")
                        tasks.start_soon(handle_wake_from_idle)
                        changed = [s async for s in parse_list(self._iter_response(stream), b"changed")]
                        if changed:
                            for c in self.idle_consumers:
                                changed_for_consumer = [s for s in changed if s in c.subsystems] if c.subsystems else changed
                                if changed_for_consumer:
                                    await c.result_q.send(changed_for_consumer)
                        tasks.cancel_scope.cancel()
                    self.wake.set()

    @staticmethod
    async def _iter_response(stream):
        while True:
            line = await stream.extract_until(b'\n')
            logger.debug("_iter_response: Got line {}", line)
            if line == b"OK\n":
                return
            if line.startswith(b"ACK "):
                raise parse_ack_line(line.decode('utf-8'))
            yield line

    async def idle(self, *subsystems, initial_trigger=False, split=True):
        if initial_trigger:
            if split:
                for x in subsystems:
                    yield x
            else:
                yield tuple(subsystems)
        consumer = IdleConsumer(frozenset(subsystems))
        try:
            self.idle_consumers.add(consumer)
            self.wake.set()
            while True:
                changed = await consumer.result_q.receive()
                logger.debug("Idle got {}", changed)
                if split:
                    for s in changed:
                        yield s
                else:
                    yield tuple(changed)
        finally:
            self.idle_consumers.remove(consumer)

    async def raw_command(self, line, forwarding_mode=False):
        item = CommandQueueItem(line, forwarding_mode)
        logger.debug("Sending command to queue: {}", line)
        await self.command_queue.send(item)
        self.wake.set()
        while True:
            result = await item.result_q.receive()
            if result is None:
                return
            if isinstance(result, Exception):
                raise result
            yield result

    async def command_returning_list(self, cmd, key):
        return [x async for x in parse_list(self.raw_command(cmd), key)]

    async def command_returning_raw_objects(self, cmd, *args, **kwargs):
        return [x async for x in parse_raw_objects(self.raw_command(cmd), *args, **kwargs)]

    async def command_returning_raw_object(self, cmd):
        return await parse_raw_object(self.raw_command(cmd))

    async def command_returning_nothing(self, cmd):
        response = [x async for x in self.raw_command(cmd)]
        assert len(response) == 0

    async def listpartitions(self):
        return await self.command_returning_list(b"listpartitions", b"partition")

    async def partition(self, name):
        assert isinstance(name, str)
        return await self.command_returning_nothing(f"partition {quote(name)}".encode('utf8'))

    async def delpartition(self, name):
        assert isinstance(name, str)
        return await self.command_returning_nothing(f"delpartition {quote(name)}".encode('utf8'))
