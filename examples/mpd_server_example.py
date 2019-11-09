#!/usr/bin/python
""" This is a simple howto example."""
from __future__ import print_function

import asyncio
import mpdserver


class Client(mpdserver.MpdClientHandler):
    @classmethod
    def define_commands(cls, register):
        # Register provided outputs command
        register(mpdserver.Outputs)

        # Register your own command implementation
        # Define a playid command based on mpdserver.PlayId squeleton
        @register
        class PlayId(mpdserver.PlayId):
            # This method is called when playid command is sent by a client
            def handle_args(self,songId):print("*** Play a file with Id '%d' ***" %songId)

        @register
        class Play(mpdserver.Command):
            def handle_args(self):
                print("*** Set player to play state ***")


# Set the user defined playlist class
# Define a MpdPlaylist based on mpdserver.MpdPlaylist
# This class permits to generate adapted mpd respond on playlist command.
class Playlist(mpdserver.MpdPlaylist):
    playlist=[mpdserver.MpdPlaylistSong(file='file0',songId=0)]
    # How to get song position from a song id in your playlist
    def songIdToPosition(self,i):
        for e in self.playlist:
            if e.id==i : return e.playlistPosition
    # Set your playlist. It must be a list a MpdPlaylistSong
    def handlePlaylist(self):
        return self.playlist
    # Move song in your playlist
    def move(self,i,j):
        self.playlist[i],self.playlist[j]=self.playlist[j],self.playlist[i]


class Server(mpdserver.MpdServer):
    def __init__(self, port):
        super().__init__(port=port, ClientHandler=Client, Playlist=Playlist)


def main():
    print("""Starting a mpd server on port 9999
    Type Ctrl+C to exit

    To try it, type in another console
    $ mpc -p 9999 play
    Or launch a MPD client with port 9999
    """)
    # Create an mpd server that listen on port 9999
    mpd = Server(9999)
    asyncio.run(mpd.run())


if __name__ == "__main__":
    main()
