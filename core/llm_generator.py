"""
LLM 通知内容生成器 - 调用对话模型生成多样化、自然的下线通知。

要点:
- 默认提示词 DEFAULT_PROMPT 为通用、中立的功能引导，不含任何特定
  人设 / 语气风格，仅规范基本指令与输出形式；自动与手动通知共用同一条。
- 超时兜底 + 重试（retry_config.llm_retries）+ 模板回退。
- 提示词「明置」：get_effective_prompts() 三级优先级
  （激活命名方案 > 配置 custom_prompt > 内置 DEFAULT_PROMPT）。
"""

import asyncio
import time
from datetime import datetime
from typing import Optional
from astrbot.api import logger

from .splitter_compat import normalize_for_splitter


# 内置默认提示词（兜底）：通用、中立的功能引导，不含任何特定人设 /
# 语气风格，仅规范基本指令与输出形式。自动与手动通知共用同一条。
# 这是 get_effective_prompts() 的最低优先级兜底；配置项 custom_prompt
# 留空时即使用本默认，填写后覆盖。
DEFAULT_PROMPT = """你是一个用于生成「暂时离开群聊通知」的文本生成助手。当触发方需要暂时离开当前群聊时，你会生成一段简洁的离场说明，由触发方发送到对应群聊。

系统会在调用时注入以下上下文（请勿在消息中复述变量名或占位符）：
- {time_context}：当前离场的情境说明（例如：准备去休息 / 临时有事 / 想稍作停顿），据此自然说明离开原因。
- {date} {day_of_week}：当前日期与星期。

生成要求：
1. 内容聚焦「暂时离开」这一事实，用一两句话说明接下来要去做的事（参照 time_context 的情境），避免使用「下线 / 离线 / 退出 / 挂机 / 系统 / 通知 / 机器人 / AI / 助手」等词汇或后台公告式口吻。
2. 保持中立、平实的表述，不设定任何特定人设、性格、年龄或语气风格。
3. 纯文本输出，不使用 Markdown、不添加前缀或解释、不包含 <think> 标签。
4. 仅输出最终要发送的那一两句话，不要输出其他内容。

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

    def __init__(self, context, config: dict, prompt_store=None,
                 retry_config: dict = None):
        """初始化 LLM 生成器

        Args:
            context: AstrBot Context 对象
            config: llm_generation_config 配置节
            prompt_store: 可选的 PromptStore 命名方案库（用于多方案覆盖）
            retry_config: retry_config 配置节（提供 LLM 生成重试参数）
        """
        self.context = context
        self.config = config
        self.enabled = config.get("enable_llm_generation", True)
        self.provider_id = config.get("llm_provider_id", "")
        # 整体超时：放宽到 40~240s，避免误掐框架本可完成的较慢响应。
        raw = int(config.get("llm_timeout", 90) or 90)
        self.timeout = min(max(raw, 40), 240)
        # LLM 生成调用重试：首次超时（多半是 provider 注册表重连）后，
        # 等待 retry_delay 秒再试。优先读 retry_config.llm_retries，
        # 向后兼容旧键 llm_max_retry。
        rc = retry_config or {}
        self.max_retry = int(
            rc.get("llm_retries", config.get("llm_max_retry", 2)) or 0)
        self.retry_delay = int(
            rc.get("llm_retry_interval", config.get("llm_retry_delay", 3)) or 3)
        self.fallback_to_template = config.get("fallback_to_template", True)

        # 提示词「明置」：配置 custom_prompt 优先于内置默认；
        # 命名方案（prompt_store）优先级最高。见 get_effective_prompts()。
        # 向后兼容旧键 custom_builtin_prompt / custom_manual_prompt。
        self.custom_prompt = (
            config.get("custom_prompt")
            or config.get("custom_builtin_prompt")
            or config.get("custom_manual_prompt")
            or ""
        ).strip()
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
        """返回当前生效的提示词（单条，自动与手动通知共用）。

        三级优先级（高 → 低）：
          1. 已激活的命名方案（PromptStore 中 set_active 的方案，整体覆盖）；
          2. 配置自定义 custom_prompt（明置编辑）；
          3. 内置默认 DEFAULT_PROMPT（通用、中立的功能引导）。

        Returns:
            str: 生效的提示词模板
        """
        prompt = DEFAULT_PROMPT
        # 2) 配置自定义（明置）
        if self.custom_prompt:
            prompt = self.custom_prompt
        # 1) 命名方案（最高优先级，整体覆盖；兼容旧方案库的双字段）
        if self.prompt_store is not None:
            active = self.prompt_store.get_active()
            if active:
                prof = self.prompt_store.get_profile(active)
                if prof:
                    p = (prof.get("prompt")
                         or prof.get("builtin_prompt")
                         or prof.get("manual_prompt"))
                    if p:
                        prompt = p
        return prompt

    async def generate(self, offline_time: str, countdown_minutes: int,
                       now: datetime = None, is_manual: bool = False) -> Optional[str]:
        """调用 LLM 生成下线通知内容

        走与普通聊天完全同源的流式路径 provider.text_chat_stream
        （同一 provider 实例、同一底层方法），因此沿用框架已验证
        可用的代理/信任环境/超时配置。思考模式是否开启交由用户在
        模型 / 提供商侧自行配置，本插件不再干预。

        Args:
            offline_time: 下线时间 (HH:MM)
            countdown_minutes: 剩余分钟数
            now: 当前时间，默认 datetime.now()
            is_manual: 是否手动触发（仅用于日志/统计区分，提示词共用同一条）

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

        # 自动与手动通知共用同一条生效提示词（见 get_effective_prompts）
        prompt_template = self.get_effective_prompts()
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

                # 流式收集：连接即返回，不阻塞等完整响应
                text = await asyncio.wait_for(
                    self._stream_collect(prov, prompt),
                    timeout=self.timeout,
                )

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
