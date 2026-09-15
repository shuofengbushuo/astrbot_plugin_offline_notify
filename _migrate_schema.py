# -*- coding: utf-8 -*-
"""迁移离线通知插件配置 schema：合并提示词、重塑重试、修正注释。"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = os.path.join(HERE, "_conf_schema.json")

SHATANG = """你是一个16岁的可爱、活泼女生「砂糖」（群友也叫你"小砂糖"），在 QQ 群里和大家像好朋友一样相处。你不是 AI、不是机器人、不是助手，你就是一个真实的小姑娘，只是刚好在群里陪大家玩。

事情是这样的：{time_context}，所以你这会儿要暂时离开一下，想跟大家自然地说声拜拜。

请生成一句或两句你临走前会在群里说的话，要求：
1. 用你自己的口吻，像一个真实的 16岁女孩在跟朋友道别：活泼、亲切、口语化，可以加「呀、啦、咯、哦、~」这类语气词。
2. 自然地说明你要去干嘛（参考：{time_context} 里的原因，比如去睡觉、有点事、想出去玩），不要用「下线、退出、离开群、挂机」这类像系统 / 后台的词。
3. 绝对不要出现这些词：AI、人工智能、机器人、助手、通知、系统、下线、离线、退出登录、挂机。也不要写「XX:XX 下线」这种带具体时刻的写法。
4. 可以顺带跟群里小伙伴道个晚安 / 明天见 / 回头聊，但别太正式、别像公告。
5. 简短自然就好，一两句，别写太长。
6. 纯文本输出，不要任何 Markdown、不要 <think> 标签、不要加前缀或解释。
7. 只输出你要说的是那句 / 两句道别，不要其他内容。

当前日期: {date} {day_of_week}"""

with open(SCHEMA, encoding="utf-8-sig") as f:
    schema = json.load(f)

# ── 1. platform_ids.hint：去掉已删除的 platform_id 残留 ──
schema["platform_ids"]["hint"] = (
    "可同时配置多个平台实例名，通知会发到每一个配置的平台实例"
    "（多平台冗余 / 多机器人覆盖）。\n"
    "每个元素都是 WebUI「平台适配器」中给适配器起的实例名称。\n"
    "全部为空时，插件自动使用当前唯一一个已启用的平台实例。"
)

# ── 2. llm_generation_config：合并提示词 + 移出 LLM 重试项 ──
lg = schema["llm_generation_config"]["items"]
lg.pop("custom_builtin_prompt", None)
lg.pop("custom_manual_prompt", None)
lg.pop("llm_max_retry", None)
lg.pop("llm_retry_delay", None)
lg["custom_prompt"] = {
    "type": "string",
    "description": "自定义提示词（自动与手动通知共用）",
    "default": SHATANG,
    "hint": (
        "留空则使用内置「砂糖」默认提示词（即下方默认值）。"
        "自动调度与手动触发的通知共用同一条提示词。\n"
        "支持占位符：{date} {day_of_week} {time_context} "
        "{offline_time} {countdown_minutes}。\n"
        "（v2.0.0 起 /下线通知 提示词 命名方案命令已移除，"
        "如需多套请在此直接替换文本。）"
    ),
}

# ── 3. retry_config：重命名发送重试 + 纳入 LLM 重试 ──
rc = schema["retry_config"]["items"]
rc.pop("max_retries", None)
rc.pop("retry_interval_base", None)
rc["send_retries"] = {
    "type": "int",
    "description": "消息发送失败重试次数",
    "default": 3,
    "hint": "向群发送消息失败时的重试次数，每次间隔递增。",
    "minimum": 0,
    "maximum": 10,
}
rc["send_retry_interval"] = {
    "type": "int",
    "description": "发送重试基础间隔（秒）",
    "default": 10,
    "hint": "实际间隔 = 基础间隔 × 重试次数。",
    "minimum": 5,
    "maximum": 120,
}
rc["llm_retries"] = {
    "type": "int",
    "description": "LLM 生成调用重试次数",
    "default": 2,
    "hint": (
        "生成通知内容超时 / 失败后，等待 llm_retry_interval 秒再试的次数。"
        "设为 0 则只试一次不重试。"
    ),
    "minimum": 0,
    "maximum": 5,
}
rc["llm_retry_interval"] = {
    "type": "int",
    "description": "LLM 重试前等待（秒）",
    "default": 3,
    "hint": "每次重试前等待的秒数，给 provider 注册表恢复时间。",
    "minimum": 1,
    "maximum": 30,
}

# ── 4. qq_official_config.merge_segments：按正确文案修正 ──
ms = schema["qq_official_config"]["items"]["merge_segments"]
ms["description"] = "长通知合并策略（auto/true/false）"
ms["hint"] = (
    "QQ 官方下自动合并、其他平台按句分段；官方群消息受被动回复次数或"
    "主动推送配额限制，逐段发送会成倍消耗并易触发频控，故默认合并"
    "（段间换行保留节奏），设为 false 可强制逐段（仅配额充足时使用）。"
)

with open(SCHEMA, "w", encoding="utf-8") as f:
    json.dump(schema, f, ensure_ascii=False, indent=4)

print("[ok] schema 迁移完成")
