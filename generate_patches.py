"""对每条选中的 bug，用 codex(DeepSeek) 单轮生成补丁并自动提取成 patch 文件。

流程: 构造 prompt(崩溃报告 + 可选坏内核源码片段) -> codex exec 一次调用 -> 提取 diff 块
输出:
  experiment/prompts/<bugId>.txt      发给模型的完整 prompt
  experiment/codex-out/<bugId>.txt    codex 原始输出
  experiment/patches/<bugId>.patch    提取出的补丁
  experiment/patches.json             生成清单 (可断点续跑: 成功过的会跳过)

用法:
  .venv/bin/python generate_patches.py
"""
import json
import os
import re
import subprocess
import sys
import urllib.request

BASE = "experiment"
BUGS_PATH = os.path.join(BASE, "bugs.json")

INCLUDE_SOURCE = True          # 是否把坏内核相关源码放进 prompt (显著提高 git apply 命中率)
SOURCE_MAX_FILES = 3           # 每个 bug 最多抓取的文件数
FETCH_TIMEOUT = 30             # 单文件抓取超时(秒)
CODEX_TIMEOUT = 2400           # 单条 codex 生成超时(秒)
MODEL = os.environ.get("CODEX_MODEL", "")   # 留空则用 ~/.codex/config.toml 默认模型

FENCE = chr(96) * 3            # 三个反引号, 避免源码里出现转义麻烦

INTERESTING_DIRS = (
    "block/", "drivers/", "fs/", "kernel/", "mm/", "net/", "lib/",
    "include/", "sound/", "security/", "ipc/", "virt/", "arch/",
)


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


def run_codex(prompt: str):
    cmd = ["codex", "exec", "-s", "read-only", "--skip-git-repo-check"]
    if MODEL:
        cmd += ["-m", MODEL]
    cmd.append("-")
    return subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=CODEX_TIMEOUT)


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
    if not os.path.exists(BUGS_PATH):
        print("缺少 " + BUGS_PATH + ", 请先运行 select_bugs.py", file=sys.stderr)
        return 1
    bugs = json.load(open(BUGS_PATH))["bugs"]
    for sub in ("prompts", "codex-out", "patches"):
        os.makedirs(os.path.join(BASE, sub), exist_ok=True)

    manifest_path = os.path.join(BASE, "patches.json")
    manifest = json.load(open(manifest_path)) if os.path.exists(manifest_path) else {}

    for i, bug in enumerate(bugs, 1):
        bid = bug["bugId"]
        patch_path = os.path.join(BASE, "patches", f"{bid}.patch")
        if (
            manifest.get(bid, {}).get("status") == "ok"
            and os.path.exists(patch_path)
            and os.path.getsize(patch_path) > 0
        ):
            print(f"[{i}/{len(bugs)}] {bid[:12]} 已生成, 跳过")
            continue

        snippets = build_snippets(bug) if INCLUDE_SOURCE else ""
        prompt = build_prompt(bug, snippets)
        with open(os.path.join(BASE, "prompts", f"{bid}.txt"), "w") as fp:
            fp.write(prompt)

        try:
            proc = run_codex(prompt)
        except subprocess.TimeoutExpired:
            manifest[bid] = {"status": "failed", "reason": "codex timeout"}
            print(f"[{i}/{len(bugs)}] {bid[:12]} codex 超时")
        else:
            with open(os.path.join(BASE, "codex-out", f"{bid}.txt"), "w") as fp:
                fp.write(proc.stdout or "")
            patch = extract_patch(proc.stdout or "")
            if patch:
                with open(patch_path, "w") as fp:
                    fp.write(patch)
                manifest[bid] = {"status": "ok", "patch_file": patch_path}
                print(f"[{i}/{len(bugs)}] {bid[:12]} 生成成功 ({len(patch)} 字节)")
            else:
                manifest[bid] = {"status": "failed", "reason": "未提取到 diff, 见 codex-out"}
                print(f"[{i}/{len(bugs)}] {bid[:12]} 输出无 diff")

        with open(manifest_path, "w") as fp:
            json.dump(manifest, fp, indent=2, ensure_ascii=False)

    ok = sum(1 for v in manifest.values() if v.get("status") == "ok")
    print(f"完成: {ok}/{len(bugs)} 条生成成功 -> {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
