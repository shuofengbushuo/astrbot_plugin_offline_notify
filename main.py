"""
AI下线通知系统 - AstrBot 插件主入口

功能概述:
- 基于 APScheduler 实现定时下线通知（浮动时间）
- 支持调用 LLM 生成多样化、自然的下线通知内容
- 内置消息模板作为 LLM 失败时的回退方案
- 浮动时间机制: 通知在 advance_minutes 到 advance_minutes+float_range 之间随机触发
- 支持工作日/周末/特定日期差异化时间
- 支持多群组同时通知
- 通知发布记录查询（WebUI + 命令）
- 通知预览功能
- 发送失败重试机制
- 调度器自我监控与告警（群聊+私聊双通道）

命令:
  /下线通知 状态     - 查看调度器运行状态和 LLM 统计
  /下线通知 预览     - 预览通知消息效果（模板）
  /下线通知 生成     - 调用 LLM 实时生成一条通知预览
  /下线通知 测试     - 手动触发一次测试通知
  /下线通知 统计     - 查看发送统计和通知历史
  /下线通知 记录 [N] - 查看最近 N 条通知发布记录
"""

import asyncio
from datetime import datetime
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig

from .core import (NotificationScheduler, GroupNotifier, TemplateEngine,
                   SchedulerMonitor, LLMGenerator, RecordStore)


@register(
    "astrbot_plugin_offline_notify",
    "AstrBot User",
    "定时向QQ群发送AI服务下线提醒，支持LLM生成多样化通知、浮动时间、多群组等",
    "v1.3.0",
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
        self.llm_generator: LLMGenerator = None
        self.notifier: GroupNotifier = None
        self.monitor: SchedulerMonitor = None
        self.record_store: RecordStore = None

        # 获取插件数据目录
        self.plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_offline_notify")

        # 平台标识（平台实例名称，如"小砂糖"，用于构造 UMO）
        self.platform_id = config.get("platform_id", "小砂糖")

    # ── 生命周期 ──────────────────────────────────────────

    async def initialize(self):
        """插件初始化：加载配置、启动调度器和监控"""
        logger.info("[离线通知] 正在初始化...")

        # 初始化模板引擎（始终初始化，作为回退方案）
        self.template_engine = TemplateEngine(self.config.get("message_template", {}))

        # 初始化 LLM 生成器
        self.llm_generator = LLMGenerator(
            self.context,
            self.config.get("llm_generation_config", {})
        )

        # 初始化通知器
        self.notifier = GroupNotifier(
            self.context,
            self.config.get("retry_config", {})
        )

        # 初始化记录存储
        self.record_store = RecordStore(self.plugin_data_dir)

        # 设置调度器回调
        self.scheduler.set_callback(self._on_schedule_trigger)

        # 配置并启动调度器
        if self.config.get("enable_notification", True):
            schedules = self.config.get("schedules", [])
            global_advance = self.config.get("global_advance_minutes", 5)
            global_float = self.config.get("global_float_range", 2)
            self.scheduler.configure_and_start(schedules, global_advance, global_float)
        else:
            logger.info("[离线通知] 通知功能已禁用，调度器未启动")

        # 初始化并启动监控器
        monitor_config = self.config.get("monitor_config", {})
        monitor_config["platform_id"] = self.platform_id
        self.monitor = SchedulerMonitor(
            self.context,
            self.scheduler,
            monitor_config
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

    # ── 消息生成核心 ──────────────────────────────────────

    async def _generate_message(self, offline_time: str,
                                countdown_minutes: float) -> str:
        """生成通知消息（LLM 优先，模板回退）

        Args:
            offline_time: 下线时间
            countdown_minutes: 剩余分钟数（可为浮点数）

        Returns:
            str: 通知消息文本
        """
        # 向下取整用于模板变量（模板只支持整数分钟）
        display_minutes = int(countdown_minutes)

        # 生成模板备用消息
        fallback_msg = self.template_engine.build_full_message(
            offline_time, display_minutes
        )

        # 尝试 LLM 生成
        message = await self.llm_generator.generate_with_fallback(
            offline_time, display_minutes, fallback_msg
        )

        return message

    def _detect_message_source(self) -> str:
        """检测本次通知的消息来源"""
        if self.llm_generator.enabled and self.llm_generator.provider_id:
            stats = self.llm_generator.get_stats()
            # 如果最近一次 LLM 调用是成功的，则为 llm 来源
            if stats.get("last_error") is None:
                return "llm"
        return "template"

    # ── 调度器回调 ─────────────────────────────────────────

    async def _on_schedule_trigger(self, schedule_name: str, offline_time: str,
                                   actual_countdown: float,
                                   float_seconds: float, float_range: int):
        """调度器触发时的回调：生成消息并发送到所有目标群组

        Args:
            schedule_name: 计划名称
            offline_time: 下线时间
            actual_countdown: 实际剩余分钟数（浮点）
            float_seconds: 实际浮动秒数
            float_range: 配置的浮动范围
        """
        target_groups = self.config.get("target_groups", [])
        if not target_groups:
            logger.warning("[离线通知] 未配置目标群组，无法发送通知")
            return

        advance = self.config.get("global_advance_minutes", 5)
        # 尝试从 plans 中找到对应计划的 advance
        for sched in self.config.get("schedules", []):
            if sched.get("name") == schedule_name:
                advance = sched.get("advance_minutes", advance)
                break

        logger.info(
            f"[离线通知] 计划 '{schedule_name}' 触发，"
            f"下线时间 {offline_time}, 实际提前 {actual_countdown:.1f} 分钟, "
            f"浮动 {float_seconds:.0f}s, 目标群组: {len(target_groups)} 个"
        )

        # 生成通知消息
        message = await self._generate_message(offline_time, actual_countdown)
        message_source = self._detect_message_source()

        if not message:
            logger.error("[离线通知] 消息生成失败（LLM 和模板均不可用），跳过发送")
            return

        # 发送通知
        result = await self.notifier.send_to_groups(
            target_groups, message, self.platform_id
        )

        # 写入通知记录
        await self.record_store.add(
            schedule_name=schedule_name,
            offline_time=offline_time,
            advance_minutes=advance,
            float_range=float_range,
            actual_trigger_minutes=actual_countdown,
            float_seconds=float_seconds,
            message_source=message_source,
            results=result,
        )

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
        """查看调度器运行状态和 LLM 统计"""
        status = self.scheduler.get_status()
        llm_stats = self.llm_generator.get_stats()
        global_float = self.config.get("global_float_range", 2)

        lines = [
            "【下线通知系统状态】",
            "",
            f"调度器运行: {'✅ 运行中' if status['running'] else '❌ 已停止'}",
            f"定时任务数: {status['job_count']}",
            f"浮动范围: 全局 ±{global_float} 分钟" if global_float > 0 else "浮动范围: 精确触发",
            f"累计触发: {status['trigger_count']} 次",
            f"累计错误: {status['error_count']} 次",
        ]

        if status["jobs"]:
            lines.append("")
            lines.append("─ 任务列表 ─")
            for job in status["jobs"]:
                next_run = job["next_run"] or "暂无"
                lines.append(f"  · {job['name']}: 下次触发 {next_run}")

        # LLM 生成统计
        if self.llm_generator.enabled:
            lines.append("")
            lines.append("─ LLM 生成统计 ─")
            lines.append(f"  总调用: {llm_stats['total_calls']} 次")
            lines.append(f"  成功: {llm_stats['success_calls']} 次")
            lines.append(f"  失败: {llm_stats['failed_calls']} 次")
            lines.append(f"  平均耗时: {llm_stats['avg_time_ms']}ms")
        else:
            lines.append("")
            lines.append("─ 消息生成: 模板引擎 ─")

        if status["last_error"]:
            lines.append("")
            lines.append(f"最近错误: {status['last_error']}")

        yield event.plain_result("\n".join(lines))

    @offline_notify.command("预览")
    async def cmd_preview(self, event: AstrMessageEvent):
        """预览模板消息效果"""
        schedules = self.config.get("schedules", [])
        offline_time = "23:00"
        advance = 5
        for sched in schedules:
            if sched.get("enabled", True):
                offline_time = sched.get("offline_time", "23:00")
                advance = sched.get("advance_minutes",
                                    self.config.get("global_advance_minutes", 5))
                break

        rendered = self.template_engine.render_preview(offline_time, advance)

        preview_lines = [
            "【模板消息预览】",
            "",
            f"下线时间: {offline_time}",
            f"提前通知: {advance} 分钟",
            "",
            "─ 效果 ─",
            "",
            rendered["title"],
            "",
            rendered["body"],
        ]
        if rendered["footer"]:
            preview_lines.extend(["", rendered["footer"]])

        if self.llm_generator.enabled:
            preview_lines.extend([
                "",
                "提示: 使用 /下线通知 生成 查看 LLM 实时生成效果",
            ])

        yield event.plain_result("\n".join(preview_lines))

    @offline_notify.command("生成")
    async def cmd_generate(self, event: AstrMessageEvent):
        """调用 LLM 生成下线通知

        群聊模式: /下线通知 生成 → 仅预览 LLM 生成结果
        私聊模式: /下线通知 生成 [QQ群号] → 管理员专用，生成并发送到指定群
          示例: /下线通知 生成 1006930720
        """
        # ── 前置检查：LLM 可用性 ──
        if not self.llm_generator.enabled:
            yield event.plain_result("❌ LLM 生成功能已禁用，请先在配置中启用")
            return
        if not self.llm_generator.provider_id:
            yield event.plain_result("❌ 未配置 LLM 提供商，请先在配置中选择")
            return

        # ── 解析命令参数 ──
        message_str = event.message_str
        parts = message_str.strip().split()
        group_id = event.get_group_id()

        if group_id:
            # ── 群聊模式：仅预览 ──
            now = datetime.now()
            schedules = self.config.get("schedules", [])
            offline_time = "23:00"
            advance = 5
            for sched in schedules:
                if sched.get("enabled", True):
                    offline_time = sched.get("offline_time", "23:00")
                    advance = sched.get("advance_minutes",
                                        self.config.get("global_advance_minutes", 5))
                    break

            yield event.plain_result("正在调用 LLM 生成通知...")

            result = await self.llm_generator.generate(offline_time, advance, now)

            if result:
                llm_stats = self.llm_generator.get_stats()
                yield event.plain_result(
                    f"【LLM 生成结果】\n"
                    f"下线时间: {offline_time} | 提前: {advance} 分钟 | "
                    f"耗时: {llm_stats['avg_time_ms']}ms\n\n{result}"
                )
            else:
                last_error = self.llm_generator.get_stats().get("last_error", "未知错误")
                yield event.plain_result(f"❌ LLM 生成失败: {last_error}")

        else:
            # ── 私聊模式：发送到指定群 ──
            # 1. 格式校验
            if len(parts) < 3:
                yield event.plain_result(
                    "❌ 私聊模式下请指定目标群号\n"
                    "格式: /下线通知 生成 [QQ群号]\n"
                    "示例: /下线通知 生成 1006930720"
                )
                return

            target_group_id = parts[2]
            if not target_group_id.isdigit():
                yield event.plain_result("❌ 群号格式不正确，请输入纯数字QQ群号")
                return

            # 2. 权限校验：仅管理员可使用
            if not event.is_admin():
                sender_name = event.get_sender_name() or event.get_sender_id()
                logger.warning(
                    f"[离线通知] 非管理员 {sender_name} 尝试使用私聊生成命令，"
                    f"目标群: {target_group_id}"
                )
                yield event.plain_result(
                    "❌ 权限不足，仅管理员可在私聊中使用此命令"
                )
                return

            # 3. 生成通知
            now = datetime.now()
            offline_time = now.strftime("%H:%M")

            yield event.plain_result(
                f"正在调用 LLM 生成通知，目标群: {target_group_id}..."
            )

            result = await self.llm_generator.generate(offline_time, 5, now, is_manual=True)

            if not result:
                # LLM 失败，回退到模板
                logger.warning(
                    f"[离线通知] 手动生成 LLM 失败，回退模板，"
                    f"目标群: {target_group_id}"
                )
                result = self.template_engine.build_full_message(offline_time, 5)

            if not result:
                yield event.plain_result("❌ 通知生成失败，LLM 和模板均不可用")
                return

            # 4. 发送到指定群
            logger.info(
                f"[离线通知] 管理员手动发送通知到群 {target_group_id}，"
                f"内容长度: {len(result)} 字符"
            )

            success = await self.notifier.send_to_group(
                target_group_id, result, self.platform_id
            )

            if success:
                llm_stats = self.llm_generator.get_stats()
                yield event.plain_result(
                    f"✅ 通知已成功发送到群 {target_group_id}\n"
                    f"下线时间: {offline_time} | "
                    f"耗时: {llm_stats['avg_time_ms']}ms\n\n"
                    f"── 已发送内容 ──\n{result}"
                )
            else:
                yield event.plain_result(
                    f"❌ 通知发送到群 {target_group_id} 失败，请查看日志"
                )

    @offline_notify.command("测试")
    async def cmd_test(self, event: AstrMessageEvent):
        """手动触发一次测试通知（仅发送到当前群）"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("❌ 此命令仅支持在群聊中使用")
            return

        now = datetime.now()
        offline_time = now.strftime("%H:%M")
        message = await self._generate_message(offline_time, 5)

        # 直接使用 event.send 发送到当前群，避免 UMO 格式问题
        try:
            await event.send(event.plain_result(message))
            yield event.plain_result("✅ 测试通知已发送到当前群")
        except Exception as e:
            logger.error(f"[离线通知] 测试通知发送失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 测试通知发送失败: {e}")

    @offline_notify.command("统计")
    async def cmd_stats(self, event: AstrMessageEvent):
        """查看发送统计和通知历史摘要"""
        stats = self.notifier.get_stats()
        sched_status = self.scheduler.get_status()
        llm_stats = self.llm_generator.get_stats()
        record_stats = await self.record_store.get_stats()
        global_float = self.config.get("global_float_range", 2)

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
            f"浮动范围: {'±' + str(global_float) + ' 分钟' if global_float > 0 else '精确触发'}",
        ]

        if record_stats["total"] > 0:
            lines.extend([
                "",
                f"通知记录: {record_stats['total']} 条",
                f"LLM 生成: {record_stats['llm_count']} 次 / 模板: {record_stats['template_count']} 次",
                f"平均浮动: {record_stats['avg_float_seconds']:.0f}s",
                f"群组发送: 成功 {record_stats['total_success_groups']} / 失败 {record_stats['total_failed_groups']}",
            ])

        if self.llm_generator.enabled:
            lines.extend([
                "",
                f"LLM 调用: {llm_stats['total_calls']} 次 "
                f"(成功 {llm_stats['success_calls']} / 失败 {llm_stats['failed_calls']})",
                f"LLM 平均耗时: {llm_stats['avg_time_ms']}ms",
            ])

        if stats["last_error"]:
            lines.append(f"最近错误: {stats['last_error']}")

        yield event.plain_result("\n".join(lines))

    @offline_notify.command("记录")
    async def cmd_records(self, event: AstrMessageEvent):
        """查看最近 N 条通知发布记录"""
        # 解析参数: /下线通知 记录 5
        message_str = event.message_str
        parts = message_str.strip().split()
        limit = 5
        if len(parts) >= 3:
            try:
                limit = int(parts[2])
                limit = max(1, min(limit, 20))
            except ValueError:
                pass

        records = await self.record_store.query_latest(limit)

        if not records:
            yield event.plain_result("暂无通知发布记录")
            return

        lines = [f"【最近 {len(records)} 条通知记录】", ""]
        for i, r in enumerate(records, 1):
            dt = r.get("datetime", "未知")
            name = r.get("schedule_name", "未知")
            offline = r.get("offline_time", "?")
            actual = r.get("actual_trigger_minutes", "?")
            float_s = r.get("float_seconds", 0)
            source = r.get("message_source", "?")
            results = r.get("results", {})
            success_count = len(results.get("success", []))
            failed_count = len(results.get("failed", []))

            float_info = f"浮动 {float_s:.0f}s" if float_s > 0 else "精确"
            lines.append(
                f"{i}. [{dt}] {name}\n"
                f"   下线 {offline} | 提前 {actual}min | {float_info} | 来源 {source}\n"
                f"   发送: 成功 {success_count} 群 / 失败 {failed_count} 群"
            )

        total = self.record_store.get_total_count()
        if total > limit:
            lines.append(f"\n... 共 {total} 条记录，显示最近 {limit} 条")

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
            f"{prefix}/generate",
            self._api_llm_generate,
            ["POST"],
            "调用 LLM 生成通知预览"
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

        self.context.register_web_api(
            f"{prefix}/records",
            self._api_get_records,
            ["GET"],
            "获取通知发布记录"
        )

    async def _api_get_status(self):
        """API: 获取调度器状态"""
        from quart import jsonify
        return jsonify(self.scheduler.get_status())

    async def _api_preview_message(self):
        """API: 预览模板消息"""
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

    async def _api_llm_generate(self):
        """API: 调用 LLM 生成通知预览"""
        from quart import jsonify, request

        try:
            data = await request.get_json()
            offline_time = data.get("offline_time", "23:00")
            countdown = data.get("countdown_minutes", 5)

            result = await self.llm_generator.generate(offline_time, countdown)
            stats = self.llm_generator.get_stats()

            if result:
                return jsonify({
                    "success": True,
                    "data": {
                        "text": result,
                        "source": "llm",
                        "avg_time_ms": stats["avg_time_ms"],
                    }
                })
            else:
                fallback = self.template_engine.build_full_message(
                    offline_time, countdown
                )
                return jsonify({
                    "success": True,
                    "data": {
                        "text": fallback,
                        "source": "template",
                        "error": stats.get("last_error", "LLM 生成失败"),
                    }
                })
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

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
            message = await self._generate_message(offline_time, 5)

            success = await self.notifier.send_to_group(
                target_group, message, self.platform_id
            )
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
        llm_stats = self.llm_generator.get_stats()
        record_stats = await self.record_store.get_stats()

        return jsonify({
            "notifier": notifier_stats,
            "scheduler": scheduler_stats,
            "llm_generator": llm_stats,
            "records": record_stats,
        })

    async def _api_get_records(self):
        """API: 获取通知发布记录"""
        from quart import jsonify, request

        try:
            limit = request.args.get("limit", 10, type=int)
            offset = request.args.get("offset", 0, type=int)
            limit = max(1, min(limit, 50))
            offset = max(0, offset)

            records = await self.record_store.query(limit, offset)
            total = self.record_store.get_total_count()

            return jsonify({
                "success": True,
                "data": {
                    "records": records,
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                }
            })
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500