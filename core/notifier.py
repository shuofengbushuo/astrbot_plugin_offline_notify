"""群组通知模块 - 负责向指定会话发送消息，包含重试机制、平台适配与错误日志"""

import asyncio
import re
import time
from typing import List, Optional, Union
from astrbot.api import logger
from astrbot.api.event import MessageChain

from .platform_compat import (
    PlatformCaps,
    inspect_qq_official_session,
    validate_target_id,
    validate_target_id_multi,
    normalize_capses,
    DEFAULT_SEGMENT_INTERVAL,
)

# 与 splitter 简单模式一致的分段规则：按句末标点切分，分隔符附加到前一段末尾。
# 说明：离线通知通过 context.send_message 直接发送给指定群，而该路径不经过
# splitter 的 on_decorating_result 装饰钩子（框架设计上 send_message 绕过装饰阶段，
# 否则 splitter 自己用 send_message 回发切分段时会无限递归重切）。因此由本插件
# 自身完成「按句切分、逐段发送」，达到与 splitter 等同的分段效果，且不依赖
# splitter 是否启用、也不受 split_scope=llm_only 的影响。
_SPLIT_DELIM = re.compile(r"([。！？!?；;\n])")
SEGMENT_SEND_INTERVAL = DEFAULT_SEGMENT_INTERVAL  # 段间发送间隔（秒），向后兼容的模块级默认值


class NotifySendError(Exception):
    """发送失败且可携带「是否值得重试」标记的异常。"""

    def __init__(self, reason: str, *, retryable: bool = True):
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


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


def _as_caps(platform: Union[str, PlatformCaps, None]) -> PlatformCaps:
    """把「平台实例名字符串」或 PlatformCaps 统一成 PlatformCaps。

    向后兼容：旧调用方（含既有测试）传的是字符串平台名，此时构造一个
    「未解析」的 caps，行为与改造前完全一致（不合并分段、0.8s 间隔）。
    """
    if isinstance(platform, PlatformCaps):
        return platform
    return PlatformCaps(platform_id=str(platform or ""))


class GroupNotifier:
    """群组通知发送器，支持重试机制与多协议适配"""

    def __init__(self, context, retry_config: dict):
        """初始化通知器

        Args:
            context: AstrBot Context 对象
            retry_config: 重试配置 {"send_retries": 3, "send_retry_interval": 10}
        """
        self.context = context
        # 优先读新键 send_retries / send_retry_interval，向后兼容旧键。
        self.max_retries = retry_config.get(
            "send_retries", retry_config.get("max_retries", 3))
        self.retry_interval_base = retry_config.get(
            "send_retry_interval", retry_config.get("retry_interval_base", 10))
        self._send_stats = {
            "total_sent": 0,
            "total_failed": 0,
            "last_send_time": None,
            "last_error": None,
        }

    # ── 发送前置检查 ────────────────────────────────────

    def _precheck(self, caps: PlatformCaps, umo: str, group_id: str,
                  *, allow_proactive: bool = False) -> Optional[str]:
        """发送前的平台前置条件检查。

        仅对「已解析到 QQ 官方平台实例」的情况生效；旧式字符串调用（caps.inst 为 None）
        一律放行，保证既有平台行为零变化。

        QQ 官方群消息能否发出，取决于适配器 `qqofficial_platform_adapter`
        `_send_by_session_common` 的 skip 条件：无 msg_id 且群主动推送不可用时，
        适配器**静默 return**（不抛异常、不返回 False）→ 上层会误判成功。
        这里提前复刻同一判定，把「发不出去」变成可识别的失败。

        Returns:
            None 表示可以发送；否则返回不可重试的失败原因（已带 marker 前缀）。
        """
        session_id = umo.split(":", 2)[-1]
        info = inspect_qq_official_session(caps, session_id)
        if not info["applicable"]:
            return None
        if info["has_msg_id"]:
            return None
        # 无被动 msg_id：仅当适配器确实会走群主动推送路径时才放行
        if allow_proactive and self._group_proactive_usable(caps, info):
            return None
        return (
            f"qq_official_session_not_ready: 群 {group_id} {info['reason']}"
        )

    @staticmethod
    def _group_proactive_usable(caps: PlatformCaps, info: dict) -> bool:
        """适配器是否会在本会话走「群主动推送」路径（而非静默丢弃）。

        复刻 adapter L370-374：``GROUP_MESSAGE and scene == "group"
        and _allow_group_proactive_send``。scene 同样只在收到该群消息时写入，
        因此「本次运行期间没人说话」⇒ scene 为空 ⇒ 主动推送也不成立。
        """
        if not getattr(caps.inst, "_allow_group_proactive_send", False):
            return False
        return info.get("scene") == "group"

    async def _send_single(self, umo: str, message: str, group_id: str,
                           caps: Optional[PlatformCaps] = None,
                           *, allow_proactive: bool = False) -> bool:
        """向单个会话发送单条消息（带重试），返回是否成功。

        关键修复：``Context.send_message`` **返回 bool 而不是抛异常**——
        找不到对应平台实例时它只打一条 warning 并返回 False。旧实现只捕获异常，
        因此这种情况会被记成「发送成功」。这里显式检查返回值。

        ``allow_proactive=True`` 时不因缺少被动 msg_id 直接放弃，允许适配器
        走群主动推送路径；但仍会校验适配器是否真的具备主动推送条件
        （scene=="group" 且 ``_allow_group_proactive_send``），否则提前判失败。
        """
        caps = caps or _as_caps(None)
        chain = MessageChain().message(message)
        if caps.force_plain_text and hasattr(chain, "use_markdown"):
            # QQ 官方事件路径默认走 Markdown（msg_type=2，需要模板报备），
            # 通知内容是纯散文，强制纯文本更稳。
            try:
                chain.use_markdown(False)
            except Exception:  # pragma: no cover - 老版本 MessageChain 无此方法
                pass

        for attempt in range(1, self.max_retries + 1):
            try:
                # 主动推送同样要检查：适配器无 msg_id 且群主动推送不成立时会静默丢弃，
                # 既不抛异常也不返回 False，只能靠发送前探测识别。
                blocker = self._precheck(
                    caps, umo, group_id, allow_proactive=allow_proactive
                )
                if blocker:
                    raise NotifySendError(blocker, retryable=False)

                ret = await self.context.send_message(umo, chain)
                # send_message 返回 False = 没有找到匹配 UMO 的平台实例，消息未发出。
                # 只有显式返回 False 才判失败：老版本/桩实现可能返回 None。
                if ret is False:
                    raise NotifySendError(
                        f"platform_not_found: 找不到平台实例 '{caps.platform_id}'，"
                        f"消息未发出（UMO={umo}）。请检查插件配置 platform_id "
                        f"是否与 WebUI 中已启用的适配器名称一致。",
                        retryable=False,
                    )

                logger.info(f"[离线通知] 成功发送通知到群 {group_id}")
                self._update_stats(success=True)
                return True

            except NotifySendError as e:
                self._update_stats(success=False, error=e.reason)
                if not e.retryable:
                    logger.error(
                        f"[离线通知] 发送到群 {group_id} 失败（不可重试）: {e.reason}"
                    )
                    return False
                logger.warning(
                    f"[离线通知] 发送到群 {group_id} 失败 "
                    f"(第 {attempt}/{self.max_retries} 次尝试): {e.reason}"
                )
                if attempt < self.max_retries:
                    await self._wait_before_retry(attempt)
                else:
                    self._log_final_failure(group_id)

            except Exception as e:
                logger.warning(
                    f"[离线通知] 发送到群 {group_id} 失败 "
                    f"(第 {attempt}/{self.max_retries} 次尝试): {e}"
                )
                self._update_stats(success=False, error=str(e))

                if attempt < self.max_retries:
                    await self._wait_before_retry(attempt)
                else:
                    self._log_final_failure(group_id)

        return False

    async def _wait_before_retry(self, attempt: int) -> None:
        """递增等待后重试。"""
        wait_seconds = self.retry_interval_base * attempt
        logger.info(f"[离线通知] 等待 {wait_seconds}s 后重试...")
        await asyncio.sleep(wait_seconds)

    def _log_final_failure(self, group_id: str) -> None:
        logger.error(
            f"[离线通知] 发送到群 {group_id} 最终失败，"
            f"已尝试 {self.max_retries} 次"
        )

    # ── 分段策略 ────────────────────────────────────────

    @staticmethod
    def _plan_segments(message: str, split: bool, caps: PlatformCaps) -> List[str]:
        """按平台能力决定分段方式。

        - ``split=False``（模板回复）：始终单条。
        - ``caps.merge_segments`` 为真（QQ 官方默认）：合并为单条。官方群消息
          占用被动回复次数或主动推送配额，逐段发送会成倍消耗并易触发频控，
          合并后仅用换行保留分句节奏。
        - 否则（其他平台 / 强制逐段）：按句分段。
        """
        if not split:
            return [message]

        segments = split_long_message(message)
        if len(segments) <= 1:
            return [message]

        if caps.merge_segments:
            merged = "\n".join(seg.strip() for seg in segments if seg.strip())
            logger.info(
                f"[离线通知] 平台 {caps.platform_type or caps.platform_id} "
                f"合并 {len(segments)} 段为单条发送（节省官方消息配额、规避频控）"
            )
            return [merged or message]

        return segments

    # ── 对外发送接口 ────────────────────────────────────

    async def send_to_group(self, group_id: str, message: str,
                            capses: Union[str, PlatformCaps, list] = "小砂糖",
                            split: bool = True,
                            platform_id: Optional[Union[str, PlatformCaps, list]] = None,
                            *, allow_proactive: bool = False) -> bool:
        """向单个群组发送消息（带重试）。

        Args:
            group_id: 群标识（QQ 官方为 group_openid）。
            message: 消息内容（应为已规范化的文本）
            capses: 平台能力画像，可为单个 PlatformCaps / 字符串平台名 / list。
            split: 是否对长消息按句自动切分后逐段发送。
            platform_id: ``capses`` 的兼容别名（旧代码用关键字 platform_id 调用）
            allow_proactive: True 时允许走主动推送（跳过被动 msg_id 前置检查）

        Returns:
            bool: 是否发送成功
        """
        if platform_id is not None:
            capses = platform_id
        caps_list = normalize_capses(capses)
        if not caps_list:
            logger.error(f"[离线通知] 没有可用的平台实例，无法发送通知到群 {group_id}")
            return False

        any_ok = False
        for caps in caps_list:
            ok = await self._send_to_group_via(
                group_id, message, caps, split, allow_proactive=allow_proactive
            )
            any_ok = any_ok or ok
        return any_ok

    async def _send_to_group_via(self, group_id: str, message: str,
                                 caps: PlatformCaps, split: bool,
                                 *, allow_proactive: bool = False) -> bool:
        """在单个平台实例上发送（含形态校验 + 分段/合并策略）。

        群号形态与本平台不符时直接跳过本平台，不计入失败。
        ``allow_proactive=True`` 时允许走群主动推送（仍需适配器具备主动推送条件）。
        """
        ok, hint = validate_target_id(caps, group_id)
        if not ok:
            logger.info(
                f"[离线通知] 群 {group_id} 在平台 {caps.platform_id} 形态不符，"
                f"跳过: {hint}"
            )
            return False

        umo = caps.group_umo(group_id)
        segments = self._plan_segments(message, split, caps)

        if len(segments) <= 1:
            return await self._send_single(
                umo, segments[0], group_id, caps, allow_proactive=allow_proactive
            )

        # 多段：逐段发送，段间略微延迟模拟真人节奏
        all_ok = True
        for idx, seg in enumerate(segments):
            ok = await self._send_single(
                umo, seg, group_id, caps, allow_proactive=allow_proactive
            )
            if not ok:
                all_ok = False
            elif idx < len(segments) - 1:
                await asyncio.sleep(caps.segment_interval)

        if not all_ok:
            logger.warning(
                f"[离线通知] 群 {group_id} 在平台 {caps.platform_id} 存在分段发送失败，"
                f"共 {len(segments)} 段"
            )

        return all_ok

    async def send_to_groups(self, target_groups: List,
                             message: str,
                             capses: Union[str, PlatformCaps, list] = "小砂糖",
                             split: bool = True,
                             platform_id: Optional[Union[str, PlatformCaps, list]] = None,
                             *, allow_proactive: bool = False) -> dict:
        """向多个群组发送消息。

        Args:
            target_groups: 目标群组列表，支持两种格式：
                          - dict: {"group_id": "xxx", "group_name": "xxx", "enabled": true}
                          - str:  "7CA0B1BF..."（直接作为 openid，默认启用）
            message: 消息内容
            capses: 平台能力画像，单个 / 字符串 / list
            split: 是否对长消息按句分段（True=LLM 长通知，False=模板单条）
            platform_id: ``capses`` 的兼容别名
            allow_proactive: True 时允许走主动推送（跳过被动 msg_id 前置检查）

        Returns:
            dict: {"success": [...], "failed": [...], "skipped": [...], "total": int}
        """
        if platform_id is not None:
            capses = platform_id
        caps_list = normalize_capses(capses)
        success_groups = []
        failed_groups = []
        skipped_groups = []

        for group in target_groups:
            if isinstance(group, str):
                group_id = group
            else:
                if not group.get("enabled", True):
                    logger.info(f"[离线通知] 群 {group.get('group_id')} 已禁用，跳过")
                    continue
                group_id = group.get("group_id", "")

            if not group_id:
                logger.warning("[离线通知] 跳过空的群号")
                continue

            ok, hint = validate_target_id_multi(caps_list, group_id)
            if not ok:
                logger.error(
                    f"[离线通知] 目标群「{group_id}」形态不匹配: {hint}"
                )
                skipped_groups.append(group_id)
                continue

            success = await self.send_to_group(
                group_id, message, caps_list, split=split,
                allow_proactive=allow_proactive,
            )
            if success:
                success_groups.append(group_id)
            else:
                failed_groups.append(group_id)

        result = {
            "success": success_groups,
            "failed": failed_groups,
            "skipped": skipped_groups,
            "total": len(success_groups) + len(failed_groups) + len(skipped_groups),
        }

        logger.info(
            f"[离线通知] 通知发送完成: 成功 {len(success_groups)}/{result['total']}, "
            f"失败 {len(failed_groups)}/{result['total']}, "
            f"跳过 {len(skipped_groups)}/{result['total']}"
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
