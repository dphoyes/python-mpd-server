import logging


class Message:
    def __init__(self, fmt, args):
        self.fmt = fmt
        self.args = args

    def __str__(self):
        if self.args:
            return self.fmt.format(*self.args)
        else:
            return self.fmt


class StyleAdapter(logging.LoggerAdapter):
    def log(self, level, msg, /, *args, **kwargs):
        if self.isEnabledFor(level):
            msg, kwargs = self.process(msg, kwargs)
            self.logger._log(level, Message(msg, args), (), **kwargs)


def Logger(name):
    return StyleAdapter(logging.getLogger(name), {})


logging.basicConfig(level=logging.DEBUG)
