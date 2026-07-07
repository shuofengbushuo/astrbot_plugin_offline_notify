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


class SchedulerMonitor:
    """调度器自我监控器"""

    def __init__(self, context, scheduler, config: dict):
        """初始化监控器

        Args:
            context: AstrBot Context 对象
            scheduler: NotificationScheduler 实例
            config: monitor_config 配置节
        """
        self.context = context
        self.scheduler = scheduler
        self.enabled = config.get("enable_monitor", True)
        self.heartbeat_interval = config.get("heartbeat_interval", 300)
        self.alert_admin_group = config.get("alert_admin_group", "")
        self.alert_qq = config.get("alert_qq", "")

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
        if heartbeat_age is not None and heartbeat_age > self.heartbeat_interval * 3:
            self._consecutive_failures += 1
            logger.error(
                f"[离线通知] 调度器心跳超时: {heartbeat_age:.0f}s, "
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

        # 发送告警到管理员群
        if self.alert_admin_group:
            try:
                umo = f"default:GroupMessage:{self.alert_admin_group}"
                chain = MessageChain().message(alert_text)
                await self.context.send_message(umo, chain)
                logger.info(
                    f"[离线通知] 已发送告警到群 {self.alert_admin_group}"
                )
            except Exception as e:
                logger.error(f"[离线通知] 告警发送失败(群): {e}")

        # 发送告警到QQ私聊
        if self.alert_qq:
            try:
                umo = f"default:FriendMessage:{self.alert_qq}"
                chain = MessageChain().message(alert_text)
                await self.context.send_message(umo, chain)
                logger.info(
                    f"[离线通知] 已发送告警到QQ {self.alert_qq}"
                )
            except Exception as e:
                logger.error(f"[离线通知] 告警发送失败(QQ): {e}")

        # 设置冷却时间
        self._alert_cooldown = now + self._alert_cooldown_seconds