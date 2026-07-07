"""
群组通知模块 - 负责向指定QQ群发送消息，包含重试机制和错误日志
"""

import asyncio
import time
from typing import List, Optional
from astrbot.api import logger
from astrbot.api.event import MessageChain


class GroupNotifier:
    """群组通知发送器，支持重试机制"""

    def __init__(self, context, retry_config: dict):
        """初始化通知器

        Args:
            context: AstrBot Context 对象
            retry_config: 重试配置 {"max_retries": 3, "retry_interval_base": 10}
        """
        self.context = context
        self.max_retries = retry_config.get("max_retries", 3)
        self.retry_interval_base = retry_config.get("retry_interval_base", 10)
        self._send_stats = {
            "total_sent": 0,
            "total_failed": 0,
            "last_send_time": None,
            "last_error": None,
        }

    async def send_to_group(self, group_id: str, message: str,
                            platform_id: str = "default") -> bool:
        """向单个群组发送消息（带重试）

        Args:
            group_id: QQ群号
            message: 消息内容
            platform_id: 平台标识，默认 "default"

        Returns:
            bool: 是否发送成功
        """
        umo = f"{platform_id}:GroupMessage:{group_id}"
        chain = MessageChain().message(message)

        for attempt in range(1, self.max_retries + 1):
            try:
                await self.context.send_message(umo, chain)
                logger.info(f"[离线通知] 成功发送通知到群 {group_id}")
                self._update_stats(success=True)
                return True

            except Exception as e:
                logger.warning(
                    f"[离线通知] 发送到群 {group_id} 失败 "
                    f"(第 {attempt}/{self.max_retries} 次尝试): {e}"
                )
                self._update_stats(success=False, error=str(e))

                if attempt < self.max_retries:
                    # 递增等待时间
                    wait_seconds = self.retry_interval_base * attempt
                    logger.info(f"[离线通知] 等待 {wait_seconds}s 后重试...")
                    await asyncio.sleep(wait_seconds)
                else:
                    logger.error(
                        f"[离线通知] 发送到群 {group_id} 最终失败，"
                        f"已尝试 {self.max_retries} 次"
                    )

        return False

    async def send_to_groups(self, target_groups: List[dict],
                             message: str, platform_id: str = "default") -> dict:
        """向多个群组发送消息

        Args:
            target_groups: 目标群组列表 [{"group_id": "xxx", "group_name": "xxx", "enabled": true}, ...]
            message: 消息内容
            platform_id: 平台标识

        Returns:
            dict: {"success": [...], "failed": [...], "total": int}
        """
        success_groups = []
        failed_groups = []

        for group in target_groups:
            if not group.get("enabled", True):
                logger.info(f"[离线通知] 群 {group.get('group_id')} 已禁用，跳过")
                continue

            group_id = group.get("group_id", "")
            if not group_id:
                logger.warning("[离线通知] 跳过空的群号")
                continue

            success = await self.send_to_group(group_id, message, platform_id)
            if success:
                success_groups.append(group_id)
            else:
                failed_groups.append(group_id)

        result = {
            "success": success_groups,
            "failed": failed_groups,
            "total": len(success_groups) + len(failed_groups),
        }

        logger.info(
            f"[离线通知] 通知发送完成: 成功 {len(success_groups)}/{result['total']}, "
            f"失败 {len(failed_groups)}/{result['total']}"
        )

        return result

    def get_stats(self) -> dict:
        """获取发送统计信息

        Returns:
            dict: 统计数据
        """
        return dict(self._send_stats)

    def _update_stats(self, success: bool, error: Optional[str] = None):
        """更新发送统计"""
        self._send_stats["last_send_time"] = time.time()
        if success:
            self._send_stats["total_sent"] += 1
        else:
            self._send_stats["total_failed"] += 1
            if error:
                self._send_stats["last_error"] = error