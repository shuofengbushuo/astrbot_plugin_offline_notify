"""
splitter_compat.py — 让离线通知的 LLM/模板内容能被 astrobot_plugin_splitter 正确分段

splitter 插件的分段规范（来自 astrbot_plugin_splitter/main.py）：
- 简单/进阶模式按 split_chars（默认 。？！ ? ! ； ; \\n）断句，
  分隔符会被「附加到前一段末尾」。
- 智能分段会整体保护以下结构，使其不被切断：
  * Markdown 代码块 ```...```
  * 思维链标签 <think>...</think>
  * 以 | 开头的 Markdown 表格
- 连续相同的句末标点（如 。。。）会切出空段或纯标点碎片段；
  缺少句末标点会导致最后一段不是完整句子。

本模块对通知正文做「无信息损失」的规范化，使其：
1) 去除结构性包裹（代码块、思维链）——这些不是正文，且会被 splitter 整体保护导致无法分段；
2) 折叠连续相同的句末标点，避免空段/纯标点段；
3) 折叠 2 个以上换行，避免空行被切成空段；
4) 去除开头/结尾多余空白与开头多余的句末标点；
5) 保证以句末标点结尾，使最后一段是完整句子。

该函数对真实正文内容（称呼、时间、祝福语等）不产生任何删改，仅做格式层面的规整。
"""

import re

# 与 splitter 默认 split_chars 对齐的句末标点集合
_SENT_DELIMS = "。！？!?；;"

# 折叠「连续相同」的句末标点，避免产生空段或纯标点碎片段
# 仅折叠完全相同的重复（如 。。。→。、！！→！），保留不同标点的组合（如 ？！）
_RUN_DELIM = re.compile(r"([。！？!?；;])\1+")

# 折叠 2 个以上连续换行为单个换行，避免空行被切成空段
_MULTI_NEWLINE = re.compile(r"\n{2,}")

# 去除开头多余的句末标点（模型偶发的前导标点属于噪声）
_LEADING_DELIM = re.compile(r"^[。！？!?；;\s]+")

# Markdown 代码块：只去除 ``` 标记本身（含可选的语言标识行），
# 保留块内正文，避免把整段通知内容误删。
_FENCE = re.compile(r"```[^\n]*")

# 思维链标签 <think>...</think> / <thinking>...</thinking>
_THINK_RE = re.compile(r"<think\s*>[\s\S]*?</think\s*>", flags=re.IGNORECASE)
_THINK_RE2 = re.compile(r"<thinking\s*>[\s\S]*?</thinking\s*>", flags=re.IGNORECASE)


def _strip_structural(text: str) -> str:
    """去除结构性包裹：Markdown 代码块标记与思维链标签（非正文内容）。"""
    text = _FENCE.sub("", text)
    text = _THINK_RE.sub("", text)
    text = _THINK_RE2.sub("", text)
    return text


def normalize_for_splitter(text: str) -> str:
    """将通知正文规范化为 splitter 友好的格式（无信息损失）。

    Args:
        text: 原始通知文本（LLM 生成或模板渲染结果）

    Returns:
        str: 规范化后的文本，可被 splitter 干净地按句分段
    """
    if not text:
        return text

    # 1. 统一换行符
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 2. 去除结构性包裹（代码块 / 思维链）——这些不是正文，且会被整体保护导致无法分段
    text = _strip_structural(text)

    # 3. 折叠连续相同的句末标点（如 。。。→。）
    text = _RUN_DELIM.sub(lambda m: m.group(1), text)

    # 4. 折叠 2+ 连续换行，避免空行被切成空段
    text = _MULTI_NEWLINE.sub("\n", text)

    # 5. 去除开头/结尾空白，以及开头多余的句末标点
    text = text.strip()
    text = _LEADING_DELIM.sub("", text).strip()

    # 6. 保证以句末标点结尾，使最后一段是完整句子
    if text and text[-1] not in _SENT_DELIMS:
        text += "。"

    return text


def is_splitter_friendly(text: str) -> bool:
    """快速判断文本是否已经是 splitter 友好格式（用于调试/日志）。"""
    if not text:
        return True
    if "```" in text or "<think" in text.lower():
        return False
    if re.search(r"([。！？!?；;])\1{2,}", text):
        return False
    if text[-1] not in _SENT_DELIMS:
        return False
    return True
