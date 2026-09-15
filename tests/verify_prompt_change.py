# -*- coding: utf-8 -*-
"""验证提示词重构是否落实：默认值中性 + 三级优先级生效。

不依赖 astrbot 运行时：用 importlib 按路径加载 core 模块，
并注册假 core 包，规避 core/__init__ 对 apscheduler 的依赖。
运行: cd 插件目录 && python tests/verify_prompt_change.py
"""
import os
import sys
import json
import types
import tempfile
import shutil
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
STUBS_DIR = os.path.join(HERE, "_astrbot_stubs")
PLUGIN_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if STUBS_DIR not in sys.path:
    sys.path.insert(0, STUBS_DIR)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# 注册假 core 包，使相对导入 .splitter_compat / .prompt_store 可解析
core_pkg = types.ModuleType("core")
core_pkg.__path__ = [os.path.join(PLUGIN_ROOT, "core")]
sys.modules.setdefault("core", core_pkg)

_splitter = _load_module("core.splitter_compat",
                         os.path.join(PLUGIN_ROOT, "core", "splitter_compat.py"))
_llm = _load_module("core.llm_generator",
                    os.path.join(PLUGIN_ROOT, "core", "llm_generator.py"))
_pstore = _load_module("core.prompt_store",
                       os.path.join(PLUGIN_ROOT, "core", "prompt_store.py"))

DEFAULT_PROMPT = _llm.DEFAULT_PROMPT
LLMGenerator = _llm.LLMGenerator
PromptStore = _pstore.PromptStore


class _FakeCtx:
    pass


def main():
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        mark = "PASS" if cond else "FAIL"
        if not cond:
            ok = False
        print("  [%s] %s%s" % (mark, name, ("  " + extra) if extra else ""))

    print("\n=== 1) 静态校验：内置默认提示词应为通用·中立 ===")
    check("DEFAULT_PROMPT 不含『砂糖』人设", "砂糖" not in DEFAULT_PROMPT)
    check("DEFAULT_PROMPT 不含年龄设定『16岁』", "16岁" not in DEFAULT_PROMPT)
    check("DEFAULT_PROMPT 仅做功能引导/格式规范",
          ("文本生成助手" in DEFAULT_PROMPT) and ("中立" in DEFAULT_PROMPT))
    check("DEFAULT_PROMPT 含占位符",
          all(k in DEFAULT_PROMPT for k in
              ["{time_context}", "{date}", "{day_of_week}"]))

    print("\n=== 2) 逻辑校验：三级优先级（方案 > 配置自定义 > 内置默认）===")
    g0 = LLMGenerator(_FakeCtx(), {"enable_llm_generation": False})
    check("无配置无方案 → 返回内置通用默认",
          g0.get_effective_prompts() == DEFAULT_PROMPT)

    g1 = LLMGenerator(_FakeCtx(), {"enable_llm_generation": False,
                                   "custom_prompt": "我的自定义提示词"})
    check("配置 custom_prompt 覆盖默认",
          g1.get_effective_prompts() == "我的自定义提示词")

    d = tempfile.mkdtemp()
    try:
        store = PromptStore(d)
        store.upsert("方案A", "方案A的提示词")
        store.set_active("方案A")
        g2 = LLMGenerator(_FakeCtx(), {"enable_llm_generation": False,
                                       "custom_prompt": "我的自定义提示词"},
                          prompt_store=store)
        check("激活命名方案覆盖配置自定义",
              g2.get_effective_prompts() == "方案A的提示词")
        store.set_active(None)
        check("取消激活后回退到配置自定义",
              g2.get_effective_prompts() == "我的自定义提示词")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    g4 = LLMGenerator(_FakeCtx(), {"enable_llm_generation": False,
                                   "custom_builtin_prompt": "旧builtin",
                                   "custom_manual_prompt": "旧manual"})
    check("向后兼容旧键 custom_builtin_prompt",
          g4.get_effective_prompts() == "旧builtin")

    print("\n=== 3) 一致校验：DEFAULT_PROMPT == schema 默认值 ===")
    schema = json.load(open("_conf_schema.json", encoding="utf-8"))
    sd = schema["llm_generation_config"]["items"]["custom_prompt"]["default"]
    check("源码默认值 == schema 默认值", DEFAULT_PROMPT == sd)

    print("\n=== 结果 ===")
    print("全部通过 ✅" if ok else "存在失败项 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
