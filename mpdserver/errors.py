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
