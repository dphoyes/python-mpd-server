import re
import anyio.abc
import contextlib
from . import errors


class WithAsyncExitStack(object):
    def __init__(self):
        super().__init__()
        self.__exit_stack = contextlib.AsyncExitStack()

    async def __aenter__(self):
        await self.__exit_stack.__aenter__()
        async with contextlib.AsyncExitStack() as stack:
            stack.push_async_exit(self)
            await self._init_exit_stack(self.__exit_stack)
            stack.pop_all()
        return self

    async def __aexit__(self, *args):
        await self.__exit_stack.__aexit__(*args)

    async def _init_exit_stack(self, stack):
        pass


class WithDaemonTasks(WithAsyncExitStack):
    async def _init_exit_stack(self, stack):
        await super()._init_exit_stack(stack)
        tasks = await stack.enter_async_context(anyio.create_task_group())
        stack.push_async_callback(tasks.cancel_scope.cancel)
        await self._spawn_daemon_tasks(tasks)

    async def _spawn_daemon_tasks(self, tasks):
        pass


class StreamBuffer:
    def __init__(self, stream: anyio.abc.ByteStream):
        self._stream = stream
        self._data = bytearray()

    async def send_all(self, data):
        return await self._stream.send(data)

    async def _fetch_more(self):
        try:
            new_bytes = await self._stream.receive(1 << 20)
        except anyio.EndOfStream:
            raise ConnectionAbortedError("Need more bytes, but we reached eof")
        if new_bytes:
            self._data += new_bytes
        else:
            raise ConnectionAbortedError("Need more bytes, but none were received")

    async def peek_at_most(self, count):
        if count == 0:
            return bytearray()
        while True:
            out = self._data[0:count]
            if out:
                return out
            await self._fetch_more()

    async def extract_at_most(self, count):
        out = await self.peek_at_most(count)
        del self._data[:len(out)]
        return out

    async def extract_until(self, needle):
        search_start = 0
        while True:
            offset = self._data.find(needle, search_start)
            if offset == -1:
                search_start = max(0, len(self._data) - len(needle) + 1)
                await self._fetch_more()
            else:
                new_start = offset + len(needle)
                out = self._data[:new_start]
                del self._data[:new_start]
                return out

    async def forward_mpd_response(self):
        needle = re.compile(rb"(?<=^)(?:(OK)\n|(ACK) |(binary):)", re.MULTILINE)
        max_match_len = 8
        while True:
            match = needle.search(self._data)
            if match is None:
                n_extractable = len(self._data) - max_match_len + 1
                if n_extractable > 0:
                    yield await self.extract_at_most(n_extractable)
                await self._fetch_more()
            else:
                ok, ack, binary = match.groups()
                if ok:
                    yield self._data[0:match.start()]
                    del self._data[:match.end()]
                    return
                elif ack:
                    yield self._data[0:match.start()]
                    del self._data[:match.start()]
                    out = await self.extract_until(b"\n")
                    parsed = re.match(r"ACK \[[^\[\]]+\] {([^{}]+)}(.*)", out.decode('utf-8'))
                    if parsed:
                        raise errors.MpdCommandError(command=parsed.group(1), msg=parsed.group(2))
                    else:
                        raise errors.MpdCommandError(command="?", msg="Received unparseable error from other MPD server")
                elif binary:
                    yield self._data[0:match.end()]
                    del self._data[:match.end()]
                    out = await self.extract_until(b"\n")
                    yield out
                    n_bytes = int(out)+1
                    while n_bytes:
                        out = await self.extract_at_most(n_bytes)
                        yield out
                        n_bytes -= len(out)
                else:
                    raise AssertionError("Shouldn't reach this line")
