# EnkiSandbox runtime v1

独立安装的 Linux 运行时。Python SDK 不包含 bubblewrap、cgroup、BPF 或特权准备代码。运行时使用系统 Python、bubblewrap、cgroup v2；client 是 C 编译的 ELF，backend 是独立 Python 包，不依赖 Enkiball。

## 安装、升级、卸载

系统依赖：Python >= 3.10（含 venv/pip）、C 编译器、make、bubblewrap、systemd、Linux cgroup v2 和 `cgroup.kill`（Linux >= 5.14）。seccomp 需要 libseccomp；按网段/网卡过滤及设备规则需要 libbpf、支持 BPF target 的 clang，以及宿主 BPF 权限。

```sh
git clone <repository-url>
cd Enkiball
sudo ./sandbox/install.sh
```

安装器编译 `/usr/local/bin/enkisandbox-client`，在 `/opt/enkisandbox` 安装独立 backend，部署并启动 `enkisandbox.service`。服务以 root 完成挂载源准备、cgroup/BPF 操作，然后在独立启动子进程中进入任务 cgroup、设置调用者 UID/GID/组和 `no_new_privs`，最后运行 bubblewrap。SDK 与 harness 留在服务预算之外。

`/etc/enkisandbox/authorization.json` 默认 `{}`，普通用户无权创建实例。将用户加入 `enkisandbox` 组，并由管理员按数字 UID 配置授权上限，例如：

```json
{
  "1000": {
    "read_paths": ["/data/reference"],
    "write_paths": ["/work"],
    "devices": [],
    "capabilities": [],
    "networks": [{"outbound": [{"interface": "lo", "destinations": ["127.0.0.0/8", "::1/128"]}]}],
    "resources": {"memory_bytes": 8589934592, "cpu_count": 4}
  }
}
```

此处 `resources` 是必须声明的单实例预算上限；省略管理员上限表示不额外限制该项。多个实例预算可以累加。网络授权比较规范化后的完整策略；若允许共享宿主网络，在 `networks` 中显式加入 `"all"`。设备按路径和方向精确匹配。其他 UID 即使知道 sandbox ID 也不能 inspect、exec、cancel 或 destroy。

修改授权后执行 `sudo systemctl restart enkisandbox`。升级再次运行安装器，然后 `sudo systemctl restart enkisandbox`。重启会终止旧任务并使旧 ID 失效。卸载：`sudo ./sandbox/uninstall.sh`；保留授权配置和系统组。

无 root 时，可使用管理员部署的服务；也可在自己的 systemd 用户服务中运行 backend，前提是获得所需 cgroup 委派。相同 UID 的服务只拥有该 UID 的宿主权限。示例（在仓库根目录运行）：

```sh
make -C sandbox
systemd-run --user --unit enkisandbox-user --property=Delegate=yes \
  --setenv="PYTHONPATH=$PWD/sandbox" /usr/bin/python3 -m enkisandbox_runtime.backend \
  --socket "$XDG_RUNTIME_DIR/enkisandbox/control.sock" \
  --state "$XDG_RUNTIME_DIR/enkisandbox/state"
export ENKISANDBOX_CLIENT="$PWD/sandbox/build/enkisandbox-client"
export ENKISANDBOX_SOCKET="$XDG_RUNTIME_DIR/enkisandbox/control.sock"
```

用户级服务通常没有 BPF 权限；上级未委派 cpuset 时不能请求 `cpu_count`。backend 不降低请求的限制。

## SDK

```python
from enkiball import Enkiball, EnkiSandbox

with EnkiSandbox(
    filesystem=[
        {"path": "/work", "access": "write"},
        {"path": "/work/.env", "access": "deny"},
        {"path": "/data/reference", "access": "read"},
    ],
    network={"outbound": []},
    resources={"memory_bytes": 8 * 1024**3, "cpu_count": 4},
) as sandbox:
    agents = [Enkiball(sandbox=sandbox, cwd="/work") for _ in range(20)]
    print(sandbox.execution_mode, sandbox.sandbox_id, sandbox.effective_policy)
    for agent in agents:
        agent.shutdown()  # 不销毁外部传入的共享 sandbox
```

`EnkiSandbox.attach(id)` 取得现有实例的实际策略，关闭时只 detach。创建对象 `close()` / 上下文退出执行 destroy。attached 对象显式 `destroy(allow_attached=True)` 才发送销毁请求；服务仍检查 UID。fork 继承的对象也不自动销毁。GC 不负责销毁；harness 崩溃后实例保留，当前任务按超时回收，可由相同 UID attach 后显式清理。backend 重启清理自身 cgroup 下的旧实例和临时目录。

`agent.sandbox = other` 仅替换后续工具调用的绑定，执行中的命令继续使用旧实例。策略不能原地修改。默认不自动授权 cwd；相对策略路径在构造时相对 `base_dir`（默认当前目录）规范化。`EnkiSandbox.host()` 显式使用宿主身份和环境。

client 缺失时构造得到 `execution_mode="host"`、`sandbox_id=None`、`effective_policy={}`，所有请求限制失效。client 存在但不可连接、版本不兼容、授权失败或策略失败时抛异常。`attach` 缺少 client 时也报错，不能用新的宿主身份冒充已有实例。

## 策略与实例语义

- `filesystem`: `read` / `write` / `deny`，更具体路径优先、重复路径拒绝。默认拒绝；运行时基础只读路径由 `inspect.base_filesystem` 和 `effective_policy.filesystem` 显示。`/proc`、最小 `/dev`、实例级 `/tmp` 另在 `runtime_mounts` 中显示。
- `write` 目录允许在目录内创建、删除、重命名；显式挂载点本身不能删除/重命名。文件级 `write` 修改原 inode，`edit` 在文件挂载点原地写入，不能提供 rename 原子替换；需要原子替换或删除文件时授权其父目录。
- 已存在的文件和目录均可挂载。尚不存在的可写路径必须指定 `type="file"` 或 `"directory"`，backend 以调用者身份创建，失败则不创建实例。创建出的宿主路径属于用户数据，销毁不删除。
- 根目录、`/proc`、`/dev`、`/sys`、`/tmp` 是运行时保留挂载点。文件策略不能挂载宿主 `/proc`、`/sys`、`/dev` 的子路径或 backend 状态/连接目录及其祖先；设备通过 devices 授权。可授权 `/tmp` 下的具体工作目录。实例 `/tmp` 与宿主 `/tmp` 不同；未授权的同名临时路径可以在私有 `/tmp` 中创建，不会修改宿主同名路径。
- 路径在创建时解析符号链接，源使用 FD 固定；沙箱内符号链接只能访问可见挂载。存在硬链接的受保护文件、可见的冲突挂载别名会拒绝。每条任务启动前复查。宿主同 UID 的外部进程可以修改已授权内容及别名；它们属于可信管理边界。实例隔离不防御同 UID 的恶意宿主管理进程。
- `/tmp` 跨任务共享。shell、cwd、env、PID/IPC/UTS 和隔离网络命名空间按任务创建。任务 shell 结束、超时或取消后，整个任务 cgroup 回收，包括 setsid/双 fork 后代；不保留跨任务后台服务。需要服务时保持该任务存活，最大单任务超时 600 秒。
- `network={"outbound": []}` 使用任务私有网络（其回环不是宿主回环）；`"all"` 共享宿主网络；非空 `outbound` 共享宿主网络并在实例 cgroup 安装 egress BPF。单条规则的 CIDR 和出口 interface 取交集，各规则取并集，IPv4/IPv6 默认拒绝。DNS 也需要授权其目标地址。不限制入站流量。
- interface 按创建时宿主 ifindex 固定。每次执行及执行期间检查接口名到 ifindex 的关系，改变或消失则失败，需要新实例。路由改变后仍按数据包实际出口过滤。允许的本机代理可继续转发；Unix socket、已授权目录中的 IPC 不属于 IP egress 规则。
- `devices`: `read` / `write` / `readwrite`；BPF 检查设备 major/minor/type 及方向，默认仅保留基础设备和显式设备，冲突别名拒绝。ioctl 仍由驱动检查。
- capabilities 仅在沙箱用户命名空间中生效。v1 接受 `NET_ADMIN`、`NET_RAW`、`NET_BIND_SERVICE`（仅隔离网络）和 `IPC_LOCK`；其余能力明确拒绝，不授予宿主 capability。
- seccomp 支持 `default="allow"` + `deny` 或 `default="deny"` + `allow`。拒绝返回 EPERM；allowlist 必须包含 shell/程序启动所需 syscall，每条任务继承同一过滤策略的独立副本。
- 实例 cgroup 持有总内存预算、零 swap、`memory.oom.group=1`；OOM 终止实例内任务，实例可再次执行。`inspect.resources` 报告内存事件，执行结果报告 `oom_killed`。CPU 从委派集合与 backend affinity 的交集中选定，绑定给实例所有任务；逻辑处理器不独占。
- 内置 read/write/edit/image 在 backend 的同一命令隔离和预算下运行。远端需有 Python 3，使用宿主 `/usr/bin/python3` 运行文件操作。MCP、自定义 Python 工具、skill 文档加载及 CLI debug 是 harness 功能，不自动进入沙箱。

## 协议 v1

client 从 stdin 读取一个 UTF-8 JSON 对象，连接 Unix stream socket，以写半关闭标识请求结束，stdout 返回一个 JSON 对象。默认 socket `/run/enkisandbox/control.sock`；`--socket PATH` 或 `ENKISANDBOX_SOCKET` 覆盖。服务使用 `SO_PEERCRED` 认证，不接受请求内的身份字段。

```sh
printf '%s\n' '{"version":1,"op":"create","policy":{"filesystem":[],"network":{"outbound":[]}}}' | enkisandbox-client
printf '%s\n' '{"version":1,"op":"inspect","sandbox_id":"<32 lowercase hex digits>"}' | enkisandbox-client
```

每个请求包含 `version:1` 和 `op`。响应为 `{"version":1,"ok":true,"result":{...}}` 或 `{"version":1,"ok":false,"error":{"type":"...","message":"..."}}`。

| op | 字段及结果 |
| --- | --- |
| create | `policy`；返回 ID、实际策略、执行模式、资源状态 |
| inspect | `sandbox_id`；返回状态和最近执行记录 |
| exec | `sandbox_id`, `command`, `cwd`；可选 `execution_id`, `env`, `timeout_sec`, `stdin`, `output_limit`；立即返回 execution ID |
| wait | `sandbox_id`, `execution_id`；最多等 250 ms，返回 running / exited / cancelled / timeout / failed 及完成输出 |
| cancel | 两个 ID；只终止对应任务及后代 |
| destroy | `sandbox_id`；终止全部任务、删除组和临时目录 |

命令作为 JSON 字段传递，client 不将其拼进自己的 shell。`stdin` 上限 1 MiB，request 上限 48 MiB；默认每个输出流 1,000,000 bytes，截断时继续排空管道。显式 `output_limit` 最大 32 MiB，用于图片。最多 128 个实例、每实例 128 个运行/保留任务，旧完成记录按需淘汰；超过 64 MiB 的已完成输出在后续 exec 时淘汰。exec 成功后断开 client 不取消任务，允许 attach/poll；SDK 中断显式 cancel，连接故障时 backend 仍执行 deadline。后台状态仅保存在内存。

client 退出码：0 表示传输成功（仍需检查 JSON `ok`），64 参数错误，69 连接失败，74 I/O 错误。协议版本不兼容必须报错；升级时 SDK、client、backend 都使用 v1。

## 验证

```sh
make -C sandbox
python -m unittest discover -s tests -q
ENKISANDBOX_CLIENT="$PWD/sandbox/build/enkisandbox-client" \
ENKISANDBOX_TEST_SOCKET="$XDG_RUNTIME_DIR/enkisandbox/control.sock" \
python -m unittest discover -s tests -p test_sandbox_integration.py -v
```

有 cpuset 委派时加 `ENKISANDBOX_TEST_CPUSET=1`；有宿主 BPF 权限时加 `ENKISANDBOX_TEST_BPF=1`。测试实际验证共享预算、单任务取消、跨进程连接、切换、文件工具隔离、路径掩蔽、重复 seccomp、输出截断和销毁。不要把未运行的特权测试视为已验证。

2026-11-10 验证记录：通过免密 sudo 创建临时 systemd 系统服务，委派 memory/cpuset 并加载 cgroup BPF；客户端和任务保持普通用户 UID 1000。开启 CPUSET/BPF 测试开关后，20 项真实集成测试全部通过，无跳过。临时服务及运行目录已清理。
