"""
群组通知模块 - 负责向指定QQ群发送消息，包含重试机制和错误日志
"""

import asyncio
import re
import time
from typing import List, Optional
from astrbot.api import logger
from astrbot.api.event import MessageChain

# 与 splitter 简单模式一致的分段规则：按句末标点切分，分隔符附加到前一段末尾。
# 说明：离线通知通过 context.send_message 直接发送给指定群，而该路径不经过
# splitter 的 on_decorating_result 装饰钩子（框架设计上 send_message 绕过装饰阶段，
# 否则 splitter 自己用 send_message 回发切分段时会无限递归重切）。因此由本插件
# 自身完成「按句切分、逐段发送」，达到与 splitter 等同的分段效果，且不依赖
# splitter 是否启用、也不受 split_scope=llm_only 的影响。
_SPLIT_DELIM = re.compile(r"([。！？!?；;\n])")
SEGMENT_SEND_INTERVAL = 0.8  # 段间发送间隔（秒），模拟真人节奏、避免刷屏/限流


def split_long_message(text: str) -> List[str]:
    """将通知文本按句末标点切分为多段（与 splitter 简单模式语义一致）。

    规则：以 。！？ ! ? ； ; 换行 作为切分点，分隔符附加到前一段末尾；
    连续分隔符视为一个切分点（折叠）；返回非空片段列表。若文本无需切分则返回 [text]。

    Args:
        text: 已规范化的通知文本

    Returns:
        List[str]: 切分后的片段列表（每个片段以句末标点结尾，末段可能无标点）
    """
    if not text:
        return []
    pieces = _SPLIT_DELIM.split(text)
    segments: List[str] = []
    buf = ""
    for i, piece in enumerate(pieces):
        buf += piece
        if i % 2 == 1:  # 奇数索引为分隔符，出现即闭合一段
            seg = buf
            buf = ""
            if seg.strip():
                segments.append(seg)
    if buf.strip():
        segments.append(buf)
    if not segments:
        return [text]
    return segments


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

    async def _send_single(self, umo: str, message: str, group_id: str) -> bool:
        """向单个群组发送单条消息（带重试），返回是否成功。"""
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

    async def send_to_group(self, group_id: str, message: str,
                            platform_id: str = "小砂糖",
                            split: bool = True) -> bool:
        """向单个群组发送消息（带重试）。

        由于 context.send_message 不经过 splitter 的 on_decorating_result 钩子，
        本插件对「长通知」自行按句切分（规则与 splitter 简单模式一致），
        使长通知以多条消息自然分段送达。

        模板回复使用 split=False：作为单条纯文本消息发送，不做任何切分
        （避免模板中的句末标点被误切成多段，保持原样一条送达）。

        Args:
            group_id: QQ群号
            message: 消息内容（应为已规范化的文本）
            platform_id: 平台实例名称（如"小砂糖"），用于构造 UMO
            split: 是否对长消息按句自动切分后逐段发送。
                  True=按句分段（LLM 生成的长通知）；
                  False=整条单条发送（模板回复，保持纯文本单段）。

        Returns:
            bool: 是否发送成功
        """
        umo = f"{platform_id}:GroupMessage:{group_id}"

        # 模板回复：不切分，整条作为单条纯文本发送
        if not split:
            return await self._send_single(umo, message, group_id)

        segments = split_long_message(message)

        # 无需切分：单条直接发送
        if len(segments) <= 1:
            return await self._send_single(umo, message, group_id)

        # 多段：逐段发送，段间略微延迟模拟真人节奏
        all_ok = True
        for idx, seg in enumerate(segments):
            ok = await self._send_single(umo, seg, group_id)
            if not ok:
                all_ok = False
            elif idx < len(segments) - 1:
                await asyncio.sleep(SEGMENT_SEND_INTERVAL)

        if not all_ok:
            logger.warning(
                f"[离线通知] 群 {group_id} 存在分段发送失败，"
                f"共 {len(segments)} 段"
            )

        return all_ok

    async def send_to_groups(self, target_groups: List[dict],
                             message: str, platform_id: str = "小砂糖",
                             split: bool = True) -> dict:
        """向多个群组发送消息

        Args:
            target_groups: 目标群组列表 [{"group_id": "xxx", "group_name": "xxx", "enabled": true}, ...]
            message: 消息内容
            platform_id: 平台标识
            split: 是否对长消息按句分段（True=LLM 长通知，False=模板单条）

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

            success = await self.send_to_group(
                group_id, message, platform_id, split=split
            )
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