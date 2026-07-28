# astrbot.api.message_components 桩
class BaseMessageComponent:
    pass


class Plain(BaseMessageComponent):
    def __init__(self, text=""):
        self.text = text

    def __repr__(self):
        return "Plain(%r)" % self.text


class Reply(BaseMessageComponent):
    def __init__(self, id=None):
        self.id = id


class Record(BaseMessageComponent):
    def __init__(self, file=None, url=None):
        self.file = file
        self.url = url
