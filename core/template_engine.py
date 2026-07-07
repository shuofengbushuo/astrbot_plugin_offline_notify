"""
消息模板引擎 - 负责渲染通知消息内容

支持变量替换:
  {time}             - 下线时间 (HH:MM)
  {date}             - 当前日期 (YYYY-MM-DD)
  {day_of_week}      - 星期几（中文）
  {countdown_minutes} - 剩余分钟数
  {emoji}            - 用户配置的表情符号
"""

from datetime import datetime


class TemplateEngine:
    """消息模板渲染引擎"""

    WEEKDAY_NAMES = {
        0: "周一", 1: "周二", 2: "周三", 3: "周四",
        4: "周五", 5: "周六", 6: "周日"
    }

    def __init__(self, config: dict):
        """初始化模板引擎

        Args:
            config: message_template 配置节
        """
        self.title_template = config.get("title_template", "【AI服务下线通知】")
        self.body_template = config.get("body_template",
            "各位成员请注意~\n\n{emoji} AI助手即将在 {time} 下线休息，"
            "预计还有 {countdown_minutes} 分钟。\n\n如有需要请尽快处理未完成的事项。\n明天见！晚安~")
        self.footer_template = config.get("footer_template", "")
        self.emoji = config.get("emoji", "🌙")

    def render(self, offline_time: str, countdown_minutes: int,
               now: datetime = None) -> dict:
        """渲染通知消息

        Args:
            offline_time: 下线时间字符串 (HH:MM)
            countdown_minutes: 距离下线的剩余分钟数
            now: 当前时间，默认为 datetime.now()

        Returns:
            dict: {"title": str, "body": str, "footer": str}
        """
        if now is None:
            now = datetime.now()

        variables = {
            "time": offline_time,
            "date": now.strftime("%Y-%m-%d"),
            "day_of_week": self.WEEKDAY_NAMES.get(now.weekday(), str(now.weekday())),
            "countdown_minutes": str(countdown_minutes),
            "emoji": self.emoji,
        }

        title = self._replace_vars(self.title_template, variables)
        body = self._replace_vars(self.body_template, variables)
        footer = self._replace_vars(self.footer_template, variables) if self.footer_template else ""

        return {"title": title, "body": body, "footer": footer}

    def render_preview(self, offline_time: str = "23:00",
                       countdown_minutes: int = 5,
                       override_vars: dict = None) -> dict:
        """渲染预览消息（用于 WebUI 预览功能）

        Args:
            offline_time: 模拟的下线时间
            countdown_minutes: 模拟的剩余分钟数
            override_vars: 覆盖的变量字典

        Returns:
            dict: {"title": str, "body": str, "footer": str}
        """
        now = datetime.now()
        variables = {
            "time": offline_time,
            "date": now.strftime("%Y-%m-%d"),
            "day_of_week": self.WEEKDAY_NAMES.get(now.weekday(), str(now.weekday())),
            "countdown_minutes": str(countdown_minutes),
            "emoji": self.emoji,
        }
        if override_vars:
            variables.update(override_vars)

        title = self._replace_vars(self.title_template, variables)
        body = self._replace_vars(self.body_template, variables)
        footer = self._replace_vars(self.footer_template, variables) if self.footer_template else ""

        return {"title": title, "body": body, "footer": footer}

    def build_full_message(self, offline_time: str, countdown_minutes: int,
                           now: datetime = None) -> str:
        """构建完整的通知消息文本

        Args:
            offline_time: 下线时间
            countdown_minutes: 剩余分钟数
            now: 当前时间

        Returns:
            str: 完整的通知消息
        """
        rendered = self.render(offline_time, countdown_minutes, now)
        parts = [rendered["title"], "", rendered["body"]]
        if rendered["footer"]:
            parts.extend(["", rendered["footer"]])
        return "\n".join(parts)

    @staticmethod
    def _replace_vars(template: str, variables: dict) -> str:
        """替换模板中的变量

        Args:
            template: 模板字符串
            variables: 变量字典

        Returns:
            str: 替换后的字符串
        """
        result = template
        for key, value in variables.items():
            result = result.replace(f"{{{key}}}", value)
        return result