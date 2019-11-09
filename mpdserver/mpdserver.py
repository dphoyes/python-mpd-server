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
from __future__ import absolute_import

import asyncio
import time
import re
import threading
import sys
#from pimp.core.playlist import *
#from pimp.core.player import *
#import pimp.core.db
import logging

from .command_base import *
from .command_skel import *

logger=logging
#logger.basicConfig(level=logging.INFO)
logger.basicConfig(level=logging.DEBUG)


##################################
### Mpd supported return types ###
##################################
class MpdErrorMsgFormat(Exception):pass
class MpdCommandError(Exception):
    def __init__(self,msg="Unknown error",command="command is not specified"):
        self.command=command
        self.msg=msg
    def toMpdMsg(self):
        return "ACK [error@command_listNum] {%s} %s\n" % (self.command,self.msg)
class CommandNotSupported(MpdCommandError):
    def __init__(self,commandName):
        self.commandName=commandName
    def toMpdMsg(self):
        return "ACK [error@command_listNum] {%s} Command '%s' not supported\n" % (self.commandName,self.commandName)
class CommandNotMPDCommand(MpdCommandError):
    def __init__(self,commandName):
        self.commandName=commandName
    def toMpdMsg(self):
        return "ACK [error@command_listNum] {%s} Command '%s' is not a MPD command\n" % (self.commandName,self.commandName)
class CommandNotImplemented(MpdCommandError):
    def __init__(self,commandName,message=""):
        self.commandName=commandName
        self.message=message
    def toMpdMsg(self):
        return "ACK [error@command_listNum] {%s} Command '%s' is not implemented (%s)\n" % (self.commandName,self.commandName,self.message)
class UserNotAllowed(MpdCommandError):
    def __init__(self,commandName,userName):
        self.commandName=commandName
        self.userName=userName
    def toMpdMsg(self):
        return "ACK [error@command_listNum] {%s} User '%s' is not allowed to execute command %s\n" % (self.commandName,self.userName,self.commandName)
class PasswordError(MpdCommandError):
    def __init__(self,pwd,format):
        self.pwd=pwd
        self.format=format
    def toMpdMsg(self):
        return "ACK [error@command_listNum] {password} Password '%s' is not allowed a valid password. You should use a password such as '%s'\n" % (self.pwd,self.format)

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
        loop = asyncio.get_running_loop()
        self.futures = {s: loop.create_future() for s in self.SUBSYSTEM_NAMES}

    def notify(self, subsystem):
        future = self.futures[subsystem]
        if not future.done():
            future.set_result(subsystem)

    async def wait(self, subsystems=()):
        if subsystems:
            futures = []
            for s in subsystems:
                try:
                    futures.append(self.futures[s])
                except KeyError:
                    raise MpdCommandError(command="idle", msg="Invalid subsystem '{}'".format(s))
        else:
            futures = self.futures.values()
        done, pending = await asyncio.wait(futures, return_when=asyncio.FIRST_COMPLETED)
        changed_subsystems = [f.result() for f in done]
        loop = asyncio.get_running_loop()
        for s in changed_subsystems:
            self.futures[s] = loop.create_future()
        return changed_subsystems

    async def wait_or_noidle(self, subsystems, client_reader):
        wait_for_event = asyncio.create_task(self.wait(subsystems))
        wait_for_command = asyncio.create_task(client_reader.readline(timeout=None))
        done, pending = await asyncio.wait({wait_for_event, wait_for_command}, return_when=asyncio.FIRST_COMPLETED)
        if wait_for_command.done():
            command = wait_for_command.result()
            if command != "noidle":
                raise MpdCommandError(command=command, msg="The only valid command in idle state is noidle")
            logger.debug("Received noidle")
        else:
            wait_for_command.cancel()
        if wait_for_event.done():
            return wait_for_event.result()
        else:
            wait_for_event.cancel()
            return []


class QueuedStreamReader(object):
    def __init__(self, reader):
        self.reader = reader
        self.lines = asyncio.Queue()
        self.task = asyncio.create_task(self.__run())

    def __del__(self):
        self.task.cancel()

    async def __run(self):
        async for line in self.reader:
            await self.lines.put(line.decode('utf-8').strip())

    async def readline(self, timeout):
        return await asyncio.wait_for(self.lines.get(), timeout=timeout)


class MpdClientHandlerBase(object):
    def __init_subclass__(cls):
        super().__init_subclass__()
        cls.__SupportedCommands = {
            'currentsong'      :{'class':CurrentSong,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':["sonata"]},
            'outputs'          :{'class':Outputs,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':["gmpc"]},
            'enableoutput'     :{'class':None,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':["gmpc"]},
            'status'           :{'class':Status,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':["sonata"]},
            'stats'            :{'class':Stats,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'notcommands'      :{'class':NotCommands,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':["gmpc"]},
            'commands'         :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'lsinfo'           :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'tagtypes'         :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'playlistinfo'     :{'class':PlaylistInfo,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'playlistid'       :{'class':PlaylistId,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'playlistfind'     :{'class':None,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'listplaylistinfo' :{'class':ListPlaylistInfo,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'plchanges'        :{'class':PlChanges,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':["sonata"]},
            'plchangesposid'   :{'class':PlChangesPosId,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'moveid'           :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'move'             :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'delete'           :{'class':Delete,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'deleteid'         :{'class':DeleteId,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'add'              :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'addid'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'list'             :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'find'             :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'playid'           :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'play'             :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'password'         :{'class':Password,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':["all"]},
            'clear'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'stop'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'seek'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'seekid'          :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'pause'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'next'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'previous'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'random'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'repeat'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'listplaylists'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'load'            :{'class':None,'users':[],'group':'write','mpdVersion':"0.12",'neededBy':None},
            'save'            :{'class':None,'users':[],'group':'write','mpdVersion':"0.12",'neededBy':None},
            'search'            :{'class':None,'users':[],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'rm'            :{'class':None,'users':[],'group':'write','mpdVersion':"0.12",'neededBy':None},
            'setvol'           :{'class':None,'users':[],'group':'control','mpdVersion':"0.12",'neededBy':None},
            'urlhandlers'           :{'class':None,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':None},
            'listallinfo'           :{'class':None,'users':['default'],'group':'read','mpdVersion':"0.12",'neededBy':None},
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

    def _getCommandClass(self,commandName,frontend):
        """ To get a command class to execute on received command
        string. This method raise supported command errors."""
        if commandName not in self.__SupportedCommands:
            logger.warning("Command '%s' is not a MPD command!" % commandName)
            raise CommandNotMPDCommand(commandName)
        elif self.__SupportedCommands[commandName]['class'] == None:
            if self.__SupportedCommands[commandName]['neededBy'] != None:
                logger.critical("Command '%s' is needed for client(s) %s" % (commandName," ".join(self.__SupportedCommands[commandName]['neededBy'])))
            logger.warning("Command '%s' is not supported!" % commandName)
            raise CommandNotSupported(commandName)
        elif not (Frontend.GetDefaultUsername() in self.__SupportedCommands[commandName]['users']
                  or frontend.getUsername() in self.__SupportedCommands[commandName]['users']):
            raise UserNotAllowed(commandName,frontend.getUsername())
        else :
            return self.__SupportedCommands[commandName]['class']

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

    def __init__(self, reader, writer, server):
        super().__init__()
        self.reader = QueuedStreamReader(reader)
        self.writer = writer
        self.server = server
        self.frontend = Frontend()
        self.idle = IdleState()
        logger.debug( "Client connected (%s)" % threading.currentThread().getName())

    async def run(self):
        """Handle connection with mpd client. It gets client command,
        execute it and send a respond."""
        self.writer.write("OK MPD 0.13.0\n".encode('utf-8'))
        await self.writer.drain()

        while True:
            msg=""
            try:
                cmdlist=None
                cmds=[]
                while True:
                    self.data = await self.reader.readline(timeout=10)
                    if len(self.data)==0 : raise IOError #To detect last EOF
                    if self.data == "command_list_ok_begin":
                        cmdlist="list_ok"
                    elif self.data == "command_list_begin":
                        cmdlist="list"
                    elif self.data == "command_list_end":
                        break
                    else:
                        cmds.append(self.data)
                        if not cmdlist:break
                logger.debug(f"Commands received from {self.writer.get_extra_info('peername')[0]}")
                respond = False
                try:
                    for c in cmds:
                        logger.debug("Command '" + c + "'...")
                        respond_to_this, rspmsg = await self.__cmdExec(c)
                        respond = respond or respond_to_this
                        msg += rspmsg
                        if cmdlist=="list_ok" :  msg=msg+"list_OK\n"
                except MpdCommandError as e:
                    logger.info("Command Error: %s"%e.toMpdMsg())
                    msg=e.toMpdMsg()
                    respond = True
                else:
                    msg=msg+"OK\n"
                if respond is True:
                    logger.debug("Message sent:\n\t\t"+msg.replace("\n","\n\t\t"))
                    self.writer.write(msg.encode('utf-8'))
                    await self.writer.drain()
            except IOError as e:
                logger.debug("Client disconnected (%s)"% threading.currentThread().getName())
                break
            except asyncio.TimeoutError as e:
                logger.debug("Client connection timed out")
                break

    async def __cmdExec(self,c):
        """ Execute mpd client command. Take a string, parse it and
        execute the corresponding server.Command function."""
        try:
            pcmd = [m.group() for m in re.compile(r'(\w+)|("([^"])+")').finditer(c)] # WARNING An argument cannot contains a '"'
            cmd = pcmd[0]
            for i in range(1, len(pcmd)):
                pcmd[i] = pcmd[i].replace('"', '')
            args = pcmd[1:]
            logger.debug("Command executed : %s %s for frontend '%s'" % (cmd,args,self.frontend.get()))
            commandCls = self._getCommandClass(cmd,self.frontend)
            msg = await commandCls(args, client=self).run()
        except MpdCommandError:
            raise
        except CommandNotSupported:
            raise
        except :
            logger.critical("Unexpected error on command %s (%s): %s" % (c,self.frontend.get(),sys.exc_info()[0]))
            raise
        logger.debug("Respond:\n\t\t"+msg.replace("\n","\n\t\t"))
        return (commandCls.respond, msg)


class MpdServer(object):
    """ Create a MPD server. By default, a request is treated via
    :class:`MpdClientHandler` class but you can specify an alternative
    request class with ClientHandler argument."""

    def __init__(self, port=6600, ClientHandler=MpdClientHandler, Playlist=MpdPlaylist):
        self.host, self.port = "", port
        self.ClientHandler = ClientHandler
        self.clients = set()
        self.playlist = Playlist()

    async def run(self):
        """Run MPD server in a coroutine"""
        logger.info("Mpd Server is listening on port " + str(self.port))

        async def handle_client(reader, writer):
            handler = self.ClientHandler(reader, writer, self)
            self.clients.add(handler)
            try:
                await handler.run()
            finally:
                writer.close()
                await writer.wait_closed()
                self.clients.remove(handler)

        async with await asyncio.start_server(handle_client, '', self.port) as srv:
            await srv.serve_forever()

    def notify_idle(self, subsystem):
        for c in self.clients:
            c.idle.notify(subsystem)
