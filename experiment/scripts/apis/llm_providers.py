"""官方平台 API 调用入口 (dispatcher, 位于 apis/ 包内)。

每个平台的调用脚本在 apis/ 目录:
  apis/dashscope.py  通义千问 (DashScope)
  apis/moonshot.py   Kimi (Moonshot)
  apis/zhipu.py      智谱 GLM

本模块是上层统一入口, 调用方式:
    from apis.llm_providers import PROVIDERS, chat_completion
    r = chat_completion(provider="dashscope", model="qwen3.8-max",
                        prompt="...", effort="max", api_key=...)

PROVIDERS 为各平台脚本元信息的汇总: {平台名: {url, apiKeyEnv, defaultStyle, effortMap}}
"""
import os
import sys

# 独立运行 (python apis/llm_providers.py) 时把 scripts/ 加进 sys.path;
# 作为包成员被 import 时 __package__ 非空, 此段跳过
if __package__ is None or __package__ == "":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apis import load_module, providers_meta

PROVIDERS = providers_meta()


def chat_completion(*, provider, model, prompt, effort="", api_key="",
                    style=None, api_base=None, timeout=600, max_tokens=0):
    """按 provider 名分发到 apis/<provider>.py 里的 chat_completion。"""
    mod = load_module(provider)
    return mod.chat_completion(
        model=model, prompt=prompt, effort=effort, api_key=api_key,
        style=style, api_base=api_base, timeout=timeout, max_tokens=max_tokens)
