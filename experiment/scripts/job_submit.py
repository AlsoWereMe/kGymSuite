"""构造并提交单个 kGym job (可独立使用, 也被 run_experiment.py 复用)。

用法:
  .venv/bin/python job_submit.py --bug-id <id> [--dataset-json data/kbench/dataset-kb.json] \
      [--patch-file patch.patch] [--image buildroot.raw] [--machine-type qemu:2-4096] \
      [--ninstance 1] [--api http://localhost:8000] [--tag k=v ...]

patch-file 留空 = 不加补丁(对照实验)。
"""
import argparse
import json
import sys

from KBDr.kclient import SyzbotDataset, SyzbotData, kBuilderArgument, kVMManagerArgument, kGymClient
from KBDr.kclient.models import kJobRequest


def build_request(
    bug,
    patch_text: str,
    image: str = "buildroot.raw",
    machine_type: str = "qemu:2-4096",
    ninstance: int = 1,
    tags: dict | None = None,
) -> kJobRequest:
    """构造 [kbuilder(坏内核+patch), kvmmanager(qemu 复现)] 两个 worker 的 job 请求。

    bug 可以是 SyzbotData 或普通 dict(来自 experiment/bugs.json)。
    """
    if isinstance(bug, dict):
        bug = SyzbotData.model_validate(bug)

    # worker 0: 在 fix 父提交上编译内核, 应用 patch
    kb = kBuilderArgument.model_from_syzbot_data(bug, userspace_image_name=image)
    kb.patch = patch_text

    # worker 1: 用 syz 复现程序在 qemu 里复现
    vm = kVMManagerArgument.model_from_syzbot_data(
        bug, machine_type=machine_type, image=0, ninstance=ninstance
    )
    return kJobRequest(jobWorkers=[kb, vm], tags=tags or {})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bug-id", required=True)
    ap.add_argument("--dataset-json", default="data/kbench/dataset-kb.json")
    ap.add_argument("--patch-file", default="")
    ap.add_argument("--image", default="buildroot.raw", help="用户态镜像 (数据集引用 kdump 镜像, 本地需覆盖)")
    ap.add_argument("--machine-type", default="qemu:2-4096", help="本地部署必须 qemu")
    ap.add_argument("--ninstance", type=int, default=1)
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--tag", action="append", default=[], metavar="K=V")
    args = ap.parse_args()

    bug = SyzbotDataset.model_validate(json.load(open(args.dataset_json))).get(args.bug_id)
    if bug is None:
        print("bug not found:", args.bug_id, file=sys.stderr)
        return 1

    patch_text = open(args.patch_file).read() if args.patch_file else ""
    req = build_request(bug, patch_text, args.image, args.machine_type, args.ninstance)
    tags = dict(t.split("=", 1) for t in args.tag if "=" in t)
    tags.setdefault("bugId", args.bug_id)
    req.tags = tags

    client = kGymClient(args.api)
    job_id = client.create_job(req)
    print("JOB_ID:", job_id)
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
