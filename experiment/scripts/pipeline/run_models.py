"""按模型批量跑 kbench 评测: 每个模型一个独立文件夹, 自动串起选 bug / 生成补丁 / 提交评估 / 统计。

API: 不再使用 OpenRouter, 每个模型直连其官方平台 (见 apis/llm_providers.py):
  qwen3.8-max -> dashscope (通义千问, DASHSCOPE_API_KEY, reasoning_effort, max->xhigh)
  kimi-k3     -> moonshot  (Kimi, MOONSHOT_API_KEY, 顶层 reasoning_effort, low/high/max)
  glm-5.2     -> zhipu     (智谱 GLM, ZHIPU_API_KEY, thinking + reasoning_effort)

流程:
  1. 首次运行时随机挑选 N 条 bug 存 experiment/shared-bugs.json
     (seed 记录在内; 所有模型共用同一批 bug, 保证结果可比)
  2. 对每个模型(顺序执行):
     - 建 experiment/<folder>/{replies,patches,results}, 复制 bugs.json 进去
     - 运行 generate_patches.py --base <folder> --provider <官方平台>
       (prompt 写入共享目录 experiment/prompts, 所有模型复用同一份 prompt
        与源码片段缓存, 保证输入完全一致)
     - 运行 run_experiment.py --base <folder> 提交评估
       (全部 job 跑完后自动运行 analyze_results.py 产出 stats.json,
        含 abort/修复成败占比、失败原因、每条回复的花费/耗时/重试次数)
  3. 失败/中断不丢已完成产物, 重跑自动跳过已完成步骤

依赖环境变量 (按模型对应):
  DASHSCOPE_API_KEY / MOONSHOT_API_KEY / ZHIPU_API_KEY

用法:
  python experiment/scripts/run_models.py                  # 按 MODELS 列表顺序跑
  python experiment/scripts/run_models.py --only glm-5.2   # 只跑某个模型(文件夹名)
  python experiment/scripts/run_models.py --skip-gen       # 跳过补丁生成, 直接提交评估
  python experiment/scripts/run_models.py --skip-eval      # 只生成补丁, 不提交
  python experiment/scripts/run_models.py --n 10 --reselect   # 重新随机选一批 bug
  python experiment/scripts/run_models.py --dry-run        # 只打印计划, 不执行
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

# 每个模型单独配置: 官方平台 / key 环境变量 / 推理强度 / effort 线格式 / 单价
# effortStyle 留空 = 用 apis/llm_providers.py 里该平台的默认线格式
# pricing: 用于 stats.json 算钱 (官方平台响应不含 cost, 按 token 数 × 单价换算,
#          思考 token 计入输出; 单位: 元/百万 token)
#   inputPerM    = 输入价格(缓存未命中)
#   inputHitPerM = 输入价格(缓存命中, 重试同一 prompt 时第二次起生效)
#   outputPerM   = 输出价格
# 价格来源: 用户提供的官方定价页报价 (2026-08)
MODELS = [
    {"slug": "qwen3.8-max", "provider": "dashscope", "apiKeyEnv": "DASHSCOPE_API_KEY",
     "reasoningEffort": "high", "effortStyle": "",
     "pricing": {"currency": "CNY", "inputPerM": 12, "inputHitPerM": 1.5, "outputPerM": 36}},
    {"slug": "kimi-k3", "provider": "moonshot", "apiKeyEnv": "MOONSHOT_API_KEY",
     "reasoningEffort": "high", "effortStyle": "",   # K3 用顶层 reasoning_effort (平台默认线格式)
     "pricing": {"currency": "CNY", "inputPerM": 20, "inputHitPerM": 2, "outputPerM": 100}},
    # glm-5.2 暂不可用 (官方账号实名认证未完成), 恢复后取消注释并填 pricing
    # {"slug": "glm-5.2", "provider": "zhipu", "apiKeyEnv": "ZHIPU_API_KEY",
    #  "reasoningEffort": "high", "effortStyle": "",   # GLM-5.2 默认 thinking + reasoning_effort
    #  "pricing": {"currency": "CNY", "inputPerM": None, "outputPerM": None}},
]
DEFAULT_REASONING_EFFORT = "high"
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))      # experiment/scripts/pipeline
EXPERIMENT = os.path.dirname(os.path.dirname(SCRIPTS_DIR))    # experiment 根目录
SHARED_BUGS = os.path.join(EXPERIMENT, "shared-bugs.json")
PY = [sys.executable]


def folder_of(slug: str) -> str:
    """qwen3.8-max -> qwen3.8-max (目录名与 slug 一致)"""
    return slug.rsplit("/", 1)[-1]


def model_of(slug: str, default_effort: str) -> dict:
    """按 slug 找 MODELS 里的配置; 未配置的 slug 默认走 dashscope。"""
    for m in MODELS:
        if m["slug"] == slug:
            return m
    return {"slug": slug, "provider": "dashscope", "apiKeyEnv": "DASHSCOPE_API_KEY",
            "reasoningEffort": default_effort, "effortStyle": ""}


def select_bugs(n: int, seed, reselect: bool, dry: bool):
    if os.path.exists(SHARED_BUGS) and not reselect:
        data = json.load(open(SHARED_BUGS))
        print(f"复用已有 bug 集: {SHARED_BUGS} (seed={data.get('seed')}, {data.get('count')} 条)")
        return
    cmd = PY + [os.path.join(SCRIPTS_DIR, "select_bugs.py"), "--n", str(n)]
    if seed is not None:
        cmd += ["--seed", str(seed)]
    cmd += ["--out", SHARED_BUGS]
    if dry:
        print("[dry-run] 执行:", " ".join(cmd))
        return
    subprocess.run(cmd, check=True)


def run_one(m: dict, folder: str, skip_gen: bool, skip_eval: bool, dry: bool):
    slug = m["slug"]
    provider = m.get("provider") or "dashscope"
    key_env = m.get("apiKeyEnv") or ""
    effort = m.get("reasoningEffort") or DEFAULT_REASONING_EFFORT
    style = m.get("effortStyle") or ""
    fdir = os.path.join(EXPERIMENT, folder)
    if not dry:
        for sub in ("replies", "patches", "results"):
            os.makedirs(os.path.join(fdir, sub), exist_ok=True)
        shutil.copy(SHARED_BUGS, os.path.join(fdir, "bugs.json"))

    steps = []
    if not skip_gen:
        gen_cmd = (PY + [os.path.join(SCRIPTS_DIR, "generate_patches.py"),
                         "--base", fdir, "--model", slug,
                         "--provider", provider, "--reasoning-effort", effort])
        if key_env:
            gen_cmd += ["--api-key-env", key_env]
        if style:
            gen_cmd += ["--effort-style", style]
        pricing = m.get("pricing") or {}
        if pricing.get("inputPerM") is not None:
            gen_cmd += ["--price-input-per-m", str(pricing["inputPerM"])]
        if pricing.get("inputHitPerM") is not None:
            gen_cmd += ["--price-input-hit-per-m", str(pricing["inputHitPerM"])]
        if pricing.get("outputPerM") is not None:
            gen_cmd += ["--price-output-per-m", str(pricing["outputPerM"])]
        if pricing.get("currency"):
            gen_cmd += ["--currency", pricing["currency"]]
        steps.append(gen_cmd)
    if not skip_eval:
        # run_experiment.py 会在全部 job 结束后自动运行 analyze_results.py
        steps.append(PY + [os.path.join(SCRIPTS_DIR, "run_experiment.py"), "--base", fdir])

    for cmd in steps:
        env = dict(os.environ)
        env["KGym_MODEL"] = slug
        env["KGym_PROVIDER"] = provider
        if dry:
            print("[dry-run] 执行:", " ".join(cmd), f"(env KGym_MODEL={slug} KGym_PROVIDER={provider})")
        else:
            rc = subprocess.run(cmd, check=False, env=env).returncode
            print(f"子流程返回码: {rc}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default=None, help="逗号分隔的模型 slug, 覆盖内置 MODELS")
    ap.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT,
                    help="覆盖所有模型的推理强度")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--reselect", action="store_true", help="重新随机选 bug")
    ap.add_argument("--skip-gen", action="store_true")
    ap.add_argument("--skip-eval", action="store_true")
    ap.add_argument("--only", default=None, help="只跑该文件夹名的模型")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划不执行")
    args = ap.parse_args()

    if args.models:
        models = [model_of(s.strip(), args.reasoning_effort)
                  for s in args.models.split(",") if s.strip()]
    else:
        models = [dict(m, reasoningEffort=args.reasoning_effort or m.get("reasoningEffort") or DEFAULT_REASONING_EFFORT)
                  for m in MODELS]
    if args.only:
        key = args.only.lower()
        models = [m for m in models if key in m["slug"].lower()]
        if not models:
            print(f"--only {args.only} 不匹配任何模型", file=sys.stderr)
            return 1

    os.makedirs(EXPERIMENT, exist_ok=True)
    select_bugs(args.n, args.seed, args.reselect, args.dry_run)

    print("模型顺序:", [(m["slug"], m["provider"], m["reasoningEffort"]) for m in models])
    for m in models:
        folder = folder_of(m["slug"])
        print(f"\n===== 模型 {m['slug']} ({m['provider']}) -> {EXPERIMENT}/{folder} "
              f"(推理强度 {m['reasoningEffort']}) =====")
        run_one(m, folder, args.skip_gen, args.skip_eval, args.dry_run)

    print("\n全部模型处理完成。汇总:")
    for m in models:
        f = os.path.join(EXPERIMENT, folder_of(m["slug"]))
        stats = os.path.join(f, "stats.json")
        results = os.path.join(f, "results.json")
        line = f"  {folder_of(m['slug'])}:"
        line += f" stats.json(有统计)" if os.path.exists(stats) else f" results.json(有结果)" if os.path.exists(results) else " (尚无结果)"
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
