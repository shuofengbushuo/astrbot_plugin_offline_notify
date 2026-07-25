"""
定时调度引擎 - 基于 APScheduler 实现可靠的定时通知调度，支持浮动时间

核心功能:
- 支持每日定时触发（工作日/周末/特定日期）
- 支持提前 N 分钟 + 浮动范围，避免所有通知集中发送
- 浮动机制: cron 在最早时间触发，回调内随机 sleep 实现浮动
- 支持多计划并行调度
- 调度器状态监控
"""

import asyncio
import random
import time
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Dict

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.base import JobLookupError
from astrbot.api import logger


class NotificationScheduler:
    """基于 APScheduler 的定时通知调度器（支持浮动时间）"""

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
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._heartbeat_interval: int = 60  # 心跳间隔（秒）

    # ── 公共 API ──────────────────────────────────────────

    def set_callback(self, callback: Callable):
        """设置触发回调函数

        Args:
            callback: async def callback(plan_name, offline_time, countdown,
                                         float_seconds, float_range) -> None
        """
        self._callback = callback

    def configure_and_start(self, schedules: List[dict],
                            global_advance_minutes: int = 5,
                            global_float_range: int = 2):
        """根据配置创建调度任务并启动

        Args:
            schedules: 调度计划列表
            global_advance_minutes: 全局提前通知分钟数
            global_float_range: 全局浮动范围（分钟）
        """
        if not schedules:
            logger.warning("[离线通知] 没有配置任何调度计划，跳过启动")
            return

        self._scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

        for schedule in schedules:
            if not schedule.get("enabled", True):
                logger.info(f"[离线通知] 计划 '{schedule.get('name')}' 已禁用，跳过")
                continue

            job_id = self._add_job(schedule, global_advance_minutes,
                                   global_float_range)
            if job_id:
                self._job_ids.append(job_id)

        if self._job_ids:
            self._scheduler.start()
            self._running = True
            self._last_heartbeat = time.time()
            self._start_heartbeat()
            logger.info(f"[离线通知] 调度器已启动，共 {len(self._job_ids)} 个定时任务")
        else:
            logger.warning("[离线通知] 没有有效的调度任务，调度器未启动")

    async def shutdown(self):
        """安全关闭调度器"""
        self._running = False
        self._stop_heartbeat()
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
                logger.info("[离线通知] 调度器已关闭")
            except Exception as e:
                logger.error(f"[离线通知] 调度器关闭异常: {e}")

    async def trigger_manual(self, schedule_name: str = "手动触发",
                             offline_time: str = None,
                             countdown_minutes: int = 5) -> bool:
        """手动触发一次通知（用于测试/预览，不应用浮动）

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
            await self._callback(schedule_name, offline_time, countdown_minutes,
                                 0, 0)
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

    def _start_heartbeat(self):
        """启动定期心跳任务，用于向监控器证明调度器仍在运行"""
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info(
            f"[离线通知] 调度器心跳已启动，间隔 {self._heartbeat_interval}s"
        )

    def _stop_heartbeat(self):
        """停止心跳任务"""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()

    async def _heartbeat_loop(self):
        """心跳循环：定期更新 _last_heartbeat 以证明调度器事件循环仍在运行"""
        while self._running:
            try:
                await asyncio.sleep(self._heartbeat_interval)
                if not self._running:
                    break
                self._last_heartbeat = time.time()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    f"[离线通知] 心跳循环异常: {e}",
                    exc_info=True
                )
                await asyncio.sleep(self._heartbeat_interval)

    def _add_job(self, schedule: dict, global_advance: int,
                 global_float_range: int) -> Optional[str]:
        """添加单个调度任务

        Args:
            schedule: 单个计划配置
            global_advance: 全局提前分钟数
            global_float_range: 全局浮动范围

        Returns:
            str | None: 任务 ID，失败返回 None
        """
        offline_time = schedule.get("offline_time", "23:00")
        advance = schedule.get("advance_minutes", global_advance)
        day_type = schedule.get("day_type", "everyday")
        schedule_name = schedule.get("name", "未命名计划")

        # 浮动范围: 计划级 > 全局级
        float_range = schedule.get("float_range", 0)
        if float_range <= 0:
            float_range = global_float_range

        try:
            hour, minute = self._parse_time(offline_time)
            # 计算 cron 触发时间（最早时间点 = advance + float_range）
            total_early = advance + float_range
            trigger_hour, trigger_minute = self._calc_trigger_time(
                hour, minute, total_early
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

        # 封装回调，传入浮动参数
        async def job_wrapper():
            await self._on_trigger(schedule_name, offline_time, advance,
                                   float_range)

        try:
            self._scheduler.add_job(
                job_wrapper,
                trigger=trigger,
                id=job_id,
                name=schedule_name,
                replace_existing=True,
            )
            float_desc = f"±{float_range}分钟浮动" if float_range > 0 else "精确触发"
            logger.info(
                f"[离线通知] 已添加计划 '{schedule_name}': "
                f"{day_type} {trigger_hour:02d}:{trigger_minute:02d} 触发 "
                f"(下线时间 {offline_time}, 提前 {advance} 分钟, {float_desc})"
            )
            return job_id
        except Exception as e:
            logger.error(f"[离线通知] 添加计划 '{schedule_name}' 失败: {e}")
            return None

    def _build_cron_kwargs(self, day_type: str, schedule: dict,
                           hour: int, minute: int) -> dict:
        """构建 CronTrigger 参数"""
        base = {"hour": hour, "minute": minute}

        if day_type == "everyday":
            return base
        elif day_type == "weekday":
            base["day_of_week"] = "mon-fri"
            return base
        elif day_type == "weekend":
            base["day_of_week"] = "sat,sun"
            return base
        elif day_type == "specific":
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
                          advance_minutes: int, float_range: int):
        """调度器触发回调（含浮动 sleep）

        浮动机制:
        1. Cron 在最早时间触发（offline_time - advance - float_range）
        2. 随机 sleep 0 ~ float_range*60 秒
        3. 实际通知提前 = advance + float_range - sleep_minutes

        Args:
            schedule_name: 计划名称
            offline_time: 下线时间 (HH:MM)
            advance_minutes: 配置的提前分钟数
            float_range: 浮动范围（分钟）
        """
        self._last_trigger_time = time.time()
        self._trigger_count += 1
        self._last_heartbeat = time.time()

        # 随机浮动 sleep
        float_seconds = 0.0
        if float_range > 0:
            float_seconds = random.uniform(0, float_range * 60)
            logger.info(
                f"[离线通知] 计划 '{schedule_name}' cron 触发，"
                f"随机浮动 {float_seconds:.0f} 秒（范围 0-{float_range * 60}s）"
            )
            await asyncio.sleep(float_seconds)

        # 计算实际剩余分钟数
        actual_countdown = advance_minutes + float_range - (float_seconds / 60.0)
        actual_countdown = max(actual_countdown, 0.1)  # 确保至少剩余 0.1 分钟

        logger.info(
            f"[离线通知] 浮动后触发: 计划 '{schedule_name}', "
            f"下线时间 {offline_time}, 实际提前 {actual_countdown:.1f} 分钟, "
            f"浮动 {float_seconds:.0f}s"
        )

        if self._callback:
            try:
                await self._callback(
                    schedule_name, offline_time,
                    round(actual_countdown, 1),
                    float_seconds, float_range
                )
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
        """解析时间字符串 HH:MM"""
        parts = time_str.strip().split(":")
        if len(parts) != 2:
            raise ValueError(f"时间格式错误，应为 HH:MM: {time_str}")
        hour = int(parts[0])
        minute = int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"时间超出范围: {time_str}")
        return hour, minute

    @staticmethod
    def _calc_trigger_time(hour: int, minute: int, early_minutes: int) -> tuple:
        """计算 cron 触发时间（向前偏移 early_minutes 分钟）

        Args:
            hour: 原小时
            minute: 原分钟
            early_minutes: 提前分钟数

        Returns:
            (trigger_hour, trigger_minute): 触发时间
        """
        total_minutes = hour * 60 + minute - early_minutes
        # 处理跨天
        total_minutes = total_minutes % (24 * 60)
        trigger_hour = total_minutes // 60
        trigger_minute = total_minutes % 60
        return trigger_hour, trigger_minute