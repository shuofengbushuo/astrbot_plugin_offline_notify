"""
PromptStore - 命名的提示词方案库（JSON 持久化）

用途:
  让「下线通知」插件的提示词从「内置硬编码」变为「明置 + 可多套独立管理」。
  管理员可以在配置面板里填写自定义提示词（custom_builtin_prompt /
  custom_manual_prompt），也可以把若干套提示词存成「命名方案」，
  互不干扰地保存 / 切换 / 删除，满足多用户 / 多场景需求。

  在 LLMGenerator.get_effective_prompts() 中，命名方案的优先级最高：
      激活方案 > 配置自定义 > 内置默认（BUILTIN_PROMPT / MANUAL_PROMPT）

存储结构（prompts.json，位于插件数据目录）:
  {
    "_active": "方案名 或 null",
    "profiles": {
      "方案名": {
        "builtin_prompt": "...",   # 自动调度场景提示词
        "manual_prompt": "...",     # /下线通知 生成 手动场景提示词
        "created_at": 1234567890,
        "updated_at": 1234567890
      },
      ...
    }
  }

设计约束:
  - 仅依赖 Python 标准库（json / os / time），不依赖 astrbot 运行时，
    保证可在无 AStrBot 环境下被单测直接 import。
  - 文件读写带容错：损坏 / 不存在时回退到空库，不抛异常阻断插件启动。
"""

import json
import os
import time
from typing import Optional


class PromptStore:
    """命名的提示词方案库 —— JSON 持久化，支持多方案独立保存、互不干扰。"""

    def __init__(self, data_dir: str):
        """初始化方案库。

        Args:
            data_dir: 插件数据目录，prompts.json 将存放于此。
        """
        self.path = os.path.join(data_dir, "prompts.json")
        self._cache: dict = {}
        self._load()

    # ── 内部：加载 / 保存 ───────────────────────────────

    def _load(self):
        """从磁盘加载方案库；文件不存在或损坏时回退到空库。"""
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._cache = json.load(f) or {}
            except (json.JSONDecodeError, OSError):
                # 损坏文件不阻断插件：回退空库，下次保存会覆盖修复。
                self._cache = {}
        else:
            self._cache = {}
        if not isinstance(self._cache, dict):
            self._cache = {}
        self._cache.setdefault("profiles", {})

    def _save(self):
        """把当前缓存写回磁盘。"""
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
        except OSError:
            # 写盘失败不应阻断运行时调用（内存中仍是最新状态）。
            pass

    # ── 查询 ────────────────────────────────────────────

    def list_profiles(self) -> list:
        """返回所有方案名列表。"""
        return list(self._cache.get("profiles", {}).keys())

    def get_profile(self, name: str) -> Optional[dict]:
        """按名取单个方案；不存在返回 None。"""
        return self._cache.get("profiles", {}).get(name)

    def get_active(self) -> Optional[str]:
        """返回当前激活的方案名（未激活返回 None）。"""
        return self._cache.get("_active")

    # ── 写入 ────────────────────────────────────────────

    def upsert(self, name: str, builtin_prompt: str, manual_prompt: str):
        """新增 / 更新一个命名方案（存在则覆盖内容、保留 created_at）。

        Args:
            name: 方案名（不能为空）
            builtin_prompt: 自动场景提示词
            manual_prompt: 手动场景提示词
        """
        if not name or not name.strip():
            return
        name = name.strip()
        now = int(time.time())
        profs = self._cache.setdefault("profiles", {})
        existing = profs.get(name, {})
        profs[name] = {
            "builtin_prompt": builtin_prompt or "",
            "manual_prompt": manual_prompt or "",
            "created_at": existing.get("created_at", now),
            "updated_at": now,
        }
        self._save()

    def delete(self, name: str) -> bool:
        """删除一个命名方案；若它是当前激活方案，则同时取消激活。

        Returns:
            bool: 是否确实删除了（不存在返回 False）
        """
        profs = self._cache.get("profiles", {})
        if name not in profs:
            return False
        del profs[name]
        if self._cache.get("_active") == name:
            self._cache["_active"] = None
        self._save()
        return True

    def set_active(self, name: Optional[str]) -> bool:
        """设置当前激活方案。

        Args:
            name: 方案名；传 None 表示取消激活（回退到配置自定义 / 内置默认）。
                  传入不存在的方案名会失败（返回 False）。

        Returns:
            bool: 是否设置成功
        """
        if name is None:
            self._cache["_active"] = None
            self._save()
            return True
        name = name.strip() if isinstance(name, str) else name
        if name in self._cache.get("profiles", {}):
            self._cache["_active"] = name
            self._save()
            return True
        return False
