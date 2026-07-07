"""
AI下线通知系统 - AstrBot 插件主入口

功能概述:
- 基于 APScheduler 实现定时下线通知
- 支持工作日/周末/特定日期差异化时间
- 支持多群组同时通知
- 自定义消息模板（标题、正文、表情符号）
- 通知预览功能
- 发送失败重试机制
- 调度器自我监控与告警

命令:
  /下线通知 状态     - 查看调度器运行状态
  /下线通知 预览     - 预览通知消息效果
  /下线通知 测试     - 手动触发一次测试通知
  /下线通知 统计     - 查看发送统计
"""

import asyncio
from datetime import datetime
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig

from .core import NotificationScheduler, GroupNotifier, TemplateEngine, SchedulerMonitor


@register(
    "astrbot_plugin_offline_notify",
    "AstrBot User",
    "定时向QQ群发送AI服务下线提醒，支持自定义通知内容、多群组、差异化时间等",
    "v1.0.0",
    "https://github.com/astrbot/astrbot_plugin_offline_notify"
)
class OfflineNotifyPlugin(Star):
    """AI下线通知系统插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config

        # 核心组件（延迟初始化）
        self.scheduler: NotificationScheduler = NotificationScheduler()
        self.template_engine: TemplateEngine = None
        self.notifier: GroupNotifier = None
        self.monitor: SchedulerMonitor = None

        # 获取插件数据目录
        self.plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_offline_notify")

    # ── 生命周期 ──────────────────────────────────────────

    async def initialize(self):
        """插件初始化：加载配置、启动调度器和监控"""
        logger.info("[离线通知] 正在初始化...")

        # 初始化模板引擎
        self.template_engine = TemplateEngine(self.config.get("message_template", {}))

        # 初始化通知器
        self.notifier = GroupNotifier(
            self.context,
            self.config.get("retry_config", {})
        )

        # 设置调度器回调
        self.scheduler.set_callback(self._on_schedule_trigger)

        # 配置并启动调度器
        if self.config.get("enable_notification", True):
            schedules = self.config.get("schedules", [])
            global_advance = self.config.get("global_advance_minutes", 5)
            self.scheduler.configure_and_start(schedules, global_advance)
        else:
            logger.info("[离线通知] 通知功能已禁用，调度器未启动")

        # 初始化并启动监控器
        self.monitor = SchedulerMonitor(
            self.context,
            self.scheduler,
            self.config.get("monitor_config", {})
        )
        await self.monitor.start()

        # 注册 WebUI API 路由
        self._register_web_apis()

        logger.info("[离线通知] 初始化完成")

    async def terminate(self):
        """插件卸载：停止调度器和监控"""
        logger.info("[离线通知] 正在停止...")

        if self.monitor:
            await self.monitor.stop()

        if self.scheduler:
            await self.scheduler.shutdown()

        logger.info("[离线通知] 已停止")

    # ── 调度器回调 ─────────────────────────────────────────

    async def _on_schedule_trigger(self, schedule_name: str, offline_time: str,
                                   countdown_minutes: int):
        """调度器触发时的回调：发送通知到所有目标群组

        Args:
            schedule_name: 计划名称
            offline_time: 下线时间
            countdown_minutes: 剩余分钟数
        """
        target_groups = self.config.get("target_groups", [])
        if not target_groups:
            logger.warning("[离线通知] 未配置目标群组，无法发送通知")
            return

        # 渲染消息
        message = self.template_engine.build_full_message(
            offline_time, countdown_minutes
        )

        logger.info(
            f"[离线通知] 计划 '{schedule_name}' 触发，"
            f"目标群组: {len(target_groups)} 个"
        )

        # 发送通知
        result = await self.notifier.send_to_groups(target_groups, message)

        # 记录结果
        if result["failed"]:
            logger.warning(
                f"[离线通知] 部分群组发送失败: {result['failed']}"
            )

    # ── 命令注册 ──────────────────────────────────────────

    @filter.command_group("下线通知")
    def offline_notify(self):
        """下线通知管理命令组"""
        pass

    @offline_notify.command("状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看调度器运行状态"""
        status = self.scheduler.get_status()

        lines = [
            "【下线通知系统状态】",
            "",
            f"调度器运行: {'✅ 运行中' if status['running'] else '❌ 已停止'}",
            f"定时任务数: {status['job_count']}",
            f"累计触发: {status['trigger_count']} 次",
            f"累计错误: {status['error_count']} 次",
        ]

        if status["jobs"]:
            lines.append("")
            lines.append("─ 任务列表 ─")
            for job in status["jobs"]:
                next_run = job["next_run"] or "暂无"
                lines.append(f"  · {job['name']}: 下次触发 {next_run}")

        if status["last_error"]:
            lines.append("")
            lines.append(f"最近错误: {status['last_error']}")

        yield event.plain_result("\n".join(lines))

    @offline_notify.command("预览")
    async def cmd_preview(self, event: AstrMessageEvent):
        """预览通知消息效果"""
        now = datetime.now()
        # 使用第一个启用的计划的下线时间，或默认 23:00
        schedules = self.config.get("schedules", [])
        offline_time = "23:00"
        advance = 5
        for sched in schedules:
            if sched.get("enabled", True):
                offline_time = sched.get("offline_time", "23:00")
                advance = sched.get("advance_minutes",
                                    self.config.get("global_advance_minutes", 5))
                break

        # 渲染预览消息
        rendered = self.template_engine.render_preview(offline_time, advance)

        preview_lines = [
            "【通知预览】",
            "",
            f"下线时间: {offline_time}",
            f"提前通知: {advance} 分钟",
            "",
            "─ 实际效果 ─",
            "",
            rendered["title"],
            "",
            rendered["body"],
        ]
        if rendered["footer"]:
            preview_lines.extend(["", rendered["footer"]])

        yield event.plain_result("\n".join(preview_lines))

    @offline_notify.command("测试")
    async def cmd_test(self, event: AstrMessageEvent):
        """手动触发一次测试通知（仅发送到当前群）"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("❌ 此命令仅支持在群聊中使用")
            return

        now = datetime.now()
        offline_time = now.strftime("%H:%M")

        message = self.template_engine.build_full_message(offline_time, 5)
        success = await self.notifier.send_to_group(group_id, message)

        if success:
            yield event.plain_result("✅ 测试通知已发送到当前群")
        else:
            yield event.plain_result("❌ 测试通知发送失败，请查看日志")

    @offline_notify.command("统计")
    async def cmd_stats(self, event: AstrMessageEvent):
        """查看发送统计"""
        stats = self.notifier.get_stats()
        sched_status = self.scheduler.get_status()

        last_send = "从未"
        if stats["last_send_time"]:
            dt = datetime.fromtimestamp(stats["last_send_time"])
            last_send = dt.strftime("%Y-%m-%d %H:%M:%S")

        lines = [
            "【通知发送统计】",
            "",
            f"成功发送: {stats['total_sent']} 次",
            f"发送失败: {stats['total_failed']} 次",
            f"最近发送: {last_send}",
            f"调度触发: {sched_status['trigger_count']} 次",
            f"调度错误: {sched_status['error_count']} 次",
        ]

        if stats["last_error"]:
            lines.append(f"最近错误: {stats['last_error']}")

        yield event.plain_result("\n".join(lines))

    # ── WebUI API ──────────────────────────────────────────

    def _register_web_apis(self):
        """注册 WebUI API 路由"""
        prefix = "/astrbot_plugin_offline_notify"

        self.context.register_web_api(
            f"{prefix}/status",
            self._api_get_status,
            ["GET"],
            "获取调度器状态"
        )

        self.context.register_web_api(
            f"{prefix}/preview",
            self._api_preview_message,
            ["POST"],
            "预览通知消息"
        )

        self.context.register_web_api(
            f"{prefix}/test",
            self._api_trigger_test,
            ["POST"],
            "手动触发测试通知"
        )

        self.context.register_web_api(
            f"{prefix}/stats",
            self._api_get_stats,
            ["GET"],
            "获取发送统计"
        )

    async def _api_get_status(self):
        """API: 获取调度器状态"""
        from quart import jsonify
        return jsonify(self.scheduler.get_status())

    async def _api_preview_message(self):
        """API: 预览通知消息"""
        from quart import jsonify, request

        try:
            data = await request.get_json()
            offline_time = data.get("offline_time", "23:00")
            countdown = data.get("countdown_minutes", 5)
            override_vars = data.get("override_vars", None)

            rendered = self.template_engine.render_preview(
                offline_time, countdown, override_vars
            )
            return jsonify({"success": True, "data": rendered})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400

    async def _api_trigger_test(self):
        """API: 手动触发测试通知"""
        from quart import jsonify, request

        try:
            data = await request.get_json()
            target_group = data.get("group_id", "")

            if not target_group:
                return jsonify({"success": False, "error": "缺少 group_id"}), 400

            now = datetime.now()
            offline_time = now.strftime("%H:%M")
            message = self.template_engine.build_full_message(offline_time, 5)

            success = await self.notifier.send_to_group(target_group, message)
            return jsonify({
                "success": success,
                "message": "通知已发送" if success else "发送失败"
            })
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    async def _api_get_stats(self):
        """API: 获取发送统计"""
        from quart import jsonify

        notifier_stats = self.notifier.get_stats()
        scheduler_stats = self.scheduler.get_status()

        return jsonify({
            "notifier": notifier_stats,
            "scheduler": scheduler_stats,
        })