# kGymSuite — 本地部署指南

本文面向刚刚克隆了仓库的人，一步步在一台 Linux 机器上完成本地部署，
从环境检查到提交第一个任务。内容整合自 `README.md`、`DEPLOY.md`，以及
[docker/kgym-vmmanager.Dockerfile](docker/kgym-vmmanager.Dockerfile) 中的修复。

> 环境需求：
> - Linux x86_64
> - RAM >= 16GiB
> - MEM >= 100GiB

## 已知问题

### Golang Version
镜像构建时从syzkaller 上游`master`分支克隆代码，但没有锁定版本。
syzkaller master现在要求 Go >= 1.26，而kGym-mmanager的Dockerfile中仍使用：

```dockerfile
FROM golang:1.24.6-bookworm AS syzkaller
```

所以 `docker compose build kvmmanager` 会因版本问题报错，需要手动修改为

```dockerfile
FROM golang:1.26.0-bookworm AS syzkaller
```

以及wget部分：
```dockerfile
RUN wget "https://dl.google.com/go/go1.24.6.linux-amd64.tar.gz" -O go.tar.gz && tar -C /usr/local -xzf go.tar.gz
```

改为

```dockerfile
RUN wget "https://dl.google.com/go/go1.26.0.linux-amd64.tar.gz" -O go.tar.gz && tar -C /usr/local -xzf go.tar.gz
```

### OOM
镜像构建时，使用命令

```dockerfile
RUN make all crush -j$(nproc)
```

这会使得拉起和内核数相同的编译进程，每个Go进程大概在1-2GB，如果内存不能保证大于2*CPU核数 GB的话，OOM几乎是必然的，需要修改

```dockerfile
ARG BUILD_JOBS=4
RUN make all crush -j${BUILD_JOBS}
```

可以根据情况调整具体构建进程数

除了构建镜像时，运行syzkaller也有OOM风险


此外，为了更有效防止OOM，可以提高交换空间大小，适合磁盘空间大的人使用
```bash
sudo swapoff /swap.img
sudo fallocate -l 16G /swap.img      # 若 fallocate 不支持，用: sudo dd if=/dev/zero of=/swap.img bs=1M count=8192
sudo chmod 600 /swap.img
sudo mkswap /swap.img
sudo swapon /swap.img
free -h && swapon --show            # 验证
```
## 仓库根目录准备文件

本地 compose 文件（`deployment/local/compose.yml`）里的相对路径都是相对于
**项目目录**解析的。本文所有命令都从仓库根目录执行并带
`--project-directory .`，因此运行文件必须放在仓库根目录，**不是**
`deployment/local/` 里面。

```bash
cd ~/workspace/kGymSuite

cp deployment/local/config.json ./config.json
cp deployment/local/kgym-runner.env ./kgym-runner.env

mkdir -p bucket/userspace-images bucket/jobs kbuilder-repo kscheduler-db
```

按单机部署修改配置：

```bash
sed -i 's/"deploymentName": "turing"/"deploymentName": "local"/' config.json
sed -i 's#"https://kgym.example.com"#"http://localhost:3000"#' config.json
sed -i 's#"https://kgym-api.example.com"#"http://localhost:8000"#' config.json
sed -i 's#@<ip>#@kmq#' kgym-runner.env
```

`@kmq` 是 Docker Compose 服务名，同一网络内的容器通过它访问 RabbitMQ，
不需要填 IP。

校验配置：

```bash
python3 -m json.tool config.json > /dev/null && echo "config.json OK"
grep -A1 '"allowedOrigins"' config.json
cat kgym-runner.env
```

预期：`"deploymentName": "local"`、`"http://localhost:3000"`、
`"http://localhost:8000"`，以及
`KGYM_MQ_CONN_URL=amqp://kbdr:...@kmq:5672/?heartbeat=60`。

## 4. 下载 userspace 镜像

Worker 需要现成的根文件系统镜像，从 Hugging Face 数据集
`chenxi-kalorona-huang/kGym-images` 下载：

```bash
wget -P bucket/userspace-images \
  https://huggingface.co/datasets/chenxi-kalorona-huang/kGym-images/resolve/main/buildroot.raw
wget -P bucket/userspace-images \
  https://huggingface.co/datasets/chenxi-kalorona-huang/kGym-images/resolve/main/bullseye.raw

ls -lh bucket/userspace-images
```

`buildroot.raw` 是 kbuilder 任务默认使用的 `userspaceImage`。

## 5. 构建 Docker 镜像

为避免内存耗尽，重镜像逐个构建：

```bash
sudo env DEPLOYMENT=local docker compose -f ./deployment/local/compose.yml --project-directory . build kvmmanager
sudo env DEPLOYMENT=local docker compose -f ./deployment/local/compose.yml --project-directory . build kmq kscheduler kdashboard kprebuilder
sudo env DEPLOYMENT=local docker compose -f ./deployment/local/compose.yml --project-directory . build kbuilder
```

`kvmmanager` 镜像会克隆 syzkaller 并用 4 个并发任务编译（`BUILD_JOBS`）；
内存非常小的机器可以加 `--build-arg BUILD_JOBS=2`。

确认镜像齐全：

```bash
sudo docker images
```

预期出现：`kgym-mq:local`、`kgym-scheduler:local`、`kgym-dashboard:local`、
`kgym-builder:local`、`kgym-vmmanager:local`、`kgym-prebuilder:local`。

## 6. 启动核心服务

```bash
sudo env DEPLOYMENT=local docker compose -f ./deployment/local/compose.yml --project-directory . up -d kmq kscheduler kdashboard
```

等几秒后验证：

```bash
sudo env DEPLOYMENT=local docker compose -f ./deployment/local/compose.yml --project-directory . ps
curl -s http://localhost:8000/system/info
```

预期：`kmq` 为 healthy，调度器和 Dashboard 为 `Up`，API 返回
`{"deploymentName":"local"}`。

Web 入口：

- Dashboard：http://localhost:3000
- 调度器 API 文档：http://localhost:8000/docs
- RabbitMQ 管理台：http://localhost:15672（`guest` / `guest`）

## 7. 启动 Worker

compose 默认是 2 个 kbuilder、4 个 kvmmanager 副本，对 16 GiB 笔记本来说
太激进。建议 1 个 kbuilder、2 个 kvmmanager：

```bash
sudo env DEPLOYMENT=local docker compose -f ./deployment/local/compose.yml --project-directory . up -d --scale kbuilder=1 --scale kvmmanager=2 kprebuilder kbuilder kvmmanager
```

> 如果 Worker 一直处于 `Restarting`，最常见的原因是容器启动时没有带
> `--project-directory .`，读到了 `deployment/local/kgym-runner.env`
> （里面还是 `<ip>` 占位符），连不上 RabbitMQ。先 `docker compose down`，
> 再用上面的命令重新启动。容器名以 `kgymsuite-` 开头才是正确的项目；
> `local-*` 说明项目目录用错了。

几秒后查看 Worker 日志：

```bash
sudo env DEPLOYMENT=local docker compose -f ./deployment/local/compose.yml --project-directory . logs kbuilder | tail -20
```

不应该有 traceback；Worker 应安静地等待任务。

## 8. 安装 Python 客户端并提交第一个任务

```bash
cd ~/workspace/kGymSuite
python3 -m venv .venv
source .venv/bin/activate
pip install -e ./kcore
pip install -e ./kclient
kclient
```

在 IPython shell 中粘贴一个纯 kbuilder 冒烟测试：

```python
job = kJobRequest(
    jobWorkers=[
        kBuilderArgument(
            kernelSource=KernelGitCommit(
                gitUrl="https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git",
                commitId="master",
                kConfig="",
                arch="amd64",
                compiler="gcc",
                linker="ld"
            ),
            userspaceImage="buildroot.raw",
            patch=""
        )
    ],
    tags={"note": "first-smoke-test"}
)

client = kGymClient("http://localhost:8000")
print(client.create_job(job))
```

返回的 8 位十六进制字符串就是任务 ID。查看任务状态：

```python
client.get_job("<job-id>")
```

状态会从 `pending` 变成 `inProgress`，表示 kbuilder 已接手。另开一个终端
跟踪构建日志：

```bash
sudo env DEPLOYMENT=local docker compose -f ./deployment/local/compose.yml --project-directory . logs -f kbuilder
```

第一个任务要克隆 Linux 内核并全量编译，耗时较长。产物存放在
`bucket/jobs/<job-id>/0_kbuilder/`（`bzImage`、`vmlinux`、可启动镜像、构建日志）。

## 9. （可选）完整崩溃复现任务

构建任务跑通后，可以在 builder 后面串一个 `kVMManagerArgument`，让构建出的
内核在 QEMU 里带复现程序启动：

```python
from KBDr.kclient import Reproducer, kVMManagerArgument

job = kJobRequest(
    jobWorkers=[
        kBuilderArgument(
            kernelSource=KernelGitCommit(
                gitUrl="https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git",
                commitId="master",
                kConfig="",
                arch="amd64",
                compiler="gcc",
                linker="ld"
            ),
            userspaceImage="buildroot.raw",
            patch=""
        ),
        kVMManagerArgument(
            reproducer=Reproducer(
                reproducerType="c",
                reproducerText="/* C reproducer code */",
                syzkallerCheckout="latest"
            ),
            image=0,                       # 使用第一个（kbuilder）worker 的产物
            machineType="qemu:2-4096"      # 每个 VM 2 核、4096 MB 内存
        )
    ],
    tags={}
)
```

这要求宿主机有 `/dev/kvm` 和 QEMU 支持。

## 10. 日常运维

重启后恢复整套服务：

```bash
sudo systemctl start docker
cd ~/workspace/kGymSuite
sudo env DEPLOYMENT=local docker compose -f ./deployment/local/compose.yml --project-directory . up -d --scale kbuilder=1 --scale kvmmanager=2
```

停止全部服务（保留容器，重启后不会自动拉起）：

```bash
sudo env DEPLOYMENT=local docker compose -f ./deployment/local/compose.yml --project-directory . stop
```

彻底拆除（容器和网络都删除）：

```bash
sudo env DEPLOYMENT=local docker compose -f ./deployment/local/compose.yml --project-directory . down
```

从零重建：

```bash
sudo docker compose -f ./deployment/local/compose.yml down
sudo docker system prune -a -f      # 删除所有未使用的镜像、容器和构建缓存
```

> `docker system prune -a` 会同时删除机器上其他项目的镜像。只想清 kGym
> 镜像，用 `sudo docker rmi -f $(sudo docker images --filter reference='kgym-*' -q)`。

## 11. 内存保护：防止系统再次被 OOM 拖垮

15 GiB 内存的机器上，内核编译（默认 `-j16`）和多个 QEMU 实例很容易把系统
内存耗尽，导致内核 OOM killer 杀进程、系统卡死甚至崩溃。以下措施按优先级
列出，建议全部执行：

**1）给容器加 CPU 和内存上限（关键，立刻生效）**

限制 kbuilder 可见 CPU 数和内存，让内核编译只占 4 核、最多 6 GiB：

```bash
sudo docker update --cpuset-cpus 0-3 --memory 6g --memory-swap 8g kgymsuite-kbuilder-1
```

限制 kvmmanager（每个副本 4 核、最多 4 GiB）：

```bash
sudo docker update --cpuset-cpus 0-3 --memory 4g --memory-swap 6g kgymsuite-kvmmanager-1 kgymsuite-kvmmanager-2
```

注意：

- `--cpuset-cpus` 会改变容器内 `nproc` 的返回值，从而让 kbuilder 的
  `make -j$(nproc)` 从 16 降到 4，这是最关键的一步；
- 加了内存上限后，即使任务超出内存，被杀死的也只是容器进程，系统不会再
  被拖垮；
- `--memory-swap` 必须大于等于 `--memory`。

**2）只保留必要的 Worker**

只测试构建链路时，可以先把 kvmmanager 缩到 0：

```bash
sudo env DEPLOYMENT=local docker compose -f ./deployment/local/compose.yml --project-directory . up -d --scale kvmmanager=0 --scale kbuilder=1 kprebuilder
```

提交复现任务时把 `nInstance` 保持为 1，不要同时跑多个重任务。

**3）加大 swap 兜底**

```bash
sudo fallocate -l 8G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

如需开机自动启用，把下面这行追加到 `/etc/fstab`（自行确认后操作）：

```
/swapfile none swap sw 0 0
```

> swap 是缓冲不是解药：严重过载时 swap 抖动照样会让系统假死，真正的解决
> 办法是上面的容器资源限制。

**4）构建镜像时避免并行**

```bash
sudo env DEPLOYMENT=local docker compose -f ./deployment/local/compose.yml --project-directory . build --parallel 1
```

## 故障排查

| 现象 | 原因与解决办法 |
| --- | --- |
| `docker: permission denied` | 加入 `docker` 组并重新登录，或命令前加 `sudo`。 |
| `env file .../kgym-runner.env not found` | compose 项目目录不对。始终从仓库根目录带 `--project-directory .` 运行，并把运行文件放在根目录。 |
| Worker 一直 `Restarting` | 容器读到了 `deployment/local/kgym-runner.env` 里的 `<ip>` 占位符，说明启动时没带 `--project-directory .`。`docker compose down` 后用第 7 节的命令重启。 |
| `build kvmmanager` 报 `go.mod requires go >= 1.26.0` | syzkaller master 未锁定 + Go 1.24.6。应用第 2 节的 Dockerfile 修复。 |
| 构建或内核编译把系统搞崩（OOM） | 按第 11 节限制容器 CPU/内存、加 swap、逐镜像构建。 |
| `/dev/kvm` 缺失 | QEMU 复现需要 KVM。`sudo modprobe kvm_intel` 或换到支持嵌套虚拟化的机器；注意沙箱环境可能看不到设备，但宿主机有。 |
| Dashboard 报 API 错误 | 检查根目录 `config.json` 的 `kGymAPIEndpoint`，本地部署应为 `http://localhost:8000`。 |
| kvmmanager 容器启动时设备挂载失败 | compose 挂载了 `/dev/kvm:/dev/kvm`，宿主机必须把该设备暴露给 Docker。 |

## 注意事项

- 项目设计上最适合跑在 Google Cloud（GCP）上；本指南覆盖的是单机本地模式
  （本地文件系统存储）。
- compose 默认副本数（2 kbuilder、4 kvmmanager）面向大机器；笔记本请使用
  `--scale` 参数。
- `DEPLOY.md` 描述的是 `deployment/example/bucket` 布局，但仓库自带的
  `deployment/local/compose.yml` 实际要求运行文件在项目根目录。本指南以
  compose 的真实行为为准。
- kbuilder 需要挂载 loop 设备并以 privileged 模式运行，必须使用真实 Linux
  主机（macOS/Windows 的 Docker Desktop 不行）。

