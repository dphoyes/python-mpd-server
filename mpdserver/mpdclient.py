import anyio
import re

from .errors import MpdCommandError
from .utils import WithDaemonTasks
from .logging import Logger

logger = Logger(__name__)


class IdleConsumer:
    def __init__(self, x):
        self.subsystems = x
        self.result_q = anyio.create_queue(1)

class CommandQueueItem:
    def __init__(self, x):
        self.command = x
        self.result_q = anyio.create_queue(1)


async def parse_raw_key_value_pairs(response_lines):
    async for line in response_lines:
        k, v = line.split(b':', maxsplit=1)
        yield k, v.strip()


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
    def __init__(self, host, port, default_partition=None):
        super().__init__()
        self.host, self.port, self.default_partition = host, port, default_partition
        self.command_queue = anyio.create_queue(1)
        self.wake = anyio.create_event()
        self.idle_consumers = set()

    async def _spawn_daemon_tasks(self, tasks):
        await tasks.spawn(self._run)

    async def _run(self):
        async with await anyio.connect_tcp(self.host, self.port) as stream:
            welcome = (await stream.receive_until(b'\n', 1024)).decode('utf-8').strip()
            logger.debug("Connected to MPD server at {}:{}: {}", self.host, self.port, welcome)

            if self.default_partition is not None:
                for _ in range(3):
                    await stream.send_all(f"partition {self.default_partition}\n".encode('utf8'))
                    try:
                        async for line in self._iter_response(stream):
                            raise RuntimeError("Unexpected response")
                        logger.debug("Switched to partition {}", self.default_partition)
                        break
                    except MpdCommandError:
                        pass
                    await stream.send_all(f"newpartition {self.default_partition}\n".encode('utf8'))
                    async for line in self._iter_response(stream):
                        raise RuntimeError("Unexpected response")
                else:
                    raise RuntimeError(f"Unable to change partition to {self.default_partition}")

            while True:
                cmd = None
                async with anyio.move_on_after(0.1):
                    cmd = await self.command_queue.get()
                if cmd is not None:
                    logger.debug("Writing command: {}", cmd.command)
                    await stream.send_all(cmd.command)
                    if not cmd.command.endswith(b'\n'):
                        await stream.send_all(b'\n')
                    try:
                        async for line in self._iter_response(stream):
                            await cmd.result_q.put(line)
                    except MpdCommandError as e:
                        await cmd.result_q.put(e)
                    await cmd.result_q.put(None)
                else:
                    if not self.idle_consumers:
                        subsystems = "database",
                    elif any(not c.subsystems for c in self.idle_consumers):
                        subsystems = ()
                    else:
                        subsystems = {s for c in self.idle_consumers for s in c.subsystems}
                    self.wake.clear()
                    logger.debug("Entering idle: {}", subsystems)
                    await stream.send_all("idle {}\n".format(' '.join(subsystems)).encode('utf-8'))
                    async with anyio.create_task_group() as tasks:
                        async def handle_wake_from_idle():
                            await self.wake.wait()
                            await stream.send_all(b"noidle\n")
                        await tasks.spawn(handle_wake_from_idle)
                        async for subsystem in parse_list(self._iter_response(stream), b"changed"):
                            for c in self.idle_consumers:
                                if not c.subsystems or subsystem in c.subsystems:
                                    await c.result_q.put(subsystem)
                        await tasks.cancel_scope.cancel()
                    await self.wake.set()

    @staticmethod
    async def _iter_response(stream):
        async for line in stream.receive_delimited_chunks(b'\n', 1024):
            logger.debug("_iter_response: Got line {}", line)
            if line == b"OK":
                return
            if line.startswith(b"ACK "):
                parsed = re.match(r"ACK \[[^\[\]]+\] {([^{}]+)}(.*)", line.decode('utf-8'))
                if parsed:
                    raise MpdCommandError(command=parsed.group(1), msg=parsed.group(2))
                else:
                    raise MpdCommandError(command="?", msg="Received unparseable error from other MPD server")
            yield line

    async def idle(self, *subsystems, initial_trigger):
        if initial_trigger:
            for x in subsystems:
                yield x
        consumer = IdleConsumer(frozenset(subsystems))
        try:
            self.idle_consumers.add(consumer)
            await self.wake.set()
            while True:
                x = await consumer.result_q.get()
                logger.debug("Idle got {}", x)
                yield x
        finally:
            self.idle_consumers.remove(consumer)

    async def raw_command(self, line):
        item = CommandQueueItem(line)
        logger.debug("Sending command to queue: {}", line)
        await self.command_queue.put(item)
        await self.wake.set()
        while True:
            result = await item.result_q.get()
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
        if isinstance(name, str):
            name = name.encode("utf8")
        return await self.command_returning_nothing(b"partition " + name)
