# S2-12 compatibility matrix

Audited at 2026-10-17T02:36:00+08:00; source snapshot `p2-41649744e057d61031d5`. This matrix separates catalog census closure from behavior compatibility and real-platform evidence.

## Gate decisions

| Gate | Result | Evidence boundary |
| --- | --- | --- |
| P2-CATALOG | PASS | 5,690 frozen entries: NoneBot 943, Koishi 4,747. |
| P2-CENSUS | PASS | 512/512 shards, 11,380/11,380 units, 68,280/68,280 terminal stage jobs; zero active, queued or leased work. |
| P2-AUTOMATION | BLOCKED | All jobs closed, but 2,158 Linux units recorded Docker Engine API unavailable; 796 Windows NoneBot units were blocked by App Control Win32 4551; 16 additional worker/preparation/timeout unknowns remain. |
| P2-BEHAVIOR | NOT EVALUATED | Federated and compare stages were skipped for all 11,380 units; no catalog behavior pass is established. |
| P2-FIELD | NOT RUN | No real-platform login, action, receipt or recipient evidence. |

## Catalog unit outcomes by ecosystem and platform

| Cohort | Denominator | Passed | Failed | Skipped | Unknown | Behavior conclusion |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| nonebot / linux | 943 | 0 | 97 | 433 | 413 | Not established |
| nonebot / windows | 943 | 0 | 0 | 147 | 796 | Not established |
| koishi / linux | 4747 | 0 | 208 | 2785 | 1754 | Not established |
| koishi / windows | 4747 | 0 | 575 | 4165 | 7 | Not established |
| **Total** | **11380** | **0** | **880** | **7530** | **2970** | **No catalog behavior pass** |

## Why unit-level passed is 0

The unit reducer returns `passed` only when all six selected stages pass. Every one of the 11,380 units has `federated=skipped` and `compare=skipped`, so no catalog unit qualifies as passed. The 24,914 passed SQLite stage jobs belong to prepare/install/native/cleanup work and use a separate job denominator; they do not establish a plugin compatibility pass.

## Selected stage job outcomes

| Stage | Passed | Failed | Skipped | Unknown |
| --- | ---: | ---: | ---: | ---: |
| prepare | 10332 | 0 | 1048 | 0 |
| install | 5081 | 249 | 3080 | 2970 |
| native | 4167 | 631 | 6582 | 0 |
| federated | 0 | 0 | 11380 | 0 |
| compare | 0 | 0 | 11380 | 0 |
| cleanup | 5334 | 0 | 3887 | 2159 |

SQLite job total: **68280** terminal jobs (passed 24914, failed 880, skipped 37357, unknown 5129); queued 0, leased 0. Job-level passed is stage-level and is not a plugin-unit compatibility pass.

## Failure and unknown classification

| Unit status | Reason code | Units |
| --- | --- | ---: |
| failed | `native_plugin_load_failed` | 631 |
| failed | `offline_npm_install_failed` | 153 |
| failed | `worker_nonzero_exit` | 96 |
| unknown | `app_control_win32_4551` | 796 |
| unknown | `worker_backend_unavailable` | 1 |
| unknown | `worker_cleanup_unconfirmed` | 2158 |
| unknown | `worker_preparation_failed` | 11 |
| unknown | `worker_timeout` | 4 |

The 2,158 `worker_cleanup_unconfirmed` outcomes all carry a Docker named-pipe API-unavailable diagnostic from the run environment. Docker Desktop was started after the completed run and `docker info` then reported 29.8.2; that later check is not treated as a rerun. The 796 Windows NoneBot unknowns carry the explicit App Control Win32 4551 restriction. Original S2-10 results remain immutable; the 2,056-unit S2-11 source attribution ledger and raw 512 S2-11 shard reports remain available. Replay commands are recorded in the machine-readable audit and original dispatcher script.

## Support claims and limits

- New catalog-wide supported-plugin claims: **0**.
- Retain only the four exact phase-one versions supported by [T18](t18-final-audit-20261012.json); phase two does not expand them.
- The S2-11 probe regression confirms that two frozen Koishi plugins calling `process.exit(0)` are reported as failed native loads, with Worker cleanup separately observed; it does not turn them into compatibility passes.
- 5,621 of 5,690 entries have no conservative behavior-category mapping; their behavior remains unevaluated.
- Real-platform delivery remains unverified. Virtual Worker results do not establish login, action, receipt or recipient behavior on a real platform.

Machine-readable audit: [phase2-s212-audit-20261017.json](phase2-s212-audit-20261017.json). Full report and queue checkpoint: [S2-11 checkpoint](phase2-s211-process-exit-regression-20261014.json). Failure provenance: [S2-11 attribution ledger](phase2-s211-failures-20261014.json).
