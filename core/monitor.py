"""
调度器自我监控模块 - 心跳检测、异常告警

功能:
- 定期检查调度器运行状态
- 当调度器异常时向管理员群发送告警
- 记录异常日志
"""

import asyncio
import time
from typing import Optional
from astrbot.api import logger
from astrbot.api.event import MessageChain

from .platform_compat import (
    PlatformCaps,
    validate_target_id,
    validate_target_id_multi,
    normalize_capses,
)


class SchedulerMonitor:
    """调度器自我监控器"""

    def __init__(self, context, scheduler, config: dict, caps: Optional[PlatformCaps] = None):
        """初始化监控器

        Args:
            context: AstrBot Context 对象
            scheduler: NotificationScheduler 实例
            config: monitor_config 配置节
            caps: 平台能力画像列表。为 None 时按 config["platform_ids"] 构造。
        """
        self.context = context
        self.scheduler = scheduler
        self.enabled = config.get("enable_monitor", True)
        self.heartbeat_interval = config.get("heartbeat_interval", 300)
        self.heartbeat_timeout = config.get("heartbeat_timeout", 180)
        self.alert_admin_group = config.get("alert_admin_group", "")
        self.alert_qq = config.get("alert_qq", "")
        raw = caps if caps is not None else config.get("platform_ids", [])
        self.capses = normalize_capses(raw)
        self.caps = self.capses[0] if self.capses else PlatformCaps(platform_id="")
        self.platform_id = self.caps.platform_id

        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._consecutive_failures = 0
        self._alert_cooldown = 0  # 告警冷却时间戳
        self._alert_cooldown_seconds = 1800  # 30 分钟冷却

    async def start(self):
        """启动监控"""
        if not self.enabled:
            logger.info("[离线通知] 自我监控已禁用")
            return

        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info(
            f"[离线通知] 自我监控已启动，心跳间隔 {self.heartbeat_interval}s"
        )

    async def stop(self):
        """停止监控"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[离线通知] 自我监控已停止")

    async def _monitor_loop(self):
        """监控循环"""
        while self._running:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                if not self._running:
                    break
                await self._check_health()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[离线通知] 监控循环异常: {e}", exc_info=True)
                await asyncio.sleep(60)  # 异常后等待 1 分钟再继续

    async def _check_health(self):
        """执行健康检查"""
        status = self.scheduler.get_status()

        if not status["running"]:
            self._consecutive_failures += 1
            logger.error(
                f"[离线通知] 调度器未运行! 连续失败: {self._consecutive_failures}"
            )
            await self._maybe_alert(
                f"调度器未运行，连续检测失败 {self._consecutive_failures} 次"
            )
            return

        # 检查心跳年龄
        heartbeat_age = status.get("heartbeat_age")
        if heartbeat_age is not None and heartbeat_age > self.heartbeat_timeout:
            self._consecutive_failures += 1
            logger.error(
                f"[离线通知] 调度器心跳超时: {heartbeat_age:.0f}s "
                f"(阈值 {self.heartbeat_timeout}s), "
                f"连续失败: {self._consecutive_failures}"
            )
            await self._maybe_alert(
                f"调度器心跳超时 ({heartbeat_age:.0f}s)，"
                f"可能已停止响应"
            )
        else:
            # 恢复正常
            if self._consecutive_failures > 0:
                logger.info(
                    f"[离线通知] 调度器已恢复正常，之前连续失败 "
                    f"{self._consecutive_failures} 次"
                )
            self._consecutive_failures = 0

        # 检查错误率
        if status["error_count"] > 0:
            logger.warning(
                f"[离线通知] 调度器累计错误: {status['error_count']}, "
                f"最近错误: {status.get('last_error', 'N/A')}"
            )

    async def _maybe_alert(self, message: str):
        """发送告警（带冷却机制）

        Args:
            message: 告警信息
        """
        now = time.time()

        # 检查冷却时间
        if now < self._alert_cooldown:
            logger.info(f"[离线通知] 告警冷却中，跳过: {message}")
            return

        logger.warning(f"[离线通知] 发送告警: {message}")

        alert_text = f"【离线通知系统告警】\n\n{message}\n\n请检查 AstrBot 插件状态。"

        # 多平台：对配置的每个平台实例都尝试发送告警。
        # 注意：QQ 官方协议下群消息需要 5 分钟内的被动 msg_id，深夜告警多半发不出去，
        # 因此私聊（C2C 主动推送不受 msg_id 限制）才是官方协议下的可靠告警通道。
        if self.alert_admin_group:
            await self._send_alert_multi(
                self.alert_admin_group, alert_text,
                channel="群", label="告警群号", friend=False,
            )
        if self.alert_qq:
            await self._send_alert_multi(
                self.alert_qq, alert_text,
                channel="私聊", label="告警QQ/openid", friend=True,
            )

        # 设置冷却时间
        self._alert_cooldown = now + self._alert_cooldown_seconds

    async def _send_alert_multi(self, target, text, *, channel, label, friend) -> None:
        """对一个告警目标，在每一个形态匹配的平台实例上都发送一遍。

        只要有一个平台实例发成功即记为已送达；全部平台都失败才提示。
        """
        ok, hint = validate_target_id_multi(self.capses, target, label=label)
        if not ok:
            logger.error(f"[离线通知] 告警发送跳过({channel}): {hint}")
            return
        any_sent = False
        for caps in self.capses:
            ok2, _ = validate_target_id(caps, target, label=label)
            if not ok2:
                continue  # 该平台形态不符（如 QQ 官方下收到数字群号），跳过本平台
            umo = caps.friend_umo(target) if friend else caps.group_umo(target)
            sent = await self._send_alert(
                umo, text, target=target, channel=channel, label=label, caps=caps
            )
            any_sent = any_sent or sent
        if not any_sent:
            logger.error(
                f"[离线通知] 告警发送失败({channel}): 所有匹配平台均未能送达"
            )

    async def _send_alert(self, umo: str, text: str, *, target: str,
                          channel: str, label: str,
                          caps: Optional[PlatformCaps] = None) -> bool:
        """发送一条告警，带平台标识校验与真实成败判定。

        改造前的问题：``context.send_message`` 找不到平台实例时只返回 False 不抛异常，
        旧代码因此会打出「已发送告警」的假日志。这里显式检查返回值。
        """
        caps = caps or self.caps
        ok, hint = validate_target_id(caps, target, label=label)
        if not ok:
            logger.error(f"[离线通知] 告警发送跳过({channel}): {hint}")
            return False

        try:
            chain = MessageChain().message(text)
            if caps.force_plain_text and hasattr(chain, "use_markdown"):
                try:
                    chain.use_markdown(False)
                except Exception:  # pragma: no cover
                    pass
            ret = await self.context.send_message(umo, chain)
            if ret is False:
                logger.error(
                    f"[离线通知] 告警发送失败({channel}): 找不到平台实例 "
                    f"'{caps.platform_id}'，消息未发出（UMO={umo}）"
                )
                return False
            logger.info(f"[离线通知] 已发送告警到{channel} {target}")
            return True
        except Exception as e:
            logger.error(f"[离线通知] 告警发送失败({channel}): {e}")
            return False