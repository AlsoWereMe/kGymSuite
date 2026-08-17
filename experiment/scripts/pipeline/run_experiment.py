"""批量评估编排: 串行执行 (同一时刻只跑 1 个 job), 每完成一个自动补一个, 直到全部跑完。

输入:
  <base>/bugs.json      (select_bugs.py 产出)
  <base>/patches.json   (generate_patches.py 产出)
输出:
  <base>/results/<bugId>.json   每个 job 的完整 JobContext
  <base>/results.json           汇总 (status / evaluation / crashes / imageAbility / exceptions)
  <base>/inflight.json          在跑 job 的 jobId 映射 (Ctrl+C 清理用)
  <base>/stats.json             全部 job 跑完后自动运行 analyze_results.py 产出
                                (abort/修复成败占比 + 失败原因 + 花费/耗时/重试统计)

断点续跑: 已写入 results.json 且 status 为 finished/aborted 的 bug 会被跳过。
Ctrl+C: 自动 abort 在跑 job 并保存结果, 可随时重启续跑。

用法:
  .venv/bin/python run_experiment.py --base experiment/<模型>
"""
import argparse
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time

from KBDr.kclient import kGymAsyncClient, kGymClient
from KBDr.kcore import JobStatus

from job_submit import build_request

API = "http://localhost:8000"
MAX_PARALLEL = 1          # 并行 job 数 (机器资源有限, 串行执行)
POLL_INTERVAL = 30        # 轮询间隔(秒)
MAX_JOB_TIME = 3 * 3600   # 单 job 超时(秒), 超时 abort 并记 timeout
PENDING_WARN = 15 * 60    # job 停留在 pending/waiting 超过此时长开始打印警告
IMAGE = "buildroot.raw"
MACHINE_TYPE = "qemu:2-4096"
NINSTANCE = 1
BASE = "experiment"
TAG = "batch"

TERMINAL = (JobStatus.Finished, JobStatus.Aborted)


@contextlib.asynccontextmanager
async def _client():
    # kGymAsyncClient 未实现 __aenter__/__aexit__, 这里包一层显式 close
    client = kGymAsyncClient(API)
    try:
        yield client
    finally:
        await client.close()


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
        print("缺少 " + os.path.join(BASE, "bugs.json") + ", 请先运行 select_bugs.py", file=sys.stderr)
        return 1
    if not os.path.exists(os.path.join(BASE, "patches.json")):
        print("缺少 " + os.path.join(BASE, "patches.json") + ", 请先运行 generate_patches.py", file=sys.stderr)
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

    inflight: dict[str, dict] = {}
    inflight_path = os.path.join(BASE, "inflight.json")

    def save_inflight():
        with open(inflight_path, "w") as fp:
            json.dump({b: str(st["job_id"]) for b, st in inflight.items()}, fp)

    async with _client() as client:
        hb = 0   # 心跳计数: 每 10 分钟打印一次各 job 真实状态
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
                    save_inflight()
                    print(f"[提交] {bid[:12]} -> {jid}  (在跑 {len(inflight)}/{MAX_PARALLEL}, 剩余 {len(queue)})")

                if not inflight:
                    break

                await asyncio.sleep(POLL_INTERVAL)
                hb += 1

                done = []
                for bid, st in inflight.items():
                    try:
                        ctx = await client.get_job(st["job_id"])
                    except Exception:
                        if hb % 20 == 0:
                            print(f"[心跳] {bid[:12]} {st['job_id']} 查询失败(网络抖动), 下轮重试")
                        continue
                    if ctx is None:
                        continue
                    el = int(time.time() - st["submitted"])
                    if hb % 20 == 0:   # 20 * 30s = 10 分钟
                        print(
                            f"[心跳] {bid[:12]} {st['job_id']} 状态={ctx.status.value} "
                            f"已提交 {el//60}m{el%60:02d}s (队列中共 {len(inflight)} 个 job)"
                        )
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
                        continue
                    if ctx.status in (JobStatus.Pending, JobStatus.Waiting) and el > PENDING_WARN:
                        print(
                            f"[警告] {bid[:12]} {st['job_id']} 已 {el//60}m 未被 worker 领取"
                            f"(状态={ctx.status.value}): 检查 kmq/kbuilder/kvmmanager 是否正常"
                        )
                    if el > MAX_JOB_TIME:
                        done.append(bid)
                        try:
                            await client.abort_job(st["job_id"])
                        except Exception:
                            pass
                        results[bid] = {"jobId": str(st["job_id"]), "status": "timeout"}
                        print(f"[超时] {bid[:12]} {st['job_id']} 已 abort 并记为 timeout")

                for bid in done:
                    inflight.pop(bid, None)
                save_inflight()
                save_results(results)
        finally:
            save_results(results)
            if inflight:
                print("清理: 中止仍在跑的 job ...")
                for bid, st in inflight.items():
                    try:
                        await client.abort_job(st["job_id"])
                        print(f"  aborted {bid[:12]} {st['job_id']}")
                    except BaseException:
                        pass
            save_inflight()

    finished = sum(1 for v in results.values() if v.get("status") in ("finished", "aborted"))
    print(f"全部结束: 共 {finished} 条完成 -> {results_path}")
    return 0


def run_auto_stats(skip: bool = False):
    """所有 job 跑完后自动运行 analyze_results.py, 产出 <base>/stats.json。"""
    if skip or not os.path.exists(os.path.join(BASE, "results.json")):
        return
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analyze_results.py")
    cmd = [sys.executable, script, "--base", BASE]
    # 若本地 scheduler.db 存在, 一并交叉校验 dashboard 记录
    # (本脚本在 pipeline/ 下, 仓库根 = __file__ 上溯 4 层)
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    db = os.path.join(repo_root, "deployment", "local", "kscheduler-db", "scheduler.db")
    if os.path.exists(db):
        cmd += ["--db", db]
    print(chr(10) + "===== 自动运行统计脚本 =====")
    subprocess.run(cmd, check=False)
    print("===== 统计完成 =====")


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE, help="实验目录 (默认 experiment)")
    ap.add_argument("--skip-stats", action="store_true", help="跑完不自动运行统计脚本")
    args = ap.parse_args()
    BASE = args.base
    try:
        rc = asyncio.run(main_async())
        run_auto_stats(skip=args.skip_stats)
        return rc
    except KeyboardInterrupt:
        print(chr(10) + "收到 Ctrl+C, 尝试中止在跑 job ...")
        inf_path = os.path.join(BASE, "inflight.json")
        if os.path.exists(inf_path):
            try:
                inflight = json.load(open(inf_path))
            except Exception:
                inflight = {}
            client = kGymClient(API)
            for bid, jid in inflight.items():
                try:
                    client.abort_job(jid)
                    print(f"  aborted {bid[:12]} {jid}")
                except Exception as e:
                    print(f"  abort 失败 {bid[:12]} {jid}: {e}")
            client.close()
        run_auto_stats(skip=args.skip_stats)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
