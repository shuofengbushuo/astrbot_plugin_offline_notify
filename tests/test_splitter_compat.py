# -*- coding: utf-8 -*-
"""
test_splitter_compat.py

验证 astrobot_plugin_offline_notify 生成的 LLM/模板通知内容
能否被 astrobot_plugin_splitter 正确识别、分段并正常发送。

测试策略（不依赖 astrbot 运行时）：
1) 把 tests/_astrbot_stubs 加入 sys.path，其中包含最小化的
   `astrbot.*` 桩包，使得真实的 splitter 插件 main.py 可直接 import。
2) 直接 import 真实的 splitter 插件代码（astrbot_plugin_splitter/main.py），
   并实例化其 MessageSplitterPlugin（简易模式默认配置）。
3) 用一批「真实且可能不规范」的 LLM 输出 / 模板输出，先经过
   offline_notify 的 normalize_for_splitter 规范化，再喂给 splitter 的
   on_decorating_result 真实管线，验证：
     - 能被正确分段（长文本切成多条）；
     - 分段后语义完整、不丢失任何非空白内容字符；
     - 不出现空段或纯标点碎片段；
     - 每段经模拟 send_message 正常「发送」。

同时单独验证：
   - splitter_compat.normalize_for_splitter 的各类边界处理；
   - offline_notify 的 LLMGenerator.generate() 真实调用路径会输出
     符合分段规范的内容（句末标点闭合、无代码块/思维链包裹）。
"""

import os
import sys
import re
import asyncio
import importlib.util
import types
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STUBS_DIR = os.path.join(HERE, "_astrbot_stubs")
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
SPLITTER_COMPAT = os.path.join(PLUGIN_ROOT, "core", "splitter_compat.py")
LLM_GEN = os.path.join(PLUGIN_ROOT, "core", "llm_generator.py")
SPLITTER_MAIN = os.path.abspath(
    os.path.join(PLUGIN_ROOT, "..", "astrbot_plugin_splitter", "main.py")
)

# 让 astrbot 桩包可被 import
if STUBS_DIR not in sys.path:
    sys.path.insert(0, STUBS_DIR)

_FAILURES = []


def _fail(msg):
    _FAILURES.append(msg)
    print("  [FAIL] " + msg)


def _ok(msg):
    print("  [PASS] " + msg)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _content_chars(s):
    """去掉所有空白/换行后比较内容字符，验证语义完整性。"""
    return re.sub(r"\s+", "", s)


async def _noop_sleep(*a, **k):
    return None


def _make_core_pkg():
    """注册 core 包，使 core.splitter_compat / core.llm_generator
    的相对导入可被解析（不触发 core/__init__ 对 apscheduler 的依赖）。"""
    core_pkg = types.ModuleType("core")
    core_pkg.__path__ = [os.path.join(PLUGIN_ROOT, "core")]
    sys.modules.setdefault("core", core_pkg)


class _FakeCtx:
    """模拟 splitter 的 self.context：记录 send_message 调用。"""

    def __init__(self):
        self.sent = []

    async def send_message(self, umo, mc):
        self.sent.append(list(mc.chain))


def run_split(text):
    """把一条文本作为事件结果喂给真实 splitter 管线，返回所有「已发送」的纯文本段。

    注意：plugin 与用于收集发送记录的 ctx 必须是【同一个实例】，
    否则 splitter 的 send_message 会写入另一个 ctx，收集不到。
    """
    from astrbot.api.message_components import Plain

    chain = [Plain(text)]
    result = types.SimpleNamespace(chain=chain, result_content_type=None)
    ctx = _FakeCtx()

    msg_obj = types.SimpleNamespace(group_id="123", message_id="msg123")
    event = types.SimpleNamespace(
        unified_msg_origin="test:GroupMessage:123",
        message_obj=msg_obj,
    )
    event.get_result = lambda: result
    setattr(event, "__is_llm_reply", True)  # 模拟 on_llm_response 已标记

    splitter_mod = _load_module("splitter_test_main", SPLITTER_MAIN)
    plugin = splitter_mod.MessageSplitterPlugin(
        context=ctx, config={"config_mode": "简易模式"}
    )

    real_sleep = asyncio.sleep
    asyncio.sleep = _noop_sleep
    try:
        asyncio.run(plugin.on_decorating_result(event))
    finally:
        asyncio.sleep = real_sleep

    delivered = []
    for ch in ctx.sent:
        delivered.append("".join(c.text for c in ch if isinstance(c, Plain)))
    delivered.append("".join(c.text for c in result.chain if isinstance(c, Plain)))
    return delivered


# ──────────────────────────────────────────────────────
# 测试集
# ──────────────────────────────────────────────────────
def test_normalize_unit():
    print("[单测] normalize_for_splitter 边界处理")
    from core.splitter_compat import normalize_for_splitter

    cases = [
        ("猪猪们，砂糖先闪啦。晚安好梦哦~", "猪猪们，砂糖先闪啦。晚安好梦哦~。"),
        ("猪猪们。。砂糖溜了。", "猪猪们。砂糖溜了。"),          # 折叠连续句号
        ("！！晚安！", "晚安！"),                                # 折叠连续感叹号 + 去前导标点
        ("```\n猪猪们，砂糖溜了。晚安好梦~\n```", "猪猪们，砂糖溜了。晚安好梦~。",),  # 去代码块（保留正文）
        ("<think>我想说点啥</think>猪猪们，砂糖溜了。晚安！", "猪猪们，砂糖溜了。晚安！"),  # 去思维链
        ("\n\n  猪猪们，砂糖溜了。  \n\n", "猪猪们，砂糖溜了。"),  # 去首尾空白/空行
        ("。猪猪们，砂糖溜了。", "猪猪们，砂糖溜了。"),       # 去前导标点
        ("【标题】\n\n猪猪们，砂糖溜了。", "【标题】\n猪猪们，砂糖溜了。"),  # 折叠 2+ 换行
        ("猪猪们，砂糖溜了。晚安好梦！", "猪猪们，砂糖溜了。晚安好梦！"),  # 已规范不变
    ]
    for inp, exp in cases:
        got = normalize_for_splitter(inp)
        if got == exp:
            _ok("normalize(%r) == %r" % (inp, exp))
        else:
            _fail("normalize(%r) == %r，实际 %r" % (inp, exp, got))

    base = ("猪猪们，砂糖先闪啦~ 今晚要下线休息咯。记得保存好手头的工作哦，"
             "明天见！晚安好梦，做个甜甜的梦~")
    norm = normalize_for_splitter(base)
    if _content_chars(norm) == _content_chars(base + "。"):
        _ok("语义完整：规范化未丢失正文内容")
    else:
        _fail("语义不完整：规范化改变了正文内容")


def test_llm_generator_path():
    print("[单测] LLMGenerator.generate() 输出符合『16岁真人女孩』人设")
    from core.llm_generator import LLMGenerator, BUILTIN_PROMPT, MANUAL_PROMPT
    from core.notifier import split_long_message

    import re

    def chinese_chars(text: str) -> int:
        return len(re.findall(r"[\u4e00-\u9fff]", text))

    _FORBIDDEN = ["AI", "人工智能", "机器人", "助手", "通知", "系统",
                   "下线", "离线", "退出登录", "挂机"]

    def _has_forbidden(text):
        t = (text or "").lower()
        return any(f.lower() in t for f in _FORBIDDEN)

    def _has_clock_offline(text):
        # 形如「15:02下线」「23:00下线」的带具体时刻写法
        return bool(re.search(r"\d{1,2}:\d{2}\s*下线", text or ""))

    # 模拟一个符合人设的合规输出：夜里说去睡觉，带晚安/明天见祝福，
    # 不出现禁用词、不写「XX:XX下线」。
    class _Resp:
        completion_text = "猪猪们，砂糖困得不行要去睡觉觉啦！都这个点咯，大家也早点歇着呀，明天见~晚安好梦！"

    class _FakeProvider:
        async def text_chat_stream(self, prompt=None, **kw):
            yield _Resp()

    class _FakeProvMgr:
        async def get_provider_by_id(self, provider_id):
            return _FakeProvider()

    class FakeCtx:
        provider_manager = _FakeProvMgr()

        async def llm_generate(self, **kw):
            return _Resp()

    gen = LLMGenerator(
        FakeCtx(),
        {"enable_llm_generation": True, "llm_provider_id": "p1",
         "llm_timeout": 15, "llm_disable_thinking": True},
    )
    out = asyncio.run(gen.generate("23:00", 5, now=datetime(2026, 7, 27, 22, 55)))

    # a) 人设：自称砂糖、含祝福，且不暴露身份/系统口吻
    if "砂糖" in out and ("晚安" in out or "好梦" in out or "明天见" in out or "拜拜" in out):
        _ok("generate() 输出含『砂糖』人设与道别祝福: %r" % out)
    else:
        _fail("generate() 输出缺少人设/祝福: %r" % out)

    if not _has_forbidden(out):
        _ok("generate() 输出未出现 AI/系统/下线 等禁用词")
    else:
        _fail("generate() 输出含禁用词: %r" % out)

    if not _has_clock_offline(out):
        _ok("generate() 输出未写「XX:XX下线」这种带具体时刻的写法")
    else:
        _fail("generate() 输出含『XX:XX下线』: %r" % out)

    # b) 可被正常按句分段（1~2 段，每段都有实质内容）
    segs = split_long_message(out)
    if 1 <= len(segs) <= 2 and all(chinese_chars(s) >= 6 for s in segs):
        _ok("generate() 输出切分为 %d 段、每段均有实质内容: %r" % (len(segs), segs))
    else:
        _fail("generate() 分段异常（%d 段）: %r" % (len(segs), segs))

    # c) 无结构性包裹
    if "```" not in out and "<think" not in out.lower():
        _ok("generate() 输出无代码块/思维链包裹")
    else:
        _fail("generate() 输出含结构性包裹: %r" % out)

    # d) 提示词已包含 16 岁真人女孩「砂糖」人设 + 昼夜情境 + 禁用词约束
    #    v1.5.0：恢复身份声明「真实的小姑娘」，自称改为「砂糖」（群友也叫"小砂糖"）
    if ("16岁" in BUILTIN_PROMPT and "{time_context}" in BUILTIN_PROMPT
            and "真实的小姑娘" in BUILTIN_PROMPT
            and "小砂糖" in BUILTIN_PROMPT and "「砂糖」" in BUILTIN_PROMPT
            and "小名叫" not in BUILTIN_PROMPT):
        _ok("BUILTIN_PROMPT 含 16岁「砂糖」人设、昼夜情境、身份声明")
    else:
        _fail("BUILTIN_PROMPT 人设/情境/身份声明不符合预期")
    if "25-35" not in BUILTIN_PROMPT and "12-18" not in BUILTIN_PROMPT:
        _ok("BUILTIN_PROMPT 已移除死板字数约束（25-35/12-18）")
    else:
        _fail("BUILTIN_PROMPT 仍含死板字数约束")
    if ("下线" in BUILTIN_PROMPT and "AI" in BUILTIN_PROMPT
            and "不要" in BUILTIN_PROMPT):
        _ok("BUILTIN_PROMPT 已要求避免 AI/系统口吻与『下线』字眼")
    else:
        _fail("BUILTIN_PROMPT 缺少避免 AI/系统口吻的约束")
    if ("16岁" in MANUAL_PROMPT and "AI" in MANUAL_PROMPT
            and "真实的小姑娘" in MANUAL_PROMPT
            and "小砂糖" in MANUAL_PROMPT and "「砂糖」" in MANUAL_PROMPT
            and "小名叫" not in MANUAL_PROMPT):
        _ok("MANUAL_PROMPT 同样含「砂糖」人设、身份声明与避免 AI 口吻约束")
    else:
        _fail("MANUAL_PROMPT 人设/身份声明/避免 AI 口吻不符合预期")

    # e) 脏输出（代码块包裹）应被规范化清洗，且仍保持合规、可分段
    class _RespFence:
        completion_text = "```\n猪猪们，砂糖要去睡懒觉啦！都这个点咯，大家晚安好梦，明天见~\n```"

    class FakeCtx2:
        provider_manager = _FakeProvMgr()

        async def llm_generate(self, **kw):
            return _RespFence()

    gen2 = LLMGenerator(
        FakeCtx2(),
        {"enable_llm_generation": True, "llm_provider_id": "p1",
         "llm_timeout": 15, "llm_disable_thinking": True},
    )
    out2 = asyncio.run(gen2.generate("23:00", 5, now=datetime(2026, 7, 27, 22, 55)))
    if "```" not in out2 and not _has_forbidden(out2) and not _has_clock_offline(out2):
        _ok("脏输出（代码块）已被规范化清洗且仍合规: %r" % out2)
    else:
        _fail("脏输出清洗/合规异常: %r" % out2)
    if 1 <= len(split_long_message(out2)) <= 2:
        _ok("清洗后仍可被正常分段")
    else:
        _fail("清洗后段数异常: %r" % split_long_message(out2))


def test_real_splitter_integration():
    print("[集成] 真实 splitter 管线对通知内容的分段与发送")
    from core.splitter_compat import normalize_for_splitter, is_splitter_friendly

    _load_module("splitter_test_main", SPLITTER_MAIN)

    raw_cases = [
        # 1) 结尾无标点 + emoji
        "猪猪们，砂糖先闪啦~ 今晚要下线休息咯。记得保存好手头的工作哦，明天见！晚安好梦，做个甜甜的梦~ 💤",
        # 2) 连续重复句末标点
        "猪猪们。。砂糖溜了。晚安好梦哦~。",
        # 3) 包裹在代码块里（历史脏输出）
        "```\n猪猪们，砂糖要下线啦。记得保存进度哦。晚安好梦~\n```",
        # 4) 思维链标签
        "<think>先想想说啥</think>猪猪们，砂糖先溜啦。明天见，晚安好梦！",
        # 5) 极短单句（不应被切成碎片）
        "晚安啦猪猪们，砂糖下线咯。",
        # 6) 模板回退风格（标题+正文，含空行）
        "【AI服务下线通知】\n\n猪猪们~\n🌙 AI助手即将在 23:00 下线休息，预计还有 5 分钟。\n如有需要请尽快处理未完成的事项。\n明天见！晚安~",
    ]

    for i, raw in enumerate(raw_cases, 1):
        norm = normalize_for_splitter(raw)
        if not is_splitter_friendly(norm):
            _fail("用例%d 规范化后仍未达 splitter 友好: %r" % (i, norm))
            continue

        delivered = run_split(norm)
        joined = "".join(delivered)

        # a) 语义完整：非空白内容字符完全一致
        if _content_chars(joined) != _content_chars(norm):
            _fail("用例%d 分段后内容丢失/变更:\n  原=%r\n  得=%r"
                   % (i, _content_chars(norm), _content_chars(joined)))
            continue

        # b) 不出现空段或纯标点碎片段
        bad = [d for d in delivered if d.strip() and d.strip() in "。！？!?；;"]
        if bad:
            _fail("用例%d 出现纯标点碎片段: %r" % (i, bad))
            continue

        # c) 长文本（用例1/6）应被切成多条；短文本（用例5）允许 1 条
        seg_count = len([d for d in delivered if d.strip()])
        if i in (1, 6) and seg_count < 2:
            _fail("用例%d 长文本未被分段（仅 %d 条）" % (i, seg_count))
            continue

        _ok("用例%d 分段%d条、内容完整、无纯标点碎片" % (i, seg_count))


def test_self_split_function():
    print("[单测] split_long_message 自分段（与 splitter 简单模式语义一致）")
    from core.notifier import split_long_message

    # 用户真实反馈的案例：约 70 字符通知此前单条发出、未被分段。
    # 注意 ~ 不是分隔符，文本真正按 ！ 切分，末段以 。 结尾。
    user_case = ("猪猪们，砂糖要暂时下线啦～午饭时间到，该去填饱肚子咯！"
                 "周一也要元气满满，记得好好吃饭呀～祝大家午安好梦，"
                 "美滋滋地休息一下吧！晚安啦，好梦～。")
    segs = split_long_message(user_case)

    if len(segs) < 2:
        _fail("用户案例未被分段（仅 %d 段）" % len(segs))
    else:
        _ok("用户案例已切成 %d 段" % len(segs))

    # 语义完整：拼接（去空白）后与原文本一致
    if _content_chars("".join(segs)) == _content_chars(user_case):
        _ok("自分段语义完整（无内容丢失）")
    else:
        _fail("自分段后内容丢失/变更")

    # 不出现空段或纯标点碎片段
    bad = [s for s in segs if s.strip() and s.strip() in "。！？!?；;"]
    if bad:
        _fail("自分段出现纯标点碎片: %r" % bad)
    else:
        _ok("无空段/纯标点碎片")

    # 除末段外，每前段应以句末标点结尾
    ok_bound = True
    for s in segs[:-1]:
        if not re.search(r"[。！？!?；;\n]\s*$", s):
            ok_bound = False
            _fail("前段未以句末标点结尾: %r" % s)
            break
    if ok_bound:
        _ok("各前段均以句末标点闭合")

    # 边界：空串返回 []
    if split_long_message("") != []:
        _fail("空串应返回 []")
    else:
        _ok("空串返回 []")

    # 边界：无分隔符返回单条 [text]
    single = "猪猪们砂糖要下线啦"
    if split_long_message(single) == [single]:
        _ok("无分隔符返回单条")
    else:
        _fail("无分隔符未返回单条: %r" % split_long_message(single))

    # 边界：连续分隔符折叠为单一切分点，不产出空段
    folded = split_long_message("猪猪们！！！砂糖溜了。")
    if all(s.strip() for s in folded):
        _ok("连续分隔符折叠、无空段（%d 段）" % len(folded))
    else:
        _fail("连续分隔符折叠异常: %r" % folded)


def test_send_to_group_self_split():
    print("[集成] GroupNotifier.send_to_group 自分段逐条发送")
    from core.notifier import GroupNotifier

    class _RecCtx:
        def __init__(self):
            self.calls = []

        async def send_message(self, umo, chain):
            text = "".join(
                c.text for c in chain.chain
                if getattr(c, "text", None) is not None
            )
            self.calls.append((umo, text))

    ctx = _RecCtx()
    notifier = GroupNotifier(ctx, {"max_retries": 1, "retry_interval_base": 0})
    msg = ("猪猪们，砂糖要暂时下线啦～午饭时间到，该去填饱肚子咯！"
            "周一也要元气满满，记得好好吃饭呀～祝大家午安好梦，"
            "美滋滋地休息一下吧！晚安啦，好梦～。")
    res = asyncio.run(notifier.send_to_group("389882949", msg, platform_id="小砂糖"))

    if not res:
        _fail("send_to_group 返回失败")
        return
    if len(ctx.calls) < 2:
        _fail("send_to_group 仅发送 %d 条，未分段" % len(ctx.calls))
        return

    umos = [u for u, _ in ctx.calls]
    if all(u == "小砂糖:GroupMessage:389882949" for u in umos):
        _ok("分 %d 条发送到正确 UMO" % len(ctx.calls))
    else:
        _fail("UMO 异常: %r" % umos)

    joined = "".join(t for _, t in ctx.calls)
    if _content_chars(joined) == _content_chars(msg):
        _ok("逐条发送内容完整、无丢失")
    else:
        _fail("逐条发送内容缺失")


def test_template_build_single():
    print("[单测] TemplateEngine 回退模板为单条纯文本（不分段）")
    from core.template_engine import TemplateEngine

    # 回退为「修改前的样子」
    cfg = {
        "title_template": "",
        "body_template": "注意~{emoji} 小砂糖即将下线休息，明天见！晚安~",
        "emoji": "🌙",
        "footer_template": "",
    }
    eng = TemplateEngine(cfg)
    msg = eng.build_full_message("23:00", 5)
    expected = "注意~🌙 小砂糖即将下线休息，明天见！晚安~"

    if msg == expected:
        _ok("回退模板渲染为单条纯文本，无多余标点/规范化: %r" % msg)
    else:
        _fail("回退模板渲染异常: 期望 %r 实际 %r" % (expected, msg))

    # 标题/底部留空时不应混入空行分隔（保持单条纯文本）
    if "\n" not in msg:
        _ok("回退模板无多余换行（单段纯文本）")
    else:
        _fail("回退模板出现多余换行: %r" % msg)


def test_send_to_group_no_split_template():
    print("[集成] GroupNotifier.send_to_group 模板回复单条发送（不分段）")
    from core.notifier import GroupNotifier

    class _RecCtx:
        def __init__(self):
            self.calls = []

        async def send_message(self, umo, chain):
            text = "".join(
                c.text for c in chain.chain
                if getattr(c, "text", None) is not None
            )
            self.calls.append((umo, text))

    ctx = _RecCtx()
    notifier = GroupNotifier(ctx, {"max_retries": 1, "retry_interval_base": 0})

    # 回退后的模板（含有 ！，若被误分段会变成 2 条）
    tpl = "注意~🌙 小砂糖即将下线休息，明天见！晚安~"
    res = asyncio.run(
        notifier.send_to_group("389882949", tpl, platform_id="小砂糖", split=False)
    )

    if not res:
        _fail("send_to_group(模板, split=False) 返回失败")
        return
    if len(ctx.calls) != 1:
        _fail("send_to_group(模板, split=False) 发送了 %d 条，应为 1 条"
              % len(ctx.calls))
        return

    umo, text = ctx.calls[0]
    if umo == "小砂糖:GroupMessage:389882949" and text == tpl:
        _ok("模板回复作为单条纯文本发送到正确 UMO，内容未切分: %r" % text)
    else:
        _fail("模板回复发送异常 UMO=%r 文本=%r" % (umo, text))


def test_generate_fallback_when_llm_disabled():
    print("[单测] LLM 禁用时 generate_with_fallback 回退模板引擎")
    from core.llm_generator import LLMGenerator
    from core.template_engine import TemplateEngine

    # 回退模板配置（即「修改前的样子」）
    cfg = {
        "title_template": "",
        "body_template": "注意~{emoji} 小砂糖即将下线休息，明天见！晚安~",
        "emoji": "🌙",
        "footer_template": "",
    }
    eng = TemplateEngine(cfg)
    fallback = eng.build_full_message("23:00", 5)

    # a) 模板引擎渲染结果即「回退模板」原样（单条纯文本，无多余换行）
    expected = "注意~🌙 小砂糖即将下线休息，明天见！晚安~"
    if fallback == expected and "\n" not in fallback:
        _ok("模板引擎渲染为回退模板原样（单条纯文本）: %r" % fallback)
    else:
        _fail("模板渲染不符: 期望 %r 实际 %r" % (expected, fallback))

    # b) LLM 禁用时，generate_with_fallback 直接返回模板
    #    （不报错、不发起 LLM 调用）
    class FakeCtx:
        def __init__(self):
            self.called = False

        async def llm_generate(self, **kw):
            self.called = True
            return None

    ctx = FakeCtx()
    gen = LLMGenerator(
        ctx,
        {
            "enable_llm_generation": False,
            "llm_provider_id": "",
            "llm_timeout": 15,
            "fallback_to_template": True,
        },
    )
    out = asyncio.run(gen.generate_with_fallback("23:00", 5, fallback))
    if out == fallback and not ctx.called:
        _ok("LLM 禁用 → 回退模板，且未发起 LLM 调用")
    else:
        _fail("LLM 禁用回退异常: out=%r, called=%s" % (out, ctx.called))

    # c) 来源判定（复刻 main._detect_message_source）：禁用时应为 template
    #    → 模板回复走单条发送、不分段
    stats = gen.get_stats()
    source = ("llm"
              if (gen.enabled and gen.provider_id
                      and stats.get("last_success"))
              else "template")
    if source == "template":
        _ok("禁用时来源判定为 template（单条发送，不分段）")
    else:
        _fail("来源判定错误: %r" % source)


def test_prompt_store():
    print("[单测] PromptStore 命名方案库 CRUD + 持久化")
    import tempfile
    import shutil
    from core.prompt_store import PromptStore

    d = tempfile.mkdtemp()
    try:
        store = PromptStore(d)
        # 初始空
        if store.list_profiles() == [] and store.get_active() is None:
            _ok("初始方案库为空、无激活方案")
        else:
            _fail("初始状态异常: %r / active=%r" % (store.list_profiles(), store.get_active()))

        # upsert 两个方案
        store.upsert("温柔版", "B1", "M1")
        store.upsert("活泼版", "B2", "M2")
        if set(store.list_profiles()) == {"温柔版", "活泼版"}:
            _ok("upsert 两个方案后 list 正确")
        else:
            _fail("upsert 后 list 异常: %r" % store.list_profiles())

        # get_profile
        prof = store.get_profile("温柔版")
        if prof and prof["builtin_prompt"] == "B1" and prof["manual_prompt"] == "M1":
            _ok("get_profile 返回正确内容")
        else:
            _fail("get_profile 异常: %r" % prof)

        # 更新保留 created_at
        old_created = prof["created_at"]
        store.upsert("温柔版", "B1-new", "M1-new")
        prof2 = store.get_profile("温柔版")
        if (prof2["builtin_prompt"] == "B1-new"
                and prof2["created_at"] == old_created
                and prof2["updated_at"] >= old_created):
            _ok("upsert 更新内容但保留 created_at")
        else:
            _fail("upsert 更新异常: %r" % prof2)

        # set_active / get_active
        if store.set_active("温柔版") and store.get_active() == "温柔版":
            _ok("set_active / get_active 正确")
        else:
            _fail("激活异常")

        # set_active 不存在 → False
        if not store.set_active("不存在"):
            _ok("激活不存在的方案返回 False")
        else:
            _fail("激活不存在的方案应失败")

        # set_active(None) 取消
        store.set_active(None)
        if store.get_active() is None:
            _ok("set_active(None) 取消激活")
        else:
            _fail("取消激活异常")

        # 持久化：重新加载
        store2 = PromptStore(d)
        if (set(store2.list_profiles()) == {"温柔版", "活泼版"}
                and store2.get_profile("活泼版")["manual_prompt"] == "M2"):
            _ok("重新加载后方案持久化正确")
        else:
            _fail("持久化异常: %r" % store2.list_profiles())

        # delete 激活中的方案 → 同时取消激活
        store2.set_active("活泼版")
        if (store2.delete("活泼版")
                and "活泼版" not in store2.list_profiles()
                and store2.get_active() is None):
            _ok("删除激活方案后自动取消激活")
        else:
            _fail("删除激活方案异常")

        # delete 不存在 → False
        if not store2.delete("不存在"):
            _ok("删除不存在方案返回 False")
        else:
            _fail("删除不存在方案应返回 False")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_prompt_override():
    print("[单测] LLMGenerator 提示词三级覆盖（激活方案 > 配置 > 默认）")
    import tempfile
    import shutil
    from core.llm_generator import LLMGenerator, BUILTIN_PROMPT, MANUAL_PROMPT
    from core.prompt_store import PromptStore

    class _FakeCtx:
        pass

    # 1) 无配置无方案 → 内置默认
    gen0 = LLMGenerator(_FakeCtx(), {"enable_llm_generation": False})
    b, m = gen0.get_effective_prompts()
    if b is BUILTIN_PROMPT and m is MANUAL_PROMPT:
        _ok("无配置无方案 → 内置默认")
    else:
        _fail("默认回退异常: b is BUILTIN=%s, m is MANUAL=%s"
              % (b is BUILTIN_PROMPT, m is MANUAL_PROMPT))

    # 2) 配置自定义 → 覆盖默认
    gen1 = LLMGenerator(_FakeCtx(), {
        "enable_llm_generation": False,
        "custom_builtin_prompt": "自定义B",
        "custom_manual_prompt": "自定义M",
    })
    b1, m1 = gen1.get_effective_prompts()
    if b1 == "自定义B" and m1 == "自定义M":
        _ok("配置自定义覆盖默认")
    else:
        _fail("配置覆盖异常: %r %r" % (b1, m1))

    # 3) 激活方案 → 覆盖配置
    d = tempfile.mkdtemp()
    try:
        store = PromptStore(d)
        store.upsert("方案A", "方案A-B", "方案A-M")
        store.set_active("方案A")
        gen2 = LLMGenerator(_FakeCtx(), {
            "enable_llm_generation": False,
            "custom_builtin_prompt": "自定义B",
            "custom_manual_prompt": "自定义M",
        }, prompt_store=store)
        b2, m2 = gen2.get_effective_prompts()
        if b2 == "方案A-B" and m2 == "方案A-M":
            _ok("激活方案覆盖配置自定义")
        else:
            _fail("方案覆盖异常: %r %r" % (b2, m2))

        # 取消激活 → 回退到配置
        store.set_active(None)
        b3, m3 = gen2.get_effective_prompts()
        if b3 == "自定义B" and m3 == "自定义M":
            _ok("取消激活后回退到配置自定义")
        else:
            _fail("回退配置异常: %r %r" % (b3, m3))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    print("=" * 64)
    print("离线通知 × splitter 分段兼容性测试")
    print("=" * 64)

    _make_core_pkg()
    _load_module("core.splitter_compat", SPLITTER_COMPAT)
    _load_module("core.llm_generator", LLM_GEN)

    test_normalize_unit()
    test_llm_generator_path()
    test_prompt_store()
    test_prompt_override()
    test_real_splitter_integration()
    test_self_split_function()
    test_send_to_group_self_split()
    test_template_build_single()
    test_send_to_group_no_split_template()
    test_generate_fallback_when_llm_disabled()

    print("=" * 64)
    if _FAILURES:
        print("结果: 失败 %d 项" % len(_FAILURES))
        for f in _FAILURES:
            print("  - " + f)
        sys.exit(1)
    else:
        print("结果: 全部通过 ✅")
        sys.exit(0)


if __name__ == "__main__":
    main()
