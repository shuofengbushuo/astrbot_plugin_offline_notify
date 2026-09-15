"""
AI下线通知系统 - AstrBot 插件主入口

功能概述:
- 基于 APScheduler 实现「监听窗口」调度（监听开始 / 监听结束两个边界 cron）
- 被动窗口事件驱动：窗口内命中目标群消息即触发发送（借被动 msg_id）
- 支持调用 LLM 生成多样化、自然的下线通知内容
- 内置消息模板作为 LLM 失败时的回退方案
- 监听窗口结束（下线后）仅清理状态；窗口内无人发言则本次不发送，无兜底补发
- 支持工作日/周末/特定日期差异化时间
- 支持多群组同时通知
- 通知发布记录查询（WebUI）
- 通知预览（WebUI）
- 发送失败重试机制
- 调度器自我监控与告警

仅支持 QQ 官方机器人（qq_official / qq_official_webhook）平台。

命令:
  /下线通知 生成     - 调用 LLM 生成一条通知（群聊仅预览，私聊加群 openid 可发送）
  /下线通知 计划     - 管理定时通知计划（无需编辑 JSON 配置！）
"""

import asyncio
import random
from datetime import datetime
from pathlib import Path
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig

from .core import (NotificationScheduler, GroupNotifier, TemplateEngine,
                   SchedulerMonitor, LLMGenerator, RecordStore, PromptStore,
                   ScheduleStore, WindowState, PlatformCaps, SessionActivityTracker,
                   resolve_platform, resolve_platforms,
                   validate_target_id, validate_target_id_multi,
                   QQ_OFFICIAL_PASSIVE_TTL, sync_changelog_from_readme)


@register(
    "astrbot_plugin_offline_notify",
    "AstrBot User",
    "定时向QQ群发送AI服务下线提醒（QQ官方机器人 qq_official），支持 LLM 生成"
    "多样化通知、被动窗口事件驱动触发、多群组等",
    "v2.0.0",
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
        self.prompt_store: PromptStore = None
        self.schedule_store: ScheduleStore = None
        self.window_state: WindowState = None

        # 窗口触发时的发送去重/并发锁（防止同一条入站消息触发多次发送）
        self._firing: set = set()

        # 获取插件数据目录
        self.plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_offline_notify")

        # 发送平台实例名（WebUI 中给适配器起的名字，用于构造 UMO）。仅支持单个实例。
        # 兼容旧版 list 配置：数组则取首个非空元素。
        raw_ids = config.get("platform_ids") or ""
        if isinstance(raw_ids, list):
            raw_ids = next((i for i in raw_ids if i), "")
        self.platform_ids = [raw_ids] if raw_ids else []
        # 代表性单值，供日志/命令回显兼容使用
        self.platform_id = self.platform_ids[0] if self.platform_ids else ""

        # 平台能力画像列表，在 initialize() 中根据运行时已启用的适配器解析
        self.capses: list = []

        # 会话活跃度跟踪（QQ 官方被动 msg_id 只有 5 分钟窗口，需要提前预警）
        self.activity = SessionActivityTracker()

    @property
    def caps(self) -> PlatformCaps:
        """代表性平台画像（第一个），供单值语义的日志/命令回显兼容使用。

        需要遍历全部平台的发送/告警逻辑请直接用 ``self.capses``。
        """
        return self.capses[0] if self.capses else PlatformCaps()

    # ── 生命周期 ──────────────────────────────────────────

    def _sync_changelog_from_readme(self) -> str:
        """把 README 的「更新日志」章节同步为 CHANGELOG.md（幂等，失败不影响插件）。"""
        return sync_changelog_from_readme(
            Path(__file__).resolve().parent, self.plugin_data_dir, logger
        )

    async def initialize(self):
        """插件初始化：加载配置、启动调度器和监控"""
        logger.info("[离线通知] 正在初始化...")

        # README「更新日志」→ CHANGELOG.md，供 WebUI 插件详情页展示
        self._sync_changelog_from_readme()

        # 解析平台能力画像：决定 UMO 形态、分段策略、纯文本/Markdown 等
        self._resolve_platform_caps()

        # 初始化模板引擎（始终初始化，作为回退方案）
        self.template_engine = TemplateEngine(self.config.get("message_template", {}))

        # 初始化提示词方案库（命名方案，互不干扰地保存 / 切换）
        self.prompt_store = PromptStore(self.plugin_data_dir)

        # 初始化计划存储（计划由命令管理，不依赖配置系统）
        self.schedule_store = ScheduleStore(self.plugin_data_dir)

        # 初始化窗口状态机（armed 标记 + 每日发送去重，持久化到磁盘）
        self.window_state = WindowState(self.plugin_data_dir)

        # 初始化 LLM 生成器（传入方案库，支持「激活方案 > 配置自定义 > 内置默认」三级覆盖）
        self.llm_generator = LLMGenerator(
            self.context,
            self.config.get("llm_generation_config", {}),
            prompt_store=self.prompt_store,
            retry_config=self.config.get("retry_config", {}),
        )

        # 初始化通知器
        self.notifier = GroupNotifier(
            self.context,
            self.config.get("retry_config", {})
        )

        # 初始化记录存储
        self.record_store = RecordStore(self.plugin_data_dir)

        # 设置调度器监听开始/监听结束回调
        self.scheduler.set_callbacks(self._on_window_open, self._on_window_close)

        # 配置并启动调度器（始终启动；无计划则不会触发任何通知，计划由 /下线通知 计划 命令管理）
        schedules = self.schedule_store.list_all()
        window_before = self.config.get("window_before_minutes", 30)
        window_after = self.config.get("window_after_minutes", 30)
        self.scheduler.configure_and_start(schedules, window_before, window_after)

        # 重建窗口状态：插件重载 / bot 重启后，把「当前正处于时间窗口内」的计划
        # 重新标记为 armed，使事件驱动触发能立即恢复。
        self._rebuild_armed_state(schedules, window_before, window_after)

        # 初始化并启动监控器
        monitor_config = self.config.get("monitor_config", {})
        monitor_config["platform_ids"] = self.platform_ids
        self.monitor = SchedulerMonitor(
            self.context,
            self.scheduler,
            monitor_config,
            caps=self.capses,
        )
        await self.monitor.start()

        # 注册 WebUI API 路由
        self._register_web_apis()

        # 启动时校验目标群标识是否符合当前平台形态，尽早暴露配置错误
        self._audit_target_groups()

        logger.info("[离线通知] 初始化完成")

    # ── 平台适配 ──────────────────────────────────────────

    def _resolve_platform_caps(self):
        """解析当前生效的平台实例，构建能力画像。

        配置的 platform_id 找不到时会自动回退到唯一一个已启用平台（并告警）。
        """
        self.capses = resolve_platforms(self.context, self.platform_ids)
        # 自动回退后同步真实生效的 ID，避免后续日志/命令回显不一致
        self.platform_id = self.caps.platform_id
        logger.info(
            "[离线通知] 生效平台: "
            + " | ".join(c.describe() for c in self.capses)
        )

    def _audit_target_groups(self):
        """校验 target_groups 中的群标识是否符合当前平台的形态要求。"""
        bad = []
        for group in self.config.get("target_groups", []) or []:
            gid = group if isinstance(group, str) else str(group.get("group_id", ""))
            if not gid:
                continue
            ok, hint = validate_target_id_multi(self.capses, gid)
            if not ok:
                bad.append((gid, hint))
        for gid, hint in bad:
            logger.error(f"[离线通知] 目标群「{gid}」配置有误: {hint}")
        if bad:
            logger.error(
                f"[离线通知] 共 {len(bad)} 个目标群标识与当前平台"
                f"({self.caps.platform_type or '未知'})不匹配，这些群的通知将无法送达。"
            )

    @filter.on_platform_loaded()
    async def _on_platform_loaded(self, *args, **kwargs):
        """平台适配器加载完成后重新解析能力画像。

        插件 initialize() 可能早于平台实例注册完成，届时解析结果为「未找到」。
        平台就绪后再解析一次，并把新画像同步给监控器。
        """
        self._resolve_platform_caps()
        if self.monitor:
            self.monitor.capses = self.capses
        self._audit_target_groups()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _track_session_activity(self, event: AstrMessageEvent):
        """记录会话活跃度，并在窗口内命中目标群消息时触发下线通知。

        不消费事件、不产生回复（触发发送走 context.send_message 主动路径）。
        """
        try:
            self.activity.touch(event.unified_msg_origin)
        except Exception:  # pragma: no cover - 绝不因监听而影响主流程
            pass
        try:
            await self._maybe_fire_windowed(event)
        except Exception:  # pragma: no cover
            logger.debug("[离线通知] 窗口触发判断异常，忽略", exc_info=True)

    def _ensure_caps(self):
        """发送前兜底：若所有平台都仍未解析成功，再尝试解析一次。"""
        if not any(c.found for c in self.capses):
            self._resolve_platform_caps()

    def _notify_result(self, event: AstrMessageEvent, text: str):
        """构造通知内容的回复结果，按平台能力决定是否强制纯文本。

        QQ 官方事件回复路径默认走 Markdown（msg_type=2），需要事先报备 Markdown
        模板；通知内容是纯散文，强制纯文本更稳妥。
        """
        result = event.plain_result(text)
        if any(c.force_plain_text for c in self.capses):
            try:
                # MessageChain.use_markdown(False) → use_markdown_ = False
                result.use_markdown(False)
            except Exception:  # pragma: no cover - 老版本内核无此方法
                try:
                    result.use_markdown_ = False
                except Exception:
                    pass
        return result

    def _warn_passive_window(self, group_ids):
        """QQ 官方专用：发送前提示被动回复窗口是否已关闭。"""
        qq_capses = [c for c in self.capses if c.is_qq_official]
        if not qq_capses:
            return
        for c in qq_capses:
            for gid in group_ids:
                umo = c.group_umo(gid)
                age = self.activity.age(umo)
                if age is None:
                    logger.warning(
                        f"[离线通知] 群 {gid}（平台 {c.platform_id}）本次运行期间未收到过消息，"
                        f"QQ 官方协议下大概率没有可用的被动 msg_id，通知可能无法送达。"
                    )
                elif age > QQ_OFFICIAL_PASSIVE_TTL:
                    logger.warning(
                        f"[离线通知] 群 {gid}（平台 {c.platform_id}）距上次消息已 {age / 60:.1f} 分钟，"
                        f"超出 QQ 官方被动回复窗口({QQ_OFFICIAL_PASSIVE_TTL}s)，"
                        f"本次将退化为主动推送并占用官方消息配额。"
                    )

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
            # 以最近一次 LLM 调用是否真正成功产出内容为准，
            # 而不是仅看 last_error 是否为空（旧逻辑会在一次成功后永远误判为 llm）
            if self.llm_generator.get_stats().get("last_success"):
                return "llm"
        return "template"

    # ── 窗口回调与事件驱动触发 ─────────────────────────────

    async def _on_window_open(self, plan_name: str, offline_time: str):
        """窗口开启回调：标记计划进入「窗口内待发送」状态。"""
        self.window_state.arm(plan_name)

    async def _on_window_close(self, plan_name: str, offline_time: str):
        """窗口关闭回调：只清理窗口状态，不再尝试发送。

        QQ 官方群消息必须携带 5 分钟内的被动 msg_id。窗口内无人说话 ⇒ 没有可用的
        msg_id ⇒ 适配器 ``qqofficial_platform_adapter._send_by_session_common``
        会走 ``skip send_by_session`` 直接 return（不抛异常、不返回 False），
        消息一条都发不出去。旧实现在这里「降级为群内主动推送」，实际无效，
        还会被 ``Context.send_message`` 的 None 返回值误判为成功并写入假记录。
        """
        if not self.window_state.is_armed(plan_name):
            # 未武装（监听结束 cron 每天触发，今天不是该计划活跃日 / 已发送）
            return
        if self.window_state.already_sent_today(plan_name):
            logger.info(
                f"[离线通知] 计划 '{plan_name}' 窗口关闭，今日已发送"
            )
            return
        self.window_state.disarm(plan_name)
        logger.warning(
            f"[离线通知] 计划 '{plan_name}' 窗口内未等到目标群消息，本次不发送："
            f"QQ 官方群消息需要 5 分钟内的被动 msg_id，窗口关闭后已无可用 msg_id"
            f"（强行发送会被适配器静默丢弃）"
        )

    async def _maybe_fire_windowed(self, event: AstrMessageEvent):
        """窗口事件驱动触发：入站消息命中 armed 计划的目标群即触发。"""
        armed = self.window_state.armed_plans()
        if not armed:
            return
        if not any(c.is_qq_official for c in self.capses):
            return

        umo = event.unified_msg_origin
        for plan_name in armed:
            if self.window_state.already_sent_today(plan_name):
                continue
            plan = self.schedule_store.get(plan_name)
            if not plan:
                continue
            for caps in self.capses:
                if not caps.is_qq_official:
                    continue
                for gid in self._target_openids():
                    if umo == caps.group_umo(gid):
                        # 命中目标群：触发发送（带随机浮动延迟）
                        # 改用后台任务（fire-and-forget），避免 asyncio.sleep + LLM
                        # 内联阻塞事件分发管线，拖慢本消息的普通回复链路。
                        _task = asyncio.create_task(self._fire_with_float(plan, gid))
                        _task.add_done_callback(self._log_fire_task_done)
                        return

    async def _fire_with_float(self, plan: dict, gid: str):
        """命中目标群后，按 trigger_float_minutes 随机延迟再发送（模拟自然节奏）。"""
        plan_name = plan.get("name", "")
        if plan_name in self._firing:
            return

        # 随机浮动延迟：默认 5 分钟内（cap 到 msg_id 被动窗口 TTL 内）
        float_minutes = self.config.get("trigger_float_minutes", 5)
        max_delay = min(float(float_minutes), QQ_OFFICIAL_PASSIVE_TTL / 60.0)
        delay = random.uniform(0, max_delay * 60) if max_delay > 0 else 0.0
        logger.info(
            f"[离线通知] 计划 '{plan_name}' 命中目标群 {gid}，"
            f"随机浮动 {delay:.0f}s 后发送"
        )
        if delay > 0:
            await asyncio.sleep(delay)
        await self._fire_plan(plan_name, plan.get("offline_time", "23:00"),
                              trigger="event")

    def _log_fire_task_done(self, task: "asyncio.Task"):
        """后台触发任务完成回调：吞掉未捕获异常，避免后台任务异常静默丢失。"""
        try:
            task.result()
        except Exception:
            logger.exception("[离线通知] 后台触发发送任务异常")

    async def _fire_plan(self, plan_name: str, offline_time: str, *,
                         allow_proactive: bool = False,
                         trigger: str = "event") -> bool:
        """生成并发送下线通知到所有目标群（去重 + 并发锁 + 记录）。

        ``allow_proactive=True`` 时不因缺少被动 msg_id 直接放弃，允许走群主动
        推送；仍会校验适配器是否具备主动推送条件（见 ``notifier._precheck``）。
        """
        if self.window_state.already_sent_today(plan_name):
            return False
        if plan_name in self._firing:
            return False
        self._firing.add(plan_name)
        try:
            target_groups = self.config.get("target_groups", [])
            if not target_groups:
                logger.warning("[离线通知] 未配置目标群组，无法发送通知")
                return False

            self._ensure_caps()

            # 距下线时刻的分钟数（正=未到，负=已过）
            countdown = self._offline_minutes_away(offline_time)

            logger.info(
                f"[离线通知] 计划 '{plan_name}' 触发发送（{trigger}），"
                f"下线时间 {offline_time}, 距下线 {countdown:.1f} 分钟, "
                f"目标群组: {len(target_groups)} 个, "
                f"平台: {' | '.join(c.describe() for c in self.capses)}"
            )

            message = await self._generate_message(offline_time, countdown)
            message_source = self._detect_message_source()
            if not message:
                logger.error("[离线通知] 消息生成失败（LLM 和模板均不可用），跳过发送")
                return False

            result = await self.notifier.send_to_groups(
                target_groups, message, self.capses,
                split=(message_source == "llm"),
                allow_proactive=allow_proactive,
            )

            await self.record_store.add(
                schedule_name=plan_name,
                offline_time=offline_time,
                advance_minutes=0,
                float_range=self.config.get("trigger_float_minutes", 5),
                actual_trigger_minutes=countdown,
                float_seconds=0.0,
                message_source=message_source,
                results=result,
            )

            if result["failed"]:
                logger.warning(
                    f"[离线通知] 部分群组发送失败: {result['failed']}"
                )

            # 至少一个群成功送达才标记去重；全部失败则允许后续重试。
            if result["success"]:
                self.window_state.mark_sent(plan_name)
            return bool(result["success"])
        finally:
            self._firing.discard(plan_name)

    # ── 窗口辅助 ───────────────────────────────────────────

    def _target_openids(self) -> list:
        """返回目标群的 openid 列表（仅启用项）。"""
        out = []
        for g in self.config.get("target_groups", []) or []:
            if isinstance(g, str):
                out.append(g)
            elif g.get("enabled", True):
                out.append(g.get("group_id", ""))
        return [g for g in out if g]

    @staticmethod
    def _offline_minutes_away(offline_time: str) -> float:
        """当前时间距下线时刻的分钟数（正=未到，负=已过）。"""
        try:
            h, m = offline_time.strip().split(":")
            target = datetime.now().replace(
                hour=int(h), minute=int(m), second=0, microsecond=0
            )
        except (ValueError, AttributeError):
            return 0.0
        return (target - datetime.now()).total_seconds() / 60.0

    def _is_active_today(self, plan: dict) -> bool:
        """判断今天是否是该计划的活跃日。"""
        day_type = plan.get("day_type", "everyday")
        if day_type == "everyday":
            return True
        wd = datetime.now().weekday()  # 0=周一 ... 6=周日
        if day_type == "weekday":
            return wd < 5
        if day_type == "weekend":
            return wd >= 5
        if day_type == "specific":
            names = ["monday", "tuesday", "wednesday", "thursday",
                     "friday", "saturday", "sunday"]
            return names[wd] in (plan.get("specific_days") or [])
        return True

    @staticmethod
    def _parse_hm(time_str: str) -> tuple:
        h, m = time_str.strip().split(":")
        return int(h), int(m)

    def _rebuild_armed_state(self, schedules, window_before, window_after):
        """重载/重启后重建 armed 状态：当前处于窗口内的活跃计划重新 arm。"""
        now_minutes = datetime.now().hour * 60 + datetime.now().minute
        for s in schedules:
            if not s.get("enabled", True):
                continue
            name = s.get("name", "")
            if not self._is_active_today(s):
                continue
            try:
                h, m = self._parse_hm(s.get("offline_time", "23:00"))
            except (ValueError, AttributeError):
                continue
            offline = h * 60 + m
            open_min = (offline - window_before) % (24 * 60)
            close_min = (offline + window_after) % (24 * 60)
            if open_min <= close_min:
                in_window = open_min <= now_minutes <= close_min
            else:
                in_window = now_minutes >= open_min or now_minutes <= close_min
            if in_window and not self.window_state.already_sent_today(name):
                self.window_state.arm(name)
                logger.info(
                    f"[离线通知] 重建窗口状态：计划 '{name}' 处于窗口内，已 armed"
                )

    # ── 命令注册 ──────────────────────────────────────────

    @filter.command_group("下线通知")
    def offline_notify(self):
        """下线通知管理命令组"""
        pass

    @offline_notify.command("生成")
    async def cmd_generate(self, event: AstrMessageEvent):
        """生成下线通知。

群聊发送 → 仅预览 LLM 结果；私聊加群 openid → 管理员专用，生成并发送到该群。
示例：/下线通知 生成 <群 openid>"""
        # ── 解析命令参数 ──
        # 说明：LLM 禁用或不可用时，本命令会自动回退到模板引擎
        # （生成并发送模板通知），不再直接报错退出。
        message_str = event.message_str
        parts = message_str.strip().split()
        group_id = event.get_group_id()

        if group_id:
            # ── 群聊模式：仅预览（LLM 优先，禁用/失败则预览模板） ──
            now = datetime.now()
            schedules = self.schedule_store.list_all()
            offline_time = "23:00"
            countdown = self.config.get("window_before_minutes", 30)
            for sched in schedules:
                if sched.get("enabled", True):
                    offline_time = sched.get("offline_time", "23:00")
                    break

            if self.llm_generator.enabled and self.llm_generator.provider_id:
                yield event.plain_result("正在调用 LLM 生成通知...")
                result = await self.llm_generator.generate(offline_time, countdown, now)
                if result:
                    llm_stats = self.llm_generator.get_stats()
                    yield event.plain_result(
                        f"【LLM 生成结果】\n"
                        f"下线时间: {offline_time} | 距下线: {countdown} 分钟 | "
                        f"耗时: {llm_stats['avg_time_ms']}ms\n\n{result}"
                    )
                    return
                else:
                    last_error = self.llm_generator.get_stats().get("last_error", "未知错误")
                    yield event.plain_result(
                        f"⚠️ LLM 生成失败，已自动回退到模板预览。\n"
                        f"失败原因：{last_error}"
                    )

            # LLM 禁用或失败 → 模板预览
            template_msg = self.template_engine.build_full_message(offline_time, countdown)
            yield event.plain_result(f"【模板预览】\n{template_msg}")

        else:
            # ── 私聊模式：发送到指定群 ──
            # 1. 格式校验
            self._ensure_caps()
            if len(parts) < 3:
                id_hint = "群 openid（32 位十六进制串）"
                yield event.plain_result(
                    f"❌ 私聊模式下请指定目标群标识\n"
                    f"格式: /下线通知 生成 [{id_hint}]\n"
                    f"当前平台: {self.caps.platform_type or '未知'}"
                )
                return

            target_group_id = parts[2]
            # 平台感知校验：QQ 官方要求 group_openid（32 位十六进制串）。
            ok, hint = validate_target_id_multi(self.capses, target_group_id)
            if not ok:
                yield event.plain_result(f"❌ {hint}")
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

            # 3. 生成通知（LLM 优先，禁用/失败回退模板引擎）
            now = datetime.now()
            offline_time = now.strftime("%H:%M")

            if self.llm_generator.enabled and self.llm_generator.provider_id:
                yield event.plain_result(
                    f"正在调用 LLM 生成通知，目标群: {target_group_id}..."
                )
                result = await self.llm_generator.generate(
                    offline_time, 5, now, is_manual=True
                )
                if not result:
                    # LLM 失败，回退到模板，并在回复中展示诊断原因
                    gen_stats = self.llm_generator.get_stats()
                    last_error = gen_stats.get("last_error", "未知错误")
                    stage = gen_stats.get("stage", "unknown")
                    logger.warning(
                        f"[离线通知] 手动生成 LLM 失败，回退模板，"
                        f"目标群: {target_group_id}，stage={stage}"
                    )
                    yield event.plain_result(
                        f"⚠️ LLM 生成失败（{last_error}），已自动回退模板。"
                    )
                    result = self.template_engine.build_full_message(offline_time, 5)
            else:
                # LLM 已禁用 → 直接使用模板引擎
                logger.info(
                    f"[离线通知] LLM 已禁用，使用模板引擎生成，"
                    f"目标群: {target_group_id}"
                )
                result = self.template_engine.build_full_message(offline_time, 5)

            if not result:
                yield event.plain_result("❌ 通知生成失败，LLM 和模板均不可用")
                return

            # 4. 发送到指定群
            # 模板回复（LLM 失败回退）作为单条纯文本发送，不分段；
            # LLM 成功生成的长通知才按句分段。
            source = self._detect_message_source()
            logger.info(
                f"[离线通知] 管理员手动发送通知到群 {target_group_id}，"
                f"内容长度: {len(result)} 字符，来源: {source}"
            )

            self._warn_passive_window([target_group_id])
            success = await self.notifier.send_to_group(
                target_group_id, result, self.capses,
                split=(source == "llm"),
            )

            if success:
                if source == "llm":
                    llm_stats = self.llm_generator.get_stats()
                    time_info = f"耗时: {llm_stats['avg_time_ms']}ms"
                else:
                    time_info = "来源: 模板引擎（LLM 已禁用/失败，已自动回退）"
                yield event.plain_result(
                    f"✅ 通知已成功发送到群 {target_group_id}\n"
                    f"下线时间: {offline_time} | {time_info}\n\n"
                    f"── 已发送内容 ──\n{result}"
                )
            else:
                yield event.plain_result(
                    f"❌ 通知发送到群 {target_group_id} 失败，请查看日志"
                )

    # ── 计划重载辅助 ──────────────────────────────────────

    def _reload_scheduler_from_store(self):
        """从 ScheduleStore 重新加载调度器（计划变更后调用）。"""
        schedules = self.schedule_store.list_all()
        window_before = self.config.get("window_before_minutes", 30)
        window_after = self.config.get("window_after_minutes", 30)
        self.scheduler.reload_jobs(schedules, window_before, window_after)
        # 重载后重建窗口状态
        self._rebuild_armed_state(schedules, window_before, window_after)

    # ── 定时计划管理命令 ─────────────────────────────────

    @offline_notify.command("计划")
    async def cmd_schedule(self, event: AstrMessageEvent):
        """管理定时通知计划（无需改 JSON 配置）。

子命令：列表 / 添加 / 删除 / 启用 / 禁用 / 修改；写操作仅管理员。"""
        import re

        message_str = event.message_str
        # 预处理：统一中文冒号 + 去掉冒号两侧空格（避免 "10: 30" 被截断）
        message_str = message_str.replace("：", ":")
        message_str = re.sub(r"\s*:\s*", ":", message_str)

        parts = message_str.strip().split()
        # parts[0]="下线通知" parts[1]="计划" parts[2]=子命令 ...

        sub = parts[2] if len(parts) >= 3 else ""
        labels = ScheduleStore.DAY_TYPE_LABELS
        valid_keys = ScheduleStore.DAY_TYPE_VALID_KEYS

        # ── 帮助信息 ──────────────────────────
        if sub in ("帮助", "help", ""):
            yield event.plain_result(
                "【定时计划管理 — 无需编辑 JSON！】\n"
                "\n"
                "📋 /下线通知 计划 列表\n"
                "   查看所有计划\n"
                "\n"
                "➕ /下线通知 计划 添加 <名称> <日期类型> <下线时间>\n"
                "   日期类型: 每天/工作日/周末\n"
                "   窗口与触发时机由全局设置统一控制，无需逐计划指定\n"
                "   示例: /下线通知 计划 添加 工作日下线 工作日 23:00\n"
                "   示例: /下线通知 计划 添加 周末晚安 周末 23:30\n"
                "\n"
                "🗑️ /下线通知 计划 删除 <名称>\n"
                "   删除指定计划\n"
                "\n"
                "✅ /下线通知 计划 启用 <名称>\n"
                "   启用指定计划\n"
                "\n"
                "⏸️ /下线通知 计划 禁用 <名称>\n"
                "   禁用指定计划（暂停但不删除）\n"
                "\n"
                "✏️ /下线通知 计划 修改 <名称> <字段> <值>\n"
                "   字段: 时间(HH:MM) / 日期(类型)\n"
                "   中英文冒号均可，空格会被自动忽略\n"
                "   示例: /下线通知 计划 修改 工作日下线 时间 22:30\n"
                "   示例: /下线通知 计划 修改 周末晚安 日期 每天"
            )
            return

        # ── 列表 ──────────────────────────────
        if sub in ("列表", "list", "ls"):
            schedules = self.schedule_store.list_all()
            if not schedules:
                yield event.plain_result(
                    "📭 暂无定时计划\n"
                    "用 /下线通知 计划 添加 创建一个吧~\n"
                    "示例: /下线通知 计划 添加 工作日下线 工作日 23:00"
                )
                return

            lines = [f"【定时计划列表 — 共 {len(schedules)} 个】", ""]
            for s in schedules:
                name = s.get("name", "?")
                day_label = labels.get(s.get("day_type", ""), s.get("day_type", "?"))
                time_str = s.get("offline_time", "?")
                enabled = s.get("enabled", True)

                status_icon = "🟢" if enabled else "🔴"
                status_text = "启用" if enabled else "禁用"

                lines.append(
                    f"{status_icon} {name}  [{status_text}]\n"
                    f"   日期: {day_label} | 下线: {time_str}"
                )

            yield event.plain_result("\n".join(lines))
            return

        # ── 写操作：admin-only ──────────────────
        if not event.is_admin():
            sender = event.get_sender_name() or event.get_sender_id()
            logger.warning(f"[离线通知] 非管理员 {sender} 尝试管理定时计划")
            yield event.plain_result(
                "❌ 仅管理员可管理定时计划\n"
                "（查看计划请用 /下线通知 计划 列表）"
            )
            return

        # ── 添加 ──────────────────────────────
        if sub in ("添加", "add", "新增", "new"):
            if len(parts) < 5:
                yield event.plain_result(
                    "❌ 参数不足\n"
                    "格式: /下线通知 计划 添加 <名称> <日期类型> <下线时间>\n"
                    "示例: /下线通知 计划 添加 工作日下线 工作日 23:00"
                )
                return

            name = parts[3]
            day_type_input = parts[4]
            offline_time = parts[5] if len(parts) > 5 else "23:00"

            # 解析日期类型
            day_type = valid_keys.get(day_type_input) or day_type_input
            if day_type not in {"everyday", "weekday", "weekend"}:
                yield event.plain_result(
                    f"❌ 不支持的日期类型「{day_type_input}」\n"
                    "可选: 每天 / 工作日 / 周末"
                )
                return

            if self.schedule_store.exists(name):
                yield event.plain_result(
                    f"❌ 计划「{name}」已存在\n"
                    "请先删除或用 /下线通知 计划 修改 来更改"
                )
                return

            ok = self.schedule_store.add(
                name=name,
                day_type=day_type,
                offline_time=offline_time,
            )
            if not ok:
                yield event.plain_result("❌ 添加失败，请检查参数格式")
                return

            self._reload_scheduler_from_store()

            yield event.plain_result(
                f"✅ 已添加计划「{name}」\n"
                f"   日期: {labels.get(day_type, day_type)} | 下线: {offline_time}\n"
                f"（窗口与触发时机由全局设置控制，调度器已自动重载，新计划立即生效）"
            )
            return

        # ── 删除 ──────────────────────────────
        if sub in ("删除", "delete", "del", "rm", "移除"):
            if len(parts) < 4:
                yield event.plain_result("❌ 请指定计划名：/下线通知 计划 删除 <名称>")
                return

            name = " ".join(parts[3:])
            if self.schedule_store.delete(name):
                self._reload_scheduler_from_store()
                yield event.plain_result(
                    f"✅ 已删除计划「{name}」\n（调度器已自动重载）"
                )
            else:
                yield event.plain_result(
                    f"❌ 计划「{name}」不存在\n"
                    f"用 /下线通知 计划 列表 查看所有计划"
                )
            return

        # ── 启用 ──────────────────────────────
        if sub in ("启用", "enable", "on"):
            if len(parts) < 4:
                yield event.plain_result("❌ 请指定计划名：/下线通知 计划 启用 <名称>")
                return

            name = " ".join(parts[3:])
            if self.schedule_store.set_enabled(name, True):
                self._reload_scheduler_from_store()
                yield event.plain_result(f"✅ 已启用计划「{name}」")
            else:
                yield event.plain_result(f"❌ 计划「{name}」不存在")
            return

        # ── 禁用 ──────────────────────────────
        if sub in ("禁用", "disable", "off"):
            if len(parts) < 4:
                yield event.plain_result("❌ 请指定计划名：/下线通知 计划 禁用 <名称>")
                return

            name = " ".join(parts[3:])
            if self.schedule_store.set_enabled(name, False):
                self._reload_scheduler_from_store()
                yield event.plain_result(f"✅ 已禁用计划「{name}」（不会删除，可随时启用）")
            else:
                yield event.plain_result(f"❌ 计划「{name}」不存在")
            return

        # ── 修改 ──────────────────────────────
        if sub in ("修改", "edit", "update", "modify", "改"):
            if len(parts) < 6:
                yield event.plain_result(
                    "❌ 参数不足\n"
                    "格式: /下线通知 计划 修改 <名称> <字段> <值>\n"
                    "字段: 时间(HH:MM) / 日期(类型)\n"
                    "中英文冒号均可，空格自动忽略\n"
                    "示例: /下线通知 计划 修改 工作日下线 时间 22:30\n"
                    "示例: /下线通知 计划 修改 周末晚安 日期 每天"
                )
                return

            # 名称可能是多段（如 "工作日 下线"），字段和值是最后两部分
            # 简单处理：取倒数第2个为字段，最后1个为值，其余为名称
            field = parts[-2]
            value = parts[-1]
            name = " ".join(parts[3:-2])

            if not name:
                yield event.plain_result("❌ 请指定计划名称")
                return

            if not self.schedule_store.exists(name):
                yield event.plain_result(f"❌ 计划「{name}」不存在")
                return

            kwargs = {}
            if field in ("时间", "time"):
                kwargs["offline_time"] = value
            elif field in ("日期", "day", "type"):
                dt = valid_keys.get(value) or value
                if dt not in {"everyday", "weekday", "weekend"}:
                    yield event.plain_result(
                        f"❌ 不支持的日期类型「{value}」\n可选: 每天 / 工作日 / 周末"
                    )
                    return
                kwargs["day_type"] = dt
            else:
                yield event.plain_result(
                    f"❌ 不支持的字段「{field}」\n"
                    "可修改: 时间 / 日期"
                )
                return

            if self.schedule_store.update(name, **kwargs):
                self._reload_scheduler_from_store()

                # 构建友好的变更描述
                field_labels = {
                    "offline_time": "下线时间",
                    "day_type": "日期类型",
                }
                changed = ", ".join(
                    f"{field_labels.get(k, k)} → {v}"
                    for k, v in kwargs.items()
                )
                yield event.plain_result(
                    f"✅ 已更新计划「{name}」: {changed}\n（调度器已自动重载）"
                )
            else:
                yield event.plain_result("❌ 修改失败")
            return

        # 未知子命令 → 帮助
        yield event.plain_result(
            "❓ 未知子命令\n\n"
            "/下线通知 计划           查看帮助\n"
            "/下线通知 计划 列表       查看所有计划\n"
            "/下线通知 计划 添加 ...    添加计划\n"
            "/下线通知 计划 删除 <名称> 删除计划\n"
            "/下线通知 计划 启用 <名称> 启用计划\n"
            "/下线通知 计划 禁用 <名称> 禁用计划\n"
            "/下线通知 计划 修改 ...    修改计划"
        )

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

        self.context.register_web_api(
            f"{prefix}/schedules",
            self._api_get_schedules,
            ["GET"],
            "获取所有定时计划"
        )

        self.context.register_web_api(
            f"{prefix}/schedules",
            self._api_manage_schedule,
            ["POST", "PUT", "DELETE"],
            "管理定时计划（添加/修改/删除/启用/禁用）"
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

            self._ensure_caps()
            ok, hint = validate_target_id_multi(self.capses, target_group)
            if not ok:
                return jsonify({"success": False, "error": hint}), 400

            now = datetime.now()
            offline_time = now.strftime("%H:%M")
            message = await self._generate_message(offline_time, 5)

            self._warn_passive_window([target_group])
            success = await self.notifier.send_to_group(
                target_group, message, self.capses
            )
            return jsonify({
                "success": success,
                "platform": " | ".join(c.describe() for c in self.capses),
                "message": "通知已发送" if success else "发送失败，请查看日志中的具体原因"
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

    async def _api_get_schedules(self):
        """API: 获取所有定时计划"""
        from quart import jsonify

        schedules = self.schedule_store.list_all()
        return jsonify({
            "success": True,
            "data": {
                "schedules": schedules,
                "total": len(schedules),
                "day_type_labels": ScheduleStore.DAY_TYPE_LABELS,
            }
        })

    async def _api_manage_schedule(self):
        """API: 管理定时计划（添加/修改/删除/启用/禁用）"""
        from quart import jsonify, request

        try:
            method = request.method
            data = await request.get_json() if method in ("POST", "PUT") else {}

            if method == "POST":
                name = data.get("name", "").strip()
                action = (data.get("action") or "").strip()

                if not name:
                    return jsonify({"success": False, "error": "计划名称不能为空"}), 400

                # bridge SDK 仅支持 GET/POST，故用 POST+action 承载 DELETE/PUT
                if action == "delete":
                    if self.schedule_store.delete(name):
                        self._reload_scheduler_from_store()
                        return jsonify({"success": True, "message": f"已删除计划「{name}」"})
                    return jsonify({"success": False, "error": f"计划「{name}」不存在"}), 404

                if action in ("enable", "disable"):
                    ok = self.schedule_store.set_enabled(name, action == "enable")
                    if ok:
                        self._reload_scheduler_from_store()
                        label = "启用" if action == "enable" else "禁用"
                        return jsonify({"success": True, "message": f"已{label}计划「{name}」"})
                    return jsonify({"success": False, "error": f"计划「{name}」不存在"}), 404

                if action == "update":
                    # 修改计划（bridge SDK 无 PUT，用 POST+action 承载）
                    if not self.schedule_store.exists(name):
                        return jsonify({"success": False, "error": f"计划「{name}」不存在"}), 404

                    ok = self.schedule_store.update(name, **{
                        k: v for k, v in data.items()
                        if k in ("offline_time", "day_type", "float_range", "enabled")
                    })
                    if ok:
                        self._reload_scheduler_from_store()
                        return jsonify({"success": True, "message": f"已更新计划「{name}」"})
                    return jsonify({"success": False, "error": "更新失败"}), 400

                # 添加（默认）
                day_type = data.get("day_type", "everyday")
                offline_time = data.get("offline_time", "23:00")
                try:
                    float_range = int(data.get("float_range", 0) or 0)
                except (TypeError, ValueError):
                    float_range = 0

                if self.schedule_store.exists(name):
                    return jsonify({"success": False, "error": f"计划「{name}」已存在"}), 409

                ok = self.schedule_store.add(
                    name=name, day_type=day_type,
                    offline_time=offline_time,
                    float_range=max(0, min(float_range, 10)),
                )
                if ok:
                    self._reload_scheduler_from_store()
                    return jsonify({"success": True, "message": f"已添加计划「{name}」"})
                return jsonify({"success": False, "error": "添加失败"}), 400

            elif method == "PUT":
                # 修改 / 启用 / 禁用
                name = data.get("name", "").strip()
                action = data.get("action", "update")

                if not name:
                    return jsonify({"success": False, "error": "计划名称不能为空"}), 400

                if action == "enable":
                    ok = self.schedule_store.set_enabled(name, True)
                elif action == "disable":
                    ok = self.schedule_store.set_enabled(name, False)
                else:
                    ok = self.schedule_store.update(name, **{
                        k: v for k, v in data.items()
                        if k in ("offline_time", "day_type", "enabled")
                    })

                if ok:
                    self._reload_scheduler_from_store()
                    return jsonify({"success": True, "message": f"已更新计划「{name}」"})
                return jsonify({"success": False, "error": f"计划「{name}」不存在"}), 404

            elif method == "DELETE":
                # 删除
                name = request.args.get("name", "").strip()
                if not name:
                    return jsonify({"success": False, "error": "计划名称不能为空"}), 400

                if self.schedule_store.delete(name):
                    self._reload_scheduler_from_store()
                    return jsonify({"success": True, "message": f"已删除计划「{name}」"})
                return jsonify({"success": False, "error": f"计划「{name}」不存在"}), 404

            return jsonify({"success": False, "error": f"不支持的请求方法: {method}"}), 405

        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500