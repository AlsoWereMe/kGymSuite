"""按模型批量跑 kbench 评测: 每个模型一个独立文件夹, 自动串起选 bug / 生成补丁 / 提交评估。

流程:
  1. 首次运行时随机挑选 N 条 bug 存 experiment/shared-bugs.json
     (seed 记录在内; 所有模型共用同一批 bug, 保证结果可比)
  2. 对每个模型(顺序执行):
     - 建 experiment/<folder>/{prompts,replies,patches,results}, 复制 bugs.json 进去
     - 通过 KGym_MODEL 环境变量指定模型, 运行 generate_patches.py --base <folder>
       (直连 OpenRouter chat/completions, 单轮对话, 不再经过 codex)
     - 运行 run_experiment.py --base <folder> 提交评估
  3. 失败/中断不丢已完成产物, 重跑自动跳过已完成步骤

依赖: 环境变量 OPENROUTER_API_KEY

用法:
  python experiment/scripts/run_models.py                  # 按 MODELS 列表顺序跑
  python experiment/scripts/run_models.py --only glm-5.2   # 只跑某个模型(文件夹名)
  python experiment/scripts/run_models.py --skip-gen       # 跳过补丁生成, 直接提交评估
  python experiment/scripts/run_models.py --skip-eval      # 只生成补丁, 不提交
  python experiment/scripts/run_models.py --n 20 --reselect   # 重新随机选一批 bug
  python experiment/scripts/run_models.py --dry-run        # 只打印计划, 不执行
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

MODELS = [
    "qwen/qwen3.8-max",
    "moonshotai/kimi-k3",
    "z-ai/glm-5.2",
    "deepseek/deepseek-v4-pro-0813",
]
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))   # experiment/scripts
EXPERIMENT = os.path.dirname(SCRIPTS_DIR)                  # experiment 根目录
SHARED_BUGS = os.path.join(EXPERIMENT, "shared-bugs.json")
PY = [sys.executable]


def folder_of(model: str) -> str:
    """deepseek/deepseek-v4-pro-0813 -> deepseek-v4-pro-0813"""
    return model.rsplit("/", 1)[-1]


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


def run_one(model: str, folder: str, skip_gen: bool, skip_eval: bool, dry: bool):
    fdir = os.path.join(EXPERIMENT, folder)
    if not dry:
        for sub in ("prompts", "replies", "patches", "results"):
            os.makedirs(os.path.join(fdir, sub), exist_ok=True)
        shutil.copy(SHARED_BUGS, os.path.join(fdir, "bugs.json"))

    steps = []
    if not skip_gen:
        steps.append(
            PY + [os.path.join(SCRIPTS_DIR, "generate_patches.py"), "--base", fdir, "--model", model]
        )
    if not skip_eval:
        steps.append(PY + [os.path.join(SCRIPTS_DIR, "run_experiment.py"), "--base", fdir])

    for cmd in steps:
        env = dict(os.environ)
        env["KGym_MODEL"] = model
        if dry:
            print("[dry-run] 执行:", " ".join(cmd), f"(env KGym_MODEL={model})")
        else:
            rc = subprocess.run(cmd, check=False, env=env).returncode
            print(f"子流程返回码: {rc}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default=",".join(MODELS), help="逗号分隔的模型列表(OpenRouter slug)")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--reselect", action="store_true", help="重新随机选 bug")
    ap.add_argument("--skip-gen", action="store_true")
    ap.add_argument("--skip-eval", action="store_true")
    ap.add_argument("--only", default=None, help="只跑该文件夹名的模型")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划不执行")
    args = ap.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if args.only:
        key = args.only.lower()
        # 允许用 文件夹名(qwen3.8-max) / 完整 slug(qwen/qwen3.8-max) / 前缀(qwen) 匹配
        models = [m for m in models if key in m.lower()]
        if not models:
            print(f"--only {args.only} 不匹配任何模型", file=sys.stderr)
            return 1

    os.makedirs(EXPERIMENT, exist_ok=True)
    select_bugs(args.n, args.seed, args.reselect, args.dry_run)

    print("模型顺序:", [folder_of(m) for m in models])
    for model in models:
        folder = folder_of(model)
        print(f"\n===== 模型 {model} -> {EXPERIMENT}/{folder} =====")
        run_one(model, folder, args.skip_gen, args.skip_eval, args.dry_run)

    print("\n全部模型处理完成。汇总:")
    for m in models:
        f = os.path.join(EXPERIMENT, folder_of(m), "results.json")
        print(f"  {folder_of(m)}: {f + ' (有结果)' if os.path.exists(f) else f + ' (尚无结果)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
