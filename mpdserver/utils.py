import anyio
import contextlib


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
