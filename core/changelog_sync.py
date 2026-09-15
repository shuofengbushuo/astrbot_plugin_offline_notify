"""README「更新日志」章节 → CHANGELOG.md 的幂等单向同步。

AstrBot WebUI 的插件详情页按固定文件名（CHANGELOG.md / changelog.md /
CHANGELOG / changelog，见 dashboard/services/plugin_service.py:1977）读取更新日志，
找不到就打 WARN。本模块把 README 里已存在的「更新日志」章节提取为 CHANGELOG.md。

三条设计原则：
1. 幂等 —— 内容一致时不写盘，避免每次启动刷新 mtime；
2. 静默降级 —— 任何异常都只记日志，绝不打断插件加载；
3. 防覆盖 —— CHANGELOG.md 与上次生成结果不一致时认定为人工改动，不再覆盖。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

SECTION_MARKER = "## 更新日志"
META_NAME = "changelog_sync.json"
HEADER = (
    "> 本文件由 README.md 的「更新日志」章节同步生成，"
    "供 AstrBot WebUI 插件详情页展示。修改记录时请与 README.md 保持一致。\n\n"
)

# 返回状态
CREATED = "created"
UPDATED = "updated"
UNCHANGED = "unchanged"
NO_SECTION = "no_section"
MANUAL_EDIT = "manual_edit"
ERROR = "error"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_changelog(readme_text: str) -> str | None:
    """从 README 全文构造 CHANGELOG 文本；无更新日志章节时返回 None。"""
    if SECTION_MARKER not in readme_text:
        return None
    body = readme_text[readme_text.index(SECTION_MARKER):].rstrip() + "\n"
    return HEADER + body.replace(SECTION_MARKER, "# 更新日志", 1)


def _load_meta(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_meta(path: Path, readme_text: str, out_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"readme": _sha(readme_text), "out": _sha(out_text)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def sync_changelog_from_readme(plugin_dir: Path, data_dir: Path, log=None) -> str:
    """单向同步 README 的更新日志章节到 CHANGELOG.md，返回状态常量。"""

    def _log(level: str, msg: str) -> None:
        if log is not None:
            getattr(log, level, log.info)(msg)

    try:
        plugin_dir = Path(plugin_dir)
        data_dir = Path(data_dir)
        readme_path = plugin_dir / "README.md"
        out_path = plugin_dir / "CHANGELOG.md"
        meta_path = data_dir / META_NAME

        if not readme_path.is_file():
            return NO_SECTION

        readme_text = readme_path.read_text(encoding="utf-8")
        target = build_changelog(readme_text)
        if target is None:
            _log("debug", "[离线通知] README 无「更新日志」章节，跳过 CHANGELOG 同步")
            return NO_SECTION

        meta = _load_meta(meta_path)
        current = out_path.read_text(encoding="utf-8") if out_path.is_file() else None

        # 内容一致：不写盘，仅在缺少同步记录时补一份（同步记录缺失会导致后续误判为人工改动）
        if current == target:
            if meta.get("out") != _sha(target):
                _save_meta(meta_path, readme_text, target)
            return UNCHANGED

        # 内容不一致：判断 CHANGELOG 是否被人工改过
        if current is not None:
            if not meta.get("out"):
                _log(
                    "warning",
                    "[离线通知] CHANGELOG.md 与 README 不一致且无同步记录，"
                    "为安全起见跳过自动覆盖（需恢复自动同步请删除插件数据目录下的 "
                    f"{META_NAME}）",
                )
                return MANUAL_EDIT
            if meta["out"] != _sha(current):
                _log(
                    "warning",
                    "[离线通知] 检测到 CHANGELOG.md 被人工修改，已跳过自动同步"
                    "（需恢复自动同步请删除插件数据目录下的 "
                    f"{META_NAME}）",
                )
                return MANUAL_EDIT

        _save_meta(meta_path, readme_text, target)
        out_path.write_text(target, encoding="utf-8", newline="\n")
        action = "新增" if current is None else "更新"
        _log("info", f"[离线通知] 已从 README {action} CHANGELOG.md（{len(target)} 字符）")
        return CREATED if current is None else UPDATED
    except Exception as exc:  # 生产路径：绝不因同步失败影响插件加载
        _log("warning", f"[离线通知] CHANGELOG 同步失败（不影响插件功能）：{exc}")
        return ERROR
