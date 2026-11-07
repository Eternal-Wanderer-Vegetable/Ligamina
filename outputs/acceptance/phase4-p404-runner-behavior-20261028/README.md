# P4-04 Windows Sandbox runner-only verification

## Scope change

On 2026-10-28 the user narrowed P4-04 / `feat-029` to verify the actual `WindowsSandboxWorker` and guest `runner.ps1` behavior. A public signing certificate, catalog plugin admission, and Windows plugin compatibility are outside this run. The earlier Event 3077 unsigned-PE block remains historical evidence and is not counted as a runner result.

The probe reuses the seven benign scenarios in `deploy/lab/windows/probes/acceptance.py`: filesystem/network/secret boundaries, process-tree cleanup, nonzero exit, sibling execution after failure, timeout, output overflow, and Sandbox loss. Commands use Windows system executables. The run does not change the host or guest App Control policy and does not execute catalog plugins.

## Reproduction

Run from the repository root on Windows with Windows Sandbox CLI available:

```powershell
$runId = Get-Date -Format 'yyyyMMddTHHmmss'
$env:UV_PROJECT_ENVIRONMENT = ".work/phase4-p404-runner-behavior-$runId"
uv run --locked --project lab --python 3.12.14 python scripts/phase4-p404-runner-behavior.py --output "outputs/acceptance/phase4-p404-runner-behavior-$runId.json"
```

The command refuses to overwrite either the report or its `.sha256` sidecar. Each probe starts its own ephemeral Windows Sandbox guest and the worker backend stops it during cleanup.

## Result

The seven-case run completed on 2026-10-28 from 14:44:40 to 14:47:50 +08:00. The Windows host reported `Windows-11-10.0.26200-SP0`; all seven expected outcomes matched and each cleanup was confirmed.

| Probe | Observed result | Cleanup |
| --- | --- | --- |
| Filesystem/network/secret boundaries | `passed`, exit 0 | confirmed |
| Process-tree cleanup | `passed`, exit 0 | confirmed |
| Nonzero exit | `failed / worker_nonzero_exit`, exit 17 (expected) | confirmed |
| Sibling after failure | `passed`, exit 0 | confirmed |
| Timeout | `unknown / worker_timeout` (expected) | confirmed |
| Output overflow | `unknown / worker_output_truncated` (expected) | confirmed |
| Sandbox loss | `unknown / worker_lost` (expected after controlled stop) | confirmed |

The report is [`phase4-p404-runner-behavior-20261028.json`](../phase4-p404-runner-behavior-20261028.json), SHA-256 `c0652d11bd8a763478cdd2151275c9d930f96198c3cf70664c73a87339d74f45`; its sidecar matches. The last worker-loss case records Windows Sandbox error `0x80370105` after the test intentionally stopped that guest. The report binds source SHA-256 values, host details, Git head, per-case observations, and cleanup states.

After the runner returned, `wsb list --raw` reported `WindowsSandboxEnvironments: []`; no Sandbox guest remained active.

`passed_scoped` means only that the runner contract probes passed with cleanup confirmed. It does not mean App Control enforcement acceptance, Windows plugin admission, catalog compatibility, or production readiness. All seven probe cases completed with their expected classifications, so P4-04 / `feat-029` is complete only for this amended runner-only scope.
