# 历史与验收证据索引

适用：查阅特定日期下的设计、执行和验收。当前状态见 [progress](../../progress.md) 与 [T18 最新裁决](../../outputs/acceptance/t18-final-audit-20261012.json)。旧 NOT READY、skipped 和中断结果保留原文。

## 历史 Markdown

- [outputs/2026-10-09-ligamina-discussion-decisions.md](../../outputs/2026-10-09-ligamina-discussion-decisions.md)
- [outputs/2026-10-09-ligamina-engineering-plan.md](../../outputs/2026-10-09-ligamina-engineering-plan.md)
- [outputs/2026-10-09-ligamina-initial-plan.md](../../outputs/2026-10-09-ligamina-initial-plan.md)
- [outputs/2026-10-09-ligamina-mvp-checklist.md](../../outputs/2026-10-09-ligamina-mvp-checklist.md)
- [outputs/2026-10-09-ligamina-mvp-contracts.md](../../outputs/2026-10-09-ligamina-mvp-contracts.md)
- [outputs/2026-10-09-workspace-migration.md](../../outputs/2026-10-09-workspace-migration.md)
- [outputs/2026-10-10-ligamina-t13-third-party-sample-research.md](../../outputs/2026-10-10-ligamina-t13-third-party-sample-research.md)
- [outputs/acceptance/t13-wsl-network-limitation-20261010.md](../../outputs/acceptance/t13-wsl-network-limitation-20261010.md)
- [outputs/acceptance/phase2-s211-failures-20261014.md](../../outputs/acceptance/phase2-s211-failures-20261014.md)
- [outputs/acceptance/windows-sandbox/README.md](../../outputs/acceptance/windows-sandbox/README.md)

## 设计记录

- [ADR 0001](../adr/0001-baseline.md)：2026-10-09/10 工具链、协议与阶段性实现；其中旧 G2 状态不是当前裁决。

## 第一阶段关键验收

- [T18 / C01–C15](../../outputs/acceptance/t18-final-audit-20261012.json)
- [Windows 代表样本](../../outputs/acceptance/t13-representative-windows-full-20261010.json)
- [WSL Linux 代表样本](../../outputs/acceptance/t13-representative-wsl-final-20261010.json)
- [WSL 24 小时稳定性](../../outputs/acceptance/t14-stability-linux-wsl-100x24h-20261011.json)
- [Windows Sandbox C14](../../outputs/acceptance/t15-windows-sandbox-c14-20261011.json)
- [Linux 容器 smoke](../../outputs/acceptance/t16-linux-wsl-container-20261010.json)

## 第二阶段目录实验

- [S2-10 全快照 census](../../outputs/acceptance/phase2-s210-census-interim-20261012.json)
- [S2-11 失败与未知归因](../../outputs/acceptance/phase2-s211-failures-20261014.json)
- [S2-11 定点 Worker 回归](../../outputs/acceptance/phase2-s211-process-exit-regression-20261014.json)
- [S2-12 最终审计与 gate 裁决](../../outputs/acceptance/phase2-s212-audit-20261017.json)
- [S2-12 官方目录兼容矩阵](../../outputs/acceptance/phase2-s212-compatibility-matrix-20261017.md)

## 第三阶段 gate 审计

- [P3-10 五 gate 审计](../../outputs/acceptance/phase3-p310-gate-audit-20261026.json)：独立复算 P3-05/P3-08 分母，分别裁决 engine、recovery、automation、behavior 与 field gate。
- [P3-10 受影响单元矩阵](../../outputs/acceptance/phase3-p310-compatibility-matrix-20261026.md)：3,179 个 P3-05 unique units 的终态、归因、清理状态与来源报告。
- [P3-08 双平台行为账本](../../outputs/acceptance/phase3-p308-behavior-accounting-20261026.json)：120 个位置、360 个 attempt slots；缺少 executable Host fixture，0 dispatch，behavior gate 未评估。

其他 JSON、锁和 traces 保留在 outputs/acceptance 下，由相应报告和历史文档引用；S2-11 复跑使用独立文件名和 SQLite 队列，不能覆盖旧 S2-10 acceptance。
