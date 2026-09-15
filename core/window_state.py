"""计划窗口状态机 —— ARMED 标记 + 每日发送去重。

设计要点：
- ``armed`` 是「窗口内待发送」的瞬时状态，仅存内存；插件重载 / bot 重启后
  由调用方按当前时刻重建（重算哪些计划此刻正处于时间窗口内）。
- ``sent`` 是「今日已发送」的去重标记，持久化到磁盘（window_state.json），
  跨重启不重复发送；跨天自动失效并清空。
"""

import json
import os
import time
from datetime import datetime


class WindowState:
    """管理各计划的时间窗口武装状态与每日发送去重。"""

    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "window_state.json")
        self._armed: set = set()
        self._sent: dict = {}  # {"YYYY-MM-DD": {"plan_name": epoch}}
        self._date = self._today()
        self._load()

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    # ── 加载 / 保存 ──────────────────────────────────────

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    sent = data.get("sent", {})
                    if isinstance(sent, dict):
                        self._sent = sent
        except (json.JSONDecodeError, OSError):
            self._sent = {}
        self._roll_if_new_day()

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(
                    {"date": self._date, "sent": self._sent},
                    f, ensure_ascii=False, indent=2,
                )
        except OSError:
            pass

    def _roll_if_new_day(self):
        today = self._today()
        if today != self._date:
            self._date = today
            self._sent = {}
            self._armed.clear()
            self._save()

    # ── armed 状态 ───────────────────────────────────────

    def arm(self, plan_name: str):
        self._roll_if_new_day()
        self._armed.add(plan_name)

    def disarm(self, plan_name: str):
        self._armed.discard(plan_name)

    def is_armed(self, plan_name: str) -> bool:
        self._roll_if_new_day()
        return plan_name in self._armed

    def armed_plans(self) -> list:
        self._roll_if_new_day()
        return sorted(self._armed)

    # ── 每日 sent 去重 ───────────────────────────────────

    def already_sent_today(self, plan_name: str) -> bool:
        self._roll_if_new_day()
        return plan_name in self._sent.get(self._date, {})

    def mark_sent(self, plan_name: str):
        self._roll_if_new_day()
        self._sent.setdefault(self._date, {})[plan_name] = time.time()
        self._armed.discard(plan_name)
        self._save()

    def snapshot(self) -> dict:
        self._roll_if_new_day()
        return {
            "date": self._date,
            "armed": sorted(self._armed),
            "sent": dict(self._sent.get(self._date, {})),
        }
