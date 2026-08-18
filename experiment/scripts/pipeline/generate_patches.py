"""对每条选中的 bug, 直连官方平台 API 单轮生成补丁并自动提取成 patch 文件。

平台 (见 apis/llm_providers.py, 不再经过 OpenRouter):
  dashscope (通义千问) / moonshot (Kimi) / zhipu (智谱 GLM)

流程: 构造 prompt(崩溃报告 + 可选坏内核源码片段) -> POST <官方平台>/chat/completions
      -> 提取 diff 块; 提取失败自动重试 RETRIES 次 (共最多 1+RETRIES 次尝试), 仍失败则跳过该条。

prompt 共享与一致性:
  - prompt 统一写入 <exp根>/prompts/<bugId>.txt (各模型目录不再单独存 prompt, 由 --prompts-dir 可覆盖);
  - 源码片段按 bugId 缓存于 <exp根>/prompts/.snippets/, 一次抓取所有模型复用, 保证同一批模型拿到完全一致的 prompt;
  - 片段从 fix commit 的父提交 (parentOfFixCommit) 抓取, 作为有依据的代码上下文。

输出 (<base> 即模型目录 experiment/<model>):
  replies/completion/<bugId>.txt   模型正文回复
  replies/reasoning/<bugId>.txt    模型思考链 (reasoning, 有则保存)
  patches/<bugId>.patch     提取出的补丁
  patches.json              生成清单 (可断点续跑: 成功过的会跳过)
  generation-stats.json     每条回复的耗时/花费/重试次数统计 (供 analyze_results.py 合并)

推理强度: 每个模型单独配置 (run_models.py 传入 --reasoning-effort);
          dashscope 档位 low/medium/high/xhigh ("max" 自动映射为 xhigh),
          kimi-k3 顶层 reasoning_effort (low/high/max),
          glm-5.2 以 thinking + reasoning_effort 配置深度与强度
          (详见 apis/llm_providers.py)。

依赖环境变量 (按模型选用, run_models.py 里配置了每个模型对应的 key):
  DASHSCOPE_API_KEY   通义千问
  MOONSHOT_API_KEY    Kimi
  ZHIPU_API_KEY       智谱 GLM (形如 id.secret)

用法:
  DASHSCOPE_API_KEY=sk-... .venv/bin/python generate_patches.py \
      --base experiment/qwen3.8-max --model qwen3.8-max --provider dashscope
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from datetime import datetime, timezone

# 独立运行时把 scripts/ 加进 sys.path 以便 import apis 包
if __package__ is None or __package__ == "":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apis.llm_providers import PROVIDERS, chat_completion

BASE = "experiment"
BUGS_PATH = os.path.join(BASE, "bugs.json")

INCLUDE_SOURCE = True          # 是否把坏内核相关源码放进 prompt (显著提高 git apply 命中率)
SOURCE_MAX_FILES = 3           # 每个 bug 最多抓取的文件数
FETCH_TIMEOUT = 30             # 单文件抓取超时(秒)
MODEL_TIMEOUT = int(os.environ.get("KGym_MODEL_TIMEOUT", "3600") or 3600)   # 单条模型请求超时(秒), 默认 1 小时
RETRIES = 2                    # 提取不到 diff 时的额外重试次数 (共最多 1+2=3 次尝试)
MODEL = os.environ.get("KGym_MODEL", "")
PROVIDER = os.environ.get("KGym_PROVIDER", "dashscope")   # dashscope / moonshot / zhipu
API_KEY_ENV = os.environ.get("KGym_API_KEY_ENV", "")      # 为空则取平台默认 key 环境变量
API_BASE = os.environ.get("KGym_API_BASE", "")            # 覆盖平台默认接口地址
EFFORT_STYLE = os.environ.get("KGym_EFFORT_STYLE", "")    # 覆盖平台默认 effort 线格式
MAX_TOKENS = int(os.environ.get("KGym_MAX_TOKENS", "131072") or 131072)  # 默认最大总输出 128K; OpenRouter 写 max_completion_tokens
REASONING_EFFORT = os.environ.get("KGym_REASONING_EFFORT", "high")  # 推理强度, 每个模型单独配置
PRICE_INPUT_PER_M = float(os.environ.get("KGym_PRICE_INPUT_PER_M", "0") or 0) or None   # 输入单价-缓存未命中 (元/百万 token)
PRICE_INPUT_HIT_PER_M = float(os.environ.get("KGym_PRICE_INPUT_HIT_PER_M", "0") or 0) or None  # 输入单价-缓存命中
PRICE_OUTPUT_PER_M = float(os.environ.get("KGym_PRICE_OUTPUT_PER_M", "0") or 0) or None  # 输出单价 (元/百万 token)
CURRENCY = os.environ.get("KGym_CURRENCY", "CNY")
SNIPPET_MARKER = "Relevant source code at the buggy commit:"       # prompt 含源码片段的标记

FENCE = chr(96) * 3            # 三个反引号, 避免源码里出现转义麻烦

INTERESTING_DIRS = (
    "block/", "drivers/", "fs/", "kernel/", "mm/", "net/", "lib/",
    "include/", "sound/", "security/", "ipc/", "virt/", "arch/",
)

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def fetch_text(url: str):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "kgym-exp"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = resp.read(5 * 1024 * 1024)
        return data.decode("utf-8", errors="replace")
    except Exception:
        return None


def plain_url(git_url: str, commit: str, path: str):
    """把内核 git 仓库 URL 转成单文件 raw 下载地址。"""
    git_url = git_url.rstrip("/")
    # 归一化: 去掉 /commits/<sha>、/commit/?id=<sha>、/log/?id=<sha> 这类页面后缀
    for pat in (r"/commits/[0-9a-f]{7,40}$", r"/commit/[0-9a-f]{7,40}$", r"/commit/?\?id=[0-9a-f]{7,40}$", r"/log/?\?id=[0-9a-f]{7,40}$"):
        git_url = re.sub(pat, "", git_url)
    if "git.kernel.org" in git_url:
        return git_url + "/plain/" + path + "?h=" + commit
    m = re.match(r"https://github\.com/([^/]+)/([^/]+?)(\.git)?$", git_url)
    if m:
        return "https://raw.githubusercontent.com/" + m.group(1) + "/" + m.group(2) + "/" + commit + "/" + path
    return None


def stack_files(raw: str):
    """从崩溃报告的栈回溯里解析出文件路径(按出现顺序去重, 最多 SOURCE_MAX_FILES 个)。"""
    seen = set()
    out = []
    for m in re.finditer(r"\b([\w\-/]+\.(?:c|h)):\d+", raw):
        p = m.group(1)
        if p in seen or not p.startswith(INTERESTING_DIRS):
            continue
        seen.add(p)
        out.append(p)
        if len(out) >= SOURCE_MAX_FILES:
            break
    return out


def first_line_of(raw: str, path: str):
    m = re.search(re.escape(path) + r":(\d+)", raw)
    return int(m.group(1)) if m else None


def build_snippets(bug: dict) -> str:
    """抓取坏内核(parentOfFixCommit)上栈回溯涉及的文件, 生成带行号的源码片段。"""
    crash = bug["crashes"][0]
    raw = bug.get("rawCrashReport") or ""
    commit = bug.get("parentOfFixCommit")
    if not commit or not raw:
        return ""
    parts = []
    for path in stack_files(raw):
        url = plain_url(crash.get("kernelSourceGit") or "", commit, path)
        if not url:
            continue
        text = fetch_text(url)
        if not text:
            continue
        lines = text.splitlines()
        if len(lines) > 400:
            ln = first_line_of(raw, path) or 1
            lo = max(0, ln - 1 - 120)
            hi = min(len(lines), ln + 120)
            lines = lines[lo:hi]
            start = lo + 1
        else:
            start = 1
        numbered = "\n".join(f"{start + i:6d}  {l}" for i, l in enumerate(lines))
        parts.append("File: " + path + "\n" + FENCE + "\n" + numbered + "\n" + FENCE)
    return "\n\n".join(parts)


def build_prompt(bug: dict, snippets: str) -> str:
    raw = bug.get("rawCrashReport") or ""
    commit = bug.get("parentOfFixCommit") or ""
    title = bug.get("title") or ""
    parts = [
        "You are a Linux kernel expert. Fix the kernel bug described below.",
        "",
        "Rules:",
        "1. Do NOT use any tools and do NOT browse or search anything. Answer directly from your own knowledge in this single reply.",
        "2. Reply with ONLY one fenced diff block containing a unified git diff ('diff --git' format with exact context lines). No explanation, no other text outside the fence.",
        "3. The patch will be applied with 'git apply' at the root of a Linux kernel tree checked out at commit " + commit + ". Context lines MUST match this tree exactly (tab-indented C code).",
        "",
        "Bug title: " + title,
        "",
        "Crash report:",
        FENCE,
        raw,
        FENCE,
    ]
    if snippets:
        parts += ["", "Relevant source code at the buggy commit:", snippets]
    parts += ["", "Provide the minimal fix."]
    return "\n".join(parts)


def cached_prompt_tokens(usage: dict) -> int:
    """从 usage 里取缓存命中的输入 token 数 (平台未上报时为 0)。"""
    d = usage.get("prompt_tokens_details") or {}
    return d.get("cached_tokens") or d.get("cache_read_tokens") or 0


def token_cost(usage: dict):
    """按每百万 token 单价计算一次调用的花费; 未配置价格时返回 None。

    官方平台响应一般不含 cost 字段, 只能按 token 数 × 单价换算:
      输入 = 未命中部分 × inputPerM + 命中部分 × inputHitPerM, 输出 × outputPerM。
    假设: 思考(reasoning) token 计入 completion_tokens (与两平台计费口径一致)。
    """
    if not PRICE_INPUT_PER_M and not PRICE_OUTPUT_PER_M:
        return None
    total_p = usage.get("prompt_tokens") or 0
    hit_p = min(cached_prompt_tokens(usage), total_p)
    miss_p = total_p - hit_p
    c = usage.get("completion_tokens") or 0
    return round(
        (miss_p / 1e6) * (PRICE_INPUT_PER_M or 0)
        + (hit_p / 1e6) * (PRICE_INPUT_HIT_PER_M or PRICE_INPUT_PER_M or 0)
        + (c / 1e6) * (PRICE_OUTPUT_PER_M or 0), 8)


def model_chat(prompt: str):
    """直连官方平台 (apis.llm_providers.chat_completion) 调用模型。

    成功时返回字典: {content, usage, reasoning, finishReason}。
    content 为模型正文 (可能为空), reasoning 为思考链 (可能为空),
    usage 以平台实际返回为准 (一般只含 token 数)。
    HTTP 错误/缺 key 等仍抛 RuntimeError。
    """
    if PROVIDER not in PROVIDERS:
        raise RuntimeError(f"未知平台 {PROVIDER}, 可选: {sorted(PROVIDERS)}")
    key_env = API_KEY_ENV or PROVIDERS[PROVIDER]["apiKeyEnv"]
    key = os.environ.get(key_env, "")
    if not key:
        raise RuntimeError(f"环境变量 {key_env} 未设置 (平台 {PROVIDER} 的 API key)")
    if not MODEL:
        raise RuntimeError("模型未指定: 请设置 KGym_MODEL 或用 --model")

    r = chat_completion(
        provider=PROVIDER,
        model=MODEL,
        prompt=prompt,
        effort=REASONING_EFFORT,
        api_key=key,
        style=EFFORT_STYLE or None,
        api_base=API_BASE or None,
        timeout=MODEL_TIMEOUT,
        max_tokens=MAX_TOKENS,
    )
    if not r["ok"]:
        raise RuntimeError(f"HTTP {r.get('httpStatus')}: {r.get('error', '')[:400]}")
    return {
        "content": r.get("content") or "",
        "usage": r.get("usage") or {},
        "reasoning": r.get("reasoning") or "",
        "finishReason": r.get("finishReason"),
    }


def run_model(prompt: str, label: str = ""):
    """在后台线程调用模型, 主线程输出旋转等待条(说明未卡死)。

    返回 SimpleNamespace(stdout=回复文本, stderr=错误文本, returncode, elapsed,
                         usage, reasoning)。
    """
    result = {}

    def worker():
        try:
            chat = model_chat(prompt)
            result["reply"] = chat["content"]
            result["usage"] = chat["usage"]
            result["reasoning"] = chat["reasoning"]
            if not chat["content"]:
                result["error"] = (
                    "模型返回空内容: finish_reason=" + str(chat["finishReason"])
                    + ", usage=" + json.dumps(chat["usage"])
                )
        except Exception as e:   # noqa: BLE001 - 统一转成 stderr 文本供上层重试
            result["error"] = str(e)

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    start = time.time()
    frame = 0
    while t.is_alive():
        time.sleep(0.4)
        elapsed = int(time.time() - start)
        mm, ss = divmod(elapsed, 60)
        n = frame % 7
        bar = "█" * n + "░" * (6 - n)
        sys.stderr.write(
            "\r  " + SPINNER_FRAMES[frame % len(SPINNER_FRAMES)] + " [" + bar + "] "
            "" + label + " 已耗时 %02d:%02d    " % (mm, ss)
        )
        sys.stderr.flush()
        frame += 1
    t.join(1)

    sys.stderr.write("\r" + " " * 78 + "\r")
    sys.stderr.flush()

    elapsed = int(time.time() - start)
    if "error" in result:
        return types.SimpleNamespace(
            stdout="", stderr=str(result["error"]), returncode=1, elapsed=elapsed,
            usage=result.get("usage") or {}, reasoning=result.get("reasoning") or ""
        )
    reply = result.get("reply") or ""
    return types.SimpleNamespace(
        stdout=reply, stderr="", returncode=0, elapsed=elapsed,
        usage=result.get("usage") or {}, reasoning=result.get("reasoning") or ""
    )


def extract_patch(out: str):
    """优先取第一个 fenced diff 块; 否则取从 'diff --git' 到最后一个合法补丁行。"""
    m = re.search(FENCE + "diff\n(.*?)\n" + FENCE, out, re.S)
    if m and "diff --git" in m.group(1):
        return m.group(1).strip("\n") + "\n"
    lines = out.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith("diff --git"))
    except StopIteration:
        return None
    patch = lines[start:]

    def is_patch_line(l: str) -> bool:
        return l.startswith(("diff ", "index ", "--- ", "+++ ", "@@", "+", "-", " ")) or l.startswith("\\")

    while patch and not is_patch_line(patch[-1]):
        patch.pop()
    return "\n".join(patch) + "\n"


def shared_prompts_dir(base: str) -> str:
    """共享 prompt 目录: experiment/<model> -> experiment/prompts (exp 根目录下)。"""
    parent = os.path.dirname(os.path.normpath(base))
    return os.path.join(parent, "prompts")


def load_snippets_cached(bug: dict, prompts_dir: str) -> str:
    """按 bugId 缓存从 parentOfFixCommit 抓取的源码片段, 跨模型复用。

    只有抓取成功才写缓存; 失败不缓存, 下次运行时重试, 尽量保证每个模型
    最终都能拿到同一份有依据的代码上下文。
    """
    if not INCLUDE_SOURCE:
        return ""
    bid = bug["bugId"]
    cache_dir = os.path.join(prompts_dir, ".snippets")
    cache_path = os.path.join(cache_dir, f"{bid}.json")
    if os.path.exists(cache_path):
        try:
            data = json.load(open(cache_path, encoding="utf-8"))
            print(f"    (复用源码片段缓存: {len(data.get('files', []))} 个文件)")
            return data.get("snippets", "")
        except (json.JSONDecodeError, OSError):
            pass
    snippets = build_snippets(bug)
    if snippets:
        os.makedirs(cache_dir, exist_ok=True)
        json.dump(
            {"bugId": bid, "snippets": snippets,
             "files": stack_files(bug.get("rawCrashReport") or "")},
            open(cache_path, "w", encoding="utf-8"),
            ensure_ascii=False,
        )
    else:
        print("    (警告: 未能从 parentOfFixCommit 抓取到源码片段, 本次 prompt 不含代码上下文)")
    return snippets


def main() -> int:
    global MODEL, REASONING_EFFORT, PROVIDER, API_KEY_ENV, API_BASE, EFFORT_STYLE
    global PRICE_INPUT_PER_M, PRICE_INPUT_HIT_PER_M, PRICE_OUTPUT_PER_M, CURRENCY
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE, help="模型目录 (默认 experiment/<model>)")
    ap.add_argument("--model", default=MODEL, help="模型名 (默认取环境变量 KGym_MODEL)")
    ap.add_argument("--provider", default=PROVIDER,
                    help="官方平台: dashscope / moonshot / zhipu (见 apis/llm_providers.py)")
    ap.add_argument("--api-key-env", default=API_KEY_ENV,
                    help="API key 环境变量名 (默认取平台配置, 如 DASHSCOPE_API_KEY)")
    ap.add_argument("--api-base", default=API_BASE, help="覆盖平台默认接口地址")
    ap.add_argument("--effort-style", default=EFFORT_STYLE,
                    help="覆盖平台默认 effort 线格式 "
                         "(reasoning_effort/enable_thinking/thinking_onoff/"
                         "thinking_effort/thinking_reasoning_effort)")
    ap.add_argument("--reasoning-effort", default=REASONING_EFFORT,
                    help="推理强度, 每个模型单独配置 (dashscope: low/medium/high/max, max->xhigh)")
    ap.add_argument("--price-input-per-m", type=float, default=PRICE_INPUT_PER_M,
                    help="输入单价-缓存未命中 (元/百万 token), 用于按 token 数算钱")
    ap.add_argument("--price-input-hit-per-m", type=float, default=PRICE_INPUT_HIT_PER_M,
                    help="输入单价-缓存命中 (元/百万 token), 未给则按未命中价算")
    ap.add_argument("--price-output-per-m", type=float, default=PRICE_OUTPUT_PER_M,
                    help="输出单价 (元/百万 token), 用于按 token 数算钱")
    ap.add_argument("--currency", default=CURRENCY, help="货币单位 (默认 CNY)")
    ap.add_argument("--prompts-dir", default=None,
                    help="共享 prompt 目录 (默认 <exp根>/prompts, 所有模型共用)")
    args = ap.parse_args()
    base = args.base
    MODEL = args.model or ""
    PROVIDER = args.provider or "dashscope"
    API_KEY_ENV = args.api_key_env or ""
    API_BASE = args.api_base or ""
    EFFORT_STYLE = args.effort_style or ""
    REASONING_EFFORT = args.reasoning_effort or ""
    PRICE_INPUT_PER_M = args.price_input_per_m
    PRICE_INPUT_HIT_PER_M = args.price_input_hit_per_m
    PRICE_OUTPUT_PER_M = args.price_output_per_m
    CURRENCY = args.currency or "CNY"
    prompts_dir = args.prompts_dir or shared_prompts_dir(base)

    bugs_path = os.path.join(base, "bugs.json")
    if not os.path.exists(bugs_path):
        print("缺少 " + bugs_path + ", 请先运行 select_bugs.py", file=sys.stderr)
        return 1
    bugs = json.load(open(bugs_path))["bugs"]
    for sub in ("patches", "replies/completion", "replies/reasoning"):
        os.makedirs(os.path.join(base, sub), exist_ok=True)
    completion_dir = os.path.join(base, "replies", "completion")
    reasoning_dir = os.path.join(base, "replies", "reasoning")

    manifest_path = os.path.join(base, "patches.json")
    manifest = json.load(open(manifest_path)) if os.path.exists(manifest_path) else {}
    gen_stats: dict = json.load(open(os.path.join(base, "generation-stats.json")))["perBug"] \
        if os.path.exists(os.path.join(base, "generation-stats.json")) else {}
    print(f"共享 prompt 目录: {prompts_dir}   推理强度: {REASONING_EFFORT or '(未设置)'}")

    for i, bug in enumerate(bugs, 1):
        bid = bug["bugId"]
        patch_path = os.path.join(base, "patches", f"{bid}.patch")
        if (
            manifest.get(bid, {}).get("status") == "ok"
            and os.path.exists(patch_path)
            and os.path.getsize(patch_path) > 0
        ):
            print(f"[{i}/{len(bugs)}] {bid[:12]} 已生成, 跳过")
            continue

        # 共享 prompt: 已存在且含源码片段则直接复用, 保证所有模型 prompt 完全一致
        prompt_path = os.path.join(prompts_dir, f"{bid}.txt")
        prompt = None
        if os.path.exists(prompt_path):
            cached = open(prompt_path, encoding="utf-8").read()
            if SNIPPET_MARKER in cached:
                prompt = cached
                print(f"[{i}/{len(bugs)}] {bid[:12]} 复用共享 prompt")
        if prompt is None:
            snippets = load_snippets_cached(bug, prompts_dir)
            prompt = build_prompt(bug, snippets)
            os.makedirs(prompts_dir, exist_ok=True)
            with open(prompt_path, "w", encoding="utf-8") as fp:
                fp.write(prompt)

        patch = None
        reason = "未提取到 diff"
        total_elapsed = 0
        attempts_log = []
        last_usage = {}
        for attempt in range(1, RETRIES + 2):   # 至少尝试 1 次; RETRIES 为额外重试次数
            proc = run_model(prompt, label=f"{bid[:12]} 第{attempt}次")
            total_elapsed += proc.elapsed
            usage = getattr(proc, "usage", {})
            attempts_log.append({
                "attempt": attempt,
                "elapsedSeconds": proc.elapsed,
                "ok": proc.returncode == 0,
                "usage": usage,
                "cost": token_cost(usage),
            })

            combined = (proc.stdout or "") + chr(10) + (proc.stderr or "")
            reasoning = getattr(proc, "reasoning", "") or ""
            with open(os.path.join(completion_dir, f"{bid}.txt"), "w") as fp:
                fp.write(combined)
            if reasoning:
                with open(os.path.join(reasoning_dir, f"{bid}.txt"), "w") as fp:
                    fp.write(reasoning)
            if attempt > 1:
                # 每次尝试的原始回复留证
                with open(os.path.join(completion_dir, f"{bid}.att{attempt}.txt"), "w") as fp:
                    fp.write(combined)
                if reasoning:
                    with open(os.path.join(reasoning_dir, f"{bid}.att{attempt}.txt"), "w") as fp:
                        fp.write(reasoning)

            patch = extract_patch(combined)
            if patch:
                last_usage = getattr(proc, "usage", {})
                break

            reason = "未提取到 diff"
            if proc.returncode != 0:
                tail = ""
                if proc.stderr:
                    lines = proc.stderr.strip().splitlines()
                    if lines:
                        tail = " | " + lines[-1][:200]
                reason = f"模型调用失败{tail}"
            print(f"[{i}/{len(bugs)}] {bid[:12]} {reason} (第 {attempt}/{1 + RETRIES} 次)")
            if attempt < 1 + RETRIES:
                time.sleep(10)

        if patch:
            with open(patch_path, "w") as fp:
                fp.write(patch)
            manifest[bid] = {"status": "ok", "patch_file": patch_path}
            mm, ss = divmod(total_elapsed, 60)
            print(f"[{i}/{len(bugs)}] {bid[:12]} 生成成功 ({len(patch)} 字节, 累计耗时 {mm}m{ss:02d}s, 共尝试 {attempt} 次)")
        else:
            manifest[bid] = {"status": "failed", "reason": reason}
            print(f"[{i}/{len(bugs)}] {bid[:12]} 最终失败: {reason} (已尝试 {1 + RETRIES} 次, 跳过)")

        attempt_costs = [a.get("cost") for a in attempts_log if a.get("cost") is not None]
        entry = {
            "bugId": bid,
            "status": "ok" if patch else "failed",
            "attempts": attempt if patch else 1 + RETRIES,
            "retries": (attempt - 1) if patch else RETRIES,
            "durationSeconds": total_elapsed,
            "usage": last_usage,
            "cost": round(sum(attempt_costs), 8) if attempt_costs else None,
            "attemptsLog": attempts_log,
        }
        if not patch:
            entry["reason"] = reason
        gen_stats[bid] = entry

        with open(manifest_path, "w") as fp:
            json.dump(manifest, fp, indent=2, ensure_ascii=False)
        costs = [v.get("cost") for v in gen_stats.values() if v.get("cost") is not None]
        stats_report = {
            "model": MODEL,
            "provider": PROVIDER,
            "reasoningEffort": REASONING_EFFORT or None,
            "effortStyle": EFFORT_STYLE or PROVIDERS.get(PROVIDER, {}).get("defaultStyle"),
            "currency": CURRENCY,
            "pricing": {"inputPerM": PRICE_INPUT_PER_M,
                        "inputHitPerM": PRICE_INPUT_HIT_PER_M,
                        "outputPerM": PRICE_OUTPUT_PER_M},
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "perBug": gen_stats,
            "totals": {
                "cost": round(sum(costs), 8) if costs else None,
                "durationSeconds": sum((v.get("durationSeconds") or 0) for v in gen_stats.values()),
            },
        }
        with open(os.path.join(base, "generation-stats.json"), "w", encoding="utf-8") as fp:
            json.dump(stats_report, fp, indent=2, ensure_ascii=False)

    ok = sum(1 for v in manifest.values() if v.get("status") == "ok")
    print(f"完成: {ok}/{len(bugs)} 条生成成功 -> {manifest_path}")
    print(f"回复统计已写入: {os.path.join(base, 'generation-stats.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
