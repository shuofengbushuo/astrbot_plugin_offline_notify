"""
LLM 通知内容生成器 - 调用对话模型生成多样化、自然的下线通知

核心功能:
- 内置提示词引导模型生成自然的通知内容
- 包含必要信息: 下线时间、剩余分钟数、预计恢复时间
- 支持超时控制和模板回退
- 记录每次生成的统计信息

调用方式:
  generator = LLMGenerator(context, config)
  message = await generator.generate(offline_time="23:00", countdown_minutes=5)
"""

import asyncio
import time
from datetime import datetime
from typing import Optional
from astrbot.api import logger


# 内置提示词 - 引导模型生成多样化、自然的下线通知
BUILTIN_PROMPT = """你是一个友好的AI助手，需要向群成员发布一条下线通知。

请根据以下信息生成一条自然、温馨的下线通知消息：

当前时间: {date} {day_of_week}
下线时间: {offline_time}
剩余时间: {countdown_minutes} 分钟后

要求：
1. 语气友好亲切、生动活泼，像朋友之间的道别，不要生硬的官方语气
2. 开头称呼必须使用"猪猪们"，不要用"朋友们"或其他称呼
3. 不要在正文中强调或重复具体时间数字（如"还有5分钟"、"17:19"等），时间信息应自然融入语境而非单独罗列
4. 以晚安祝福为核心收尾，强烈强调"晚安好梦"等温馨祝福语，可用叠词、波浪号等增强语气（如"晚安好梦哦~"、"晚安啦，好梦~"等），让祝福成为文案最突出的情感落点
5. 可以加入温馨提醒等元素，文案要生动有趣
6. 本次是{day_of_week}，请根据星期几调整语气（如周五可以更欢快，周日可以提醒明天工作日）
7. 你（AI助手）在文案中应统一称呼自己为"砂糖"，如"砂糖先闪啦"、"砂糖溜了"等，不要用"AI助手"或其他称呼
8. 注意避免相邻语句中使用相同的词汇，特别是不要连续使用同一个词来修饰不同的动作（如"马上要闪了，马上就下线"中的重复"马上"），应使用不同的表达方式，使语言自然流畅
9. 长度控制在 3-6 句话，不要太长
10. 每次生成的内容风格要有变化，不要总是用相同的句式
11. 纯文本输出，不要使用 markdown 格式
12. 不要输出任何前缀、解释或额外内容，只输出通知正文"""

# 手动生成场景专用提示词 — 管理员手动触发，非自动调度
MANUAL_PROMPT = """你是一个友好的AI助手，需要向群成员发布一条下线通知。

**重要上下文**：这条通知是管理员手动触发的，**不是按计划时间自动发送的**。因此不需要强调"还有几分钟"、"XX分钟后"等倒计时概念，只需自然地告知大家砂糖即将下线即可。

请根据以下信息生成一条自然、温馨的下线通知消息：

当前时间: {date} {day_of_week}
下线时间: {offline_time}

要求：
1. 语气友好亲切、生动活泼，像朋友之间的道别，不要生硬的官方语气
2. 开头称呼必须使用"猪猪们"，不要用"朋友们"或其他称呼
3. **不要使用倒计时表述**（如"还有X分钟"、"X分钟后"等），因为这是手动触发的通知
4. 不要在正文中强调或重复具体时间数字，时间信息应自然融入语境而非单独罗列
5. 以晚安祝福为核心收尾，强烈强调"晚安好梦"等温馨祝福语，可用叠词、波浪号等增强语气（如"晚安好梦哦~"、"晚安啦，好梦~"等），让祝福成为文案最突出的情感落点
6. 可以加入温馨提醒等元素，文案要生动有趣
7. 本次是{day_of_week}，请根据星期几调整语气（如周五可以更欢快，周日可以提醒明天工作日）
8. 你（AI助手）在文案中应统一称呼自己为"砂糖"，如"砂糖先闪啦"、"砂糖溜了"等，不要用"AI助手"或其他称呼
9. 注意避免相邻语句中使用相同的词汇，特别是不要连续使用同一个词来修饰不同的动作（如"马上要闪了，马上就下线"中的重复"马上"），应使用不同的表达方式，使语言自然流畅
10. 长度控制在 3-6 句话，不要太长
11. 每次生成的内容风格要有变化，不要总是用相同的句式
12. 纯文本输出，不要使用 markdown 格式
13. 不要输出任何前缀、解释或额外内容，只输出通知正文"""


class LLMGenerator:
    """基于 LLM 的下线通知内容生成器"""

    # 星期映射
    WEEKDAY_NAMES = {
        0: "周一", 1: "周二", 2: "周三", 3: "周四",
        4: "周五", 5: "周六", 6: "周日"
    }

    def __init__(self, context, config: dict):
        """初始化 LLM 生成器

        Args:
            context: AstrBot Context 对象
            config: llm_generation_config 配置节
        """
        self.context = context
        self.enabled = config.get("enable_llm_generation", True)
        self.provider_id = config.get("llm_provider_id", "")
        self.timeout = config.get("llm_timeout", 15)
        self.fallback_to_template = config.get("fallback_to_template", True)

        # 统计信息
        self._stats = {
            "total_calls": 0,
            "success_calls": 0,
            "failed_calls": 0,
            "total_time_ms": 0,
            "last_call_time": None,
            "last_error": None,
        }

    async def generate(self, offline_time: str, countdown_minutes: int,
                       now: datetime = None, is_manual: bool = False) -> Optional[str]:
        """调用 LLM 生成下线通知内容

        Args:
            offline_time: 下线时间 (HH:MM)
            countdown_minutes: 剩余分钟数
            now: 当前时间，默认 datetime.now()
            is_manual: 是否手动触发（True=手动场景，使用 MANUAL_PROMPT）

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

        # 根据场景选择提示词
        prompt_template = MANUAL_PROMPT if is_manual else BUILTIN_PROMPT
        prompt = prompt_template.format(
            date=date_str,
            day_of_week=day_of_week,
            offline_time=offline_time,
            countdown_minutes=countdown_minutes,
        )

        self._stats["total_calls"] += 1
        start_time = time.time()

        try:
            # 调用 LLM，带超时保护
            # context.llm_generate 是异步函数，直接 await 即可
            llm_resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=self.provider_id,
                    prompt=prompt
                ),
                timeout=self.timeout
            )

            elapsed_ms = (time.time() - start_time) * 1000
            self._stats["total_time_ms"] += elapsed_ms
            self._stats["last_call_time"] = time.time()

            if llm_resp and llm_resp.completion_text:
                text = llm_resp.completion_text.strip()
                if text:
                    self._stats["success_calls"] += 1
                    logger.info(
                        f"[离线通知] LLM 生成成功，耗时 {elapsed_ms:.0f}ms，"
                        f"内容长度: {len(text)} 字符"
                    )
                    return text

            # LLM 返回空内容
            self._stats["failed_calls"] += 1
            self._stats["last_error"] = "LLM 返回空内容"
            logger.warning(f"[离线通知] LLM 返回空内容，耗时 {elapsed_ms:.0f}ms")
            return None

        except asyncio.TimeoutError:
            self._stats["failed_calls"] += 1
            self._stats["last_error"] = f"LLM 调用超时 ({self.timeout}s)"
            logger.error(
                f"[离线通知] LLM 调用超时 ({self.timeout}s)，"
                f"provider: {self.provider_id}"
            )
            return None

        except Exception as e:
            self._stats["failed_calls"] += 1
            self._stats["last_error"] = str(e)
            logger.error(
                f"[离线通知] LLM 调用异常: {e}",
                exc_info=True
            )
            return None

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