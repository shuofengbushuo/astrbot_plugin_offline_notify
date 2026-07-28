"""
LLM 通知内容生成器 - 调用对话模型生成多样化、自然的下线通知

核心功能:
- 内置提示词引导模型扮演 16岁真人女孩「砂糖」自然道别
- 包含必要信息，但不暴露 AI / 系统身份，也不写「XX:XX 下线」字眼
- 支持超时控制和模板回退
- 记录每次生成的统计信息
- 提示词「明置」：支持配置自定义 + 命名方案库覆盖（见 get_effective_prompts）

调用方式:
  generator = LLMGenerator(context, config)
  message = await generator.generate(offline_time="23:00", countdown_minutes=5)

实现要点（v 人设重构 · 思考模式提速 · 提示词明置）:
  1) 人设：提示词让模型扮演一个 16岁的活泼可爱真人女孩「砂糖」
     （群友也叫她"小砂糖"），自然和大家道别；不出现「下线 / AI /
     机器人 / 通知 / 系统」等暴露身份或像后台公告的字眼，也不写
     「XX:XX 下线」这种带具体时刻的写法。白天说有事/想玩，晚上说
     去睡觉。
  2) 提速真因：deepseek-v4 系列默认开启「思考/推理模式」，模型会
     先花十几秒做 reasoning 才吐字。普通聊天是流式逐步显示，用户
     感知不到这十几秒；而本插件要等【完整】响应才发送，于是整体
     被拖到 20s+。修复：仅对本次调用临时关闭思考模式
     （thinking={type:disabled}，等同旧 deepseek-chat 的非思考路径），
     调用后还原，不影响普通聊天。
  3) 调用方式：与普通聊天同源的流式 text_chat_stream，逐块收集
     completion_text；保留整体超时兜底（默认 90s）、重试与模板回退。
  4) 提示词明置（v1.5.0）：提示词不再只硬编码——管理员可在
     _conf_schema.json 的 custom_builtin_prompt / custom_manual_prompt
     中填写自定义提示词（明置编辑），也可用 PromptStore 保存多套命名
     方案、互不干扰地切换。三级优先级：激活方案 > 配置自定义 > 内置默认。
"""

import asyncio
import time
from datetime import datetime
from typing import Optional
from astrbot.api import logger

from .splitter_compat import normalize_for_splitter

# 用于「临时关闭思考模式」的补丁哨兵：无法补丁时返回它，还原时跳过。
_NO_PATCH = object()


# 内置默认提示词 - 让模型扮演 16岁真人女孩「砂糖」，自然地和大家道别。
# 不出现「下线 / AI / 机器人 / 通知 / 系统」等暴露身份或像后台公告的字眼，
# 也不写「XX:XX 下线」这种带具体时刻的写法；昼夜情境由 {time_context} 注入。
# 这是 get_effective_prompts() 三级解析中的最低优先级（兜底默认）。
BUILTIN_PROMPT = """你是一个16岁的可爱、活泼女生「砂糖」（群友也叫你"小砂糖"），在 QQ 群里和大家像好朋友一样相处。你不是 AI、不是机器人、不是助手，你就是一个真实的小姑娘，只是刚好在群里陪大家玩。

事情是这样的：{time_context}，所以你这会儿要暂时离开一下，想跟大家自然地说声拜拜。

请生成一句或两句你临走前会在群里说的话，要求：
1. 用你自己的口吻，像一个真实的 16岁女孩在跟朋友道别：活泼、亲切、口语化，可以加「呀、啦、咯、哦、~」这类语气词。
2. 自然地说明你要去干嘛（参考：{time_context} 里的原因，比如去睡觉、有点事、想出去玩），不要用「下线、退出、离开群、挂机」这类像系统 / 后台的词。
3. 绝对不要出现这些词：AI、人工智能、机器人、助手、通知、系统、下线、离线、退出登录、挂机。也不要写「XX:XX 下线」这种带具体时刻的写法。
4. 可以顺带跟群里小伙伴道个晚安 / 明天见 / 回头聊，但别太正式、别像公告。
5. 简短自然就好，一两句，别写太长。
6. 纯文本输出，不要任何 Markdown、不要 <think> 标签、不要加前缀或解释。
7. 只输出你要说的那句 / 两句道别，不要其他内容。

当前日期: {date} {day_of_week}"""

# 手动生成场景专用提示词 — 管理员手动触发，非自动调度
MANUAL_PROMPT = """你是一个16岁的可爱、活泼女生「砂糖」（群友也叫你"小砂糖"），在 QQ 群里和大家像好朋友一样相处。你不是 AI、不是机器人、不是助手，你就是一个真实的小姑娘，只是刚好在群里陪大家玩。

这会儿你想暂时离开一下，跟大家自然地说声拜拜（就当是临时想跟小伙伴说声，不用提倒计时或几点）。

请生成一句或两句你临走前会在群里说的话，要求：
1. 用你自己的口吻，像一个真实的 16岁女孩在跟朋友道别：活泼、亲切、口语化，可以加「呀、啦、咯、哦、~」这类语气词。
2. 自然地说明你要去干嘛（比如去睡觉、有点事、想出去玩），不要用「下线、退出、离开群、挂机」这类像系统 / 后台的词。
3. 绝对不要出现这些词：AI、人工智能、机器人、助手、通知、系统、下线、离线、退出登录、挂机。也不要写「XX:XX 下线」这种带具体时刻的写法。
4. 可以顺带道个晚安 / 明天见 / 回头聊，但别太正式、别像公告。
5. 简短自然就好，一两句，别写太长。
6. 纯文本输出，不要任何 Markdown、不要 <think> 标签、不要加前缀或解释。
7. 只输出你要说的那句 / 两句道别，不要其他内容。

当前日期: {date} {day_of_week}"""


class LLMGenerator:
    """基于 LLM 的下线通知内容生成器"""

    # 星期映射
    WEEKDAY_NAMES = {
        0: "周一", 1: "周二", 2: "周三", 3: "周四",
        4: "周五", 5: "周六", 6: "周日"
    }

    def _time_context(self, now: datetime) -> str:
        """根据当前小时给出昼/夜情境，供提示词让模型挑选合适的道别理由。"""
        h = now.hour
        if 21 <= h or h < 6:
            return "现在是深夜，你困得不行，正准备去睡觉"
        if 18 <= h < 21:
            return "现在是傍晚，你准备休息 / 去睡觉"
        if 12 <= h < 18:
            return "现在是下午，你有点事要忙，或者想溜出去玩"
        return "现在是上午 / 中午，你有点事要忙"

    def __init__(self, context, config: dict, prompt_store=None):
        """初始化 LLM 生成器

        Args:
            context: AstrBot Context 对象
            config: llm_generation_config 配置节
            prompt_store: 可选的 PromptStore 命名方案库（用于多方案覆盖）
        """
        self.context = context
        self.config = config
        self.enabled = config.get("enable_llm_generation", True)
        self.provider_id = config.get("llm_provider_id", "")
        # 整体超时：放宽到 40~240s。
        # 这是此前「全部超时」的真凶——旧阈值 8~12s 过早取消了一次
        # 较慢的 DeepSeek 响应（首字/完整响应常需 10~20s）。
        # 普通聊天没这层紧 timeout 包裹，所以它能正常拿到结果。
        # 这里给足余量，避免再次误掐框架本可完成的调用。
        raw = int(config.get("llm_timeout", 90) or 90)
        self.timeout = min(max(raw, 40), 240)
        # 自动重试：单次失败后，等待 retry_delay 秒再整体重试。
        self.max_retry = int(config.get("llm_max_retry", 2) or 0)
        self.retry_delay = int(config.get("llm_retry_delay", 3) or 3)
        self.fallback_to_template = config.get("fallback_to_template", True)
        # 关闭 DeepSeek 思考模式（仅对本次插件调用生效，调用后还原）。
        # 这是把通知生成从 20s+ 降到几秒的关键：v4 系列默认开启
        # 思考，会先 reasoning 十几秒才吐字，而本插件要等完整响应。
        self.disable_thinking = bool(config.get("llm_disable_thinking", True))

        # 提示词「明置」（v1.5.0）：配置自定义优先于内置默认；
        # 命名方案（prompt_store）优先级最高。见 get_effective_prompts()。
        self.custom_builtin = (config.get("custom_builtin_prompt") or "").strip()
        self.custom_manual = (config.get("custom_manual_prompt") or "").strip()
        self.prompt_store = prompt_store

        # 统计信息
        self._stats = {
            "total_calls": 0,
            "success_calls": 0,
            "failed_calls": 0,
            "total_time_ms": 0,
            "last_call_time": None,
            "last_error": None,
            "last_success": False,
            "stage": "idle",
        }

    def get_effective_prompts(self):
        """返回当前生效的 (builtin, manual) 提示词模板。

        三级优先级（高 → 低）：
          1. 已激活的命名方案（PromptStore 中 set_active 的方案，整体覆盖）；
          2. 配置自定义（custom_builtin_prompt / custom_manual_prompt，明置编辑）；
          3. 内置默认（BUILTIN_PROMPT / MANUAL_PROMPT 常量，即「砂糖」人设）。

        这样管理员既能在配置面板里快速改提示词（明置），也能把多套
        提示词存成命名方案、互不干扰地切换，满足多用户/多场景需求。

        Returns:
            (builtin_template, manual_template)
        """
        builtin_tpl, manual_tpl = BUILTIN_PROMPT, MANUAL_PROMPT
        # 2) 配置自定义（明置）
        if self.custom_builtin:
            builtin_tpl = self.custom_builtin
        if self.custom_manual:
            manual_tpl = self.custom_manual
        # 1) 命名方案（最高优先级，整体覆盖）
        if self.prompt_store is not None:
            active = self.prompt_store.get_active()
            if active:
                prof = self.prompt_store.get_profile(active)
                if prof:
                    if prof.get("builtin_prompt"):
                        builtin_tpl = prof["builtin_prompt"]
                    if prof.get("manual_prompt"):
                        manual_tpl = prof["manual_prompt"]
        return builtin_tpl, manual_tpl

    async def generate(self, offline_time: str, countdown_minutes: int,
                       now: datetime = None, is_manual: bool = False) -> Optional[str]:
        """调用 LLM 生成下线通知内容

        走与普通聊天完全同源的流式路径 provider.text_chat_stream
        （同一 provider 实例、同一底层方法），因此沿用框架已验证
        可用的代理/信任环境/超时配置；并在本次调用临时关闭思考
        模式以提速（调用后还原，不影响普通聊天）。

        Args:
            offline_time: 下线时间 (HH:MM)
            countdown_minutes: 剩余分钟数
            now: 当前时间，默认 datetime.now()
            is_manual: 是否手动触发（True=手动场景，使用 manual 提示词）

        Returns:
            str | None: 生成的通知文本，失败返回 None
        """
        if not self.enabled:
            logger.info("[离线通知] LLM 生成已禁用")
            return None

        if not self.provider_id:
            logger.warning("[离线通知] 未配置 LLM 提供商，无法生成通知")
            return None

        if now is None:
            now = datetime.now()

        day_of_week = self.WEEKDAY_NAMES.get(now.weekday(), str(now.weekday()))
        date_str = now.strftime("%Y-%m-%d")

        # 根据场景选择提示词（支持配置/命名方案覆盖，见 get_effective_prompts）
        builtin_tpl, manual_tpl = self.get_effective_prompts()
        prompt_template = manual_tpl if is_manual else builtin_tpl
        time_context = self._time_context(now)
        prompt = prompt_template.format(
            date=date_str,
            day_of_week=day_of_week,
            offline_time=offline_time,
            countdown_minutes=countdown_minutes,
            time_context=time_context,
        )

        self._stats["total_calls"] += 1
        start_time = time.time()
        last_err = None
        self._stats["stage"] = "llm_stream"

        for attempt in range(1, self.max_retry + 1):
            # ── 主方法：流式 text_chat_stream（与 AStrBot 普通聊天
            #   agent runner 完全同源）：stream=True，连接建立即返回、
            #   逐 token 流式吐字，不因推理模型整段缓冲而卡死 ──
            logger.info(
                f"[离线通知][诊断] 调用框架 LLM（流式 text_chat_stream，"
                f"与普通聊天同源），provider={self.provider_id}，"
                f"整体超时={self.timeout}s"
            )
            try:
                # 取 provider 实例（与普通聊天同一 inst_map 实例）
                prov = await self.context.provider_manager.get_provider_by_id(
                    self.provider_id
                )
                if prov is None:
                    raise RuntimeError(f"Provider {self.provider_id} 不存在")

                # 提速关键：deepseek-v4 默认开启「思考/推理模式」，会先花
                # 十几秒做 reasoning 才吐字；普通聊天因流式逐步显示，
                # 用户感知不到这十几秒，而本插件要等【完整】响应才发送，
                # 于是整体被 reasoning 拖到 20s+。这里仅对本次调用临时
                # 关闭思考模式（等同旧 deepseek-chat 的非思考路径），
                # 调用后还原，不影响普通聊天。
                saved_think = _patch_thinking(prov, self.disable_thinking)
                try:
                    # 流式收集：连接即返回，不阻塞等完整响应
                    text = await asyncio.wait_for(
                        self._stream_collect(prov, prompt),
                        timeout=self.timeout,
                    )
                finally:
                    if saved_think is not _NO_PATCH:
                        _restore_thinking(prov, saved_think)

                text = (text or "").strip()
                if text:
                    text = normalize_for_splitter(text)
                    elapsed_ms = (time.time() - start_time) * 1000
                    self._stats["total_time_ms"] += elapsed_ms
                    self._stats["last_call_time"] = time.time()
                    self._stats["success_calls"] += 1
                    self._stats["last_success"] = True
                    self._stats["stage"] = "done"
                    logger.info(
                        f"[离线通知] LLM 生成成功（流式），耗时 {elapsed_ms:.0f}ms，"
                        f"内容长度: {len(text)} 字符"
                    )
                    return text
                # 返回空 → 当作本次失败，进入重试
                last_err = "LLM 返回为空 [stage=llm_stream]"
                logger.warning(f"[离线通知][诊断] {last_err}")
            except asyncio.TimeoutError:
                last_err = (
                    f"LLM 调用超时({self.timeout}s) [stage=llm_stream]"
                )
                logger.warning(f"[离线通知][诊断] {last_err}")
            except Exception as e:
                last_err = (
                    f"LLM 调用异常: {type(e).__name__}: {e} "
                    f"[stage=llm_stream]"
                )
                logger.warning(f"[离线通知][诊断] {last_err}")

            # 失败 → 重试或结束
            if attempt < self.max_retry:
                logger.warning(
                    f"[离线通知] 第 {attempt} 次 LLM 生成失败（{last_err}），"
                    f"{self.retry_delay}s 后重试"
                )
                await asyncio.sleep(self.retry_delay)
                continue
            break

        # 全部失败
        self._stats["failed_calls"] += 1
        self._stats["last_error"] = last_err or "未知失败"
        self._stats["last_success"] = False
        logger.error(
            f"[离线通知] LLM 生成失败（{last_err}），"
            f"provider: {self.provider_id}，stage={self._stats.get('stage')}"
        )
        return None

    async def _stream_collect(self, prov, prompt: str) -> str:
        """流式收集 provider 输出文本（与普通聊天同源）。

        prov.text_chat_stream 为 AsyncGenerator[LLMResponse, None]，
        每帧携带增量 completion_text，末帧为完整结果。我们跟踪
        最后一帧的 completion_text 作为最终结果（既兼容增量流，
        也兼容末帧完整文本），避免重复拼接。
        """
        # 老版本 provider 无流式方法，退回非流式
        if not hasattr(prov, "text_chat_stream"):
            resp = await prov.text_chat(prompt=prompt)
            if hasattr(resp, "completion_text"):
                return resp.completion_text or ""
            return str(resp)

        last_text = ""
        async for resp in prov.text_chat_stream(prompt=prompt):
            chunk = getattr(resp, "completion_text", None)
            if chunk:
                last_text = chunk
        return last_text

    async def generate_with_fallback(self, offline_time: str,
                                     countdown_minutes: int,
                                     fallback_message: str,
                                     now: datetime = None) -> str:
        """生成通知内容，失败时回退到模板消息

        Args:
            offline_time: 下线时间
            countdown_minutes: 剩余分钟数
            fallback_message: 模板生成的备用消息
            now: 当前时间

        Returns:
            str: 通知消息文本（LLM 生成或模板回退）
        """
        llm_result = await self.generate(offline_time, countdown_minutes, now)

        if llm_result:
            return llm_result

        if self.fallback_to_template:
            logger.info("[离线通知] LLM 生成失败，回退到模板消息")
            return fallback_message

        logger.warning("[离线通知] LLM 生成失败且回退已禁用，返回空消息")
        return ""

    def get_stats(self) -> dict:
        """获取生成统计信息

        Returns:
            dict: 统计数据
        """
        stats = dict(self._stats)
        avg_time = 0
        if self._stats["success_calls"] > 0:
            avg_time = self._stats["total_time_ms"] / self._stats["success_calls"]
        stats["avg_time_ms"] = round(avg_time, 1)
        return stats


def _patch_thinking(prov, disable: bool):
    """临时在 provider 的 custom_extra_body 注入 / 移除 thinking=disabled。

    仅影响本次插件调用；调用方须在 finally 中调用 _restore_thinking 还原。
    deepseek-v4 默认开启思考模式（十几秒 reasoning 才吐字），关闭后
    走非思考路径，单次生成从 20s+ 降到几秒。返回原 thinking 值
    （或 _NO_PATCH 表示无法补丁）。
    """
    cfg = getattr(prov, "provider_config", None)
    if not isinstance(cfg, dict):
        return _NO_PATCH
    eb = cfg.get("custom_extra_body")
    if not isinstance(eb, dict):
        eb = {}
        cfg["custom_extra_body"] = eb
    saved = eb.get("thinking", _NO_PATCH)
    if disable:
        eb["thinking"] = {"type": "disabled"}
    else:
        eb.pop("thinking", None)
    return saved


def _restore_thinking(prov, saved):
    """还原 _patch_thinking 对 provider 的临时修改。"""
    if saved is _NO_PATCH:
        return
    cfg = getattr(prov, "provider_config", None)
    if not isinstance(cfg, dict):
        return
    eb = cfg.get("custom_extra_body")
    if not isinstance(eb, dict):
        return
    if saved is _NO_PATCH:
        eb.pop("thinking", None)
    else:
        eb["thinking"] = saved
