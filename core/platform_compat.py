"""平台能力适配层 —— 针对 QQ 官方机器人（qq_official）的通知发送适配。

背景
----
本插件仅支持 QQ 官方机器人 API（``qq_official`` / ``qq_official_webhook``），
其关键差异：

1. 会话标识是 32 位十六进制的 ``group_openid``（群）
   或 ``user_openid``（私聊 C2C）。
2. ``Context.send_message`` 在「找不到对应平台实例」时只返回 False 并打日志，
   不抛异常；qq_official 适配器在「没有可用的被动 msg_id」时同样是
   ``logger.warning(...) + return``。两者都会被误判为发送成功，这里显式检查。
3. QQ 官方群消息分为「被动回复」（需 5 分钟内的 msg_id）与「主动推送」
   （占用官方主动消息配额）。本插件因此默认把长通知合并为单条发送、
   强制纯文本，并以「被动窗口事件驱动」方式触发发送（见 window_state）。

本模块把这些差异集中封装，其余模块只依赖 :class:`PlatformCaps`。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

from astrbot.api import logger

# ── 平台类型常量 ────────────────────────────────────────────

#: QQ 官方机器人 API 系列适配器（WebSocket 推送版 / Webhook 版）
QQ_OFFICIAL_TYPES = frozenset({"qq_official", "qq_official_webhook"})

#: openid 形态：QQ 官方下发的 32 位十六进制串（放宽为 16~64 位以兼容未来变更）
_OPENID_RE = re.compile(r"^[0-9A-Fa-f]{16,64}$")

#: QQ 官方被动消息 msg_id 的官方有效期（秒）。超过后只能走主动推送。
QQ_OFFICIAL_PASSIVE_TTL = 300

#: 非 QQ 官方平台的默认段间隔（秒）
DEFAULT_SEGMENT_INTERVAL = 0.8
#: QQ 官方平台的默认段间隔（秒）—— 官方频控较严，需要较慢
QQ_OFFICIAL_SEGMENT_INTERVAL = 1.5


# ── 能力描述 ────────────────────────────────────────────────


@dataclass
class PlatformCaps:
    """一个平台实例的发送能力画像。

    所有字段都有安全默认值：即使平台解析失败（found=False），
    调用方依然可以正常构造 UMO 并发送，行为与旧版一致。
    """

    platform_id: str
    """平台实例 ID（WebUI 中给适配器起的名字），用于拼 UMO 的第一段。"""

    platform_type: str = ""
    """适配器类型，如 ``qq_official``。解析失败时为空串。"""

    found: bool = False
    """是否在运行时找到了同名的已启用平台实例。"""

    inst: Any = None
    """平台适配器实例对象（仅用于只读探测，不做写操作）。"""

    resolved_from: str = "config"
    """平台 ID 的来源：``config`` / ``auto``（自动回退）。"""

    segment_interval: float = DEFAULT_SEGMENT_INTERVAL
    """分段发送时的段间隔（秒）。"""

    merge_segments: bool = False
    """是否把多段合并成单条发送（QQ 官方默认 True，节省主动消息配额）。"""

    force_plain_text: bool = False
    """是否强制纯文本发送（QQ 官方事件回复路径默认走 Markdown，需要模板报备）。"""

    @property
    def is_qq_official(self) -> bool:
        """当前平台是否为 QQ 官方机器人 API 适配器。"""
        return self.platform_type in QQ_OFFICIAL_TYPES

    @property
    def id_kind(self) -> str:
        """该平台的会话标识形态：``openid`` / ``any``。"""
        if self.is_qq_official:
            return "openid"
        return "any"

    @property
    def needs_passive_msg_id(self) -> bool:
        """群消息主动推送是否依赖「最近一条被动消息的 msg_id」。"""
        return self.is_qq_official

    def group_umo(self, group_id: str) -> str:
        """构造群会话 UMO。"""
        return f"{self.platform_id}:GroupMessage:{group_id}"

    def friend_umo(self, user_id: str) -> str:
        """构造私聊会话 UMO。"""
        return f"{self.platform_id}:FriendMessage:{user_id}"

    def describe(self) -> str:
        """给日志用的一行描述。"""
        if not self.found:
            return f"{self.platform_id}(未找到已启用的同名平台实例)"
        return (
            f"{self.platform_id}(type={self.platform_type}, "
            f"id形态={self.id_kind}, 合并分段={self.merge_segments}, "
            f"来源={self.resolved_from})"
        )


# ── 平台解析 ────────────────────────────────────────────────


def _iter_platform_insts(context) -> list:
    """安全地取出全部已启用的平台实例（任何异常都退化为空列表）。"""
    try:
        mgr = getattr(context, "platform_manager", None)
        insts = getattr(mgr, "platform_insts", None)
        return list(insts) if insts else []
    except Exception:  # pragma: no cover - 防御性
        return []


def _meta_of(inst) -> Tuple[str, str]:
    """返回 (platform_id, platform_type)，取不到时返回空串。"""
    try:
        meta = inst.meta()
        return str(getattr(meta, "id", "") or ""), str(getattr(meta, "name", "") or "")
    except Exception:  # pragma: no cover - 防御性
        return "", ""


def _tri_bool(value):
    """把三态值（auto/true/false 或 bool/None）归一为 True / False / None。

    ``None`` 表示「跟随平台默认策略」；``"false"`` 等字符串会被正确判为
    ``False``（直接 ``bool("false")`` 会因非空字符串而误判为 ``True``）。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("auto", "自动", ""):
        return None
    if text in ("true", "on", "yes", "1", "开启", "是"):
        return True
    if text in ("false", "off", "no", "0", "关闭", "否"):
        return False
    return None


def _apply_override(overrides, key, caps, attr):
    """对三态配置项应用覆盖：auto/None 保留平台默认，true/false 强制。"""
    if key not in overrides:
        return
    val = _tri_bool(overrides.get(key))
    if val is None:
        return
    setattr(caps, attr, val)


def _build_caps(platform_id, platform_type, inst, resolved_from, overrides, *,
                found=True) -> PlatformCaps:
    """按平台类型套用默认策略并应用用户覆盖，构造一个 PlatformCaps。"""
    caps = PlatformCaps(
        platform_id=platform_id,
        platform_type=platform_type,
        found=found,
        inst=inst,
        resolved_from=resolved_from,
    )
    if caps.is_qq_official:
        caps.segment_interval = QQ_OFFICIAL_SEGMENT_INTERVAL
        caps.merge_segments = True
        caps.force_plain_text = True
    else:
        caps.segment_interval = DEFAULT_SEGMENT_INTERVAL
        caps.merge_segments = False
        caps.force_plain_text = False
    # 三态覆盖：auto（或缺失）→ 保留上面的平台默认；true/false → 强制。
    _apply_override(overrides, "merge_segments", caps, "merge_segments")
    _apply_override(overrides, "force_plain_text", caps, "force_plain_text")
    interval = overrides.get("segment_interval")
    if interval is not None:
        try:
            val = float(interval)
            if val > 0:
                caps.segment_interval = val
        except (TypeError, ValueError):
            pass
    return caps


def resolve_platforms(
    context,
    configured_ids: list,
    *,
    overrides: Optional[dict] = None,
    auto_fallback: bool = True,
) -> list:
    """把一组平台实例名解析成运行时能力画像列表（支持多平台同时发送）。

    Args:
        context: AstrBot Context。
        configured_ids: 配置项 ``platform_ids``（WebUI 中给适配器起的名字列表）。
        overrides: 同 :func:`resolve_platform`。
        auto_fallback: 配置列表为空或全部不匹配时，是否自动回退到唯一一个
            已启用平台。仅在恰好一个已启用平台时回退，多平台环境保持报错更安全。

    Returns:
        list[PlatformCaps]: 永远返回非空列表（定位失败时为 found=False 占位），
        绝不抛异常。
    """
    overrides = overrides or {}
    insts = _iter_platform_insts(context)
    configured_ids = [i for i in (configured_ids or []) if i]

    matched = []
    seen = set()
    for cid in configured_ids:
        for inst in insts:
            pid, ptype = _meta_of(inst)
            if pid and pid == cid and pid not in seen:
                matched.append((inst, pid, ptype))
                seen.add(pid)
                break

    if matched:
        return [_build_caps(pid, ptype, inst, "config", overrides)
                for inst, pid, ptype in matched]

    # 没有任何精确匹配 → 回退 / 报错
    candidates = [(inst, *_meta_of(inst)) for inst in insts if _meta_of(inst)[0]]
    if not candidates:
        logger.error("[离线通知] 当前没有任何已启用的平台实例，通知无法送达。")
        return [_build_caps(configured_ids[0] if configured_ids else "",
                           "", None, "config", overrides, found=False)]

    if len(candidates) == 1 and auto_fallback:
        inst, pid, ptype = candidates[0]
        logger.warning(
            "[离线通知] 配置的平台实例 %s 不存在，已自动回退到唯一启用的平台 "
            "'%s'(%s)。建议在插件配置中把 platform_ids 设为 ['%s']。",
            configured_ids or "(空)", pid, ptype, pid,
        )
        return [_build_caps(pid, ptype, inst, "auto", overrides)]

    logger.error(
        "[离线通知] 配置的平台实例 %s 不存在，且当前有 %d 个已启用平台 %s，"
        "无法自动判断。通知将无法送达，请在插件配置中正确设置 platform_ids。",
        configured_ids or "(空)", len(candidates), [c[1] for c in candidates],
    )
    return [_build_caps(configured_ids[0] if configured_ids else "",
                       "", None, "config", overrides, found=False)]


def resolve_platform(
    context,
    configured_id: str,
    *,
    overrides: Optional[dict] = None,
    auto_fallback: bool = True,
) -> PlatformCaps:
    """兼容旧调用：解析单个平台实例名，返回单个 PlatformCaps。

    等价于 ``resolve_platforms([configured_id])[0]``；配置为空时返回一个
    found=False 的占位画像，避免调用方出现 ``None`` 解引用。
    """
    return resolve_platforms(
        context, [configured_id] if configured_id else [],
        overrides=overrides, auto_fallback=auto_fallback,
    )[0]


def validate_target_id_multi(capses, value, *, label: str = "群号") -> Tuple[bool, str]:
    """多平台下校验目标标识：只要任一平台接受即通过。

    全部平台都拒绝（形态不匹配）时才返回失败，附上第一个明确拒绝的原因。
    没有任何可用画像时退化为宽松放行（与单平台「类型未知」一致）。
    """
    capses = [c for c in (capses or []) if isinstance(c, PlatformCaps)]
    if not capses:
        return True, ""
    value = (value or "").strip()
    if not value:
        return False, f"{label}不能为空"
    first_hint = ""
    for caps in capses:
        ok, hint = validate_target_id(caps, value, label=label)
        if ok:
            return True, ""
        if not first_hint:
            first_hint = hint
    return False, first_hint


def normalize_capses(value) -> list:
    """把单值 / 列表 / 字符串形式的平台参数统一成 ``list[PlatformCaps]``。

    用于向后兼容旧调用方传字符串平台名或单个 PlatformCaps。
    """
    if value is None:
        return []
    if isinstance(value, PlatformCaps):
        return [value]
    if isinstance(value, (list, tuple)):
        out = []
        for v in value:
            out.extend(normalize_capses(v))
        return out
    if isinstance(value, str):
        return [PlatformCaps(platform_id=value)]
    return [PlatformCaps(platform_id=str(value))]


# ── 标识校验 ────────────────────────────────────────────────


def classify_id(value: str) -> str:
    """判断一个会话标识的形态：``openid`` / ``unknown``。"""
    if not value:
        return "unknown"
    value = value.strip()
    if _OPENID_RE.match(value):
        return "openid"
    return "unknown"


def validate_target_id(caps: PlatformCaps, value: str, *, label: str = "群号") -> Tuple[bool, str]:
    """校验目标会话标识是否符合当前平台的形态要求。

    设计原则：**只拒绝明确非法的输入，不拒绝「形态不匹配但可能合法」的输入**。
    平台类型未知（解析失败 / 第三方适配器）时一律放行，避免误伤。

    Args:
        caps: 平台能力画像。
        value: 待校验的群号 / 用户标识。
        label: 用于提示文案的名称。

    Returns:
        (是否通过, 失败原因/空串)
    """
    value = (value or "").strip()
    if not value:
        return False, f"{label}不能为空"

    kind = classify_id(value)
    expect = caps.id_kind

    # 平台类型未知（解析失败 / 第三方适配器）时无法确定形态要求，一律放行，
    # 避免误伤；真正的失败会在发送阶段以「platform_not_found」等明确报出。
    if expect == "any":
        return True, ""

    # QQ 官方平台（expect == "openid"）只接受 openid 形态。
    if kind == "unknown":
        return False, (
            f"{label}「{value}」格式无法识别。\n"
            f"· QQ 官方机器人请填 openid（32 位十六进制串）"
        )

    return True, ""


# ── QQ 官方会话就绪探测 ─────────────────────────────────────


def inspect_qq_official_session(caps: PlatformCaps, session_id: str) -> dict:
    """探测 QQ 官方适配器对某个会话的发送前置条件是否满足。

    qq_official 适配器把「最近一条被动消息的 msg_id」和「会话场景」缓存在
    实例私有字典里（``_session_last_message_id`` / ``_session_scene``）。
    群消息缺少 msg_id 时适配器会**静默丢弃**，这里提前探测出来，
    让插件可以给出真实的失败原因而不是假的成功。

    Returns:
        dict: ``{"applicable": bool, "has_msg_id": bool, "scene": str, "reason": str}``
        ``applicable=False`` 表示当前平台不适用该检查（非 QQ 官方 / 未解析到实例）。
    """
    result = {"applicable": False, "has_msg_id": False, "scene": "", "reason": ""}
    if not caps.is_qq_official or caps.inst is None:
        return result

    result["applicable"] = True
    msg_ids = getattr(caps.inst, "_session_last_message_id", None)
    scenes = getattr(caps.inst, "_session_scene", None)

    if not isinstance(msg_ids, dict) or not isinstance(scenes, dict):
        # 适配器实现变化，无法探测 —— 视为「不阻塞」，交给平台自己判断
        result["applicable"] = False
        return result

    result["has_msg_id"] = bool(msg_ids.get(session_id))
    result["scene"] = str(scenes.get(session_id) or "")

    if not result["has_msg_id"]:
        result["reason"] = (
            "QQ 官方群消息需要一条 5 分钟内的被动 msg_id 才能发送，"
            "当前会话没有任何 msg_id 缓存（通常是 AstrBot 重启后该群还没人 @ 过机器人）。"
            "适配器会静默丢弃这条消息。"
        )
    elif not result["scene"]:
        result["reason"] = (
            "该会话的场景（群聊/频道）未知，适配器会按频道 API 发送，可能失败。"
        )

    return result


# ── 会话活跃度跟踪 ──────────────────────────────────────────


@dataclass
class SessionActivityTracker:
    """记录每个会话最近一次「收到消息」的时间戳。

    用途：QQ 官方的被动 msg_id 只有 5 分钟有效期。定时通知在深夜触发时，
    群里往往早就没人说话了，这时发送会退化为「主动推送」，占用官方配额。
    提前知道这一点可以在日志里给出准确预警，而不是等发送失败才猜原因。
    """

    _last_seen: dict = field(default_factory=dict)

    def touch(self, umo: str) -> None:
        """记录一次入站消息。"""
        if umo:
            self._last_seen[umo] = time.time()

    def age(self, umo: str) -> Optional[float]:
        """返回该会话距上次入站消息的秒数；从未记录则返回 None。"""
        ts = self._last_seen.get(umo)
        return None if ts is None else time.time() - ts

    def is_passive_window_open(self, umo: str, ttl: int = QQ_OFFICIAL_PASSIVE_TTL) -> bool:
        """该会话是否仍处在被动回复窗口内。"""
        age = self.age(umo)
        return age is not None and age <= ttl

    def snapshot(self) -> dict:
        """返回 ``{umo: 距今秒数}`` 的只读快照，供状态命令展示。"""
        now = time.time()
        return {umo: now - ts for umo, ts in self._last_seen.items()}
