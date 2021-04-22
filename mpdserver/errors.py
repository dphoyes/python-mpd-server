import enum
import re


class Ack(enum.IntEnum):
    ERROR_NOT_LIST = 1
    ERROR_ARG = 2
    ERROR_PASSWORD = 3
    ERROR_PERMISSION = 4
    ERROR_UNKNOWN = 5

    ERROR_NO_EXIST = 50
    ERROR_PLAYLIST_MAX = 51
    ERROR_SYSTEM = 52
    ERROR_PLAYLIST_LOAD = 53
    ERROR_UPDATE_ALREADY = 54
    ERROR_PLAYER_SYNC = 55
    ERROR_EXIST = 56


##################################
### Mpd supported return types ###
##################################
class MpdCommandError(Exception):
    error = Ack.ERROR_UNKNOWN
    command_listNum = 0
    current_command = None
    message_text = "This error has no message"

    def set_current_command(self, current_command):
        self.current_command = current_command

    def toMpdMsg(self):
        return f"ACK [{self.error}@{self.command_listNum}] {{{self.current_command}}} {self.message_text}\n"

    def __str__(self):
        return self.toMpdMsg()


class MpdCommandErrorCustom(MpdCommandError):
    def __init__(self, message_text="Unknown error", error=Ack.ERROR_UNKNOWN):
        self.message_text = message_text
        self.error = error


class CommandNotSupported(MpdCommandError):
    error = Ack.ERROR_UNKNOWN
    message_text = "Command not supported"


class CommandNotMPDCommand(MpdCommandError):
    message_text = "Not an MPD command"
    error = Ack.ERROR_UNKNOWN


class CommandNotImplemented(MpdCommandError):
    error = Ack.ERROR_UNKNOWN

    def __init__(self, explanation=None):
        self.explanation = explanation

    @property
    def message_text(self):
        msg = "Command not implemented"
        if self.explanation:
            msg = f"{msg} ({self.explanation})"
        return msg


class InvalidArguments(MpdCommandError):
    error = Ack.ERROR_ARG

    def __init__(self, message_text="Invalid arguments"):
        self.message_text = message_text


class InvalidArgumentValue(MpdCommandError):
    error = Ack.ERROR_ARG

    def __init__(self, explanation, value):
        self.explanation = explanation
        self.value = value

    @property
    def message_text(self):
        return f"{self.explanation}: {self.value}"


class UserNotAllowed(MpdCommandError):
    error = Ack.ERROR_PERMISSION

    def __init__(self, userName):
        self.userName = userName

    @property
    def message_text(self):
        return f"User '{self.userName}' is not allowed to execute this command"


class PasswordError(MpdCommandError):
    error = Ack.ERROR_PASSWORD

    def __init__(self, pwd, format):
        self.pwd = pwd
        self.format = format

    @property
    def message_text(self):
        return f"Password '{self.pwd}' is not a valid password. You should use a password such as '{self.format}'"


def parse_ack_line(ack, _RE = re.compile(r"ACK \[(\d+)@(\d+)\] {([^{}]*)} *(.*)")):
    parsed = _RE.match(ack)
    if parsed:
        error, command_listNum, current_command, message_text = parsed.groups()
        error = int(error)
        command_listNum = int(command_listNum)
        exc = MpdCommandErrorCustom(message_text=message_text, error=error)
        exc.command_listNum += command_listNum
        exc.set_current_command(current_command)
        return exc
    else:
        return MpdCommandErrorCustom(f"Received unparseable error from other MPD server: {ack}")
