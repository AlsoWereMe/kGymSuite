"""随机从 kbench 数据集中挑选 N 条 bug，供批量实验使用。

用法:
  .venv/bin/python select_bugs.py --n 20 [--seed 42] [--out experiment/bugs.json]

输出 experiment/bugs.json: {"seed":..., "count":..., "bugs":[...]}
注意: 已剔除 ground-truth 字段 patch/patchMessage，防止泄漏给 LLM。
"""
import argparse
import json
import os
import random

POOL = [
    # 只从经典 kBenchSyz 主基准 (kb) 抽, 不用 kb-25 / kmsan
    "data/kbench/dataset-kb.json",
]
STRIP_FIELDS = ("patch", "patchMessage")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=20, help="挑选条数 (默认 20)")
    ap.add_argument("--seed", type=int, default=None, help="随机种子; 不传则真随机并打印种子")
    ap.add_argument("--out", default="experiment/bugs.json")
    args = ap.parse_args()

    pool = []
    for f in POOL:
        if not os.path.exists(f):
            print("跳过缺失的数据集:", f)
            continue
        for b in json.load(open(f)):
            b = dict(b)
            b["_dataset"] = f
            pool.append(b)

    if args.n > len(pool):
        print(f"池中只有 {len(pool)} 条, 少于 {args.n} 条", file=__import__("sys").stderr)
        return 1

    seed = args.seed if args.seed is not None else random.randrange(1 << 63)
    rng = random.Random(seed)
    chosen = rng.sample(pool, args.n)
    bugs = [{k: v for k, v in b.items() if k not in STRIP_FIELDS} for b in chosen]

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as fp:
        json.dump({"seed": seed, "count": len(bugs), "bugs": bugs}, fp, indent=2, ensure_ascii=False)

    print(f"seed={seed}, 已挑选 {len(bugs)} 条 -> {args.out}")
    for b in bugs:
        print(" ", b["bugId"][:12], "|", b["title"][:72])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
