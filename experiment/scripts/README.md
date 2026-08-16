# experiment/scripts — 脚本说明与执行流程

五个脚本构成一条 LLM 内核补丁评估流水线：**选 bug → 生成补丁 → 提交评估**。
顶层 `run_models.py` 做批量编排，其余脚本均可独立使用。

## 流水线总览

```
data/kbench/dataset-kb.json (279条, 含 ground-truth patch)
        │ select_bugs.py        随机挑 N 条, 剔除 patch/patchMessage 防泄漏
        ▼
<base>/bugs.json          {"seed","count","bugs":[...]}
        │ generate_patches.py   OpenRouter 单轮对话生成 diff
        ▼
<base>/patches/*.patch    <base>/patches.json  {"<bugId>": {"status":"ok","patch_file":...}}
        │ run_experiment.py     构造 job (import job_submit) → 提交 :8000 → 轮询汇总
        ▼
<base>/results.json       <base>/results/<bugId>.json (完整 JobContext)
```

```
run_models.py (批量司机)
  ├─ 首次运行调 select_bugs.py → shared-bugs.json (各模型共用同一批, 保证可比)
  └─ 每个模型目录子进程依次跑 generate_patches.py → run_experiment.py
       (通过环境变量 KGym_MODEL 指定模型)
```

## 脚本清单

| 脚本 | 输入 | 输出 | 依赖 |
|---|---|---|---|
| `select_bugs.py` | 数据集 JSON | `<out>` bugs.json | 标准库 |
| `generate_patches.py` | `<base>/bugs.json` + `OPENROUTER_API_KEY` + `KGym_MODEL` | `prompts/` `replies/` `patches/` `patches.json` | 标准库 |
| `job_submit.py` | 数据集 JSON(精确匹配 40 位 bugId) + 可选 patch 文件 | 提交单个 job, 打印 `JOB_ID` | `KBDr.kclient` |
| `run_experiment.py` | `<base>/bugs.json` + `patches.json` | `results.json` `results/<bugId>.json` `inflight.json` | `KBDr.kclient` + import `job_submit` |
| `run_models.py` | `shared-bugs.json` + `OPENROUTER_API_KEY` | 每模型一个实验目录 | 标准库(子进程调其它脚本) |

耦合方式：`run_models.py` 通过**子进程**调用其余脚本；`run_experiment.py` 通过 **import** 复用
`job_submit.build_request`；其余衔接全靠 **bugs.json / patches.json / results.json 文件契约**。

评估阶段**串行执行**：`run_experiment.py` 内 `MAX_PARALLEL=1`，同一时刻只跑 1 个 job（机器资源有限时避免同时编译/起 VM）。

## 执行流程

### 1. 批量评估（默认用法）

```bash
# 需要: OPENROUTER_API_KEY 已导出
.venv/bin/python experiment/scripts/run_models.py              # 按 MODELS 列表顺序跑全部模型
.venv/bin/python experiment/scripts/run_models.py --only qwen3.8-max   # 只跑某个模型(文件夹名/slug/前缀)
.venv/bin/python experiment/scripts/run_models.py --dry-run    # 只打印计划
```

其它选项：`--n 10 --reselect` 重新随机选 bug；`--skip-gen` / `--skip-eval` 跳过某阶段。

### 2. 单条 bug 评估

已有补丁 → 直接提交：

```bash
.venv/bin/python experiment/scripts/job_submit.py \
  --bug-id <40位完整ID> \
  --dataset-json data/kbench/dataset-kb.json \
  --patch-file experiment/<模型>/patches/<bugId>.patch
```

没有补丁 → 构造"只含一条"的 bugs.json，走完整三步：

```bash
# ① 从数据集挑出目标 bug 写成单条 bugs.json (剔除 ground-truth 字段)
# ② 生成补丁
OPENROUTER_API_KEY=... .venv/bin/python experiment/scripts/generate_patches.py \
  --base experiment/<目录> --model qwen/qwen3.8-max
# ③ 提交评估 (本地 API, 自动轮询)
.venv/bin/python experiment/scripts/run_experiment.py --base experiment/<目录>
```

`job_submit.py` 不带 `--patch-file` 即对照实验（坏内核上复现崩溃，用于验证环境/可复现性）。
本机 QEMU 可复现子集见仓库根 `misc/reproducible-bugs-on-qemu.json`。

## 断点续跑

- `generate_patches.py`：`patches.json` 中 `status=ok` 的跳过，重跑只补失败项；
- `run_experiment.py`：`results.json` 中已 `finished/aborted` 的跳过；Ctrl+C 按 `inflight.json` 批量 abort 在跑 job；
- `run_models.py`：`shared-bugs.json` 存在即复用，保证各模型同一批 bug。

注意：`run_experiment.py` 要求 `results.json` 为合法 JSON（空文件会崩溃，续跑前先写 `{}`）。

## 结果判定

`results.json` 每条的 `evaluation`：`notReproduced` = 补丁后崩溃消失(**修复成功**)；
`reproduced` = 崩溃仍可复现(失败)。`exceptions` 非空或 `imageAbility` 异常属工程/环境失败，不计入模型分。
