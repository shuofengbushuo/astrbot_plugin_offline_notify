# -*- coding: utf-8 -*-
"""verify_float_record.py —— 验证「浮动」列的数据链路

覆盖 ``_fire_with_float`` -> ``_fire_plan`` -> ``RecordStore.add`` 全链路：

  1. 抽样出来的 delay 是否真的写进了记录（旧实现恒为 0.0）
  2. 0.4s 这种亚秒浮动是否还会被 round 成 0
  3. 同一窗口内多次命中是否会重复抽样 / 重复发送
  4. 计划级 float_range 是否覆盖全局 trigger_float_minutes（旧实现是死配置）
  5. notifier 的 skipped 维度是否被记录保留
  6. advance_minutes 是否取计划真实值（旧实现硬编码 0）

不发送真实消息（stub notifier / LLM / 模板），不读写真实记录文件。

用法::

    python tests/verify_float_record.py
"""

import os
import sys
import json
import types
import asyncio
import random
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
STUBS_DIR = os.path.join(HERE, "_astrbot_stubs")
PLUGIN_PARENT = os.path.dirname(PLUGIN_ROOT)

sys.path.insert(0, STUBS_DIR)
sys.path.insert(0, PLUGIN_PARENT)

import astrbot.api as api                      # noqa: E402
import astrbot.api.event as event_mod          # noqa: E402
import astrbot.api.star as star_mod            # noqa: E402


# ── 补齐 stub：main.py 在导入期需要的装饰器/工具 ──────────────
_DATA_ROOT = {"path": tempfile.mkdtemp(prefix="offline_notify_test_")}


def _install_subcommands(obj):
    """给被装饰对象挂上子命令装饰器（main.py 用 @group.command(...) 注册子命令）。"""
    for attr in ("command", "command_group", "group", "permission_type"):
        try:
            setattr(obj, attr, _passthrough)
        except (AttributeError, TypeError):
            pass
    return obj


def _passthrough(*a, **k):
    """同时兼容 @deco 与 @deco(...) 两种用法的空装饰器。"""
    if len(a) == 1 and callable(a[0]) and not k:
        return _install_subcommands(a[0])

    def deco(func):
        return _install_subcommands(func)
    return deco


class _Filter(event_mod._Filter):
    """任意 @filter.xxx(...) 都退化为空装饰器，只关心被测逻辑。"""

    def __getattr__(self, name):
        return lambda *a, **k: _passthrough()


event_mod.filter = _Filter()
star_mod.register = lambda *a, **k: _passthrough()


class _StarTools:
    @staticmethod
    def get_data_dir(name=""):
        path = os.path.join(_DATA_ROOT["path"], name or "plugin")
        os.makedirs(path, exist_ok=True)
        return path


star_mod.StarTools = _StarTools

from astrbot_plugin_offline_notify.main import OfflineNotifyPlugin  # noqa: E402
from astrbot_plugin_offline_notify.core import (                    # noqa: E402
    RecordStore, ScheduleStore, WindowState,
)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  [{}] {}{}".format("PASS" if cond else "FAIL", name,
                               "" if cond else "  <- " + str(detail)))


class _FakeLLM:
    enabled = True
    provider_id = "fake/provider"

    def get_stats(self):
        return {"last_success": True}

    async def generate_with_fallback(self, offline_time, minutes, fallback):
        return "测试通知内容"


class _FakeTemplate:
    def build_full_message(self, offline_time, minutes):
        return "模板兜底内容"


class _FakeNotifier:
    def __init__(self, skipped=None):
        self.calls = 0
        self.skipped = skipped or []

    async def send_to_groups(self, groups, message, capses,
                             split=True, allow_proactive=False):
        self.calls += 1
        return {"success": list(groups), "failed": [], "skipped": list(self.skipped),
                "total": len(groups) + len(self.skipped)}


def make_plugin(global_float=5, plan_float=0, skipped=None):
    """构造一个只用于记录链路验证的插件实例（不启动调度器）。"""
    tmp = tempfile.mkdtemp(prefix="offline_notify_case_")
    cfg = api.AstrBotConfig({
        "trigger_float_minutes": global_float,
        "window_before_minutes": 3,
        "target_groups": ["GROUP_A"],
    })
    p = OfflineNotifyPlugin(context=None, config=cfg)
    p.plugin_data_dir = tmp
    p.record_store = RecordStore(tmp)
    p.schedule_store = ScheduleStore(tmp)
    p.window_state = WindowState(tmp)
    p.template_engine = _FakeTemplate()
    p.llm_generator = _FakeLLM()
    p.notifier = _FakeNotifier(skipped)
    p.capses = []
    p._ensure_caps = lambda: None          # 不解析真实平台
    p._resolve_platform_caps = lambda *a, **k: None
    # 直接落一条与真实 schedules.json 同构的计划（含历史字段 advance_minutes）
    p.schedule_store._schedules.append({
        "name": "工作日下线",
        "day_type": "weekday",
        "offline_time": "23:15",
        "advance_minutes": 5,
        "float_range": plan_float,
        "enabled": True,
    })
    return p, tmp


def _patch_uniform(value):
    random.uniform = lambda a, b: value


async def case_basic():
    print("\n[1] 抽样 delay 必须落到记录里（旧实现恒 0.0）")
    p, tmp = make_plugin(global_float=1)
    _patch_uniform(3.4)
    plan = p.schedule_store.get("工作日下线")
    await p._fire_with_float(plan, "GROUP_A")
    r = p.record_store._records[-1]
    check("float_seconds == 3.4", r["float_seconds"] == 3.4, r["float_seconds"])
    check("记录带 float_recorded 标记（前端据此区分旧记录）",
          r.get("float_recorded") is True, r.get("float_recorded"))
    check("float_range 记录为 1（全局值）", r["float_range"] == 1, r["float_range"])
    check("advance_minutes 记录为计划值 5（旧实现硬编码 0）",
          r["advance_minutes"] == 5, r["advance_minutes"])
    check("只发送一次", p.notifier.calls == 1, p.notifier.calls)
    shutil.rmtree(tmp, ignore_errors=True)


async def case_subsecond():
    print("\n[2] 亚秒浮动不再被 round 抹成 0")
    p, tmp = make_plugin(global_float=1)
    _patch_uniform(0.4)
    await p._fire_with_float(p.schedule_store.get("工作日下线"), "GROUP_A")
    r = p.record_store._records[-1]
    check("float_seconds == 0.4（旧实现 round(0.4,0)=0.0）",
          r["float_seconds"] == 0.4, r["float_seconds"])
    check("stats 平均值不为 0", p.record_store._records[0]["float_seconds"] > 0)
    shutil.rmtree(tmp, ignore_errors=True)


async def case_dedup():
    print("\n[3] 同一窗口内多条群消息只抽样一次、只发送一次")
    p, tmp = make_plugin(global_float=1)
    calls = {"n": 0}

    def counting_uniform(a, b):
        calls["n"] += 1
        return 1.0
    random.uniform = counting_uniform
    plan = p.schedule_store.get("工作日下线")
    await asyncio.gather(
        p._fire_with_float(plan, "GROUP_A"),
        p._fire_with_float(plan, "GROUP_A"),
        p._fire_with_float(plan, "GROUP_A"),
    )
    check("只抽样一次（旧实现会抽 3 次，实际生效 = min 抽样）",
          calls["n"] == 1, calls["n"])
    check("只发送一次", p.notifier.calls == 1, p.notifier.calls)
    check("只产生一条记录", len(p.record_store._records) == 1,
          len(p.record_store._records))
    shutil.rmtree(tmp, ignore_errors=True)


async def case_plan_override():
    print("\n[4] 计划级 float_range 覆盖全局（旧实现是死配置）")
    p, tmp = make_plugin(global_float=1, plan_float=2)
    check("_resolve_float_minutes == 2", p._resolve_float_minutes(
        p.schedule_store.get("工作日下线")) == 2.0,
        p._resolve_float_minutes(p.schedule_store.get("工作日下线")))
    observed = {"max": None}
    random.uniform = lambda a, b: (observed.__setitem__("max", b), b / 2)[1]
    await p._fire_with_float(p.schedule_store.get("工作日下线"), "GROUP_A")
    check("抽样上界 = 2min = 120s", observed["max"] == 120.0, observed["max"])
    check("记录 float_range == 2", p.record_store._records[-1]["float_range"] == 2,
          p.record_store._records[-1]["float_range"])
    shutil.rmtree(tmp, ignore_errors=True)


async def case_skipped():
    print("\n[5] skipped 维度必须保留（旧实现被丢弃）")
    p, tmp = make_plugin(global_float=1, skipped=["GROUP_X"])
    _patch_uniform(0.0)
    await p._fire_with_float(p.schedule_store.get("工作日下线"), "GROUP_A")
    r = p.record_store._records[-1]["results"]
    check("results.skipped 非空", r.get("skipped") == ["GROUP_X"], r)
    shutil.rmtree(tmp, ignore_errors=True)


async def case_zero_float():
    print("\n[6] 未启用浮动：delay 恒 0，但不再冒充「精确」")
    p, tmp = make_plugin(global_float=0)
    _patch_uniform(5.0)          # 即使抽到值，上界为 0 时也应归零
    await p._fire_with_float(p.schedule_store.get("工作日下线"), "GROUP_A")
    r = p.record_store._records[-1]
    check("float_seconds == 0", r["float_seconds"] == 0, r["float_seconds"])
    check("float_range == 0（前端据此显示「未启用」）", r["float_range"] == 0,
          r["float_range"])
    shutil.rmtree(tmp, ignore_errors=True)


async def case_advance_fallback():
    print("\n[7] 计划缺 advance_minutes（WebUI 新建计划）时退回窗口前置分钟数")
    p, tmp = make_plugin(global_float=1)
    p.schedule_store._schedules[0].pop("advance_minutes", None)
    _patch_uniform(0.0)
    await p._fire_with_float(p.schedule_store.get("工作日下线"), "GROUP_A")
    r = p.record_store._records[-1]
    check("advance_minutes == 3（window_before_minutes）",
          r["advance_minutes"] == 3, r["advance_minutes"])
    shutil.rmtree(tmp, ignore_errors=True)


async def case_caps_selfheal():
    print("\n[8] 热重载后平台画像自愈（否则窗口触发门永久不放行）")
    p, tmp = make_plugin(global_float=1)
    p._ensure_caps = type(p)._ensure_caps.__get__(p)   # 恢复真实实现
    p.capses = []                       # 模拟重载后 on_platform_loaded 未再触发
    calls = {"n": 0}

    def fake_resolve():
        calls["n"] += 1
        p.capses = [types.SimpleNamespace(found=True, is_qq_official=True)]
    p._resolve_platform_caps = fake_resolve

    p._ensure_caps()
    check("未解析时补解析一次", calls["n"] == 1, calls["n"])
    check("解析后 capses 可用", any(c.is_qq_official for c in p.capses))

    p.capses = []                       # 再次失效：冷却期内不应重复解析
    p._ensure_caps()
    check("10s 冷却期内不重复解析（避免每条消息刷日志）",
          calls["n"] == 1, calls["n"])
    shutil.rmtree(tmp, ignore_errors=True)


async def main():
    print("=" * 68)
    print("离线通知 · 浮动记录链路验证")
    print("=" * 68)
    for case in (case_basic, case_subsecond, case_dedup,
                 case_plan_override, case_skipped, case_zero_float,
                 case_advance_fallback, case_caps_selfheal):
        try:
            await case()
        except Exception as e:      # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAIL.append(case.__name__ + " 抛异常: " + repr(e))

    print("\n" + "=" * 68)
    print("通过 {} 项，失败 {} 项".format(len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  - " + f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
