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


class MpdClient(WithDaemonTasks):
    def __init__(self, host, port):
        super().__init__()
        self.host, self.port = host, port
        self.command_queue = anyio.create_queue(1)
        self.wake = anyio.create_event()
        self.idle_consumers = set()

    async def _spawn_daemon_tasks(self, tasks):
        await tasks.spawn(self._run)

    async def _run(self):
        async with await anyio.connect_tcp(self.host, self.port) as stream:
            welcome = (await stream.receive_until(b'\n', 1024)).decode('utf-8').strip()
            logger.debug("Connected to MPD server at {}:{}: {}", self.host, self.port, welcome)
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
                    logger.debug("Entering idle")
                    await stream.send_all("idle {}\n".format(' '.join(subsystems)).encode('utf-8'))
                    async with anyio.create_task_group() as tasks:
                        async def handle_wake_from_idle():
                            await self.wake.wait()
                            await stream.send_all(b"noidle\n")
                        await tasks.spawn(handle_wake_from_idle)
                        async for line in self._iter_response(stream):
                            prefix, subsystem = line.split(b':')
                            assert prefix == b"changed"
                            subsystem = subsystem.strip().decode('utf-8')
                            for c in self.idle_consumers:
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

    async def idle(self, *subsystems):
        consumer = IdleConsumer(subsystems)
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
            yield result + b'\n'
