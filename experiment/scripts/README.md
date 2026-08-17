# experiment/scripts — 脚本说明与执行流程

目录结构:

    scripts/
    ├── apis/                     官方平台 API 调用脚本 (每个平台一个)
    │   ├── __init__.py             公共工具: post_json / base_reply / apply_effort / 加载器
    │   ├── llm_providers.py        API 分发器 (上层统一入口, 按 provider 名路由到各平台脚本)
    │   ├── dashscope.py            通义千问 (DASHSCOPE_API_KEY)
    │   ├── moonshot.py             Kimi (MOONSHOT_API_KEY)
    │   └── zhipu.py                智谱 GLM (ZHIPU_API_KEY)
    ├── pipeline/                  生成 / 评估 patch 流程脚本
    │   ├── select_bugs.py           随机挑 N 条 bug (剔除 ground-truth patch 防泄漏)
    │   ├── generate_patches.py      直连官方平台生成补丁 (共享 prompt + 源码片段缓存)
    │   ├── job_submit.py            构造/提交单个 kGym job (被 run_experiment 复用)
    │   ├── run_experiment.py        批量提交评估 + 全部结束后自动跑统计
    │   ├── analyze_results.py       统计 abort/修复成败占比、失败原因、花费/耗时/重试
    │   └── run_models.py            批量司机 (选 bug → 生成 → 评估 → 统计, 逐模型串行)
    └── README.md

## 流水线总览

```
data/kbench/dataset-kb.json (279条, 含 ground-truth patch)
        │ pipeline/select_bugs.py      随机挑 N 条, 剔除 patch/patchMessage 防泄漏
        ▼
experiment/shared-bugs.json     {"seed","count","bugs":[...]}
        │ pipeline/generate_patches.py  直连官方平台 (apis/), prompt 写到共享目录
        ▼
experiment/prompts/<bugId>.txt  实验根目录共享, 所有模型复用同一份 prompt
experiment/<model>/patches/     每个模型目录存各自的回复/补丁/结果
        │ pipeline/run_experiment.py    构造 job (import pipeline/job_submit) → 提交 :8000
        ▼
experiment/<model>/results.json  + results/<bugId>.json (完整 JobContext)
        │ pipeline/analyze_results.py   (run_experiment 跑完后自动调用)
        ▼
experiment/<model>/stats.json    abort/修复成败占比 + 失败原因 + 花费/耗时/重试
```

```
pipeline/run_models.py (批量司机)
  ├─ 首次运行调 pipeline/select_bugs.py → shared-bugs.json (各模型共用同一批, 保证可比)
  └─ 每个模型目录子进程依次跑 pipeline/generate_patches.py → pipeline/run_experiment.py
       (通过 --provider/--api-key-env/--reasoning-effort/--price-* 指定平台与计价)
```

## 脚本清单

| 脚本 | 输入 | 输出 | 依赖 |
|---|---|---|---|
| `pipeline/select_bugs.py` | 数据集 JSON | <out> bugs.json | 标准库 |
| `pipeline/generate_patches.py` | <base>/bugs.json + 官方平台 key | 共享 prompts/ + <base>/{replies,patches,generation-stats.json} | apis/ |
| `pipeline/job_submit.py` | 数据集 JSON(精确匹配 40 位 bugId) + 可选 patch 文件 | 提交单个 job | KBDr.kclient |
| `pipeline/run_experiment.py` | <base>/{bugs,patches}.json | <base>/{results,inflight,stats}.json | KBDr.kclient + analyze_results |
| `pipeline/analyze_results.py` | <base>/{results,generation-stats}.json | <base>/stats.json | 标准库 |
API 平台 (apis/ 每个平台一个脚本, 统一接口 chat_completion(...)):

| 平台 | 脚本 | key 环境变量 | 推理强度线格式 |
|---|---|---|---|
| 通义千问 | apis/dashscope.py | DASHSCOPE_API_KEY | reasoning_effort (max → xhigh) |
| Kimi | apis/moonshot.py | MOONSHOT_API_KEY | 顶层 reasoning_effort (K3: low/high/max) |
| 智谱 GLM | apis/zhipu.py | ZHIPU_API_KEY | thinking: {type} + reasoning_effort (GLM-5.2+) |
