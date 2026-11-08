# ADR 0003：Host 受信任等级与 Core 强制的容器隔离

- 日期：2026-11-08
- 状态：已采纳；实施与验收见[第六阶段计划](../plans/2026-11-08-phase-6-runtime-reliability.md) P6-03
- 关联：[ADR 0002](0002-plugins-as-tools.md)（插件统一为工具面）

## 背景

兼容性实验室一直在 OS 隔离 Worker 中运行目录插件：固定镜像摘要、禁网、只读根、资源上限、清理确认。生产 Core 的 `LocalCommand` 只负责启动和管理进程树，**不提供 OS 级隔离**。

第五、六阶段的现场联调已经在用容器：Core 的 Host 配置把 `program` 写成 `wsl.exe` 或 `docker`，`args` 里手写 `docker run --network none --read-only …`。这有三个问题：

- 隔离完全靠配置作者手写，Core 无法区分“容器里的插件”和“直接在宿主上跑的插件”，漏一个 `--network none` 也不会被发现。
- `docker` 客户端进程被结束时，容器本身未必退出（插件卡住、不响应 stdin 关闭），Core 不会清理。
- Core 被强杀后重启，上一轮遗留的容器没有任何回收机制。

## 决策

1. **每个 Host 有受信任等级 `trust`。**
   - `trusted`：自写或已审阅的插件。可以用 `program` / `args` 直接作为本地进程运行，也可以声明 `isolation`。
   - `untrusted`：目录插件等第三方代码。**必须**声明 `isolation`，不得声明 `program`、`args`、`current_dir`、`environment`；Core 自己生成容器命令。
   - 缺省值：声明了 `isolation` 时为 `untrusted`，否则为 `trusted`。保持既有配置可用；启动时对每个未隔离的 Host 打一条 WARNING，说明它直接运行在宿主上。
2. **隔离由 Core 生成并强制，不由配置手写。** `isolation` 只描述“用哪个镜像、挂哪些目录、给多少资源”，Core 据此生成 `docker run`，固定加上：`--rm -i`、`--network none`、`--read-only`、`--cap-drop ALL`、`--security-opt no-new-privileges`、`--pids-limit`、`--memory`、`--cpus`、唯一的 `--name` 与归属标签。
3. **Core 拒绝不安全的隔离配置，启动失败并给出原因。** 规则：
   - 镜像必须按摘要固定（`name@sha256:<64 位十六进制>`）。
   - 绑定挂载一律只读；目标路径必须是容器内绝对路径，且不得覆盖 `/`、`/proc`、`/sys`、`/dev`。
   - `memory_mb`、`cpus`、`pids_limit` 必须给出且为正数。
   - 网络只允许 `none`（本阶段不提供网络白名单）。
   - 未知字段拒绝（`deny_unknown_fields`），避免拼错的安全字段被静默忽略。
   - 被拒绝的配置让守护进程以非零状态退出，错误信息写明 Host ID 与违反的规则，这就是拒绝记录。
4. **容器生命周期由 Core 收尾。**
   - 每次启动的容器名为 `ligamina-<host>-<spawn>`，`<spawn>` 每次启动都不同，带标签 `io.ligamina.host=<host>` 与 `io.ligamina.owner=<owner>`（`owner` 由数据库路径派生，同一台机器上多个守护进程互不干扰）。
   - Host 进程退出、被停止或重启后，Core 执行 `docker rm -f <name>`（尽力而为，有超时，失败写日志）。
   - 守护进程启动时先按 `owner` 与 Host 标签清理上一轮遗留的容器（覆盖 Core 被强杀的情形）。
5. **调用 docker 的方式可配置。** `isolation.docker` 是命令前缀，缺省 `["docker"]`；Windows 上经 WSL 时写 `["C:\\Windows\\System32\\wsl.exe", "-e", "docker"]`。Core 不经 shell 拼接任何命令。

## 后果

- 目录插件只能以 `untrusted` 运行：配置里不再能“忘记”隔离，生成的命令可在测试中逐项断言。
- 现场联调与实验室使用同一套规则；`lab/field/three_host_config.py` 改用 `isolation` 声明，不再手写 `docker run`。
- 不覆盖的范围：Windows 原生隔离（Windows Sandbox / App Control）仍按 P4-04 的门槛单独验收；网络白名单、seccomp/AppArmor 自定义配置、rootless 检测不在本阶段。`trusted` Host 的安全性仍取决于配置者，本 ADR 只让这一点显式化。
- 实验室 Worker（`lab/workers/linux.py`）有自己的准入与清理确认，不受本 ADR 改动影响。
