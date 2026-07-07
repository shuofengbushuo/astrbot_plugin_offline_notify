"""
定时调度引擎 - 基于 APScheduler 实现可靠的定时通知调度

核心功能:
- 支持每日定时触发（工作日/周末/特定日期）
- 支持提前 N 分钟发送通知
- 支持多计划并行调度
- 调度器状态监控
"""

import asyncio
import time
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Dict

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.base import JobLookupError
from astrbot.api import logger


class NotificationScheduler:
    """基于 APScheduler 的定时通知调度器"""

    # 星期映射: 关键字 -> cron day_of_week
    DAY_MAP = {
        "monday": "mon", "tuesday": "tue", "wednesday": "wed",
        "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun"
    }

    def __init__(self):
        self._scheduler: Optional[AsyncIOScheduler] = None
        self._callback: Optional[Callable] = None
        self._job_ids: List[str] = []
        self._running = False
        self._last_trigger_time: Optional[float] = None
        self._trigger_count: int = 0
        self._error_count: int = 0
        self._last_error: Optional[str] = None
        self._last_heartbeat: Optional[float] = None

    # ── 公共 API ──────────────────────────────────────────

    def set_callback(self, callback: Callable):
        """设置触发回调函数

        Args:
            callback: async def callback(schedule_name: str, offline_time: str,
                                         countdown_minutes: int) -> None
        """
        self._callback = callback

    def configure_and_start(self, schedules: List[dict],
                            global_advance_minutes: int = 5):
        """根据配置创建调度任务并启动

        Args:
            schedules: 调度计划列表
            global_advance_minutes: 全局提前通知分钟数
        """
        if not schedules:
            logger.warning("[离线通知] 没有配置任何调度计划，跳过启动")
            return

        self._scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

        for schedule in schedules:
            if not schedule.get("enabled", True):
                logger.info(f"[离线通知] 计划 '{schedule.get('name')}' 已禁用，跳过")
                continue

            job_id = self._add_job(schedule, global_advance_minutes)
            if job_id:
                self._job_ids.append(job_id)

        if self._job_ids:
            self._scheduler.start()
            self._running = True
            self._last_heartbeat = time.time()
            logger.info(f"[离线通知] 调度器已启动，共 {len(self._job_ids)} 个定时任务")
        else:
            logger.warning("[离线通知] 没有有效的调度任务，调度器未启动")

    async def shutdown(self):
        """安全关闭调度器"""
        self._running = False
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
                logger.info("[离线通知] 调度器已关闭")
            except Exception as e:
                logger.error(f"[离线通知] 调度器关闭异常: {e}")

    async def trigger_manual(self, schedule_name: str = "手动触发",
                             offline_time: str = None,
                             countdown_minutes: int = 5) -> bool:
        """手动触发一次通知（用于测试/预览）

        Args:
            schedule_name: 计划名称
            offline_time: 下线时间，默认使用当前时间
            countdown_minutes: 剩余分钟数

        Returns:
            bool: 是否成功触发
        """
        if not self._callback:
            logger.error("[离线通知] 未设置回调函数，无法手动触发")
            return False

        if offline_time is None:
            offline_time = datetime.now().strftime("%H:%M")

        try:
            await self._callback(schedule_name, offline_time, countdown_minutes)
            return True
        except Exception as e:
            logger.error(f"[离线通知] 手动触发失败: {e}")
            return False

    def get_status(self) -> dict:
        """获取调度器运行状态

        Returns:
            dict: 状态信息
        """
        now = time.time()
        jobs_info = []
        if self._scheduler:
            for job in self._scheduler.get_jobs():
                jobs_info.append({
                    "id": job.id,
                    "name": job.name,
                    "next_run": str(job.next_run_time) if job.next_run_time else None,
                })

        return {
            "running": self._running,
            "job_count": len(self._job_ids),
            "jobs": jobs_info,
            "last_trigger_time": self._last_trigger_time,
            "trigger_count": self._trigger_count,
            "error_count": self._error_count,
            "last_error": self._last_error,
            "last_heartbeat": self._last_heartbeat,
            "heartbeat_age": (now - self._last_heartbeat) if self._last_heartbeat else None,
        }

    # ── 内部方法 ──────────────────────────────────────────

    def _add_job(self, schedule: dict, global_advance: int) -> Optional[str]:
        """添加单个调度任务

        Args:
            schedule: 单个计划配置
            global_advance: 全局提前分钟数

        Returns:
            str | None: 任务 ID，失败返回 None
        """
        offline_time = schedule.get("offline_time", "23:00")
        advance = schedule.get("advance_minutes", global_advance)
        day_type = schedule.get("day_type", "everyday")
        schedule_name = schedule.get("name", "未命名计划")

        try:
            hour, minute = self._parse_time(offline_time)
            # 计算实际触发时间（提前 advance 分钟）
            trigger_hour, trigger_minute = self._calc_trigger_time(
                hour, minute, advance
            )
        except ValueError as e:
            logger.error(f"[离线通知] 计划 '{schedule_name}' 时间格式错误: {e}")
            return None

        try:
            cron_kwargs = self._build_cron_kwargs(day_type, schedule,
                                                  trigger_hour, trigger_minute)
        except ValueError as e:
            logger.error(f"[离线通知] 计划 '{schedule_name}' 配置错误: {e}")
            return None

        job_id = f"offline_notify_{schedule_name}_{trigger_hour:02d}{trigger_minute:02d}"
        trigger = CronTrigger(**cron_kwargs, timezone="Asia/Shanghai")

        # 封装回调，传递计划参数
        async def job_wrapper():
            await self._on_trigger(schedule_name, offline_time, advance)

        try:
            self._scheduler.add_job(
                job_wrapper,
                trigger=trigger,
                id=job_id,
                name=schedule_name,
                replace_existing=True,
            )
            logger.info(
                f"[离线通知] 已添加计划 '{schedule_name}': "
                f"{day_type} {trigger_hour:02d}:{trigger_minute:02d} 触发 "
                f"(下线时间 {offline_time}, 提前 {advance} 分钟)"
            )
            return job_id
        except Exception as e:
            logger.error(f"[离线通知] 添加计划 '{schedule_name}' 失败: {e}")
            return None

    def _build_cron_kwargs(self, day_type: str, schedule: dict,
                           hour: int, minute: int) -> dict:
        """构建 CronTrigger 参数

        Args:
            day_type: 日期类型
            schedule: 计划配置
            hour: 触发小时
            minute: 触发分钟

        Returns:
            dict: cron 参数
        """
        base = {"hour": hour, "minute": minute}

        if day_type == "everyday":
            # 每天触发
            return base

        elif day_type == "weekday":
            # 周一至周五
            base["day_of_week"] = "mon-fri"
            return base

        elif day_type == "weekend":
            # 周六、周日
            base["day_of_week"] = "sat,sun"
            return base

        elif day_type == "specific":
            # 特定星期
            specific_days = schedule.get("specific_days", [])
            if not specific_days:
                raise ValueError("day_type 为 specific 但未指定 specific_days")

            day_codes = []
            for day in specific_days:
                code = self.DAY_MAP.get(day.lower())
                if code:
                    day_codes.append(code)
                else:
                    logger.warning(f"[离线通知] 未知的星期: {day}")

            if not day_codes:
                raise ValueError(f"无法解析的特定日期: {specific_days}")

            base["day_of_week"] = ",".join(day_codes)
            return base

        else:
            raise ValueError(f"未知的 day_type: {day_type}")

    async def _on_trigger(self, schedule_name: str, offline_time: str,
                          countdown_minutes: int):
        """调度器触发回调"""
        self._last_trigger_time = time.time()
        self._trigger_count += 1
        self._last_heartbeat = time.time()

        logger.info(
            f"[离线通知] 定时触发: 计划 '{schedule_name}', "
            f"下线时间 {offline_time}, 剩余 {countdown_minutes} 分钟"
        )

        if self._callback:
            try:
                await self._callback(schedule_name, offline_time, countdown_minutes)
            except Exception as e:
                self._error_count += 1
                self._last_error = str(e)
                logger.error(
                    f"[离线通知] 回调执行失败: {e}",
                    exc_info=True
                )
        else:
            logger.warning("[离线通知] 未设置回调函数，通知未发送")

    # ── 工具方法 ──────────────────────────────────────────

    @staticmethod
    def _parse_time(time_str: str) -> tuple:
        """解析时间字符串 HH:MM

        Args:
            time_str: 时间字符串

        Returns:
            (hour, minute): 元组

        Raises:
            ValueError: 格式错误
        """
        parts = time_str.strip().split(":")
        if len(parts) != 2:
            raise ValueError(f"时间格式错误，应为 HH:MM: {time_str}")

        hour = int(parts[0])
        minute = int(parts[1])

        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"时间超出范围: {time_str}")

        return hour, minute

    @staticmethod
    def _calc_trigger_time(hour: int, minute: int,
                           advance_minutes: int) -> tuple:
        """计算提前通知的实际触发时间

        Args:
            hour: 下线小时
            minute: 下线分钟
            advance_minutes: 提前分钟数

        Returns:
            (trigger_hour, trigger_minute): 实际触发时间
        """
        offline_dt = datetime.now().replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        trigger_dt = offline_dt - timedelta(minutes=advance_minutes)
        return trigger_dt.hour, trigger_dt.minute