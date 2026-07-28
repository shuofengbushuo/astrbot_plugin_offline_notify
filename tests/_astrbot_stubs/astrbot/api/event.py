# astrbot.api.event 桩
class _Filter:
    class EventMessageType:
        ALL = "ALL"

    def event_message_type(self, *a, **k):
        return lambda f: f

    def on_llm_request(self, *a, **k):
        return lambda f: f

    def on_llm_response(self, *a, **k):
        return lambda f: f

    def on_decorating_result(self, *a, **k):
        return lambda f: f


filter = _Filter()


class AstrMessageEvent:
    pass


class MessageChain:
    def __init__(self, chain=None):
        self.chain = chain if chain is not None else []

    def message(self, text):
        from astrbot.api.message_components import Plain
        self.chain = [Plain(text)]
        return self
