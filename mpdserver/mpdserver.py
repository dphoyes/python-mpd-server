# Pimp is a highly interactive music player.
# Copyright (C) 2011 kedals0@gmail.com

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""To launch a mpd server, use :class:`MpdServer` or
:class:`MpdServerDaemon` classes.

:class:`MpdClientHandler` manages a client connection. It parses
client requests and executes corresponding commands. Supported MPD
commands are specified with method
:func:`MpdClientHandler.RegisterCommand`. Skeletons commands are
provided by module :mod:`command_skel`. They can easily be override.

A client connection can begin by a password command. In this case, a
:class:`Frontend` is created by client password command. This object
is provided to commands treated during this session.
"""
from __future__ import absolute_import, annotations

import anyio
import threading
import operator
import itertools
import sys
import contextlib
#from pimp.core.playlist import *
#from pimp.core.player import *
#import pimp.core.db

from .command_base import *
from .command_skel import *
from .errors import *
from .utils import StreamBuffer, _await_if_awaitable
from .logging import Logger

logger = Logger(__name__)


class Frontend(object):
    """ To define a frontend. To specify a frontend and user , use MPD
    password command with format 'frontend:user'. If password command
    is not used, frontend is set to 'unknown' and user to 'default'."""
    _DefaultUsername='default'
    username=_DefaultUsername
    _DefaultFrontend='unknown'
    frontend=_DefaultFrontend

    def set(self,frontendPassword):
        """ Password from frontend contains the name of frontend (mpc,
        sonata, ...) and a user name. The format is 'frontend:user'"""
        (self.frontend,t,self.username)=frontendPassword.partition(':')
        if self.frontend == '' or self.username == '':
            logger.warning("Wrong password request '%s'" % frontendPassword)

            raise PasswordError(frontendPassword,"frontend:user")
        return True
    def get(self):
        """ Get frontend information. Return a dict."""
        return {'username':self.username,'frontend':self.frontend}
    def getUsername(self):
        return self.username
    @classmethod
    def GetDefaultUsername(cls):
        return cls._DefaultUsername


class IdleState(object):
    SUBSYSTEM_NAMES = (
        "database",
        "update",
        "stored_playlist",
        "playlist",
        "player",
        "mixer",
        "output",
        "options",
        "partition",
        "sticker",
        "subscription",
        "message",
    )

    def __init__(self):
        self.events = {s: anyio.Event() for s in self.SUBSYSTEM_NAMES}

    def notify(self, subsystem):
        logger.debug("Idle notify: {}", subsystem)
        self.events[subsystem].set()

    async def wait(self, subsystems=()):
        if subsystems:
            try:
                events_to_watch = [self.events[s] for s in subsystems]
            except KeyError as e:
                raise InvalidArgumentValue("Unrecognized idle event", e.args[0])
        else:
            events_to_watch = self.events.values()

        logger.debug("Going into idle ({})", subsystems)
        async with anyio.create_task_group() as tg:
            async def wait_for(e):
                await e.wait()
                tg.cancel_scope.cancel()
            for e in events_to_watch:
                tg.start_soon(wait_for, e)
        logger.debug("Coming out of idle")

        changed_subsystems = []
        for name, event in self.events.items():
            if event.is_set():
                changed_subsystems.append(name)
                self.events[name] = anyio.Event()
        logger.debug("Changed subsystems: {}", changed_subsystems)
        return changed_subsystems

    async def wait_or_noidle(self, subsystems, client_stream):
        changed_subsystems = []

        async with anyio.create_task_group() as tg:
            async def wait_for_change():
                changed_subsystems[:] = await self.wait(subsystems)
                tg.cancel_scope.cancel()
            tg.start_soon(wait_for_change)

            async def wait_for_noidle():
                line = await client_stream.extract_until(b"\n")
                if line != b"noidle\n":
                    raise MpdCommandErrorCustom("The only valid command in idle state is noidle")
                logger.debug("Received noidle")
                tg.cancel_scope.cancel()
            tg.start_soon(wait_for_noidle)

        return changed_subsystems


class MpdClientHandlerBase(object):
    def __init_subclass__(cls):
        super().__init_subclass__()
        cls.__SupportedCommands = {
            'currentsong'      :{'class':CurrentSong,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':["sonata"]},
            'outputs'          :{'class':Outputs,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':["gmpc"]},
            'enableoutput'     :{'class':None,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':["gmpc"]},
            'disableoutput'    :{'class':None,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':["gmpc"]},
            'toggleoutput'     :{'class':None,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':["gmpc"]},
            'outputset'        :{'class':None,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':["gmpc"]},
            'config'           :{'class':None,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':["gmpc"]},
            'decoders'           :{'class':None,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':["gmpc"]},
            'status'           :{'class':Status,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':["sonata"]},
            'stats'            :{'class':Stats,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'notcommands'      :{'class':NotCommands,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':["gmpc"]},
            'commands'         :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'lsinfo'           :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'tagtypes'         :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'playlist'         :{'class':None,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'playlistinfo'     :{'class':PlaylistInfo,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'playlistid'       :{'class':PlaylistId,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'playlistfind'     :{'class':None,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'playlistsearch'   :{'class':None,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'listplaylistinfo' :{'class':ListPlaylistInfo,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'plchanges'        :{'class':PlChanges,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':["sonata"]},
            'plchangesposid'   :{'class':PlChangesPosId,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'moveid'           :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'move'             :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'delete'           :{'class':Delete,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'deleteid'         :{'class':DeleteId,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'prio'             :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'prioid'           :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'rangeid'          :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'add'              :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'addid'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'swap'             :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'swapid'           :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'addtagid'         :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'cleartagid'       :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'getfingerprint'   :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'count'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'list'             :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'listall'          :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'listfiles'        :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'readcomments'     :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'find'             :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'findadd'          :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'playid'           :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'play'             :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'password'         :{'class':Password,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':["all"]},
            'clear'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'stop'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'seek'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'seekcur'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'seekid'          :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'pause'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'next'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'previous'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'random'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'shuffle'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'consume'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'repeat'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'single'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'crossfade'         :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'mixrampdb'         :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'mixrampdelay'         :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'listplaylist'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'listplaylists'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'load'            :{'class':None,'users':[],'group':'write','mpdVersion':"0.12",'neededBy':None},
            'save'            :{'class':None,'users':[],'group':'write','mpdVersion':"0.12",'neededBy':None},
            'albumart'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'readpicture'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'search'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'searchadd'         :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'searchaddpl'       :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'update'       :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'rescan'       :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'rm'            :{'class':None,'users':[],'group':'write','mpdVersion':"0.12",'neededBy':None},
            'rename'        : {'class': None, 'users': [], 'group': 'write', 'mpdVersion': "0.12", 'neededBy': None},
            'playlistadd'      :{'class':None,'users':[],'group':'write','mpdVersion':"0.12",'neededBy':None},
            'playlistclear'    :{'class':None,'users':[],'group':'write','mpdVersion':"0.12",'neededBy':None},
            'playlistdelete'   :{'class':None,'users':[],'group':'write','mpdVersion':"0.12",'neededBy':None},
            'playlistmove'     :{'class':None,'users':[],'group':'write','mpdVersion':"0.12",'neededBy':None},
            'sticker'          :{'class':None,'users':[],'group':'write','mpdVersion':"0.12",'neededBy':None},
            'setvol'           :{'class':None,'users':[],'group':'control','mpdVersion':"0.12",'neededBy':None},
            'volume'           :{'class':None,'users':[],'group':'control','mpdVersion':"0.12",'neededBy':None},
            'channels'         :{'class':None,'users':[],'group':'control','mpdVersion':"0.12",'neededBy':None},
            'mount'         :{'class':None,'users':[],'group':'control','mpdVersion':"0.12",'neededBy':None},
            'unmount'         :{'class':None,'users':[],'group':'control','mpdVersion':"0.12",'neededBy':None},
            'listmounts'         :{'class':None,'users':[],'group':'control','mpdVersion':"0.12",'neededBy':None},
            'listneighbors'         :{'class':None,'users':[],'group':'control','mpdVersion':"0.12",'neededBy':None},
            'ping'         :{'class':None,'users':[],'group':'control','mpdVersion':"0.12",'neededBy':None},
            'partition'         :{'class':None,'users':[],'group':'control','mpdVersion':"0.12",'neededBy':None},
            'listpartitions'         :{'class':None,'users':[],'group':'control','mpdVersion':"0.12",'neededBy':None},
            'newpartition'         :{'class':None,'users':[],'group':'control','mpdVersion':"0.12",'neededBy':None},
            'delpartition'         :{'class':None,'users':[],'group':'control','mpdVersion':"0.12",'neededBy':None},
            'moveoutput'         :{'class':None,'users':[],'group':'control','mpdVersion':"0.12",'neededBy':None},
            'subscribe'         :{'class':None,'users':[],'group':'control','mpdVersion':"0.12",'neededBy':None},
            'unsubscribe'         :{'class':None,'users':[],'group':'control','mpdVersion':"0.12",'neededBy':None},
            'readmessages'         :{'class':None,'users':[],'group':'control','mpdVersion':"0.12",'neededBy':None},
            'sendmessage'         :{'class':None,'users':[],'group':'control','mpdVersion':"0.12",'neededBy':None},
            'urlhandlers'           :{'class':None,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'listallinfo'           :{'class':None,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'replay_gain_mode'           :{'class':None,'users':['default'],'group':'write','mpdVersion':"0.12",'neededBy':None},
            'replay_gain_status'           :{'class':None,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'idle'             :{'class':Idle,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'noidle'           :{'class':NoIdle,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
        }

        def RegisterCommand(cls_cmd, users=['default']):
            """ Register a command. Make this command supported by a mpd
            server which use this request handler class. cls_cmd is a
            class which inherits from :class:`command_base.Command`."""
            cls.__SupportedCommands[cls_cmd.GetCommandName()]['class']=cls_cmd
            for a in users : cls.__SupportedCommands[cls_cmd.GetCommandName()]['users'].append(a)
            return cls_cmd
        cls.define_commands(RegisterCommand)

        def UserPermissionsCommand(user, commandName=None, group=None):
            """ Add permissions for user 'user'. If commandName is not specified, group should be specified. """
            if commandName != None:
                 cls.__SupportedCommands[commandNames]['users'].append(user)
            elif group != None:
                for c in cls.__SupportedCommands.values():
                    if c['group']==group:
                        c['users'].append(user)
            else:
                raise TypeError
        cls.define_command_user_permissions(UserPermissionsCommand)

        for u in cls.define_super_users():
            for cmd in cls.__SupportedCommands.values():
                cmd['users'].append(u)

    def _make_command_object(self, command_listNum, raw_line):
        """ To get a command class to execute on received command
        string. This method raise supported command errors."""
        commandName = raw_line.split(maxsplit=1)[0].decode('utf8')
        try:
            if commandName not in self.__SupportedCommands:
                logger.warning("Command '%s' is not a MPD command!" % commandName)
                raise CommandNotMPDCommand()
            elif self.__SupportedCommands[commandName]['class'] == None:
                if self.__SupportedCommands[commandName]['neededBy'] != None:
                    logger.critical("Command '%s' is needed for client(s) %s" % (commandName," ".join(self.__SupportedCommands[commandName]['neededBy'])))
                logger.warning("Command '%s' is not supported!" % commandName)
                raise CommandNotSupported()
            elif not (Frontend.GetDefaultUsername() in self.__SupportedCommands[commandName]['users']
                      or self.frontend.getUsername() in self.__SupportedCommands[commandName]['users']):
                raise UserNotAllowed(self.frontend.getUsername())
            else :
                cls = self.__SupportedCommands[commandName]['class']
                return cls(raw_line, client=self)
        except MpdCommandError as e:
            e.set_current_command(commandName)
            e.command_listNum += command_listNum
            raise

    @classmethod
    def define_commands(cls, register):
        pass

    @classmethod
    def define_command_user_permissions(cls, register):
        pass

    @classmethod
    def define_super_users(cls):
        return ["default"]

    @classmethod
    def SupportedCommand(cls):
        """Return a list of command and allowed users."""
        return ["%s\t\t%s"%(k,v['users']) for (k,v) in cls.__SupportedCommands.items() if v['class']!=None ]


class MpdClientHandler(MpdClientHandlerBase):
    """ Manage the connection from a mpd client. Each client
    connection instances this object."""

    def __init__(self, stream, server):
        super().__init__()
        self.stream = StreamBuffer(stream)
        self.server = server
        self.frontend = Frontend()
        self.idle = IdleState()
        self.partition = None
        logger.debug( "Client connected (%s)" % threading.currentThread().getName())

    async def run(self):
        """Handle connection with mpd client. It gets client command,
        execute it and send a respond."""
        if self.partition is None:
            self.partition = self.server.partitions["default"]

        await self.stream.send_all("OK MPD 0.22.0\n".encode('utf-8'))

        re_command_list_begin = re.compile(b"^command_list_(ok_)?begin\n$")
        re_line = re.compile(b"[^\n]+\n")
        cmdlist_handler_getter = operator.attrgetter("CommandListHandler")

        while True:
            try:
                with anyio.fail_after(10):
                    raw_line = await self.stream.extract_until(b"\n")
                cmdlist_match = re_command_list_begin.match(raw_line)
                if cmdlist_match is None:
                    list_ok = False
                    cmds = [self._make_command_object(0, raw_line)]
                else:
                    list_ok = cmdlist_match.group(1) is not None
                    with anyio.fail_after(20):
                        rest = await self.stream.extract_until(b"command_list_end\n")
                    lines = re_line.findall(rest)
                    del lines[-1]
                    cmds = [self._make_command_object(n, raw_line) for n, raw_line in enumerate(lines)]
            except MpdCommandError as e:
                logger.info("Command Error: {}", e.toMpdMsg())
                await self.stream.send_all(e.toMpdMsg().encode('utf-8'))
                continue
            except TimeoutError:
                logger.debug("Client connection timed out")
                return

            logger.info("Received MPD commands: {}", [c.raw_command for c in cmds])
            command_listNum = 0
            try:
                for list_handler_cls, group in itertools.groupby(cmds, key=cmdlist_handler_getter):
                    group = list(group)
                    len_group = len(group)
                    if len_group <= 1:
                        list_handler_cls = CommandListDefault
                    async with contextlib.aclosing(list_handler_cls(group, list_ok=list_ok, client=self).run()) as iter_chunks:
                        async for chunk in iter_chunks:
                            await self.stream.send_all(chunk)
                    command_listNum += len_group
            except MpdCommandError as e:
                e.command_listNum += command_listNum
                logger.info("Command Error: {}", e.toMpdMsg())
                await self.stream.send_all(e.toMpdMsg().encode('utf-8'))
            else:
                if any(c.respond for c in cmds):
                    logger.debug("Response: OK\n")
                    await self.stream.send_all(b"OK\n")


class MpdPartition(object):
    name: str
    server: MpdServer

    def __init__(self, name: str, server: MpdServer):
        super().__init__()
        self.name = name
        self.server = server
        self.playlist = server.Playlist()
        self.__delete_lock = anyio.Lock()
        self.__delete_request = anyio.Event()
        self.__delete_status_sender = None

    @property
    def clients(self):
        return {c for c in self.server.clients if c.partition is self}

    async def run(self, *, task_status=anyio.TASK_STATUS_IGNORED):
        task_status.started()
        await self.__delete_request.wait()
        self.__delete_request = anyio.Event()

    async def _run_and_handle_deletion(self, *, task_status=anyio.TASK_STATUS_IGNORED):
        while True:
            try:
                await self.run(task_status=task_status)
            except Exception as e:
                if self.__delete_status_sender is None:
                    raise
                else:
                    await self.__delete_status_sender.send(e)
                    task_status = anyio.TASK_STATUS_IGNORED
            else:
                await self.__delete_status_sender.send(None)
                return

    async def _delete(self):
        async with self.__delete_lock:
            if self.__delete_status_sender is None:
                self.__delete_status_sender, delete_status_receiver = anyio.create_memory_object_stream()
                self.__delete_request.set()
                result = await delete_status_receiver.receive()
                if isinstance(result, Exception):
                    self.__delete_status_sender = None
                    raise result
                assert result is None

    def notify_idle(self, subsystem):
        for c in self.clients:
            c.idle.notify(subsystem)


class MpdPartitionSet:
    def __init__(self, server, Partition):
        super().__init__()
        self.server = server
        self.Partition = Partition
        self.partitions = dict()
        self.tasks = None
        self.__lock = anyio.Lock()
        self.__cm = self.__ContextManager()

    @contextlib.asynccontextmanager
    async def __ContextManager(self):
        async with anyio.create_task_group() as self.tasks:
            yield self
            tasks.cancel_scope.cancel()

    async def __aenter__(self):
        return await self.__cm.__aenter__()

    async def __aexit__(self, *args):
        return await self.__cm.__aexit__(*args)

    def __len__(self):
        return len(self.partitions)

    def __iter__(self):
        return iter(self.partitions.values())

    def __getitem__(self, name):
        return self.partitions[name]

    async def new(self, name):
        async with self.__lock:
            if name in self.partitions:
                raise KeyError("Partition with that name already exists")
            p = self.Partition(name=name, server=self.server)
            await self.tasks.start(p._run_and_handle_deletion)
            self.partitions[name] = p
            self.server.notify_idle("partition")
            return p

    async def delete(self, name):
        async with self.__lock:
            p = self.partitions[name]
            del self.partitions[name]
            self.server.notify_idle("partition")
            try:
                await p._delete()
            except Exception:
                self.partitions[name] = p
                self.server.notify_idle("partition")
                raise


class MpdServer(object):
    """ Create a MPD server. By default, a request is treated via
    :class:`MpdClientHandler` class but you can specify an alternative
    request class with ClientHandler argument."""

    def __init__(self, ClientHandler=MpdClientHandler, Playlist=MpdPlaylist, Partition=MpdPartition):
        self.ClientHandler = ClientHandler
        self.Playlist = Playlist
        self.partitions = MpdPartitionSet(server=self, Partition=Partition)
        self.clients = set()

    async def run(self):
        """Run MPD server in a coroutine"""
        async with self.partitions:
            await self.init_partitions()
            await self.run_all_listeners()

    async def init_partitions(self):
        await self.partitions.new("default")

    async def run_all_listeners(self, host=None, port=6600):
        logger.info("Mpd Server is listening on port " + str(port))
        await self.run_listener(anyio.create_tcp_listener(local_host=host, local_port=port))

    async def run_listener(self, listener, **client_kwargs):
        async def handle_client(client):
            try:
                async with client:
                    handler = self.ClientHandler(client, self, **client_kwargs)
                    self.clients.add(handler)
                    try:
                        await handler.run()
                    finally:
                        self.clients.remove(handler)
                        logger.debug("Client connection closed")
            except (ConnectionError, anyio.BrokenResourceError):
                pass
        listener = await _await_if_awaitable(listener)
        await listener.serve(handle_client)

    def notify_idle(self, subsystem):
        for c in self.clients:
            c.idle.notify(subsystem)
