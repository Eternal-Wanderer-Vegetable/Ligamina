# 隔离插件 Worker

S2-04 worker backends execute entry workloads only inside an OS boundary. The Docker backend requires a locally available, digest-pinned image and a running Docker daemon. It uses `--network=none`, a read-only artifact mount, a read-only image root, a private writable tmpfs workspace, dropped Linux capabilities, no-new-privileges, CPU/memory/PID limits, bounded output capture, and explicit container cleanup checks. It never mounts the repository or Docker socket into the container, and it passes only a small fixed environment to the process inside the container.

The Windows backend uses the Windows Sandbox CLI (`wsb.exe`). Networking, vGPU, clipboard, printer, audio input, and video input are disabled. It starts with no mapped host folders, then shares the artifact and control directories read-only. The guest runs plugin code as a disposable standard user with a low-integrity token and a Windows Job Object with CPU/memory/active-process limits and kill-on-close. Only after the guest confirms process-tree cleanup does the host add a writable output share; a trusted SYSTEM collector copies the internal result to that share. This prevents plugin code from writing or spoofing the host result while it is running.

## Inputs

- `WorkerSpec.input_dir` must be an external, sanitized artifact directory outside the repository. Symlinks and junctions are rejected.
- `WorkerSpec.command` is an argument vector; no shell is invoked by the host.
- Images must use `repository@sha256:<64 lowercase hex>`; mutable image tags are rejected.
- Host environment variables are not passed to untrusted processes. No credentials are mounted.
- Artifact input is read-only. Writable scratch is disposable and scoped to a single run.

## Verification

`lab/tests/test_workers.py` checks generated policy and mount invariants without requiring Docker or launching Windows Sandbox. Runtime acceptance uses dedicated benign probes for read/write boundaries, network denial, resource limits, timeout, output overflow, process-tree cleanup, worker loss, and sibling isolation. Unit policy checks are not runtime isolation evidence; an unavailable engine remains `unknown` and cannot pass an OS gate.

Runtime acceptance is recorded in [the S2-04 worker report](../../outputs/acceptance/phase2-workers-20261012.json), with per-platform reports beside it. Linux used the pinned Python 3.12.14 slim image digest and passed boundary, network, resource, timeout, output, process-tree, worker-loss, and sibling-isolation probes. Windows used the Windows Sandbox CLI and passed its corresponding boundary, timeout, output, process-tree, worker-loss, and sibling probes. The Windows runner deliberately shares no writable host directory until the guest confirms its process tree has exited; see [Microsoft's Windows Sandbox CLI documentation](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-cli).

WSL user/mount namespaces were evaluated but are not accepted as the Linux boundary: the namespace root could remount a read-only bind mount read-write. The reproducible negative result is in [the WSL boundary report](../../outputs/acceptance/phase2-wsl-userns-boundary-20261012.json). These benign worker probes do not run catalog plugins; full-catalog behavior remains future work.
