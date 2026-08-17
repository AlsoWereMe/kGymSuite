"""智谱 GLM 官方 API 调用脚本。

接口: chat/completions (v4)
认证: Authorization: Bearer $ZHIPU_API_KEY (key 形如 id.secret)
推理强度: GLM-5.2+ 支持 reasoning_effort 控制思维链深度, 档位
          max(默认)/xhigh/high/medium/low/minimal/none (none/minimal=放弃思考,
          low/medium 映射 high, xhigh 映射 max);
          建议同时带 thinking:{type:enabled|disabled} 开关 (默认线格式
          thinking_reasoning_effort 一次发送两者)。

独立冒烟测试 (在 experiment/scripts 目录下):
  ZHIPU_API_KEY=id.secret python apis/zhipu.py --model glm-5.2 --effort high
"""
import argparse
import json
import os
import sys

# 独立运行 (python apis/<name>.py) 时把 experiment/scripts 加进 sys.path,
# 以便 import apis; 作为包成员被 import 时 __package__ 非空, 此段跳过
if __package__ is None or __package__ == "":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apis import post_json, base_reply, apply_effort

PROVIDER = "zhipu"
DEFAULT_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
DEFAULT_API_KEY_ENV = "ZHIPU_API_KEY"
DEFAULT_EFFORT_STYLE = "thinking_reasoning_effort"
SUPPORTED_STYLES = ("thinking_reasoning_effort", "thinking_onoff",
                    "reasoning_effort", "enable_thinking")
EFFORT_MAP = {}               # 官方档位直接透传 (max/xhigh/high/medium/low/minimal/none)


def build_body(model: str, prompt: str, effort: str, style: str, max_tokens: int) -> dict:
    body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    if max_tokens > 0:
        body["max_tokens"] = max_tokens
    if effort is not None:
        mapped = EFFORT_MAP.get(effort.lower(), effort)
        apply_effort(body, style, effort, mapped)
    return body


def chat_completion(*, model, prompt, effort="", api_key="", style=None,
                    api_base=None, timeout=600, max_tokens=0) -> dict:
    style = style or DEFAULT_EFFORT_STYLE
    if style not in SUPPORTED_STYLES:
        raise ValueError(f"zhipu 不支持的线格式 {style}, 可选 {SUPPORTED_STYLES}")
    ok, status, elapsed, payload = post_json(
        api_base or DEFAULT_URL, api_key,
        build_body(model, prompt, effort, style, max_tokens), timeout)
    if not ok:
        err = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)[:800]
        return {"ok": False, "httpStatus": status, "elapsedSeconds": elapsed, "error": err}
    try:
        return base_reply(payload, status, elapsed)
    except (KeyError, IndexError, TypeError):
        return {"ok": False, "httpStatus": status, "elapsedSeconds": elapsed,
                "error": "响应无 choices: " + json.dumps(payload, ensure_ascii=False)[:400]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="模型名 (如 glm-5.2)")
    ap.add_argument("--effort", default="high", help="推理强度 (非空即开启深度思考)")
    ap.add_argument("--style", default=None, help="线格式 (默认 " + DEFAULT_EFFORT_STYLE + ")")
    ap.add_argument("--prompt", default="Reply with exactly two characters: OK")
    ap.add_argument("--api-base", default=None, help="覆盖默认接口地址")
    args = ap.parse_args()
    key = os.environ.get(DEFAULT_API_KEY_ENV, "")
    if not key:
        print(f"环境变量 {DEFAULT_API_KEY_ENV} 未设置", file=sys.stderr)
        return 1
    r = chat_completion(model=args.model, prompt=args.prompt, effort=args.effort,
                        api_key=key, style=args.style, api_base=args.api_base)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
