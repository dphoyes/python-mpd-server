"""
This module permits to define mpd commands. Each command inherits from
:class:`Command` which is a command base class. There are
also some specialized subclasses of :class:`Command`:

- :class:`CommandItems` can be used when respond is a list of items
- :class:`CommandSongs` can be used when respond is a list of songs
- :class:`CommandPlaylist` can be used when respond is a list of songs

MPD songs are represented by classes:

- :class:`MpdPlaylistSong` which contains a song position and a song id.
- :class:`MpdLibrarySong`

Moreover, you can map your playlist to the mpd playlist by overriding
:class:`MpdPlaylist`. Then when you override this class, a lot of
commands are defined (for example :class:`Move`, :class:`MoveId`,
:class:`Delete`, etc.).

MPD protocol supports playlist diff (for example
command :class:`command_skel.PlChanges`). This feature is internally
management with :class:`PlaylistHistory`.


Note: 'command' and 'notcommand' commands seems to not be used by
gmpc. Then, we have to implement a lot of commands with dummy
respond. However, gmpc use 'command' command to allow user to play,
pause ...
"""
from __future__ import print_function
from __future__ import absolute_import
import re
import shlex
from . import errors
from .logging import Logger
from .utils import _await_if_awaitable, _async_yield_from_if_generator

logger = Logger(__name__)


class HandlerBase(object):
    def __init__(self, client):
        self.client = client

    @property
    def frontend(self):
        return self.client.frontend

    @property
    def server(self):
        return self.client.server

    @property
    def partition(self):
        return self.client.partition

    @property
    def playlist(self):
        return self.partition.playlist


class CommandListBase(HandlerBase):
    def __init__(self, commands, list_ok: bool, client):
        super().__init__(client=client)
        self.commands = commands
        self.list_ok = list_ok

    async def run(self):
        raise NotImplementedError


class CommandListDefault(CommandListBase):
    async def run(self):
        for command_listNum, command in enumerate(self.commands):
            try:
                async for chunk in command.run():
                    yield chunk
                if self.list_ok:
                    yield b"list_OK\n"
            except errors.MpdCommandError as e:
                e.command_listNum += command_listNum
                raise


class CommandBase(HandlerBase):
    respond = True
    CommandListHandler = CommandListDefault

    formatArg={}
    varArg=False
    listArg=False
    """ To specify command arguments format. For example, ::

       formatArg={"song": OptStr}

    means command accept a optionnal
    string argument bound to `song` attribute name."""

    def __init__(self, command, client):
        super().__init__(client=client)
        self.raw_command = command

    @classmethod
    def GetCommandName(cls):
        """ MPD command name. Command name is the lower class
        name. This string is used to parse a client request. You can
        override this classmethod to define particular commandname."""
        return cls.__name__.lower()

    async def run(self):
        raise NotImplementedError

    def parse_args(self):
        args = shlex.split(self.raw_command.decode('utf-8'))[1:]
        if self.listArg == True:
            d = dict()
            d['args'] = args
            return d
        if self.varArg == True:
            d = dict()
            for i in range(0, len(args), 2):
                key = args[i]
                if key not in d:
                    d[key] = []
                d[key].append(args[i+1])
            return d
        if len(args) > len(self.formatArg):
            raise errors.InvalidArguments("Too many arguments: %s command arguments should be %s instead of %s" % (self.__class__,self.formatArg,args))
        try:
            d = dict()
            for i, (arg_name, arg_type) in enumerate(self.formatArg.items()):
                try:
                    d.update({arg_name: arg_type(args[i])})
                except IndexError:
                    if Opt not in arg_type.__bases__:
                        raise
        except IndexError :
            raise errors.InvalidArguments("Not enough arguments: %s command arguments should be %s instead of %s" %(self.__class__,self.formatArg,args))
        except ValueError as e:
            raise errors.InvalidArguments("Wrong argument type: %s command arguments should be %s instead of %s (%s)" %(self.__class__,self.formatArg,args,e))
        return d


class Command(CommandBase):
    """ Command class is the base command class. You can define
    argument format by setting :attr:`formatArg`. Command argument
    can be accessed with :attr:`args` dictionnary.

    Each command has a playlist attribute which is given by
    MpdRequestHandler. This playlist must implement MpdPlaylist class
    and by default, this one is used.

    :class:`Command` contains command
    arguments definition via :attr:`Command.formatArg`. You can handle
    them with :func:`Command.handle_args`. An argument is :

    - an int
    - a string
    - an optionnal int (:class:`OptInt`)
    - an optionnal string (:class:`OptStr`)

    """

    def __init__(self, command, client):
        super().__init__(command, client)
        self.args = self.parse_args()

    async def run(self):
        """To treat a command. This class handle_args method and toMpdMsg method."""
        try:
            await _await_if_awaitable(self.handle_args(**(self.args)))
            result = await _await_if_awaitable(self.toMpdMsg())
            async for x in _async_yield_from_if_generator(result, (result,)):
                if isinstance(x, str):
                    x = x.encode('utf-8')
                yield x
        except NotImplementedError as e:
            new_e = errors.CommandNotImplemented(explanation=str(e))
            new_e.set_current_command(self.GetCommandName())
            raise new_e from e
        except errors.MpdCommandError as e:
            e.set_current_command(self.GetCommandName())
            e.command_listNum *= 0
            raise

    def handle_args(self,**kwargs):
        """ Override this method to treat commands arguments."""
        logger.debug("Parsing arguments %s in %s" % (str(kwargs),str(self.__class__)))
    def toMpdMsg(self):
        """ Override this method to send a specific respond to mpd client."""
        logger.debug("Not implemented respond for command %s"%self.__class__)
        return
        yield

class CommandDummy(Command):
    def toMpdMsg(self):
        logger.info("Dummy respond sent for command %s"%self.__class__)
        return "ACK [error@command_listNum] {%s} Dummy respond for command '%s'\n" % (self.__class__,self.__class__)

class CommandItems(Command):
    """ This is a subclass of :class:`Command` class. CommandItems is
    used to send items respond such as :class:`command.Status` command."""
    def items(self):
        """ Overwrite this method to send items to mpd client. This method
        must return a list a tuples ("key",value)."""
        return []
    async def toMpdMsg(self):
        to_bytes = lambda x: x if isinstance(x, bytes) else str(x).encode('utf8')
        items = await _await_if_awaitable(self.items())
        async for i,v in _async_yield_from_if_generator(items, items):
            yield b''.join(token for token in (to_bytes(i), b': ', to_bytes(v), b'\n'))

class CommandSongs(Command):
    """ This is a subclass of :class:`Command` class. Respond songs
    informations for mpd clients."""
    def songs(self):
        """ Override it to adapt this command. This must return a list of
        :class:`MpdPlaylistSong` """
        return [] #self.helper_mkSong("/undefined/")
    def toMpdMsg(self):
        return ''.join([MpdPlaylistSong.toMpdMsg(s) for s in self.songs()])

class CommandPlaylist(CommandSongs):
    """ This class of commands is used on mpd internal playlist."""
    def songs(self):
        """ Overide it to specify a real playlist.
        Should return a list of dict song informations"""
        return [] # self.playlist.generateMpdPlaylist()

class MpdPlaylistSong(object):
    """ To create a mpd song which is in a playtlist.

    MPD use a song id which is unique for all song in
    playlist and stable over time. This field is automatically
    generated.
    """
    def __init__(self,file,songId,playlistPosition=None,title=" ",time=0,album=" ",artist=" ",track=0):
        self.file=file
        self.title=title
        self.time=time
        self.album=album
        self.artist=artist
        self.track=track
        self.playlistPosition=playlistPosition
        self.songId=songId #self.generatePlaylistSongId(self)

    def generatePlaylistSongId(self,song):
        return id(song)

    def toMpdMsg(self):
        if self.playlistPosition == None :
            logger.warning("MpdPlaylistSong.playlistposition attribute is not set.")
        return ('file: '+self.file+"\n"+
                'Time: '+str(self.time)+"\n"+
                'Album: '+self.album+"\n"+
                'Artist: '+self.artist+"\n"+
                'Title: '+self.title+"\n"+
                'Track: '+str(self.track)+"\n"+
                'Pos: '+str(self.playlistPosition)+"\n"+
                'Id: '+ str(self.songId)+"\n")

class MpdLibrarySong(object):
    """ To create a mpd library song. This is actually not used."""
    def __init__(self,filename):
        self.file=filename
        self.lastModDate="2011-12-17T22:47:58Z"
        self.time="0"
        self.artist= ""
        self.title=filename
        self.album=""
        self.track=""
        self.genre=""

    def toMpdMsg(self):
        return ("file: "+self.filename+"\n"+
                "Last-Modified: "+self.lastModDate+"\n"+
                "Time: "+self.time+"\n"+
                "Artist: "+self.artist+"\n"+
                "Title: "+self.title+"\n"+
                "Album: "+self.album+"\n"+
                "Track: "+self.track+"\n"+
                "Genre :"+self.genre+"\n")


import types
class Opt(object):pass
class OptInt(Opt,int):
    """ Represent optionnal integer command argument"""
    pass
class OptStr(Opt,str):
    """ Represent optionnal string command argument"""
    pass


class PlaylistHistory(object):
    """ Contains all playlist version to generate playlist diff (see
    plchanges* commands). This class is a singleton and it is used by
    MpdPlaylist.
    """
    _instance = None
    playlistHistory=[]

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(PlaylistHistory, cls).__new__(
                cls, *args, **kwargs)
        return cls._instance

    def addPlaylist(self,version,playlist):
        """ Add playlist version if not exist in history """
        for (v,p) in self.playlistHistory:
            if v == version:
                return None
        self.playlistHistory.append((version,playlist))

    def getPlaylist(self,version):
        """ Get playlist from version"""
        for (i,p) in self.playlistHistory:
            if i==version:
                return p
        return None

    def diff(self,version):
        """ Return new songs in current playlist since version """
        plOld=self.getPlaylist(version)
        plCur=self.playlistHistory[len(self.playlistHistory)-1][1]
        if plOld == None:
            return plCur
        diff=[]
        try :
            for i in range(0,len(plOld)):
                if plOld[i] != plCur[i]:
                    diff.append(plCur[i])
            for i in range(len(plOld),len(plCur)):
                diff.append(plCur[i])
        except IndexError: pass
        return diff

    def show(self):
        print("show playlistHistory")
        print("number of version: " + str(len(self.playlistHistory)))
        for i in self.playlistHistory:
            print(i)
            print("------")
        print("show playlistHistory end")


class MpdPlaylist(object):
    """ MpdPlaylist is a list of song.
    Use it to create a mapping between your player and the fictive mpd
    server.

    Some methods must be implemented, otherwise, NotImplementedError
    is raised.

    To bind a playlist to this class, use overide
    `handlePlaylist` method.
    """
    def __init__(self):
        self.playlistHistory=PlaylistHistory()

    def handlePlaylist(self):
        """ Implement this method to bind your playlist with mpd
        playlist. This method should return a list of :class:`MpdPlaylistSong`."""
        raise NotImplementedError("you should implement MpdPlaylist.handlePlaylist method (or use MpdPlaylistDummy) ")

    def generateMpdPlaylist(self):
        """ This is an internal method to automatically add playlistPosition to songs in playlist. """
        p=self.handlePlaylist()
        for i in range(len(p)):
            p[i].playlistPosition=i
        self.playlistHistory.addPlaylist(self.version(),p)
        return p

    def generateMpdPlaylistDiff(self,oldVersion):
        self.generateMpdPlaylist()
        return self.playlistHistory.diff(oldVersion)

    def songIdToPosition(self,id):
        """ This method MUST be implemented. It permits to generate the position from a mpd song id."""
        raise NotImplementedError("you should implement MpdPlaylist.songIdToPosition method")
    def version(self):
        return 0
    def length(self):
        return len(self.generateMpdPlaylist())
    def move(self,fromPostion,toPosition):
        """ Implement it to support move* commands """
        raise NotImplementedError("you should implement MpdPlaylist.move method")
    def moveId(self,fromId,toPosition):
        self.move(self.songIdToPosition(fromId),toPosition)
    def delete(self,position):
        """ Implement it to support delete* commands """
        raise NotImplementedError("you should implement MpdPlaylist.delete method")
    def deleteId(self,songId):
        self.delete(self.songIdToPosition(songId))


class MpdPlaylistDummy(MpdPlaylist):
    def handlePlaylist(self):
        logger.warning("Dummy implementation of handlePlaylist method")
        return []

