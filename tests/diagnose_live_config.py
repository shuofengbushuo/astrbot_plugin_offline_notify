# -*- coding: utf-8 -*-
"""
diagnose_live_config.py —— 离线体检工具

不启动 AstrBot，直接读取本机真实的 ``data/cmd_config.json``（已启用的平台实例）
和本插件的配置文件，跑一遍平台适配层的判定逻辑，回答三个问题：

  1. 插件配置的 platform_ids（兼容旧字段 platform_id）能不能找到真实存在的平台实例？会不会自动回退？
  2. target_groups / 告警目标的标识形态跟当前平台匹配吗？
  3. QQ 官方协议下，这些群当前有没有可用的被动 msg_id？

用法::

    python tests/diagnose_live_config.py
    python tests/diagnose_live_config.py --astrbot-root D:/AstrBot

退出码：0 = 无阻塞问题；1 = 存在会导致通知发不出去的配置问题。
"""

import os
import sys
import json
import types
import argparse
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
STUBS_DIR = os.path.join(HERE, "_astrbot_stubs")
PLUGIN_NAME = os.path.basename(PLUGIN_ROOT)

if STUBS_DIR not in sys.path:
    sys.path.insert(0, STUBS_DIR)


def _load_platform_compat():
    """只加载 platform_compat，绕开 core/__init__ 对 apscheduler 的依赖。"""
    core_pkg = types.ModuleType("core")
    core_pkg.__path__ = [os.path.join(PLUGIN_ROOT, "core")]
    sys.modules["core"] = core_pkg
    path = os.path.join(PLUGIN_ROOT, "core", "platform_compat.py")
    spec = importlib.util.spec_from_file_location("core.platform_compat", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["core.platform_compat"] = mod
    spec.loader.exec_module(mod)
    return mod


def _read_json(path):
    # AstrBot 的配置文件可能带 UTF-8 BOM
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


# ── 用真实配置构造出「假的但形态一致」的平台实例 ──────────────


class _Meta:
    def __init__(self, inst_id, type_name):
        self.id = inst_id
        self.name = type_name


class _Inst:
    def __init__(self, inst_id, type_name):
        self._m = _Meta(inst_id, type_name)
        if str(type_name).startswith("qq_official"):
            # 离线体检拿不到运行时缓存，按「最坏情况」空字典模拟
            self._session_last_message_id = {}
            self._session_scene = {}

    def meta(self):
        return self._m


class _Ctx:
    def __init__(self, insts):
        self.platform_manager = type("M", (), {"platform_insts": insts})()


def _tri(value):
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in ("auto", "自动", ""):
        return None
    if text in ("true", "on", "yes", "1", "开启", "是"):
        return True
    if text in ("false", "off", "no", "0", "关闭", "否"):
        return False
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--astrbot-root",
                    default=os.path.abspath(
                        os.path.join(PLUGIN_ROOT, "..", "..", "..")),
                    help="AstrBot 根目录（含 data/ 的那一层）")
    args = ap.parse_args(argv)

    root = args.astrbot_root
    cmd_path = os.path.join(root, "data", "cmd_config.json")
    cfg_path = os.path.join(root, "data", "config",
                            PLUGIN_NAME + "_config.json")

    for p in (cmd_path, cfg_path):
        if not os.path.isfile(p):
            print("找不到配置文件: " + p)
            print("请用 --astrbot-root 指定 AstrBot 根目录。")
            return 2

    pc = _load_platform_compat()
    cmd = _read_json(cmd_path)
    cfg = _read_json(cfg_path)

    problems = []

    print("=" * 66)
    print("离线通知插件 · 平台配置体检")
    print("=" * 66)

    enabled = [p for p in cmd.get("platform", []) if p.get("enable")]
    print("\n[1] 已启用的平台实例")
    if not enabled:
        print("  (无) —— 没有任何已启用的适配器，通知无法送达")
        problems.append("没有已启用的平台适配器")
    for p in enabled:
        print("  · {}   type={}".format(p.get("id"), p.get("type")))

    ctx = _Ctx([_Inst(p.get("id"), p.get("type")) for p in enabled])

    raw_ids = cfg.get("platform_ids") or []
    if isinstance(raw_ids, str):
        raw_ids = [raw_ids]
    single = cfg.get("platform_id", "") or ""
    ids = list(dict.fromkeys(
        [i for i in list(raw_ids) + ([single] if single else []) if i]))
    qq = cfg.get("qq_official_config", {}) or {}
    overrides = {
        "merge_segments": _tri(qq.get("merge_segments", "auto")),
        "force_plain_text": _tri(qq.get("force_plain_text", "auto")),
        "segment_interval": qq.get("segment_interval") or None,
    }
    caps_list = pc.resolve_platforms(ctx, ids, overrides=overrides)

    print("\n[2] 平台解析")
    print("  配置 platform_ids: {!r}".format(ids))
    print("  配置 platform_id  : {!r}（兼容单值，已并入列表）".format(
        cfg.get("platform_id", "") or "(未设置)"))
    if not caps_list:
        print("  实际生效         : (无)")
    for i, c in enumerate(caps_list):
        print("  {}. {}".format(i + 1, c.describe()))
    if caps_list and caps_list[0].resolved_from == "auto":
        print("  提示: 已自动回退。建议把 platform_ids 显式设为 {}".format(
            [c.platform_id for c in caps_list]))
    any_found = any(c.found for c in caps_list)
    if not any_found:
        problems.append(
            "配置的平台实例 {} 都未匹配到已启用实例，通知会静默失败".format(ids))

    print("\n[3] 目标群标识校验")
    targets = []
    for g in cfg.get("target_groups", []) or []:
        gid = g if isinstance(g, str) else str(g.get("group_id", ""))
        if isinstance(g, dict) and not g.get("enabled", True):
            continue
        if gid:
            targets.append(gid)
    if not targets:
        print("  (未配置目标群)")
    for gid in targets:
        ok, hint = pc.validate_target_id_multi(caps_list, gid)
        print("  · {} -> {}".format(gid, "OK" if ok else "不匹配(所有平台)"))
        if not ok:
            print("      " + hint.replace("\n", "\n      "))
            problems.append("目标群 {} 标识形态与所有平台均不匹配".format(gid))

    print("\n[4] 告警目标校验")
    mc = cfg.get("monitor_config", {}) or {}
    for key, label in (("alert_admin_group", "告警群号"),
                       ("alert_qq", "告警QQ/openid")):
        val = mc.get(key, "")
        if not val:
            print("  · {}: (未设置)".format(label))
            continue
        ok, hint = pc.validate_target_id_multi(caps_list, val, label=label)
        print("  · {}={} -> {}".format(label, val, "OK" if ok else "不匹配(所有平台)"))
        if not ok:
            print("      " + hint.split("\n")[0])
            problems.append("{} 标识形态与平台不匹配".format(label))

    if any(c.is_qq_official for c in caps_list):
        print("\n[5] QQ 官方被动窗口（按「AstrBot 刚重启」的最坏情况估算）")
        print("  官方群消息需要一条 5 分钟内的被动 msg_id，否则适配器静默丢弃。")
        print("  运行时的真实状态请用 /下线通知 状态 查看。")
        for c in [x for x in caps_list if x.is_qq_official]:
            for gid in targets:
                info = pc.inspect_qq_official_session(c, gid)
                print("  · [{}] {}: has_msg_id={}".format(c.platform_id, gid, info["has_msg_id"]))
        print("  建议: 把 alert_qq 配成管理员的 user_openid —— "
              "C2C 私聊主动推送不受 msg_id 限制，是官方协议下最可靠的告警通道。")

    print("\n" + "=" * 66)
    if problems:
        print("发现 {} 个会导致通知发不出去的问题:".format(len(problems)))
        for p in problems:
            print("  - " + p)
        return 1
    print("未发现阻塞性配置问题 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
