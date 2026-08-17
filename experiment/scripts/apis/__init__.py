"""官方平台 API 调用脚本目录: 每个平台一个独立脚本。

目录结构:
  llm_providers.py  统一入口/dispatcher (上层从这里 import, 按 provider 名路由)
  dashscope.py      通义千问 (DashScope)
  moonshot.py       Kimi (Moonshot)
  zhipu.py          智谱 GLM

统一接口 (每个脚本都实现):
  chat_completion(*, model, prompt, effort="", api_key="", style=None,
                  api_base=None, timeout=600, max_tokens=0) -> dict
  返回 dict 统一为:
    {ok, httpStatus, elapsedSeconds, content, finishReason, reasoningPreview, usage, error}

统一元信息 (每个脚本以模块级常量暴露):
  PROVIDER / DEFAULT_URL / DEFAULT_API_KEY_ENV / DEFAULT_EFFORT_STYLE /
  EFFORT_MAP / SUPPORTED_STYLES

公共工具 (供各平台脚本复用):
  post_json()    统一 HTTP POST + 错误捕获
  base_reply()   统一把平台响应解析成上述 dict
  apply_effort() 统一把"推理强度"写进请求体 (多线格式)

推理强度线格式 (apply_effort 的 style 参数, 各平台在 SUPPORTED_STYLES 里声明):
  reasoning_effort          请求体顶层 reasoning_effort=<档位> (如 DashScope/Kimi K3)
  enable_thinking           请求体 enable_thinking=<true|false> (如 DashScope 部分模型)
  thinking_onoff            请求体 thinking={type: enabled|disabled} (如智谱经典格式)
  thinking_effort           请求体 thinking={type, effort} (Kimi K2.x 系列)
  thinking_reasoning_effort 请求体 thinking={type} + 顶层 reasoning_effort (智谱 GLM-5.2+)

effort 语义: 非空档位 (low/medium/high/max/...) 按平台映射写入;
            空字符串或 off/none/disabled/0 视为"关闭思考" (始终思考型模型不支持关闭,
            此时 reasoning_effort 线格式直接不写该字段, 由平台默认档位决定)。

独立冒烟测试 (每个脚本可直接运行):
  cd experiment/scripts
  DASHSCOPE_API_KEY=sk-... python apis/dashscope.py --model qwen3.8-max --effort max
"""
import importlib
import json
import time
import urllib.error
import urllib.request

PLATFORMS = ("dashscope", "moonshot", "zhipu")
OFF_EFFORTS = ("", "off", "none", "disabled", "0")


def post_json(url: str, api_key: str, body: dict, timeout: int):
    """POST JSON 并统一异常处理; 返回 (ok, http_status, elapsed, data_or_error_text)。"""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (True, resp.status, round(time.time() - t0, 1),
                    json.loads(resp.read().decode("utf-8", errors="replace")))
    except urllib.error.HTTPError as e:
        return (False, e.code, round(time.time() - t0, 1),
                e.read().decode("utf-8", errors="replace")[:800])
    except Exception as e:
        return False, None, round(time.time() - t0, 1), str(e)


def base_reply(data: dict, http_status, elapsed) -> dict:
    """各平台把响应解析出 content/usage 后统一收尾。"""
    choice = data["choices"][0]
    msg = choice.get("message", {})
    reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
    return {
        "ok": True,
        "httpStatus": http_status,
        "elapsedSeconds": elapsed,
        "content": msg.get("content") or "",
        "finishReason": choice.get("finish_reason"),
        "reasoningPreview": (reasoning or "")[:300],
        "usage": data.get("usage") or {},
    }


def apply_effort(body: dict, style: str, effort: str, mapped: str) -> dict:
    """按线格式把推理强度写进请求体 (effort 为空/off 等 = 关闭思考)。

    reasoning_effort 线格式在"关闭"时不写该字段: 部分新模型始终思考
    (如 qwen3.8-max / kimi-k3), 平台没有合法的关闭值, 只能省略参数用默认档位。
    """
    on = effort.lower() not in OFF_EFFORTS
    if style == "reasoning_effort":
        if on:
            body["reasoning_effort"] = mapped
    elif style == "enable_thinking":
        body["enable_thinking"] = on
    elif style == "thinking_onoff":
        body["thinking"] = {"type": "enabled" if on else "disabled"}
    elif style == "thinking_effort":
        body["thinking"] = {"type": "enabled" if on else "disabled"}
        if on:
            body["thinking"]["effort"] = mapped
    elif style == "thinking_reasoning_effort":
        body["thinking"] = {"type": "enabled" if on else "disabled"}
        if on:
            body["reasoning_effort"] = mapped
    else:
        raise ValueError(f"未知 effort 线格式: {style}")
    return body


def load_module(provider: str):
    """按平台名加载对应调用脚本 (apis.<provider>)。"""
    if provider not in PLATFORMS:
        raise ValueError(f"未知平台 {provider}, 可选: {list(PLATFORMS)}")
    return importlib.import_module("apis." + provider)


def providers_meta() -> dict:
    """汇总所有平台脚本的元信息 (url / apiKeyEnv / defaultStyle / effortMap)。"""
    meta = {}
    for p in PLATFORMS:
        m = load_module(p)
        meta[p] = {
            "url": m.DEFAULT_URL,
            "apiKeyEnv": m.DEFAULT_API_KEY_ENV,
            "defaultStyle": m.DEFAULT_EFFORT_STYLE,
            "effortMap": m.EFFORT_MAP,
        }
    return meta
