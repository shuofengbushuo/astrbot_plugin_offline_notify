"""
通知发布记录存储模块 - 持久化存储通知发送历史，支持查询和统计

记录格式:
{
    "timestamp": 1712345678.0,
    "schedule_name": "工作日下线",
    "offline_time": "23:00",
    "advance_minutes": 5,
    "float_range": 3,
    "actual_trigger_minutes": 7.2,
    "float_seconds": 132,
    "message_source": "llm",
    "results": {
        "success": ["123456789", "987654321"],
        "failed": [],
        "total": 2
    }
}

存储方式: JSON 文件，最大 200 条，循环覆盖
"""

import json
import os
import time
import asyncio
from datetime import datetime
from typing import List, Optional, Dict
from astrbot.api import logger


class RecordStore:
    """通知记录存储，支持持久化和查询"""

    MAX_RECORDS = 200
    LOCK_TIMEOUT = 5  # 文件锁超时

    def __init__(self, data_dir: str):
        """初始化记录存储

        Args:
            data_dir: 插件数据目录
        """
        self._file_path = os.path.join(data_dir, "notification_records.json")
        self._records: List[dict] = []
        self._lock = asyncio.Lock()
        self._load()

    def _load(self):
        """从文件加载记录"""
        if not os.path.exists(self._file_path):
            logger.info("[离线通知] 记录文件不存在，将创建新文件")
            return

        try:
            with open(self._file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    self._records = data[-self.MAX_RECORDS:]
                    logger.info(
                        f"[离线通知] 已加载 {len(self._records)} 条历史记录"
                    )
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"[离线通知] 记录文件读取失败: {e}")
            self._records = []

    async def _save(self):
        """持久化保存记录到文件"""
        async with self._lock:
            try:
                os.makedirs(os.path.dirname(self._file_path), exist_ok=True)
                with open(self._file_path, "w", encoding="utf-8") as f:
                    json.dump(
                        self._records[-self.MAX_RECORDS:],
                        f,
                        ensure_ascii=False,
                        indent=2
                    )
            except IOError as e:
                logger.error(f"[离线通知] 记录保存失败: {e}")

    async def add(self, schedule_name: str, offline_time: str,
                  advance_minutes: int, float_range: int,
                  actual_trigger_minutes: float, float_seconds: float,
                  message_source: str, results: dict):
        """添加一条通知记录

        Args:
            schedule_name: 计划名称
            offline_time: 下线时间 (HH:MM)
            advance_minutes: 配置的提前分钟数
            float_range: 配置的浮动范围
            actual_trigger_minutes: 实际触发提前分钟数
            float_seconds: 实际浮动秒数
            message_source: 消息来源 (llm / template)
            results: 发送结果 {"success": [...], "failed": [...], "total": int}
        """
        record = {
            "timestamp": time.time(),
            "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "schedule_name": schedule_name,
            "offline_time": offline_time,
            "advance_minutes": advance_minutes,
            "float_range": float_range,
            "actual_trigger_minutes": round(actual_trigger_minutes, 1),
            "float_seconds": round(float_seconds, 0),
            "message_source": message_source,
            "results": {
                "success": results.get("success", []),
                "failed": results.get("failed", []),
                "total": results.get("total", 0),
            }
        }

        self._records.append(record)
        # 超出上限时裁剪
        if len(self._records) > self.MAX_RECORDS:
            self._records = self._records[-self.MAX_RECORDS:]

        # 异步保存
        await self._save()

        logger.info(
            f"[离线通知] 已记录: 计划 '{schedule_name}', "
            f"实际提前 {actual_trigger_minutes:.1f} 分钟, "
            f"浮动 {float_seconds:.0f}s, "
            f"来源 {message_source}, "
            f"发送 {results.get('total', 0)} 个群组"
        )

    async def query(self, limit: int = 10, offset: int = 0) -> List[dict]:
        """查询通知记录

        Args:
            limit: 最大返回条数
            offset: 偏移量

        Returns:
            List[dict]: 记录列表（按时间倒序）
        """
        records = sorted(
            self._records,
            key=lambda r: r.get("timestamp", 0),
            reverse=True
        )
        return records[offset:offset + limit]

    async def query_latest(self, limit: int = 5) -> List[dict]:
        """查询最近的通知记录

        Args:
            limit: 最大返回条数

        Returns:
            List[dict]: 记录列表
        """
        return await self.query(limit=limit)

    def get_total_count(self) -> int:
        """获取记录总数"""
        return len(self._records)

    async def clear(self):
        """清空所有记录"""
        self._records = []
        await self._save()
        logger.info("[离线通知] 已清空所有通知记录")

    async def get_stats(self) -> dict:
        """获取统计摘要

        Returns:
            dict: 统计信息
        """
        total = len(self._records)
        if total == 0:
            return {
                "total": 0,
                "llm_count": 0,
                "template_count": 0,
                "avg_float_seconds": 0,
                "total_success_groups": 0,
                "total_failed_groups": 0,
            }

        llm_count = sum(1 for r in self._records if r.get("message_source") == "llm")
        template_count = total - llm_count
        float_seconds = [r.get("float_seconds", 0) for r in self._records]
        avg_float = sum(float_seconds) / total if total > 0 else 0
        total_success = sum(len(r.get("results", {}).get("success", [])) for r in self._records)
        total_failed = sum(len(r.get("results", {}).get("failed", [])) for r in self._records)

        return {
            "total": total,
            "llm_count": llm_count,
            "template_count": template_count,
            "avg_float_seconds": round(avg_float, 0),
            "total_success_groups": total_success,
            "total_failed_groups": total_failed,
        }