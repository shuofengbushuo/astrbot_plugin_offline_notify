# astrbot.api 桩
class _Logger:
    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


logger = _Logger()


class AstrBotConfig(dict):
    pass
