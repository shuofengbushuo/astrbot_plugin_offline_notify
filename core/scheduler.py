"""定时调度引擎 —— 基于 APScheduler 实现「时间窗口」调度。

核心功能:
- 每个计划生成两个 cron 任务：监听开始（offline_time - window_before_minutes）
  与监听结束（offline_time + window_after_minutes）。
- 监听开始回调：武装计划，标记进入「监听窗口」，开始留意群消息。
- 监听结束回调：退出监听窗口，清理状态。窗口内若一直没收到群消息则当天不发送
  （无被动 msg_id 时适配器会直接跳过，不再做无效的主动推送降级）。
- 实际发送由事件监听器在窗口内命中目标群消息时触发（见 main.py）。
- 支持工作日 / 周末 / 特定日期。

监听开始 / 监听结束两个 cron 均按 day_type 触发（工作日=周一~五、周末=周六/日、特定日=指定日），
二者 day_of_week 一致，保证「代表周期」准确。窗口的武装/退出完全由 armed 状态机驱动，
与 cron 是否在当天触发无关；跨天（offline_time 临近 0 点、偏移后落在非匹配日）的极端情形
由 _build_cron_kwargs 同构处理，避免「关窗时刻跨天导致 day_of_week 错位」。
"""

import time
from typing import Callable, List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.base import JobLookupError
from astrbot.api import logger


class NotificationScheduler:
    """基于 APScheduler 的时间窗口调度器。"""

    # 星期映射: 关键字 -> cron day_of_week
    DAY_MAP = {
        "monday": "mon", "tuesday": "tue", "wednesday": "wed",
        "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun"
    }

    def __init__(self):
        self._scheduler: Optional[AsyncIOScheduler] = None
        self._on_open: Optional[Callable] = None
        self._on_close: Optional[Callable] = None
        self._job_ids: List[str] = []
        self._running = False
        self._last_trigger_time: Optional[float] = None
        self._trigger_count: int = 0
        self._error_count: int = 0
        self._last_error: Optional[str] = None
        self._last_heartbeat: Optional[float] = None
        self._heartbeat_task = None
        self._heartbeat_interval: int = 60  # 心跳间隔（秒）

    # ── 公共 API ──────────────────────────────────────────

    def set_callbacks(self, on_open: Callable, on_close: Callable):
        """设置窗口开 / 关回调。

        Args:
            on_open: async def(plan_name, offline_time) —— 监听开始，武装计划，开始留意群消息。
            on_close: async def(plan_name, offline_time) —— 监听窗口结束，清理状态（不再发送）。
        """
        self._on_open = on_open
        self._on_close = on_close

    def configure_and_start(self, schedules: List[dict],
                            window_before_minutes: int = 30,
                            window_after_minutes: int = 30):
        """根据配置创建窗口调度任务并启动。

        Args:
            schedules: 调度计划列表
            window_before_minutes: 窗口在 offline_time 之前提前开启的分钟数
            window_after_minutes: 窗口在 offline_time 之后延后关闭的分钟数
        """
        if not schedules:
            logger.warning("[离线通知] 没有配置任何调度计划，跳过启动")
            return

        self._scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

        for schedule in schedules:
            if not schedule.get("enabled", True):
                logger.info(f"[离线通知] 计划 '{schedule.get('name')}' 已禁用，跳过")
                continue
            self._add_window_jobs(schedule, window_before_minutes,
                                  window_after_minutes)

        if self._job_ids:
            self._scheduler.start()
            self._running = True
            self._last_heartbeat = time.time()
            self._start_heartbeat()
            logger.info(
                f"[离线通知] 调度器已启动，共 {len(self._job_ids)} 个窗口任务"
            )
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

    def reload_jobs(self, schedules: List[dict],
                    window_before_minutes: int = 30,
                    window_after_minutes: int = 30):
        """动态重载所有窗口任务（先移除旧任务，再添加新任务）。"""
        if not self._scheduler:
            logger.warning("[离线通知] 调度器未初始化，无法重载")
            return

        for job_id in list(self._job_ids):
            try:
                self._scheduler.remove_job(job_id)
            except JobLookupError:
                pass
        self._job_ids.clear()

        if not schedules:
            logger.info("[离线通知] 计划列表为空，已清除所有任务")
            return

        for schedule in schedules:
            if not schedule.get("enabled", True):
                logger.info(f"[离线通知] 计划 '{schedule.get('name')}' 已禁用，跳过")
                continue
            self._add_window_jobs(schedule, window_before_minutes,
                                  window_after_minutes)

        logger.info(f"[离线通知] 计划已重载，当前 {len(self._job_ids)} 个窗口任务")

    @staticmethod
    def _job_kind(job_id: str) -> str:
        """从 job id 判定任务类型：open=监听开始 / close=监听结束 / other。"""
        jid = job_id or ""
        if "_open_" in jid:
            return "open"
        if "_close_" in jid:
            return "close"
        return "other"

    def get_status(self) -> dict:
        """获取调度器运行状态"""
        now = time.time()
        jobs_info = []
        if self._scheduler:
            for job in self._scheduler.get_jobs():
                jobs_info.append({
                    "id": job.id,
                    "name": job.name,
                    # 每个计划会拆成两个任务：open=监听开始，close=监听结束。
                    # 二者 name 相同，前端需靠 kind 区分，否则看起来像重复条目。
                    "kind": self._job_kind(job.id),
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

    # ── 内部：心跳 ─────────────────────────────────────────

    def _start_heartbeat(self):
        import asyncio
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info(f"[离线通知] 调度器心跳已启动，间隔 {self._heartbeat_interval}s")

    def _stop_heartbeat(self):
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()

    async def _heartbeat_loop(self):
        import asyncio
        while self._running:
            try:
                await asyncio.sleep(self._heartbeat_interval)
                if not self._running:
                    break
                self._last_heartbeat = time.time()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[离线通知] 心跳循环异常: {e}", exc_info=True)
                await asyncio.sleep(self._heartbeat_interval)

    # ── 内部：窗口任务 ─────────────────────────────────────

    def _add_window_jobs(self, schedule: dict, window_before: int,
                         window_after: int):
        """为单个计划添加监听开始 / 监听结束两个 cron 任务。"""
        offline_time = schedule.get("offline_time", "23:00")
        day_type = schedule.get("day_type", "everyday")
        plan_name = schedule.get("name", "未命名计划")

        try:
            hour, minute = self._parse_time(offline_time)
            open_h, open_m = self._offset_time(hour, minute, -window_before)
            close_h, close_m = self._offset_time(hour, minute, window_after)
        except ValueError as e:
            logger.error(f"[离线通知] 计划 '{plan_name}' 时间格式错误: {e}")
            return

        try:
            open_kwargs = self._build_cron_kwargs(day_type, schedule, open_h, open_m)
        except ValueError as e:
            logger.error(f"[离线通知] 计划 '{plan_name}' 配置错误: {e}")
            return

        # 监听结束任务与监听开始任务使用相同的 day_of_week，代表周期保持一致
        # （工作日=周一~五、周末=周六/日）。armed 状态机仍负责实际武装/退出，
        # cron 是否在当天触发仅决定「何时尝试退出」，不影响正确性。
        close_kwargs = self._build_cron_kwargs(day_type, schedule, close_h, close_m)

        open_job_id = f"offline_notify_open_{plan_name}_{open_h:02d}{open_m:02d}"
        close_job_id = f"offline_notify_close_{plan_name}_{close_h:02d}{close_m:02d}"

        async def open_wrapper():
            await self._on_window_open(plan_name, offline_time)

        async def close_wrapper():
            await self._on_window_close(plan_name, offline_time)

        try:
            self._scheduler.add_job(
                open_wrapper,
                trigger=CronTrigger(**open_kwargs, timezone="Asia/Shanghai"),
                id=open_job_id, name=plan_name, replace_existing=True,
            )
            self._scheduler.add_job(
                close_wrapper,
                trigger=CronTrigger(**close_kwargs, timezone="Asia/Shanghai"),
                id=close_job_id, name=plan_name, replace_existing=True,
            )
            self._job_ids.extend([open_job_id, close_job_id])
            logger.info(
                f"[离线通知] 已添加计划 '{plan_name}': {day_type} "
                f"窗口 [{open_h:02d}:{open_m:02d}, {close_h:02d}:{close_m:02d}] "
                f"(下线 {offline_time}, 前 {window_before}min / 后 {window_after}min)"
            )
        except Exception as e:
            logger.error(f"[离线通知] 添加计划 '{plan_name}' 失败: {e}")

    def _build_cron_kwargs(self, day_type: str, schedule: dict,
                           hour: int, minute: int) -> dict:
        """构建监听开始任务的 CronTrigger 参数（含 day_of_week）。"""
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

    async def _on_window_open(self, plan_name: str, offline_time: str):
        self._mark_trigger()
        logger.info(
            f"[离线通知] 计划 '{plan_name}' 进入时间窗口（下线 {offline_time}）"
        )
        if self._on_open:
            try:
                await self._on_open(plan_name, offline_time)
            except Exception as e:
                self._error_count += 1
                self._last_error = str(e)
                logger.error(f"[离线通知] 监听开始回调执行失败: {e}", exc_info=True)

    async def _on_window_close(self, plan_name: str, offline_time: str):
        self._mark_trigger()
        logger.info(
            f"[离线通知] 计划 '{plan_name}' 时间窗口关闭（下线 {offline_time}）"
        )
        if self._on_close:
            try:
                await self._on_close(plan_name, offline_time)
            except Exception as e:
                self._error_count += 1
                self._last_error = str(e)
                logger.error(f"[离线通知] 监听结束回调执行失败: {e}", exc_info=True)

    def _mark_trigger(self):
        self._last_trigger_time = time.time()
        self._trigger_count += 1
        self._last_heartbeat = time.time()

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
    def _offset_time(hour: int, minute: int, offset_minutes: int) -> tuple:
        """计算偏移 offset_minutes 后的 (hour, minute)，跨天回绕（0~1439）。"""
        total = (hour * 60 + minute + offset_minutes) % (24 * 60)
        return total // 60, total % 60
