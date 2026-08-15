"""批量评估编排: 2 个 job 并行, 每完成一个自动补一个, 直到全部跑完。

输入:
  experiment/bugs.json      (select_bugs.py 产出)
  experiment/patches.json   (generate_patches.py 产出)
输出:
  experiment/results/<bugId>.json   每个 job 的完整 JobContext
  experiment/results.json           汇总 (status / evaluation / crashes / imageAbility / exceptions)

断点续跑: 已写入 results.json 且 status 为 finished/aborted 的 bug 会被跳过;
可反复重启该脚本, 直到全部完成。中断(Ctrl+C)不会丢已完成的结果。

用法:
  .venv/bin/python run_experiment.py
"""
import asyncio
import json
import os
import sys
import time

from KBDr.kclient import kGymAsyncClient
from KBDr.kcore import JobStatus

from job_submit import build_request

API = "http://localhost:8000"
MAX_PARALLEL = 2          # 并行 job 数
POLL_INTERVAL = 30        # 轮询间隔(秒)
MAX_JOB_TIME = 3 * 3600   # 单 job 超时(秒), 超时 abort 并记 timeout
IMAGE = "buildroot.raw"
MACHINE_TYPE = "qemu:2-4096"
NINSTANCE = 1
BASE = "experiment"
TAG = "deepseek-v4-flash-batch"

TERMINAL = (JobStatus.Finished, JobStatus.Aborted)


def summarize(ctx) -> dict:
    out: dict = {"jobId": str(ctx.jobId), "status": str(ctx.status.value)}
    excs = []
    for w in ctx.jobWorkers:
        r = w.workerResult
        if r is None:
            continue
        if r.jobException is not None:
            excs.append(r.jobException.code)
        if r.workerException is not None:
            excs.append(r.workerException.code)
    out["exceptions"] = excs
    kb_res = ctx.jobWorkers[0].workerResult
    if kb_res is not None and kb_res.compilationTime:
        out["compilationTime"] = kb_res.compilationTime
    if ctx.status == JobStatus.Finished and len(ctx.jobWorkers) > 1:
        vm_res = ctx.jobWorkers[1].workerResult
        if vm_res is not None:
            out["imageAbility"] = vm_res.imageAbility
            crashes = vm_res.crashes or []
            out["crashes"] = [
                {"title": c.title, "crashType": c.crashType} for c in crashes
            ]
            real = [c for c in crashes if c.crashType == "crash"]
            out["evaluation"] = "reproduced" if real else "notReproduced"
    return out


def save_results(results: dict):
    with open(os.path.join(BASE, "results.json"), "w") as fp:
        json.dump(results, fp, indent=2, ensure_ascii=False)


async def main_async() -> int:
    if not os.path.exists(os.path.join(BASE, "bugs.json")):
        print("缺少 experiment/bugs.json, 请先运行 select_bugs.py", file=sys.stderr)
        return 1
    if not os.path.exists(os.path.join(BASE, "patches.json")):
        print("缺少 experiment/patches.json, 请先运行 generate_patches.py", file=sys.stderr)
        return 1

    bugs = json.load(open(os.path.join(BASE, "bugs.json")))["bugs"]
    manifest = json.load(open(os.path.join(BASE, "patches.json")))
    os.makedirs(os.path.join(BASE, "results"), exist_ok=True)

    results_path = os.path.join(BASE, "results.json")
    results = json.load(open(results_path)) if os.path.exists(results_path) else {}

    queue = []
    for b in bugs:
        bid = b["bugId"]
        entry = manifest.get(bid)
        if not entry or entry.get("status") != "ok":
            print(f"跳过 {bid[:12]}: 补丁未生成 ({entry.get('reason') if entry else '无记录'})")
            continue
        if bid in results and results[bid].get("status") in ("finished", "aborted"):
            continue
        patch = open(entry["patch_file"]).read()
        queue.append({"bug": b, "bugId": bid, "patch": patch})

    print(f"待提交 {len(queue)} 条, 并行度 {MAX_PARALLEL}, 轮询间隔 {POLL_INTERVAL}s")
    if not queue:
        print("没有待提交的 bug, 退出")
        return 0

    async with kGymAsyncClient(API) as client:
        inflight: dict[str, dict] = {}
        try:
            while queue or inflight:
                # 补位: 保持 MAX_PARALLEL 个在跑
                while len(inflight) < MAX_PARALLEL and queue:
                    item = queue.pop(0)
                    bid = item["bugId"]
                    req = build_request(item["bug"], item["patch"], IMAGE, MACHINE_TYPE, NINSTANCE)
                    req.tags = {
                        "bugId": bid,
                        "dataset": item["bug"].get("_dataset", ""),
                        "experiment": TAG,
                    }
                    try:
                        jid = await client.create_job(req)
                    except Exception as e:
                        results[bid] = {"status": "submit-error", "error": str(e)}
                        print(f"[提交失败] {bid[:12]}: {e}")
                        save_results(results)
                        continue
                    inflight[bid] = {"job_id": jid, "submitted": time.time()}
                    print(f"[提交] {bid[:12]} -> {jid}  (在跑 {len(inflight)}/{MAX_PARALLEL}, 剩余 {len(queue)})")

                if not inflight:
                    break

                await asyncio.sleep(POLL_INTERVAL)

                done = []
                for bid, st in inflight.items():
                    try:
                        ctx = await client.get_job(st["job_id"])
                    except Exception:
                        continue  # 网络抖动, 下轮再看
                    if ctx is None:
                        continue
                    if ctx.status in TERMINAL:
                        done.append(bid)
                        sm = summarize(ctx)
                        results[bid] = sm
                        with open(os.path.join(BASE, "results", f"{bid}.json"), "w") as fp:
                            fp.write(ctx.model_dump_json(indent=2))
                        print(
                            f"[完成] {bid[:12]} {st['job_id']} "
                            f"status={sm['status']} eval={sm.get('evaluation')} "
                            f"crashes={len(sm.get('crashes', []))} excs={sm.get('exceptions')}"
                        )
                    elif time.time() - st["submitted"] > MAX_JOB_TIME:
                        done.append(bid)
                        try:
                            await client.abort_job(st["job_id"])
                        except Exception:
                            pass
                        results[bid] = {"jobId": str(st["job_id"]), "status": "timeout"}
                        print(f"[超时] {bid[:12]} {st['job_id']} 已 abort 并记为 timeout")

                for bid in done:
                    inflight.pop(bid, None)
                save_results(results)
        except KeyboardInterrupt:
            print("\n中断: 正在 abort 在跑的 job...")
            for bid, st in inflight.items():
                try:
                    await client.abort_job(st["job_id"])
                    print(f"  aborted {bid[:12]} {st['job_id']}")
                except Exception:
                    pass
            save_results(results)
            return 130

    finished = sum(1 for v in results.values() if v.get("status") in ("finished", "aborted"))
    print(f"全部结束: 共 {finished} 条完成 -> {results_path}")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
