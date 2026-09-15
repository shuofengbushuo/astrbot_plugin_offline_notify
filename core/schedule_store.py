"""
ScheduleStore - 定时通知计划持久化存储（JSON）

用途:
  解决 AstrBot 配置系统对嵌套列表对象显示 [object Object] 的问题。
  将「定时通知计划」从复杂 JSON 配置中剥离，改为通过 QQ 聊天命令
  （/下线通知 计划）自然语言管理，底层 JSON 文件存储。

  优先级：ScheduleStore 中的计划 > 配置文件中的 schedules（兼容旧版）

存储结构（schedules.json，位于插件数据目录）:
  [
    {
      "name": "工作日下线",
      "day_type": "weekday",
      "offline_time": "23:00",
      "float_range": 0,            # >0 覆盖全局；0 表示继承全局设置
      "enabled": true
    },
    ...
  ]

设计约束:
  - 仅依赖 Python 标准库（json / os），不依赖 astrbot 运行时
  - 文件读写带容错：损坏 / 不存在时回退到空列表
"""

import json
import os
from typing import List, Optional


class ScheduleStore:
    """定时通知计划持久化存储 — 绕过配置系统的嵌套对象限制。"""

    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "schedules.json")
        self._schedules: List[dict] = []
        self._load()

    # ── 内部：加载 / 保存 ───────────────────────────────

    def _load(self):
        """从磁盘加载计划列表；文件不存在或损坏时回退到空列表。"""
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self._schedules = data
                    else:
                        self._schedules = []
            except (json.JSONDecodeError, OSError):
                self._schedules = []
        else:
            self._schedules = []

    def _save(self):
        """把当前列表写回磁盘。"""
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._schedules, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ── 查询 ────────────────────────────────────────────

    def list_all(self) -> List[dict]:
        """返回所有计划列表。"""
        return list(self._schedules)

    def get(self, name: str) -> Optional[dict]:
        """按名称查找计划；不存在返回 None。"""
        for s in self._schedules:
            if s.get("name") == name:
                return dict(s)
        return None

    def exists(self, name: str) -> bool:
        """判断计划名是否已存在。"""
        return self.get(name) is not None

    # ── 写入 ────────────────────────────────────────────

    def add(self, name: str, day_type: str, offline_time: str,
            float_range: int = 0, enabled: bool = True) -> bool:
        """新增一个定时计划。提前时间由全局配置统一控制。

        Args:
            name: 计划名称（唯一标识）
            day_type: 日期类型 (everyday/weekday/weekend/specific)
            offline_time: 下线时间 (HH:MM)
            float_range: 浮动范围（0 表示继承全局设置 global_float_range）
            enabled: 是否启用

        Returns:
            bool: 是否添加成功（重名或参数非法返回 False）
        """
        if not name or not name.strip():
            return False
        name = name.strip()
        if self.exists(name):
            return False

        valid_types = {"everyday", "weekday", "weekend", "specific"}
        if day_type not in valid_types:
            return False

        # 简单校验时间格式 HH:MM
        if not self._is_valid_time(offline_time):
            return False

        schedule = {
            "name": name,
            "day_type": day_type,
            "offline_time": offline_time,
            "float_range": max(0, min(float_range, 10)),
            "enabled": enabled,
        }
        self._schedules.append(schedule)
        self._save()
        return True

    def delete(self, name: str) -> bool:
        """删除一个计划。

        Returns:
            bool: 是否确实删除了
        """
        for i, s in enumerate(self._schedules):
            if s.get("name") == name:
                self._schedules.pop(i)
                self._save()
                return True
        return False

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """启用/禁用一个计划。

        Returns:
            bool: 是否操作成功
        """
        for s in self._schedules:
            if s.get("name") == name:
                s["enabled"] = enabled
                self._save()
                return True
        return False

    def update(self, name: str, **kwargs) -> bool:
        """更新计划的字段。

        支持更新的字段:
          - offline_time (str): 下线时间 HH:MM
          - float_range (int): 浮动范围（0 = 继承全局）
          - day_type (str): 日期类型
          - enabled (bool): 是否启用

        Returns:
            bool: 是否更新成功
        """
        for s in self._schedules:
            if s.get("name") == name:
                if "offline_time" in kwargs:
                    if self._is_valid_time(kwargs["offline_time"]):
                        s["offline_time"] = kwargs["offline_time"]
                if "float_range" in kwargs:
                    s["float_range"] = max(0, min(int(kwargs["float_range"]), 10))
                if "day_type" in kwargs:
                    valid_types = {"everyday", "weekday", "weekend", "specific"}
                    if kwargs["day_type"] in valid_types:
                        s["day_type"] = kwargs["day_type"]
                if "enabled" in kwargs:
                    s["enabled"] = bool(kwargs["enabled"])
                self._save()
                return True
        return False

    def get_enabled_schedules(self) -> List[dict]:
        """返回所有启用的计划。"""
        return [s for s in self._schedules if s.get("enabled", True)]

    # ── 工具 ────────────────────────────────────────────

    @staticmethod
    def _is_valid_time(time_str: str) -> bool:
        """校验时间格式 HH:MM"""
        if not time_str or not isinstance(time_str, str):
            return False
        parts = time_str.strip().split(":")
        if len(parts) != 2:
            return False
        try:
            h, m = int(parts[0]), int(parts[1])
            return 0 <= h <= 23 and 0 <= m <= 59
        except ValueError:
            return False

    # ── 方便展示用的常量 ──────────────────────────────

    DAY_TYPE_LABELS = {
        "everyday": "每天",
        "weekday": "工作日",
        "weekend": "周末",
        "specific": "特定日期",
    }

    DAY_TYPE_VALID_KEYS = {
        "每天": "everyday",
        "工作日": "weekday",
        "周末": "weekend",
        "特定日期": "specific",
    }
