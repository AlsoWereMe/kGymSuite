# kGymSuite — 本地部署指南

本文面向刚刚克隆了仓库的人，一步步在一台 Linux 机器上完成本地部署，从环境检查到提交第一个任务。

> 环境需求：
> - Linux x86_64
> - RAM >= 16GiB
> - MEM >= 100GiB

## Implementation
kGym为了评测修复补丁，先在一个确定的坏内核上复现崩溃并缓存构建产物，然后把patch应用在坏内核上增量重建，在重建的新内核上执行崩溃复现程序，最终根据"崩溃是否消失"与"环境是否健康"判定补丁是否通过。

工程上，这些概念是这样的：
- 坏内核的定义是一个确定的fix commit的父提交，因为一个fix commit代表着一个bug，没修复该bug的父提交自然能作为坏内核
- 复现崩溃分为
  - 编译内核，由kbuilder负责
  - 产出编译产物，存储在kcache
  - kvmmanager在编译产物上使用reproducer复现出崩溃三个步骤
- patch应用通过在kcache上git apply patch并重新make实现
- 新内核上kvmmanager同样使用reproducer尝试复现出崩溃

## Repository Analysis
### Client
kGymSuite提供了一个可交互的client给开发者使用。
kclient是一个Python SDK，包含REST 客户端、任务模型、数据集/评测工具、LLM agent 接口和一个交互式 IPython shell，用于提交任务。

### Microservice
一共包括三个特殊微服务：
- kscheduler
  - 任务调度服务，使用8000端口为用户提供文档
- kmq
  - 消息队列
- kdashboard
  - 控制面板，使用3000端口为用户提供服务
### Worker
一共包含三个执行任务的组件
- kbuilder
  - 从git commit上编译内核、应用patch并生成可启动的VM镜像
- kvmmanager
  - 虚拟机管理器，在QEMU/GCE上运行syzkaller复现崩溃
- kprebuilder
  - 补丁预构建器，在此前编译过的linux内核中试运行patch，确定能否成功编译

## Issues

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
此外，为了更有效防止OOM，可以提高交换空间大小，适合磁盘空间大的人使用
```bash
sudo swapoff /swap.img
sudo fallocate -l 16G /swap.img      # 若 fallocate 不支持，用: sudo dd if=/dev/zero of=/swap.img bs=1M count=8192
sudo chmod 600 /swap.img
sudo mkswap /swap.img
sudo swapon /swap.img
free -h && swapon --show            # 验证
```

## Deploy
### Local Storage
首先配置本地存储相关的设置。
```bash
mkdir -p ./deployment/example/bucket/userspace-images
mkdir -p ./deployment/example/bucket/jobs
hf download chenxi-kalorona-huang/kGym-images \
  --repo-type dataset \
  --local-dir ./kGym-images
cp kGym-images/buildroot.raw ./deployment/example/bucket/userspace-images/
cp kGym-images/bullseye.raw ./deployment/example/bucket/userspace-images/
```

接下来更改容器相关的config.json

allowedOrigins当前是https://kgym.example.com。调度器用这个做CORS白名单，在本地跑则需要使用dashboard的地址：
```json
"allowedOrigins": ["http://localhost:3000"]
```

kGymAPIEndpoint当前是https://kgym-api.example.com，dashboard前端会直接用这个URL调API。本地应为：
```json
"kGymAPIEndpoint": "http://localhost:8000"
```

还有一处地方有个占位符：deployment/local/kgym-runner.en

KGYM_MQ_CONN_URL=amqp://kbdr:ey4lai1the7peeGh@<ip>:5672/?heartbeat=60

<ip> 必须替换，否则 kbuilder/kvmmanager/kprebuilder 三个 worker 连不上 RabbitMQ。所有服务在同一个Docker 网络里，用 compose 服务名替换即可

KGYM_MQ_CONN_URL=amqp://kbdr:ey4lai1the7peeGh@kmq:5672/?heartbeat=60

### Build & Start
构建镜像：
```bash
DEPLOYMENT=local docker compose -f ./deployment/local/compose.yml --project-directory . build
```

构建完成后，启动kcore服务
```bash
DEPLOYMENT=local docker compose -f ./deployment/local/compose.yml up -d kmq kscheduler kdashboard
```

然后将worker的服务启动
```bash
DEPLOYMENT=local docker compose -f ./deployment/local/compose.yml up -d kbuilder kvmmanager kprebuilder
```

最后测试服务是否启动成功
```bash
DEPLOYMENT=local docker compose -f ./deployment/local/compose.yml ps
```

服务的端口位置在：
- Dashboard: http://localhost:3000
- API: http://localhost:8000/docs

## Jobs
