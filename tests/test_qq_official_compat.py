# -*- coding: utf-8 -*-
"""
test_qq_official_compat.py

验证 astrbot_plugin_offline_notify 在 **QQ 官方机器人 API（qq_official）**
协议适配器下能正确工作。

v2.0.0 起仅支持 QQ 官方平台（openid 会话标识），不再支持 aiocqhttp（OneBot）
与数字群号；触发机制改为「被动窗口事件驱动」。

测试策略（不依赖 astrbot 运行时）：
1) 把 tests/_astrbot_stubs 加入 sys.path，提供最小化的 astrbot.* 桩包；
2) 直接以文件路径加载被测模块（core/platform_compat.py、core/notifier.py、
   core/monitor.py、core/window_state.py），避免触发 core/__init__ 对
   apscheduler 的依赖；
3) 用「假 Context + 假平台适配器实例」模拟运行环境：
   - 仅启用 qq_official（当前真机环境）
   - 多个 qq_official 实例（多平台冗余）
   - 非 QQ 官方平台（验证 else 分支健壮性，不误伤）
4) 断言真实行为：UMO 形态、静默失败识别、分段合并、纯文本强制、
   openid 校验、被动窗口探测、告警通道、窗口去重。

覆盖的 7 个已识别风险点：
  R1 send_message 返回 False 被误判成功
  R2 platform_id 与实际启用实例不匹配
  R3 isdigit() 校验拒绝合法 openid
  R4 群消息缺被动 msg_id 时适配器静默丢弃
  R5 QQ 官方默认 Markdown 回复
  R6 长通知分段成倍消耗官方配额
  R7 告警群聊深夜发不出、私聊更可靠
"""

import os
import sys
import asyncio
import importlib.util
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
STUBS_DIR = os.path.join(HERE, "_astrbot_stubs")
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
CORE_DIR = os.path.join(PLUGIN_ROOT, "core")

if STUBS_DIR not in sys.path:
    sys.path.insert(0, STUBS_DIR)

_FAILURES = []


def _fail(msg):
    _FAILURES.append(msg)
    print("  [FAIL] " + msg)


def _ok(msg):
    print("  [PASS] " + msg)


def _check(cond, ok_msg, fail_msg):
    if cond:
        _ok(ok_msg)
    else:
        _fail(fail_msg)
    return bool(cond)


def _load_core_modules():
    """注册一个轻量 core 包，按依赖顺序加载被测模块。

    core/__init__.py 会 import scheduler（依赖 apscheduler），测试环境不一定有，
    因此这里手工构造包对象，只加载真正需要的几个模块。
    """
    core_pkg = types.ModuleType("core")
    core_pkg.__path__ = [CORE_DIR]
    sys.modules["core"] = core_pkg

    def _load(name):
        path = os.path.join(CORE_DIR, name + ".py")
        spec = importlib.util.spec_from_file_location("core." + name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["core." + name] = mod
        spec.loader.exec_module(mod)
        setattr(core_pkg, name, mod)
        return mod

    pc = _load("platform_compat")
    nt = _load("notifier")
    mn = _load("monitor")
    ws = _load("window_state")
    return pc, nt, mn, ws


# ── 假运行时 ────────────────────────────────────────────────


class FakeMeta:
    def __init__(self, inst_id, type_name):
        self.id = inst_id      # 平台实例名（WebUI 里起的名字）
        self.name = type_name  # 适配器类型名


class FakePlatform:
    """假平台适配器实例。

    qq_official 适配器把被动 msg_id / 会话场景缓存在这两个私有字典里，
    这里如实模拟，用于测试 inspect_qq_official_session 的探测能力。
    """

    def __init__(self, inst_id, type_name, sessions=None, scenes=None,
                 allow_group_proactive_send=True):
        self._meta = FakeMeta(inst_id, type_name)
        if type_name in ("qq_official", "qq_official_webhook"):
            self._session_last_message_id = dict(sessions or {})
            self._session_scene = dict(scenes or {})
            # 真实适配器默认 True（qqofficial_platform_adapter L321）
            self._allow_group_proactive_send = allow_group_proactive_send

    def meta(self):
        return self._meta


class FakePlatformManager:
    def __init__(self, insts):
        self.platform_insts = list(insts)


class FakeContext:
    """假 Context：记录所有 send_message 调用，并按 UMO 决定返回值。

    真实 Context.send_message 找不到平台实例时**返回 False 不抛异常**，
    这是本次修复的核心场景，必须如实模拟。
    """

    def __init__(self, insts=(), known_prefixes=None, return_value=True):
        self.platform_manager = FakePlatformManager(insts)
        self.sent = []  # [(umo, text, use_markdown_)]
        self.known_prefixes = known_prefixes
        self.return_value = return_value

    async def send_message(self, umo, chain):
        text = " ".join(
            getattr(c, "text", "") for c in getattr(chain, "chain", [])
        )
        self.sent.append((umo, text, getattr(chain, "use_markdown_", None)))
        if self.known_prefixes is not None:
            pid = umo.split(":", 1)[0]
            if pid not in self.known_prefixes:
                return False  # 平台实例不存在 → 静默失败
            return True
        return self.return_value


OPENID_GROUP = "7CA0B1BF3E5D4A8C9F2B6E1D0A4C8B93"
OPENID_USER = "A1B2C3D4E5F60718293A4B5C6D7E8F90"
QQ_INST = "松板砂糖（测试用）"


def _qq_ctx(sessions=None, scenes=None, **kw):
    proactive = kw.pop("allow_group_proactive_send", True)
    inst = FakePlatform(QQ_INST, "qq_official", sessions=sessions, scenes=scenes,
                        allow_group_proactive_send=proactive)
    return FakeContext([inst], known_prefixes={QQ_INST}, **kw), inst


# ── 用例 1：平台解析与自动回退（R2）────────────────────────


def test_resolve_platform(pc):
    print("\n[1] 平台解析与自动回退（R2: platform_id 与实际实例不匹配）")

    # 1.1 精确匹配
    ctx, _ = _qq_ctx()
    caps = pc.resolve_platform(ctx, QQ_INST)
    _check(caps.found and caps.platform_type == "qq_official",
           f"精确匹配到 qq_official 实例: {caps.describe()}",
           f"精确匹配失败: {caps.describe()}")
    _check(caps.resolved_from == "config",
           "来源标记为 config", f"来源应为 config，实际 {caps.resolved_from}")

    # 1.2 配置名不存在 → 自动回退唯一启用平台
    caps = pc.resolve_platform(ctx, "不存在的名字")
    _check(caps.found and caps.platform_id == QQ_INST
           and caps.resolved_from == "auto",
           f"配置名不存在时自动回退到唯一启用平台 '{caps.platform_id}'",
           f"自动回退失败: {caps.describe()}")

    # 1.3 多平台时禁止回退（避免发错地方）
    multi = FakeContext([FakePlatform(QQ_INST, "qq_official"),
                         FakePlatform("QQ2", "qq_official")])
    caps = pc.resolve_platform(multi, "不存在的名字")
    _check(not caps.found and caps.platform_id == "不存在的名字",
           "多平台环境下不自动回退，保持配置值并报错",
           f"多平台环境下不应回退，实际 {caps.describe()}")

    # 1.4 无任何平台
    caps = pc.resolve_platform(FakeContext([]), "任意名")
    _check(not caps.found and caps.platform_type == "",
           "无已启用平台时返回未解析画像且不抛异常",
           f"无平台场景异常: {caps.describe()}")

    # 1.5 QQ 官方默认策略
    ctx, _ = _qq_ctx()
    caps = pc.resolve_platform(ctx, QQ_INST)
    _check(caps.merge_segments and caps.force_plain_text
           and caps.segment_interval == pc.QQ_OFFICIAL_SEGMENT_INTERVAL,
           "QQ 官方默认: 合并分段 + 强制纯文本 + 1.5s 段间隔",
           f"QQ 官方默认策略不符: merge={caps.merge_segments}, "
           f"plain={caps.force_plain_text}, gap={caps.segment_interval}")

    # 1.6 非 QQ 官方平台保持旧行为（else 分支健壮性，不误伤未来第三方适配器）
    other = FakeContext([FakePlatform("OTHER", "onebot")])
    caps = pc.resolve_platform(other, "OTHER")
    _check(not caps.merge_segments and not caps.force_plain_text
           and caps.segment_interval == pc.DEFAULT_SEGMENT_INTERVAL,
           "非 QQ 官方平台保持: 逐段发送 + 不改 Markdown + 0.8s 段间隔",
           f"非 QQ 官方平台行为异常: merge={caps.merge_segments}, "
           f"plain={caps.force_plain_text}, gap={caps.segment_interval}")

    # 1.7 用户覆盖生效
    ctx, _ = _qq_ctx()
    caps = pc.resolve_platform(
        ctx, QQ_INST,
        overrides={"merge_segments": False, "force_plain_text": None,
                   "segment_interval": 3.0},
    )
    _check(caps.merge_segments is False and caps.force_plain_text is True
           and caps.segment_interval == 3.0,
           "用户覆盖: False 生效 / None 跟随平台默认 / 数值覆盖间隔",
           f"覆盖逻辑异常: merge={caps.merge_segments}, "
           f"plain={caps.force_plain_text}, gap={caps.segment_interval}")


# ── 用例 2：会话标识校验（R3）───────────────────────────────


def test_validate_id(pc):
    print("\n[2] 会话标识校验（R3: isdigit() 拒绝合法 openid）")

    qq_caps = pc.PlatformCaps(platform_id=QQ_INST, platform_type="qq_official",
                              found=True)
    unknown = pc.PlatformCaps(platform_id="x")  # platform_type="" → id_kind="any"

    _check(pc.classify_id(OPENID_GROUP) == "openid",
           "32 位十六进制识别为 openid", "openid 识别失败")
    _check(pc.classify_id("389882949") == "unknown",
           "纯数字识别为 unknown（v2.0.0 起不再有 numeric 形态）", "纯数字识别失败")
    _check(pc.classify_id("群里那个群") == "unknown",
           "乱填内容识别为 unknown", "unknown 识别失败")

    ok, _ = pc.validate_target_id(qq_caps, OPENID_GROUP)
    _check(ok, "QQ 官方下 openid 通过校验（旧 isdigit() 会误拒）",
           "QQ 官方下 openid 被误拒")

    ok, hint = pc.validate_target_id(qq_caps, "389882949")
    _check(not ok and "openid" in hint,
           "QQ 官方下数字群号被拒绝并给出 openid 获取指引",
           f"QQ 官方下数字群号未被拦截: ok={ok}")

    ok, _ = pc.validate_target_id(unknown, OPENID_GROUP)
    _check(ok, "平台类型未知时 openid 通过（放行）", "平台未知时 openid 被误拒")
    ok, _ = pc.validate_target_id(unknown, "389882949")
    _check(ok, "平台类型未知时数字放行（不误伤，无法确定形态要求）",
           "平台未知时数字被误拒")

    ok, hint = pc.validate_target_id(qq_caps, "")
    _check(not ok and "不能为空" in hint, "空标识被拒绝", "空标识未被拒绝")


# ── 用例 3：被动窗口探测（R4）───────────────────────────────


def test_session_probe(pc):
    print("\n[3] QQ 官方被动 msg_id 探测（R4: 适配器静默丢弃）")

    ctx, inst = _qq_ctx(sessions={OPENID_GROUP: "MSGID_123"},
                        scenes={OPENID_GROUP: "group"})
    caps = pc.resolve_platform(ctx, QQ_INST)

    info = pc.inspect_qq_official_session(caps, OPENID_GROUP)
    _check(info["applicable"] and info["has_msg_id"] and info["scene"] == "group",
           "有被动 msg_id 时探测为「可发送」", f"探测结果异常: {info}")

    info = pc.inspect_qq_official_session(caps, "OTHER_OPENID_FFFFFFFFFFFFFFFF")
    _check(info["applicable"] and not info["has_msg_id"] and info["reason"],
           "无 msg_id 缓存时探测为「不可发送」并给出原因",
           f"未能识别缺失 msg_id: {info}")

    # 非 QQ 官方平台不适用该检查
    other = FakeContext([FakePlatform("OTHER", "onebot")])
    other_caps = pc.resolve_platform(other, "OTHER")
    info = pc.inspect_qq_official_session(other_caps, "389882949")
    _check(not info["applicable"],
           "非 QQ 官方平台不做该探测（零影响）",
           f"非 QQ 官方平台被错误纳入探测: {info}")

    # 适配器实现变化（没有私有字典）→ 不阻塞
    weird = FakeContext([FakePlatform(QQ_INST, "qq_official")])
    weird.platform_manager.platform_insts[0]._session_last_message_id = None
    caps2 = pc.resolve_platform(weird, QQ_INST)
    info = pc.inspect_qq_official_session(caps2, OPENID_GROUP)
    _check(not info["applicable"],
           "适配器内部结构变化时降级为不阻塞（向前兼容）",
           f"结构变化时未降级: {info}")


# ── 用例 4：活跃度跟踪 ──────────────────────────────────────


def test_activity_tracker(pc):
    print("\n[4] 会话活跃度跟踪（被动窗口预警数据源）")

    tracker = pc.SessionActivityTracker()
    umo = f"{QQ_INST}:GroupMessage:{OPENID_GROUP}"

    _check(tracker.age(umo) is None, "未记录过的会话 age 为 None",
           "未记录会话 age 应为 None")
    _check(not tracker.is_passive_window_open(umo),
           "未记录过的会话视为窗口关闭", "未记录会话被误判为窗口开启")

    tracker.touch(umo)
    age = tracker.age(umo)
    _check(age is not None and age < 1.0, "touch 后 age 接近 0",
           f"touch 后 age 异常: {age}")
    _check(tracker.is_passive_window_open(umo), "刚收到消息 → 窗口开启",
           "刚收到消息却判为窗口关闭")

    # 手工把时间戳推回 6 分钟前
    tracker._last_seen[umo] -= 360
    _check(not tracker.is_passive_window_open(umo),
           f"6 分钟前的消息 → 窗口关闭（TTL={pc.QQ_OFFICIAL_PASSIVE_TTL}s）",
           "超时消息仍被判为窗口开启")

    snap = tracker.snapshot()
    _check(umo in snap and snap[umo] > 300, "snapshot 返回距今秒数",
           f"snapshot 异常: {snap}")

    tracker.touch("")
    _check("" not in tracker._last_seen, "空 UMO 不入表", "空 UMO 被写入")


# ── 用例 5：静默失败识别（R1）───────────────────────────────


def test_silent_failure(pc, nt):
    print("\n[5] 发送静默失败识别（R1: send_message 返回 False）")

    async def run():
        # 配置的平台名不存在于运行时 → send_message 返回 False
        ctx = FakeContext([FakePlatform(QQ_INST, "qq_official",
                                        sessions={OPENID_GROUP: "M1"},
                                        scenes={OPENID_GROUP: "group"})],
                          known_prefixes={QQ_INST})
        caps = pc.PlatformCaps(platform_id="不存在的平台", platform_type="",
                               found=False)
        notifier = nt.GroupNotifier(ctx, {"max_retries": 3,
                                          "retry_interval_base": 1})
        ok = await notifier.send_to_group(OPENID_GROUP, "测试。", caps,
                                          split=False)
        _check(ok is False, "平台不存在时返回 False（旧实现会误报成功）",
               "平台不存在时仍返回成功")
        _check(len(ctx.sent) == 1,
               "不可重试的失败只尝试 1 次，不做无意义重试",
               f"重试次数异常: {len(ctx.sent)}")
        stats = notifier.get_stats()
        _check(stats["total_failed"] == 1 and "platform_not_found" in
               (stats["last_error"] or ""),
               "统计记录失败原因 platform_not_found",
               f"统计异常: {stats}")

        # 正常平台 → 成功
        ctx2, _ = _qq_ctx(sessions={OPENID_GROUP: "M1"},
                          scenes={OPENID_GROUP: "group"})
        caps2 = pc.resolve_platform(ctx2, QQ_INST)
        n2 = nt.GroupNotifier(ctx2, {"send_retries": 3, "send_retry_interval": 1})
        ok = await n2.send_to_group(OPENID_GROUP, "测试。", caps2, split=False)
        _check(ok is True and len(ctx2.sent) == 1, "平台正常时发送成功",
               f"正常发送失败: ok={ok}, sent={ctx2.sent}")

        # 缺被动 msg_id → 提前拦截，不做无谓重试（R4）
        ctx3, _ = _qq_ctx(sessions={}, scenes={})
        caps3 = pc.resolve_platform(ctx3, QQ_INST)
        n3 = nt.GroupNotifier(ctx3, {"send_retries": 3, "send_retry_interval": 1})
        ok = await n3.send_to_group(OPENID_GROUP, "测试。", caps3, split=False)
        _check(ok is False and len(ctx3.sent) == 0,
               "缺被动 msg_id 时提前拦截，压根不调用 send_message",
               f"未拦截静默丢弃: ok={ok}, sent={ctx3.sent}")
        _check("qq_official_session_not_ready" in
               (n3.get_stats()["last_error"] or ""),
               "失败原因指明 QQ 官方会话未就绪",
               f"失败原因不明确: {n3.get_stats()}")

        # allow_proactive=True 且适配器具备群主动推送条件（scene=group + 开关开）
        # → 放行，走主动推送路径
        ctx4, _ = _qq_ctx(sessions={}, scenes={OPENID_GROUP: "group"})
        caps4 = pc.resolve_platform(ctx4, QQ_INST)
        n4 = nt.GroupNotifier(ctx4, {"send_retries": 1, "send_retry_interval": 1})
        ok = await n4.send_to_group(OPENID_GROUP, "测试。", caps4, split=False,
                                    allow_proactive=True)
        _check(ok is True and len(ctx4.sent) == 1,
               "allow_proactive=True 且 scene=group 时放行主动推送",
               f"主动推送异常: ok={ok}, sent={ctx4.sent}")

        # 冷群（本次运行没收到过消息 ⇒ scene 为空）即使 allow_proactive=True
        # 也发不出去：适配器会 skip send_by_session 静默丢弃，必须提前拦截
        ctx5, _ = _qq_ctx(sessions={}, scenes={})
        caps5 = pc.resolve_platform(ctx5, QQ_INST)
        n5 = nt.GroupNotifier(ctx5, {"send_retries": 1, "send_retry_interval": 1})
        ok = await n5.send_to_group(OPENID_GROUP, "测试。", caps5, split=False,
                                    allow_proactive=True)
        _check(ok is False and len(ctx5.sent) == 0,
               "冷群（无 msg_id 且 scene 空）即使 allow_proactive 也拦截，不记假成功",
               f"冷群误判成功: ok={ok}, sent={ctx5.sent}")
        _check("qq_official_session_not_ready" in
               (n5.get_stats()["last_error"] or ""),
               "冷群拦截原因指明 QQ 官方会话未就绪",
               f"失败原因不明确: {n5.get_stats()}")

        # 适配器关闭群主动推送开关 → 无 msg_id 时同样拦截
        ctx6, _ = _qq_ctx(sessions={}, scenes={OPENID_GROUP: "group"},
                          allow_group_proactive_send=False)
        caps6 = pc.resolve_platform(ctx6, QQ_INST)
        n6 = nt.GroupNotifier(ctx6, {"send_retries": 1, "send_retry_interval": 1})
        ok = await n6.send_to_group(OPENID_GROUP, "测试。", caps6, split=False,
                                    allow_proactive=True)
        _check(ok is False and len(ctx6.sent) == 0,
               "适配器关闭群主动推送时，无 msg_id 仍拦截",
               f"开关关闭未生效: ok={ok}, sent={ctx6.sent}")

    asyncio.run(run())


# ── 用例 6：分段策略与纯文本（R5 / R6）──────────────────────


LONG_MSG = "注意~小砂糖要下线啦！大家早点休息哦。明天见~晚安！"


def test_segments_and_plaintext(pc, nt):
    print("\n[6] 分段策略与纯文本（R5: Markdown / R6: 配额消耗）")

    async def run():
        # QQ 官方：合并为单条
        ctx, _ = _qq_ctx(sessions={OPENID_GROUP: "M1"},
                         scenes={OPENID_GROUP: "group"})
        caps = pc.resolve_platform(ctx, QQ_INST)
        n = nt.GroupNotifier(ctx, {"send_retries": 1, "send_retry_interval": 1})
        ok = await n.send_to_group(OPENID_GROUP, LONG_MSG, caps, split=True)
        _check(ok and len(ctx.sent) == 1,
               f"QQ 官方下 {len(nt.split_long_message(LONG_MSG))} 段合并为 1 条发送"
               f"（节省官方主动消息配额、规避频控）",
               f"QQ 官方未合并分段: sent={len(ctx.sent)}")
        merged = ctx.sent[0][1]
        no_ws = lambda s: "".join(s.split())
        _check(no_ws(merged) == no_ws(LONG_MSG),
               "合并后内容字符完整无丢失", "合并过程丢失内容")
        _check(ctx.sent[0][2] is False,
               "QQ 官方下 use_markdown_ 被强制置为 False（R5）",
               f"未强制纯文本: use_markdown_={ctx.sent[0][2]}")
        _check(ctx.sent[0][0] == f"{QQ_INST}:GroupMessage:{OPENID_GROUP}",
               "UMO 使用 group_openid 作为 session_id",
               f"UMO 形态错误: {ctx.sent[0][0]}")

        # split=False：任何平台都单条
        ctx3, _ = _qq_ctx(sessions={OPENID_GROUP: "M1"},
                          scenes={OPENID_GROUP: "group"})
        caps3 = pc.resolve_platform(ctx3, QQ_INST)
        n3 = nt.GroupNotifier(ctx3, {"send_retries": 1, "send_retry_interval": 1})
        await n3.send_to_group(OPENID_GROUP, LONG_MSG, caps3, split=False)
        _check(len(ctx3.sent) == 1 and ctx3.sent[0][1] == LONG_MSG,
               "split=False 时原文单条发送", "split=False 行为异常")

    asyncio.run(run())


# ── 用例 7：旧式字符串调用向后兼容 ──────────────────────────


def test_backward_compat(pc, nt):
    print("\n[7] 旧式字符串调用向后兼容（不破坏既有调用方/测试）")

    async def run():
        ctx, _ = _qq_ctx(sessions={OPENID_GROUP: "M1"},
                         scenes={OPENID_GROUP: "group"})
        n = nt.GroupNotifier(ctx, {"send_retries": 1, "send_retry_interval": 1})

        # 旧签名：第三个位置参数传字符串平台名
        ok = await n.send_to_group(OPENID_GROUP, "单条。", QQ_INST, split=False)
        _check(ok, "旧式 send_to_group(gid, msg, '平台名') 仍可用",
               "旧式字符串调用失败")
        _check(ctx.sent[0][0] == f"{QQ_INST}:GroupMessage:{OPENID_GROUP}",
               "旧式调用 UMO 形态不变", f"UMO 变了: {ctx.sent[0][0]}")

        # 旧关键字别名 platform_id=
        ctx.sent.clear()
        ok = await n.send_to_group(OPENID_GROUP, "单条。",
                                   platform_id=QQ_INST, split=False)
        _check(ok and ctx.sent[0][0].startswith(QQ_INST + ":"),
               "旧关键字别名 platform_id= 仍可用", "platform_id= 别名失效")

        # send_to_groups 混合 dict / str 配置
        ctx.sent.clear()
        res = await n.send_to_groups(
            [{"group_id": OPENID_GROUP, "enabled": True},
             {"group_id": "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF", "enabled": False},
             OPENID_USER, ""],
            "单条。", QQ_INST, split=False)
        _check(res["total"] == 2 and set(res["success"]) ==
               {OPENID_GROUP, OPENID_USER},
               "send_to_groups 正确处理 dict/str/禁用/空值",
               f"send_to_groups 结果异常: {res}")

    asyncio.run(run())

    _check(nt.SEGMENT_SEND_INTERVAL == pc.DEFAULT_SEGMENT_INTERVAL,
           "模块级 SEGMENT_SEND_INTERVAL 常量保留（向后兼容）",
           "SEGMENT_SEND_INTERVAL 常量丢失")


# ── 用例 8：告警通道（R7）───────────────────────────────────


class FakeScheduler:
    def get_status(self):
        return {"running": False, "error_count": 0, "heartbeat_age": None}


def test_monitor_alert(pc, mn):
    print("\n[8] 监控告警通道（R7: 群告警易失败、私聊更可靠）")

    async def run():
        ctx, _ = _qq_ctx(sessions={}, scenes={})
        caps = pc.resolve_platform(ctx, QQ_INST)

        # 配置里还是旧的数字 QQ / 群号 → 应被拦截并明确报错，而不是假装发出去
        m = mn.SchedulerMonitor(
            ctx, FakeScheduler(),
            {"alert_admin_group": "389882949", "alert_qq": "653767598"},
            caps=caps)
        await m._maybe_alert("调度器未运行")
        _check(len(ctx.sent) == 0,
               "QQ 官方下数字告警目标被拦截，不产生无效发送",
               f"数字告警目标未被拦截: {ctx.sent}")

        # 换成 openid → 群聊、私聊都走出去
        ctx2, _ = _qq_ctx(sessions={}, scenes={})
        caps2 = pc.resolve_platform(ctx2, QQ_INST)
        m2 = mn.SchedulerMonitor(
            ctx2, FakeScheduler(),
            {"alert_admin_group": OPENID_GROUP, "alert_qq": OPENID_USER},
            caps=caps2)
        await m2._maybe_alert("调度器未运行")
        umos = [s[0] for s in ctx2.sent]
        _check(f"{QQ_INST}:GroupMessage:{OPENID_GROUP}" in umos
               and f"{QQ_INST}:FriendMessage:{OPENID_USER}" in umos,
               "openid 告警目标正确构造群聊 + 私聊 UMO",
               f"告警 UMO 异常: {umos}")
        _check(all(s[2] is False for s in ctx2.sent),
               "QQ 官方告警同样强制纯文本", "告警未强制纯文本")

        # 平台不存在 → 返回 False 被识别为失败
        ghost = FakeContext([FakePlatform(QQ_INST, "qq_official")],
                            known_prefixes={"别的平台"})
        m3 = mn.SchedulerMonitor(ghost, FakeScheduler(),
                                 {"alert_qq": OPENID_USER},
                                 caps=pc.resolve_platform(ghost, QQ_INST))
        sent_ok = await m3._send_alert(
            m3.caps.friend_umo(OPENID_USER), "x",
            target=OPENID_USER, channel="私聊", label="告警QQ")
        _check(sent_ok is False,
               "告警发送遇到 send_message 返回 False 时判为失败（R1）",
               "告警静默失败未被识别")

        # 未传 caps → 退化：从 config['platform_ids'] 读取
        ctx5, _ = _qq_ctx(sessions={}, scenes={})
        m5 = mn.SchedulerMonitor(ctx5, FakeScheduler(),
                                 {"platform_ids": [QQ_INST],
                                  "alert_qq": OPENID_USER})
        await m5._maybe_alert("调度器未运行")
        _check(len(ctx5.sent) == 1
               and ctx5.sent[0][0] == f"{QQ_INST}:FriendMessage:{OPENID_USER}",
               "未传 caps 时从 platform_ids 退化构造画像",
               f"旧行为退化失败: {ctx5.sent}")

    asyncio.run(run())


# ── 用例 9：多平台实例（platform_ids 列表）────────────────


def test_multi_platform(pc, nt, mn):
    print("\n[9] 多平台实例发送（platform_ids 列表，多 qq_official 冗余）")

    async def run():
        # 9.1 resolve_platforms 解析多个 qq_official 实例
        multi = FakeContext(
            [FakePlatform(QQ_INST, "qq_official"),
             FakePlatform("QQ2", "qq_official")],
            known_prefixes={QQ_INST, "QQ2"})
        caps_list = pc.resolve_platforms(multi, [QQ_INST, "QQ2"])
        _check(len(caps_list) == 2
               and caps_list[0].platform_type == "qq_official"
               and caps_list[1].platform_type == "qq_official",
               "resolve_platforms 解析出 2 个平台画像，类型正确",
               f"多平台解析异常: {[c.describe() for c in caps_list]}")

        # 9.2 单元素列表与单值 resolve_platform 等价（兼容）
        single = pc.resolve_platforms(multi, [QQ_INST])
        _check(len(single) == 1 and single[0].platform_type == "qq_official",
               "单元素列表解析出 1 个平台画像",
               f"单元素列表异常: {single}")

        # 9.3 部分匹配：配置名含不存在项，但已有匹配则不回退
        partial = pc.resolve_platforms(multi, [QQ_INST, "不存在的平台"])
        _check(len(partial) == 1 and partial[0].platform_id == QQ_INST,
               "部分匹配时只返回能匹配到的平台，不回退、不报错",
               f"部分匹配异常: {partial}")

        # 9.4 多平台校验：数字群号被所有 qq_official 拒绝；openid 被接受
        qq1 = pc.PlatformCaps(platform_id=QQ_INST, platform_type="qq_official", found=True)
        qq2 = pc.PlatformCaps(platform_id="QQ2", platform_type="qq_official", found=True)
        ok, _ = pc.validate_target_id_multi([qq1, qq2], OPENID_GROUP)
        _check(ok, "多 qq_official 下 openid 通过校验", "openid 多平台校验误拒")
        ok, _ = pc.validate_target_id_multi([qq1, qq2], "389882949")
        _check(not ok, "多 qq_official 下数字群号被拒绝", "数字群号多平台校验误放行")
        ok, _ = pc.validate_target_id_multi([], "389882949")
        _check(ok, "无可用画像时宽松放行（不误伤）", "空画像未放行")

        # 9.5 notifier 多平台冗余：同一 openid 通知发到两个 qq_official 实例
        ctx = FakeContext(
            [FakePlatform(QQ_INST, "qq_official",
                          sessions={OPENID_GROUP: "M1"}, scenes={OPENID_GROUP: "group"}),
             FakePlatform("QQ2", "qq_official",
                          sessions={OPENID_GROUP: "M1"}, scenes={OPENID_GROUP: "group"})],
            known_prefixes={QQ_INST, "QQ2"})
        caps2 = pc.resolve_platforms(ctx, [QQ_INST, "QQ2"])
        n = nt.GroupNotifier(ctx, {"send_retries": 1, "send_retry_interval": 1})
        ok = await n.send_to_group(OPENID_GROUP, "测试。", caps2, split=False)
        _check(ok and len(ctx.sent) == 2,
               "两个 qq_official 实例都收到同一 openid 通知（多平台冗余）",
               f"冗余发送异常: {ctx.sent}")

        # 9.6 monitor 多平台告警：openid 目标在每个 qq_official 平台群聊+私聊各发一条
        ctx3 = FakeContext(
            [FakePlatform(QQ_INST, "qq_official"),
             FakePlatform("QQ2", "qq_official")],
            known_prefixes={QQ_INST, "QQ2"})
        caps3 = pc.resolve_platforms(ctx3, [QQ_INST, "QQ2"])
        m = mn.SchedulerMonitor(
            ctx3, FakeScheduler(),
            {"alert_admin_group": OPENID_GROUP, "alert_qq": OPENID_USER},
            caps=caps3)
        await m._maybe_alert("调度器未运行")
        umos = [s[0] for s in ctx3.sent]
        _check(len(ctx3.sent) == 4
               and umos.count(f"{QQ_INST}:GroupMessage:{OPENID_GROUP}") == 1
               and umos.count(f"{QQ_INST}:FriendMessage:{OPENID_USER}") == 1
               and umos.count(f"QQ2:GroupMessage:{OPENID_GROUP}") == 1
               and umos.count(f"QQ2:FriendMessage:{OPENID_USER}") == 1,
               "openid 告警目标在每个 qq_official 平台群聊+私聊各发一条",
               f"多平台告警异常: {umos}")

    asyncio.run(run())


# ── 用例 10：窗口状态机去重（v2.0.0 新增）──────────────────


def test_window_state(ws):
    print("\n[10] 窗口状态机（armed 标记 + 每日发送去重）")

    with tempfile.TemporaryDirectory() as tmp:
        st = ws.WindowState(tmp)

        _check(not st.is_armed("工作日下线"), "初始未武装", "初始不应武装")
        _check(st.armed_plans() == [], "初始武装列表为空", "初始武装列表异常")
        _check(not st.already_sent_today("工作日下线"), "初始未发送", "初始不应已发送")

        # 武装
        st.arm("工作日下线")
        _check(st.is_armed("工作日下线"), "arm 后处于窗口内", "arm 未生效")
        _check(st.armed_plans() == ["工作日下线"], "武装列表包含该计划",
               f"武装列表异常: {st.armed_plans()}")

        # 发送去重：首次可发，标记后同日不再发
        _check(not st.already_sent_today("工作日下线"), "发送前未去重",
               "发送前不应去重")
        st.mark_sent("工作日下线")
        _check(st.already_sent_today("工作日下线"), "mark_sent 后同日已发送",
               "mark_sent 未生效")
        _check(not st.is_armed("工作日下线"), "发送后自动 disarm", "发送后应 disarm")

        # 跨天重置：模拟日期回拨
        st._date = "2000-01-01"
        st._roll_if_new_day()
        _check(not st.already_sent_today("工作日下线"), "跨天后去重失效",
               "跨天后应允许重新发送")

        # 持久化：重新加载后 sent 仍在（同一天内）
        st.arm("工作日下线")
        st.mark_sent("工作日下线")
        st2 = ws.WindowState(tmp)
        _check(st2.already_sent_today("工作日下线"),
               "重启后（同一天）去重标记保留",
               "持久化去重丢失")

        # snapshot
        st3 = ws.WindowState(tmp)
        snap = st3.snapshot()
        _check(snap["date"] == st3._today() and "sent" in snap,
               "snapshot 返回日期与 sent 结构", f"snapshot 异常: {snap}")


# ── 主入口 ──────────────────────────────────────────────────


def main():
    print("=" * 68)
    print("QQ Official 协议兼容性测试 - astrbot_plugin_offline_notify")
    print("=" * 68)

    pc, nt, mn, ws = _load_core_modules()

    test_resolve_platform(pc)
    test_validate_id(pc)
    test_session_probe(pc)
    test_activity_tracker(pc)
    test_silent_failure(pc, nt)
    test_segments_and_plaintext(pc, nt)
    test_backward_compat(pc, nt)
    test_monitor_alert(pc, mn)
    test_multi_platform(pc, nt, mn)
    test_window_state(ws)

    print("\n" + "=" * 68)
    if _FAILURES:
        print(f"结果: 失败 {len(_FAILURES)} 项")
        for f in _FAILURES:
            print("  - " + f)
        return 1
    print("结果: 全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
