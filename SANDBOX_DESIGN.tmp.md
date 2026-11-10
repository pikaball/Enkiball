# Enkiball 沙箱设计草案

本文汇总本次讨论的设计决策、接口草案和实现状态。目标是将沙箱作为独立运行时，通过 EnkiSandbox 为多个 Agent 提供共享执行环境和资源预算。

状态：v1 实现已落地。本文保留原始设计讨论；已定案语义、安装、协议和限制以 `sandbox/README.md` 为准，实施记录见第 12 节。

## 1 定位与边界

Enkiball 是通过 Python 包安装的 LLM SDK。Python 包负责配置对象和执行适配；系统隔离实现由独立安装的沙箱组件负责。

| 组件 | 职责 |
| --- | --- |
| Enkiball | 使用指定 EnkiSandbox 执行工具命令。 |
| EnkiSandbox | 持有策略、沙箱身份及 client 连接信息。 |
| client | 创建、连接、执行和销毁沙箱，传递输出与状态。 |
| backend | 管理沙箱生命周期、资源预算、任务及隔离机制。 |
| 系统运行时 | 提供命名空间、挂载、cgroup、seccomp 和网络过滤能力。 |

Python 包包含 EnkiSandbox 和 client 调用适配层；bwrap、cgroup、BPF 及特权操作实现迁出 Python 包。

沙箱组件与 SDK 可以在同一仓库维护，独立安装。用户通过 clone 仓库执行系统组件安装，部署常驻 backend，并将 client 安装到 PATH，例如 `/usr/local/bin`。client elf不一定要命名为“client”

backend 使用特权完成环境准备，命令按调用者身份及已授权策略执行。普通命令默认保持调用者的宿主身份。

## 2 已确定的设计方向

1. EnkiSandbox 独立于 Enkiball，全部沙箱策略进入 EnkiSandbox。
2. 构造 EnkiSandbox 时通过 client 创建 backend 中的沙箱实例。
3. 多个 Enkiball 可以引用同一个 EnkiSandbox，共享资源预算。
4. 策略在沙箱创建后固定；变更策略通过创建新对象完成。
5. Agent 可以切换到另一 EnkiSandbox，后续命令使用新对象。
6. client 支持选择不同沙箱，供 harness 调度使用。
7. client 缺失时沿用用户要求的宿主执行回退，沙箱参数忽略。
8. SDK 向 Agent 和 harness 明确暴露实际执行模式及生效策略。
9. 配置采用可组合的基础能力，避免为已有能力组合增加场景模式。
10. schema、提示词、注释和接口文本采用简短、直接的技术表述。

不同限制对应不同沙箱实例。一个 backend 进程管理多个实例是当前接口建议；实例与守护进程保持独立身份。

## 3 典型用法

下列 Python 接口为拟定形态。

```python
from enkiball import Enkiball, EnkiSandbox

with EnkiSandbox(
    filesystem=[
        {"path": "/work", "access": "write"},
        {"path": "/work/.env", "access": "deny"},
        {"path": "/data/reference", "access": "read"},
    ],
    network={"outbound": []},
    resources={
        "memory_bytes": 8 * 1024**3,
        "cpu_count": 4,
    },
) as sandbox:
    agents = [Enkiball(sandbox=sandbox) for _ in range(20)]
```

这 20 个 Agent 的命令及后代进程共同使用 8 GiB 内存预算和所选的 4 个逻辑处理器。harness 与 Agent 的 Python 进程位于预算之外。

沙箱切换：

```python
other = EnkiSandbox(
    filesystem=[{"path": "/work", "access": "read"}],
    resources={"memory_bytes": 2 * 1024**3, "cpu_count": 2},
)
agent.sandbox = other
```

正在执行的任务继续属于原沙箱，后续调用使用新沙箱。开发者负责原沙箱的保留和销毁。

跨进程连接是建议补充的接口：

```python
sandbox = EnkiSandbox.attach(sandbox_id)
agent = Enkiball(sandbox=sandbox)
```

`attach` 复用既有身份、策略和预算。连接端取得 backend 返回的实际策略。

## 4 策略模型

以下字段用于 EnkiSandbox 构造；它们与 client 和 backend 的策略数据保持一致。

| 字段 | 表达能力 |
| --- | --- |
| `filesystem` | 文件或目录的读取、写入和拒绝规则。 |
| `devices` | 指定设备节点的读、写或读写访问。 |
| `network` | 网络隔离、共享网络或按网段与出口网卡授权。 |
| `capabilities` | Linux capability 集合，附带明确的作用范围。 |
| `seccomp` | 系统调用允许列表或拒绝列表。 |
| `resources` | 共享内存上限与逻辑处理器数量。 |

### 文件

```python
filesystem=[
    {"path": "/work", "access": "write"},
    {"path": "/work/secrets", "access": "deny"},
    {"path": "/work/secrets/public", "access": "read"},
]
```

`access` 取 `read`、`write`、`deny`。`write` 包含读写、创建、删除和重命名。更具体的路径优先，同一路径重复规则应报错。

路径规则覆盖文件和目录。未匹配路径遵循默认拒绝；运行时必需的系统文件应作为基础策略明确展示。

符号链接的最终指向参与权限检查。硬链接和独立挂载别名需要一致的规则处理。

共享沙箱的策略应锚定固定路径。相对路径的解析基准，以及旧版 `cwd` 自动可写行为的迁移方式，需要在新接口实现时定案。

未创建路径的授权是目标能力，文件与目录类型、创建时机仍需定义。

### 设备

```python
devices=[
    {"path": "/dev/ttyUSB0", "access": "readwrite"},
    {"path": "/dev/net/tun", "access": "readwrite"},
]
```

设备节点访问、驱动操作权限和 Linux capability 分别表达。设备的 `ioctl` 仍遵循驱动自身的检查；当前模型尚未定义设备专用操作白名单。

创建沙箱内 TUN/TAP 通常涉及设备访问与沙箱网络管理能力。宿主虚拟网卡管理、内核模块加载涉及宿主权限，需明确执行范围。

### 网络

默认使用隔离网络；`network="all"` 表达共享宿主网络。

允许宿主回环与指定 Docker 网段：

```python
network={
    "outbound": [
        {"interface": "lo", "destinations": ["127.0.0.0/8", "::1/128"]},
        {"destinations": ["172.18.0.0/16"]},
    ],
}
```

通过有线网卡访问指定内网：

```python
network={
    "outbound": [
        {
            "interface": "enp3s0",
            "destinations": ["192.168.1.0/24", "10.20.0.0/16"],
        },
    ],
}
```

规则内各条件取交集，多条规则取并集；未匹配的出站流量拒绝。IPv4 与 IPv6 使用相同语义。

`interface` 指宿主实际出口网卡。过滤策略中的回环指宿主回环；隔离命名空间自身的回环需在状态中区分。Docker 网段取实际配置，DNS 按目标地址授权。

接口重建、路由变化与本机代理的影响应纳入 backend 的策略语义和验证。

### Capability 与 seccomp

```python
capabilities=["NET_ADMIN"]

seccomp={
    "default": "allow",
    "deny": ["ptrace"],
}
```

`seccomp.default="allow"` 配合 `deny`；`default="deny"` 配合 `allow`。规则项为系统调用名称。

capability 必须明确作用范围。沙箱用户命名空间内的能力与宿主能力分开处理。backend 拒绝无法保持其余策略边界的组合。

### 资源

```python
resources={
    "memory_bytes": 8 * 1024**3,
    "cpu_count": 4,
}
```

仅保留内存和 CPU 两项。`cpu_count` 表示绑定的逻辑处理器数量，由 backend 从可用集合选择编号；处理器可与其他任务共享。

`memory_bytes` 约束沙箱内全部任务及后代进程的合计内存。预算属于沙箱实例，跨多条命令及多个 Agent 生效。

swap 策略、共享预算耗尽后的终止范围及状态报告需要定案；当前过渡实现采用禁用该组 swap。

## 5 生命周期与执行协议

以下 client 名称和子命令为协议草案。

| 操作 | 输入 | 输出 |
| --- | --- | --- |
| `create` | 策略 | 沙箱 ID、实际策略、执行模式 |
| `inspect` | 沙箱 ID | 状态、策略、资源信息 |
| `exec` | 沙箱 ID、命令、cwd、env、超时 | 执行 ID、stdout、stderr、退出状态 |
| `cancel` | 执行 ID | 任务终止状态 |
| `destroy` | 沙箱 ID | 沙箱及其任务回收结果 |

```text
EnkiSandbox 构造
  → client create(policy)
  → backend 创建沙箱实例
  → 返回 sandbox_id

Enkiball.run_command
  → client exec(sandbox_id, command, cwd, env, timeout)
  → backend 在共享预算下创建独立任务
  → client 返回输出与状态

取消单条命令
  → client cancel(execution_id)
  → backend 终止对应任务及其后代

销毁沙箱
  → client destroy(sandbox_id)
  → backend 终止全部任务并回收资源
```

建议使用版本化结构化数据传递策略，命令作为独立数据字段传输。传输格式、Unix socket 路径、client 参数名及退出码表仍待定案。

共享资源预算可以由沙箱级 cgroup 管理，每条任务使用子组。任务取消针对子组，实例销毁针对整个沙箱。具体机制由执行运行时决定。

共享沙箱意味着共享资源预算；shell、环境变量和命令 cwd 仍按任务传递。网络命名空间、临时目录和后台服务是否跨任务共享，需要明确为实例语义。

## 6 所有权与清理

以下为接口建议：

- 创建对象拥有沙箱生命周期；上下文退出时显式销毁。
- 连接对象关闭时释放连接，销毁操作需单独授权。
- 单个 Agent 关闭只释放其引用，共享沙箱继续运行。
- `agent.sandbox` 替换影响后续调用；旧任务保留原执行身份。
- backend 记录任务状态，支持取消、断连和异常退出后的回收。

对象 GC、client 断连、harness 崩溃和 backend 重启的处理需要具体协议。跨进程共享沙箱应采用明确的生命周期规则，避免仅依赖 Python 引用计数。

## 7 可用性与回退

用户已确定：当前系统缺少沙箱 client 时，SDK 忽略沙箱参数，按当前宿主用户直接执行。

对象应提供可检查的实际状态，拟定为：

```python
sandbox.execution_mode  # "sandbox" 或 "host"
sandbox.sandbox_id      # 宿主回退时为 None
sandbox.effective_policy
```

宿主回退时，共享内存与 CPU 预算也随之失效。harness 根据实际状态决定是否继续调度。

建议保持以下错误边界：client 已存在，但连接失败、授权失败、策略无效或后端执行失败时，返回对应错误。此类故障与 client 缺失分开处理。

Agent 工具描述使用实际模式和生效策略。配置固定后，默认无需单条命令的参数匹配、前缀匹配或增量授权流程。

旧版 `privilleged` 表示宿主执行，其兼容方式待迁移时确定。直接宿主执行使用当前用户身份。

## 8 权限与安装

基础 SDK 安装保持 Python 包形式。沙箱组件单独安装，并承担系统依赖检测和 backend 部署。

宿主权限用于创建环境与安装限制，普通任务在完成准备后使用其授权身份。backend 按连接者身份检查沙箱的创建、使用与销毁权限。

常驻服务提供权限边界，具体授权策略应限制调用者可请求的路径、设备及宿主能力。拥有沙箱 ID 与拥有该沙箱使用权限分别校验。

无 root 环境可能已有命名空间支持、cgroup 委派或系统管理员部署的 backend。可用能力以运行时检测结果为准。

client 与 backend 的版本兼容、安装路径、服务账户、升级和卸载流程需随系统组件一起定义。

## 9 执行运行时选型

独立沙箱组件是最终架构方向，底层运行时尚未确定。优先复用成熟工具，组件负责策略映射和共享实例生命周期。

| 方案 | 已知能力与定位 |
| --- | --- |
| bwrap | 文件挂载、命名空间、capability 和加载 seccomp；资源及细粒度网络策略需其他机制配合。 |
| systemd | 资源、设备、文件、capability、seccomp、网段和接口控制；需核对用户级权限及策略语义。 |
| Podman | 容器挂载、设备、capability、seccomp、资源限制；引入镜像或 rootfs 管理。 |
| NsJail | 命令式进程隔离、文件、seccomp、capability 与 cgroup 集成；需核对目标网络语义。 |

systemd 曾被考虑为每次命令创建临时 service。最终讨论转向持久沙箱实例与共享预算，因此每条命令创建独立、互不共享预算的 service 不再是目标模型。

systemd 的网段集合与网卡集合分别配置，不能直接替代任意配对规则；其 IP 过滤还涉及双向流量。运行时适配需保持策略语义一致。

参考资料：

- [bubblewrap](https://github.com/containers/bubblewrap/blob/main/README.md)
- [systemd 执行隔离](https://www.freedesktop.org/software/systemd/man/258/systemd.exec.html)
- [systemd 资源控制](https://www.freedesktop.org/software/systemd/man/257/systemd.resource-control.html)
- [systemd-run](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html)
- [Podman](https://docs.podman.io/en/latest/markdown/podman-run.1.html)
- [NsJail](https://github.com/google/nsjail)

## 10 当前代码与目标架构的差异

当前修改属于过渡实现，尚未实现上述 EnkiSandbox、client 和常驻 backend。

| 当前代码 | 当前作用 | 目标处理 |
| --- | --- | --- |
| `src/enkiball/sandbox.py` | Python 进程直接配置和执行 bwrap。 | 转为 SDK 对象及 client 调用适配。 |
| `src/enkiball/sandbox_policy.py` | 校验策略并编译 seccomp。 | SDK 保留接口校验，执行编译移至运行时。 |
| `src/enkiball/sandbox_resources.py` | 每条命令创建 cgroup。 | 迁出 Python 包，资源预算改为沙箱实例级。 |
| `src/enkiball/sandbox_bpf.py` | 编译和挂载网络、设备 BPF 过滤器。 | 优先由成熟运行时承担；定制实现需独立验证。 |
| `Enkiball.configure_sandbox` | 原地修改下一条命令的配置。 | 由创建或切换 EnkiSandbox 替代。 |
| `Enkiball.sandbox_context` | 返回当前配置。 | 从绑定对象获取身份、模式及生效策略。 |

最后一次全量测试结果为 294 项，291 项通过、3 项跳过。文件隔离和 seccomp 已实际执行验证，BPF 规则已验证编译；cgroup 资源限制、网络过滤、设备方向限制的特权集成测试仍待运行。

当前实现的其他限制：命令挂载源需已存在；部分 capability 组合被明确拒绝；系统委派和权限由外部准备。

## 11 待定事项与实现顺序

先定接口和语义，再选择运行时并迁移实现。

1. 确定 EnkiSandbox 构造参数、默认路径、显式宿主执行方式及旧 API 迁移方案。
2. 确定创建、连接、销毁的所有权，实例的临时文件、网络和后台进程共享语义。
3. 明确 SDK 原生文件工具的权限检查位置；它们当前绕过命令 backend 直接访问宿主文件。
4. 用共享内存预算、单任务取消和配对网络规则验证候选运行时。
5. 确定 client 协议、身份认证、版本和状态模型。
6. 实现组件安装、权限准备、故障恢复和资源回收。
7. 验证多 Agent 合计预算、跨进程连接、切换沙箱及缺少 client 的回退行为。

验收核心是：多个 Agent 的并发命令受同一预算约束，单任务取消保持其他任务运行，执行状态与实际生效策略一致。


## 12 v1 实施记录（2026-11-10）

- SDK 导出 `EnkiSandbox`，构造时创建实例，策略固定；提供 attach/inspect/cancel/destroy/close、host 和上下文管理。
- Agent 可共享或切换实例。默认不向显式 EnkiSandbox 自动授予 cwd 或 skill 目录；旧版 bool 配置通过兼容 adapter 转换为新实例。
- 系统实现迁入独立 `sandbox/` 目录，包含 ELF client、SO_PEERCRED 鉴权 backend、bubblewrap、实例 cgroup/任务子组、seccomp 和 cgroup BPF。
- 内置文件工具进入同一执行边界。实例共享临时目录；网络/PID/IPC/UTS 按任务隔离，后台进程随任务回收。宿主/过滤网络使用宿主网络命名空间。
- 相对策略路径固定到构造时 base_dir。不存在的可写路径必须声明 file/directory 类型。具体路径覆盖宽泛规则；受保护硬链接和冲突挂载别名拒绝。
- 文件/目录挂载点本身不允许删除或重命名；write 目录内部支持完整文件变更。文件级 write/编辑修改原 inode；需要 rename 原子替换或删除时授权父目录。
- 内存预算跨 Agent/命令合计，禁用该实例 swap，OOM 按实例终止任务并报告事件；CPU 使用委派 cpuset 中选定的逻辑处理器。
- owner 关闭销毁、attached 关闭 detach、Agent 关闭不销毁外部实例。GC 不管理生命周期；harness 崩溃保留实例供 attach，任务仍有 deadline；backend 重启清理旧组和临时数据，旧 ID 失效。
- SDK 只在 client 缺失时回退宿主并暴露空 effective_policy；已安装 client 的其他失败不回退。attach 缺少 client 报错。
- 安装/升级/卸载脚本、systemd unit、管理员 UID 授权上限、版本化 JSON 协议和退出码见 `sandbox/README.md`。
- 真实集成已验证共享内存 OOM、取消单条任务且保留其他任务、脱离进程组的后代回收、跨进程 attach、Agent 切换、命令和文件隔离、文件级编辑、大图、重复/并发 seccomp、超时和销毁。
- 已使用免密 sudo 启动临时 root backend，获得 memory/cpuset 委派和 BPF 权限；普通用户 UID 1000 连接后执行全部集成测试，验证 CPU 绑定、BPF 网络过滤与设备读写方向，20 项全部通过。

最终验证：常规测试 304 项（284 通过，20 项需运行时环境而跳过）；另行连接临时特权 backend 并启用 ENKISANDBOX_TEST_CPUSET=1、ENKISANDBOX_TEST_BPF=1 后，20 项真实集成测试全部通过，无跳过。backend 为 root，客户端及任务为普通用户 UID 1000。ELF 编译、基础静态检查、SDK wheel/sdist 与独立运行时 wheel 构建及包边界检查通过。临时测试 backend 已停止并清理。
