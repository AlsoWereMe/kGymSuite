"""对每条选中的 bug, 用 OpenRouter 直连 API 单轮生成补丁并自动提取成 patch 文件。

流程: 构造 prompt(崩溃报告 + 可选坏内核源码片段) -> POST openrouter chat/completions
      -> 提取 diff 块; 提取失败自动重试 RETRIES 次, 仍失败则跳过该条。

输出 (--base 指定目录, 默认 experiment):
  prompts/<bugId>.txt       发给模型的完整 prompt
  replies/<bugId>.txt       模型原始回复 (含重试轮次 attN 存档)
  patches/<bugId>.patch     提取出的补丁
  patches.json              生成清单 (可断点续跑: 成功过的会跳过)

依赖环境变量:
  OPENROUTER_API_KEY   必填
  KGym_MODEL           模型 slug (如 deepseek/deepseek-v4-pro-0813), 也可用 --model 指定

用法:
  KGym_MODEL="deepseek/deepseek-v4-pro-0813" .venv/bin/python generate_patches.py --base experiment/deepseek-v4-pro-0813
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

BASE = "experiment"
BUGS_PATH = os.path.join(BASE, "bugs.json")

INCLUDE_SOURCE = True          # 是否把坏内核相关源码放进 prompt (显著提高 git apply 命中率)
SOURCE_MAX_FILES = 3           # 每个 bug 最多抓取的文件数
FETCH_TIMEOUT = 30             # 单文件抓取超时(秒)
MODEL_TIMEOUT = 2400           # 单条模型请求超时(秒)
RETRIES = 3                    # 提取不到 diff 时的额外重试次数 (共最多 1+3 次)
MODEL = os.environ.get("KGym_MODEL", "")
MAX_TOKENS = int(os.environ.get("KGym_MAX_TOKENS", "0") or 0)      # >0 时写入 max_tokens (应对推理模型输出被吃光)
REASONING_EFFORT = os.environ.get("KGym_REASONING_EFFORT", "")    # 如 low/high, 空 = 不设置

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

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


def openrouter_chat(prompt: str) -> str:
    """一次直连 OpenRouter 的 chat/completions 调用, 返回模型回复文本。"""
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError("环境变量 OPENROUTER_API_KEY 未设置")
    if not MODEL:
        raise RuntimeError("模型未指定: 请设置 KGym_MODEL 或用 --model")

    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
    }
    if MAX_TOKENS > 0:
        payload["max_tokens"] = MAX_TOKENS
    if REASONING_EFFORT:
        payload["reasoning"] = {"effort": REASONING_EFFORT}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": "kgym-exp",
            "X-Title": "kgym-exp",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=MODEL_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e
    except Exception as e:
        raise RuntimeError(f"请求失败: {e}") from e

    try:
        choice = data["choices"][0]
        content = choice.get("message", {}).get("content")
        if content:
            return content
        # 空回复: 把 finish_reason / usage / 原始响应带进错误信息, 便于定位
        raise RuntimeError(
            "模型返回空内容: finish_reason=" + str(choice.get("finish_reason"))
            + ", usage=" + json.dumps(data.get("usage", {}))
            + ", 原始响应=" + json.dumps(data)[:600]
        )
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError("响应无 choices: " + json.dumps(data)[:400]) from e


def run_model(prompt: str, label: str = ""):
    """在后台线程调用模型, 主线程输出旋转等待条(说明未卡死)。

    返回 SimpleNamespace(stdout=回复文本, stderr=错误文本, returncode, elapsed)。
    """
    result = {}

    def worker():
        try:
            result["reply"] = openrouter_chat(prompt)
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
            stdout="", stderr=str(result["error"]), returncode=1, elapsed=elapsed
        )
    reply = result.get("reply") or ""
    return types.SimpleNamespace(stdout=reply, stderr="", returncode=0, elapsed=elapsed)


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


def main() -> int:
    global MODEL
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE, help="实验目录 (默认 experiment)")
    ap.add_argument("--model", default=MODEL, help="模型 slug (默认取环境变量 KGym_MODEL)")
    args = ap.parse_args()
    base = args.base
    MODEL = args.model or ""

    bugs_path = os.path.join(base, "bugs.json")
    if not os.path.exists(bugs_path):
        print("缺少 " + bugs_path + ", 请先运行 select_bugs.py", file=sys.stderr)
        return 1
    bugs = json.load(open(bugs_path))["bugs"]
    for sub in ("prompts", "replies", "patches"):
        os.makedirs(os.path.join(base, sub), exist_ok=True)

    manifest_path = os.path.join(base, "patches.json")
    manifest = json.load(open(manifest_path)) if os.path.exists(manifest_path) else {}

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

        snippets = build_snippets(bug) if INCLUDE_SOURCE else ""
        prompt = build_prompt(bug, snippets)
        with open(os.path.join(base, "prompts", f"{bid}.txt"), "w") as fp:
            fp.write(prompt)

        patch = None
        reason = "未提取到 diff"
        for attempt in range(1, 1 + RETRIES):
            proc = run_model(prompt, label=f"{bid[:12]} 第{attempt}次")

            combined = (proc.stdout or "") + chr(10) + (proc.stderr or "")
            with open(os.path.join(base, "replies", f"{bid}.txt"), "w") as fp:
                fp.write(combined)
            if attempt > 1:
                # 每次尝试的原始回复留证
                with open(os.path.join(base, "replies", f"{bid}.att{attempt}.txt"), "w") as fp:
                    fp.write(combined)

            patch = extract_patch(combined)
            if patch:
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
            mm, ss = divmod(proc.elapsed, 60)
            print(f"[{i}/{len(bugs)}] {bid[:12]} 生成成功 ({len(patch)} 字节, 耗时 {mm}m{ss:02d}s, 共尝试 {attempt} 次)")
        else:
            manifest[bid] = {"status": "failed", "reason": reason}
            print(f"[{i}/{len(bugs)}] {bid[:12]} 最终失败: {reason} (已重试 {RETRIES} 次, 跳过)")

        with open(manifest_path, "w") as fp:
            json.dump(manifest, fp, indent=2, ensure_ascii=False)

    ok = sum(1 for v in manifest.values() if v.get("status") == "ok")
    print(f"完成: {ok}/{len(bugs)} 条生成成功 -> {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
