"""统计 kGym 实验任务的 abort / crash修复失败 / crash修复成功 占比, 并记录每个失败任务的原因。

数据来源 (实验目录 --base, 默认 experiment/qwen3.8-max):
  1. results.json           任务汇总: status / exceptions / compilationTime /
                            imageAbility / crashes / evaluation
  2. results/<bugId>.json   每个任务的完整 JobContext,
                            失败原因取自 workerResult.jobException.{code, content}
  3. bugs.json              题目元信息 (title 等)

分类口径 (与 run_experiment.py 的 summarize() 一致):
  - abort             : status == "aborted"          -> 任务被中止 (补丁无法应用 / 编译失败等)
  - crash_fix_failed  : status == "finished" 且 evaluation == "reproduced"
                        -> 应用模型补丁后原始 crash 仍能复现, 视为修复失败
  - crash_fix_success : status == "finished" 且 evaluation == "notReproduced"
                        -> 应用模型补丁后 crash 未复现, 视为修复成功
  - other             : timeout / submit-error / 无 VM 评估结果等其他情况
  占比分母 = 参与统计的全部任务数 (三类互斥, 之和为 100%)。

可选 --db: 传入 scheduler.db 路径, 与 jobDigest/jobTag 表交叉校验 dashboard 记录。

输出: <base>/stats.json (summary + 任务明细 + 每个失败任务的原因)
  额外合并 <base>/generation-stats.json (每条回复的花费/耗时/重试次数)
  与 results/<bugId>.json 中的 job 创建/结束时间 (jobDurationSeconds)。

用法:
  .venv/bin/python analyze_results.py --base experiment/qwen3.8-max
  .venv/bin/python analyze_results.py --base experiment/qwen3.8-max \
      --db deployment/local/kscheduler-db/scheduler.db
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

# 分类名
CLS_ABORT = "aborted"
CLS_FIX_FAILED = "crash_fix_failed"
CLS_FIX_SUCCESS = "crash_fix_success"
CLS_OTHER = "other"

# 需要记录失败原因的分类
FAILED_CLASSES = (CLS_ABORT, CLS_FIX_FAILED, CLS_OTHER)


def pct(count: int, total: int) -> float:
    return round(count * 100.0 / total, 2) if total else 0.0


def load_bugs(base: str) -> dict:
    """bugId -> bug 元信息 (title 等)。"""
    path = os.path.join(base, "bugs.json")
    if not os.path.exists(path):
        return {}
    data = json.load(open(path, encoding="utf-8"))
    bugs = data.get("bugs", data) if isinstance(data, dict) else data
    return {b["bugId"]: b for b in bugs} if isinstance(bugs, list) else {}


def load_job_times(base: str, bug_id: str) -> dict:
    """从 results/<bugId>.json 提取 job 的创建/结束时间与耗时(秒)。"""
    out = {"createdTime": None, "modifiedTime": None, "jobDurationSeconds": None}
    path = os.path.join(base, "results", f"{bug_id}.json")
    if not os.path.exists(path):
        return out
    try:
        data = json.load(open(path, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return out
    out["createdTime"] = data.get("createdTime")
    out["modifiedTime"] = data.get("modifiedTime")
    try:
        c = datetime.fromisoformat(str(data["createdTime"]).replace("Z", "+00:00"))
        m = datetime.fromisoformat(str(data["modifiedTime"]).replace("Z", "+00:00"))
        out["jobDurationSeconds"] = round((m - c).total_seconds(), 1)
    except (KeyError, TypeError, ValueError):
        pass
    return out


def classify(entry: dict) -> str:
    status = entry.get("status")
    if status == "aborted":
        return CLS_ABORT
    if status == "finished":
        evaluation = entry.get("evaluation")
        if evaluation == "reproduced":
            return CLS_FIX_FAILED
        if evaluation == "notReproduced":
            return CLS_FIX_SUCCESS
        return CLS_OTHER  # finished 但没有 VM 评估结果
    return CLS_OTHER      # timeout / submit-error / 其他


def extract_failure_reason(base: str, bug_id: str, entry: dict, cls: str) -> dict:
    """提取该失败任务的原因。abort 时优先读完整 JobContext 的 jobException。"""
    if cls == CLS_ABORT:
        detail_path = os.path.join(base, "results", f"{bug_id}.json")
        if os.path.exists(detail_path):
            try:
                ctx = json.load(open(detail_path, encoding="utf-8"))
                for w in ctx.get("jobWorkers", []):
                    je = (w.get("workerResult") or {}).get("jobException")
                    if je:
                        tail = [ln for ln in je.get("traceback", "").splitlines() if ln.strip()]
                        return {
                            "code": je.get("code"),
                            "content": je.get("content"),
                            "tracebackTail": tail[-6:],
                        }
            except (json.JSONDecodeError, OSError) as e:
                return {"code": "results.readError", "content": str(e)}
        excs = entry.get("exceptions") or []
        return {
            "code": excs[0] if excs else "unknown",
            "content": f"任务被中止, 异常: {excs if excs else '未记录'}",
        }
    if cls == CLS_FIX_FAILED:
        real = [c for c in (entry.get("crashes") or []) if c.get("crashType") == "crash"]
        titles = [c.get("title") or "(无标题)" for c in real]
        return {
            "code": "evaluation.reproduced",
            "content": "应用模型补丁后原始 crash 仍复现"
                       + (f": {titles}" if titles else " (未记录 crash 标题)"),
            "reproducedCrashes": real,
        }
    # other: timeout / submit-error / finished 但无 VM 评估
    status = entry.get("status")
    if status == "timeout":
        return {"code": "timeout", "content": "任务超过单 job 时限被 abort"}
    if status == "submit-error":
        return {"code": "submit-error", "content": entry.get("error", "提交失败")}
    if status == "finished":
        return {"code": "evaluation.missing",
                "content": "任务 finished 但缺少 VM 评估结果 (无 evaluation 字段)"}
    return {"code": status or "unknown", "content": f"未识别的结束状态: {status}"}


def db_cross_check(db_path: str, entries: dict) -> dict:
    """与 scheduler.db 的 jobDigest/jobTag 交叉校验 dashboard 记录。

    直接按 results.json 里的 jobId 比对 jobDigest, 不再用 bugId 反查 jobTag:
    同一 bug 会被多次提交 (不同模型/重跑), jobTag 会累积多行, 旧实现用
    dict 去重会取到最后一条, 拿错 job 造成假不一致。
    同时校验该 job 上的 bugId 标签与 results.json 一致。
    jobId 统一按 JobId 语义转成 int (results.json 存的是 8 位十六进制字符串)。
    """
    out = {"db": db_path, "matched": 0, "mismatches": [], "error": None}
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT jobId, status FROM jobDigest")
        digest = {int(r["jobId"]): r["status"] for r in cur.fetchall()}
        cur.execute("SELECT jobId, tagValue FROM jobTag WHERE tagKey='bugId'")
        tag_rows = list(cur.fetchall())
        con.close()
    except sqlite3.Error as e:
        out["error"] = str(e)
        return out

    tags: dict[int, list[str]] = {}
    for r in tag_rows:
        tags.setdefault(int(r["jobId"]), []).append(r["tagValue"])

    for bug_id, entry in entries.items():
        jid = entry.get("jobId")
        try:
            key = int(jid, 16) if isinstance(jid, str) else int(jid)
        except (TypeError, ValueError):
            out["mismatches"].append({"bugId": bug_id, "jobId": jid,
                                      "resultsStatus": entry.get("status"),
                                      "dbStatus": "bad-jobId"})
            continue
        db_status = digest.get(key)
        if db_status is None:
            out["mismatches"].append({"bugId": bug_id, "jobId": jid,
                                      "resultsStatus": entry.get("status"),
                                      "dbStatus": "missing"})
        elif bug_id not in tags.get(key, []):
            out["mismatches"].append({"bugId": bug_id, "jobId": jid,
                                      "resultsStatus": entry.get("status"),
                                      "dbStatus": db_status,
                                      "taggedBugIds": tags.get(key, [])})
        elif db_status != entry.get("status"):
            out["mismatches"].append({"bugId": bug_id, "jobId": jid,
                                      "resultsStatus": entry.get("status"),
                                      "dbStatus": db_status})
        else:
            out["matched"] += 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="统计 kGym 实验任务的 abort/修复失败/修复成功占比")
    ap.add_argument("--base", default="experiment/qwen3.8-max",
                    help="实验目录 (默认 experiment/qwen3.8-max)")
    ap.add_argument("--db", default=None,
                    help="可选: scheduler.db 路径, 用于与 dashboard 后台库交叉校验")
    ap.add_argument("--out", default=None,
                    help="输出 JSON 路径 (默认 <base>/stats.json)")
    ap.add_argument("--price-input-per-m", type=float, default=None,
                    help="补算花费: 输入单价-缓存未命中(元/百万 token), 按 generation-stats 里的 token 数重算")
    ap.add_argument("--price-input-hit-per-m", type=float, default=None,
                    help="补算花费: 输入单价-缓存命中(元/百万 token)")
    ap.add_argument("--price-output-per-m", type=float, default=None,
                    help="补算花费: 输出单价(元/百万 token)")
    ap.add_argument("--currency", default=None,
                    help="货币单位 (默认取 generation-stats.json 里记录的)")
    args = ap.parse_args()

    base = args.base
    results_path = os.path.join(base, "results.json")
    if not os.path.exists(results_path):
        print(f"找不到 {results_path}", file=sys.stderr)
        return 1
    entries = json.load(open(results_path, encoding="utf-8"))
    bugs = load_bugs(base)

    # 合并补丁生成统计 (每条回复的花费/耗时/重试次数), 由 generate_patches.py 产出
    gen_report = None
    gen_path = os.path.join(base, "generation-stats.json")
    if os.path.exists(gen_path):
        try:
            gen_report = json.load(open(gen_path, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            gen_report = None
    per_bug_gen = (gen_report or {}).get("perBug", {})

    # 可选补算: 生成时没配单价, 事后可按已记录的 token 数重算每条回复的花费
    # (缓存命中部分用 inputHitPerM 计价, 平台未上报命中数则全部按未命中价)
    if args.price_input_per_m is not None or args.price_output_per_m is not None:
        hit_price = args.price_input_hit_per_m if args.price_input_hit_per_m is not None else args.price_input_per_m
        for v in per_bug_gen.values():
            costs = []
            for a in (v.get("attemptsLog") or []):
                u = a.get("usage") or {}
                total_p = u.get("prompt_tokens") or 0
                det = u.get("prompt_tokens_details") or {}
                hit_p = min(det.get("cached_tokens") or det.get("cache_read_tokens") or 0, total_p)
                c = u.get("completion_tokens") or 0
                costs.append(round((total_p - hit_p) / 1e6 * (args.price_input_per_m or 0)
                                   + hit_p / 1e6 * (hit_price or 0)
                                   + c / 1e6 * (args.price_output_per_m or 0), 8))
            if costs:
                v["cost"] = round(sum(costs), 8)

    tasks, failures, summary_counts = [], [], {k: 0 for k in
                                              (CLS_ABORT, CLS_FIX_FAILED, CLS_FIX_SUCCESS, CLS_OTHER)}
    for bug_id, entry in entries.items():
        cls = classify(entry)
        summary_counts[cls] += 1
        meta = bugs.get(bug_id, {})
        task = {
            "bugId": bug_id,
            "jobId": entry.get("jobId"),
            "title": meta.get("title"),
            "status": entry.get("status"),
            "classification": cls,
            "evaluation": entry.get("evaluation"),
            "imageAbility": entry.get("imageAbility"),
            "crashes": entry.get("crashes", []),
            "exceptions": entry.get("exceptions", []),
            "failureReason": None,
            "generation": per_bug_gen.get(bug_id),
        }
        task.update(load_job_times(base, bug_id))
        if cls in FAILED_CLASSES:
            task["failureReason"] = extract_failure_reason(base, bug_id, entry, cls)
            # imageAbility 异常时评估结果不可靠, 附加 caveat
            if entry.get("status") == "finished" and entry.get("imageAbility") in ("error", "warning"):
                task["failureReason"]["caveat"] = \
                    f"imageAbility={entry.get('imageAbility')}, VM 未正常运行, 评估结果不可靠"
            failures.append(task)
        tasks.append(task)

    total = len(tasks)
    finished_count = sum(1 for t in tasks if t["status"] == "finished")
    abort_reasons: dict = {}
    for f in failures:
        if f["classification"] == CLS_ABORT and f.get("failureReason"):
            code = f["failureReason"].get("code") or "unknown"
            abort_reasons[code] = abort_reasons.get(code, 0) + 1
    summary = {
        "totalTasks": total,
        CLS_ABORT: {"count": summary_counts[CLS_ABORT],
                    "percentage": pct(summary_counts[CLS_ABORT], total)},
        CLS_FIX_FAILED: {"count": summary_counts[CLS_FIX_FAILED],
                         "percentage": pct(summary_counts[CLS_FIX_FAILED], total)},
        CLS_FIX_SUCCESS: {"count": summary_counts[CLS_FIX_SUCCESS],
                          "percentage": pct(summary_counts[CLS_FIX_SUCCESS], total)},
        CLS_OTHER: {"count": summary_counts[CLS_OTHER],
                    "percentage": pct(summary_counts[CLS_OTHER], total)},
        "finished": {"count": finished_count, "percentage": pct(finished_count, total)},
        "abortReasons": abort_reasons,
        "jobTotals": {
            "totalDurationSeconds": round(sum(
                (t.get("jobDurationSeconds") or 0) for t in tasks), 1),
        },
        "generationTotals": None,
    }
    if gen_report:
        totals = gen_report.get("totals", {})
        costs = [v.get("cost") for v in per_bug_gen.values() if v.get("cost") is not None]
        summary["generationTotals"] = {
            "model": gen_report.get("model"),
            "provider": gen_report.get("provider"),
            "reasoningEffort": gen_report.get("reasoningEffort"),
            "totalCost": round(sum(costs), 8) if costs else totals.get("cost"),
            "totalDurationSeconds": totals.get("durationSeconds"),
            "retriedBugs": sum(1 for v in per_bug_gen.values() if (v.get("retries") or 0) > 0),
            "currency": gen_report.get("currency") or args.currency or None,
            "pricing": gen_report.get("pricing"),
        }

    report = {
        "model": os.path.basename(os.path.normpath(base)),
        "base": base,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "dbCrossCheck": db_cross_check(args.db, entries) if args.db else None,
        "tasks": tasks,
        "failures": failures,
    }

    out_path = args.out or os.path.join(base, "stats.json")
    with open(out_path, "w", encoding="utf-8") as fp:
        json.dump(report, fp, indent=2, ensure_ascii=False)

    # 控制台摘要
    print(f"模型: {report['model']}   任务总数: {total}")
    print(f"  abort            : {summary_counts[CLS_ABORT]:>2}  ({summary[CLS_ABORT]['percentage']}%)")
    print(f"  crash 修复失败    : {summary_counts[CLS_FIX_FAILED]:>2}  ({summary[CLS_FIX_FAILED]['percentage']}%)")
    print(f"  crash 修复成功    : {summary_counts[CLS_FIX_SUCCESS]:>2}  ({summary[CLS_FIX_SUCCESS]['percentage']}%)")
    print(f"  其他(other)       : {summary_counts[CLS_OTHER]:>2}  ({summary[CLS_OTHER]['percentage']}%)")
    print(f"  finished 合计     : {finished_count:>2}  ({summary['finished']['percentage']}%)")
    print(f"  job 总耗时        : {summary['jobTotals']['totalDurationSeconds']}s")
    gt = summary.get("generationTotals")
    if gt:
        print(f"  回复生成合计      : 花费 {gt.get('totalCost')} {gt.get('currency') or ''}, "
              f"耗时 {gt.get('totalDurationSeconds')}s, "
              f"重试过 {gt.get('retriedBugs')} 条 (推理强度 {gt.get('reasoningEffort')})")
    if abort_reasons:
        print(f"  abort 原因分布    : {abort_reasons}")
    print()
    print("失败任务原因:")
    for f in failures:
        reason = f["failureReason"] or {}
        print(f"  [{f['jobId']}] {f['classification']:<16} {(f.get('title') or '')[:40]:<40} "
              f"-> {reason.get('code')}: {reason.get('content')}")
    if args.db:
        x = report["dbCrossCheck"]
        if x["error"]:
            print(f"\nDB 交叉校验失败: {x['error']}")
        else:
            print(f"\nDB 交叉校验: 匹配 {x['matched']}/{total}, 不一致 {len(x['mismatches'])} 条")
    print(f"\n结果已写入: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
