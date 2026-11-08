# 兼容性矩阵（自动生成，勿手改）

由 `python -m lab.compat_matrix build` 从已提交的证据生成；`check` 在证据变化而矩阵未重建时失败。每条非 `untested` 结论都引用证据文件、其 SHA-256 与文件内记录，见同目录 JSON。

| 能力 | 含义 | 证据来源 |
| --- | --- | --- |
| `load` | 隔离 Worker 中安装并原生加载，清理已确认（约 L0–L2） | S2-11 全量重跑，P3-10 恢复矩阵覆盖；AstrBot 取 AB-04 |
| `mcp_tool` | 命令作为 MCP 工具的工具级验收 | TOOL-04、AB-05 |
| `federated_event` | Native/Federated 双路径行为（约 L3–L4） | P4-01 v10、P4-02 v2r3 |
| `platform_delivery` | 回复送达真实聊天平台 | P6-01（仅正例）；P4-05 canary 作为历史 |

取值只有 `verified`、`failed`、`skipped`、`unknown`、`untested`；`scope` 写明覆盖范围。P4-03 的恢复只作注释，不改变 P3 结论。Windows `load` 来自 S2/P3 的 Windows Sandbox profile，不覆盖 App Control Enforcement 准入（P4-04）。本矩阵是证据索引，不是新的支持名单。

## 按生态与能力的计数

| 生态 | 条目 | 能力 | 计数（平台:取值） |
| --- | --- | --- | --- |
| astrbot | 3271 | `load` | linux:failed 1, linux:skipped 2, linux:verified 21, untested 3247 |
| astrbot | 3271 | `mcp_tool` | linux:verified 3, untested 3268 |
| astrbot | 3271 | `federated_event` | untested 3271 |
| astrbot | 3271 | `platform_delivery` | qq:verified 1, untested 3270 |
| koishi | 4747 | `load` | linux:failed 367, linux:skipped 2002, linux:unknown 39, linux:verified 2339, windows:failed 529, windows:skipped 1613, windows:verified 2605 |
| koishi | 4747 | `mcp_tool` | linux:verified 1, untested 4746 |
| koishi | 4747 | `federated_event` | linux:skipped 8, linux:verified 2, untested 4737 |
| koishi | 4747 | `platform_delivery` | qq:verified 1, untested 4746 |
| nonebot | 943 | `load` | linux:failed 217, linux:skipped 211, linux:verified 515, windows:failed 253, windows:skipped 676, windows:unknown 14 |
| nonebot | 943 | `mcp_tool` | linux:verified 1, untested 942 |
| nonebot | 943 | `federated_event` | linux:failed 1, linux:skipped 8, linux:verified 1, untested 933 |
| nonebot | 943 | `platform_delivery` | qq:verified 1, untested 942 |

## 有加载以外结论的插件

| 条目 | 生态 | 包 | 版本 | load | mcp_tool | federated_event | platform_delivery |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `ab-00d908510026dba4d2cc6acd` | astrbot | astrbot_plugin_gallery | 2.1.0 | linux: verified | linux: verified | untested | untested |
| `ab-01803ad6c618f2321ed12e48` | astrbot | astrbot_plugin_teamup | 1.0.5 | linux: verified | linux: verified | untested | qq: verified |
| `ab-026af14d509a0e3b248ae734` | astrbot | astrbot_plugin_pet | 1.6 | linux: verified | linux: verified | untested | untested |
| `ko-0a6452b59539c2d91ee57448` | koishi | @koishijs/plugin-config | 2.8.7 | linux: skipped<br>windows: skipped | untested | linux: skipped | untested |
| `ko-163c3bd4cf87c72c24210251` | koishi | @koishijs/plugin-echo | 2.2.5 | linux: verified<br>windows: verified | linux: verified | linux: verified | qq: verified |
| `ko-4fa1f6f3a9cfbddb2c218917` | koishi | koishi-plugin-guess-number | 1.2.1 | linux: verified<br>windows: verified | untested | linux: skipped | untested |
| `ko-5076fa7479872fd07f4b4fe9` | koishi | koishi-plugin-help-md | 1.2.0 | linux: verified<br>windows: verified | untested | linux: skipped | untested |
| `ko-8be6f0a7682e575c75a4b3e9` | koishi | @koishijs/plugin-database-sqlite | 4.7.0 | linux: skipped<br>windows: skipped | untested | linux: skipped | untested |
| `ko-97f39cf7212bea26b51ea8b3` | koishi | @koishijs/plugin-commands | 3.5.5 | linux: skipped<br>windows: skipped | untested | linux: skipped | untested |
| `ko-aa34b6d87f81a7e263607dbc` | koishi | koishi-plugin-calculator | 1.2.0 | linux: skipped<br>windows: verified | untested | linux: skipped | untested |
| `ko-f106ee0baf0f51431776f698` | koishi | koishi-plugin-countdown-clock | 1.0.1 | linux: skipped<br>windows: skipped | untested | linux: skipped | untested |
| `ko-f6dfe45f2f1b06768eb14b06` | koishi | koishi-plugin-simple-echo | 1.0.4 | linux: verified<br>windows: verified | untested | linux: skipped | untested |
| `ko-fb5aa9ada6cacd62ec595c66` | koishi | @koishijs/plugin-help | 2.4.6 | linux: verified<br>windows: verified | untested | linux: verified | untested |
| `nb-0b37b4bb782b82c00e95a87f` | nonebot | nonebot_plugin_help | 0.6.1 | linux: failed<br>windows: failed | untested | linux: failed | untested |
| `nb-10a76db9822976efe2679433` | nonebot | nonebot_plugin_word_bank2 | 0.2.1 | linux: verified<br>windows: skipped | untested | linux: skipped | untested |
| `nb-16c80a826ca035f6c49ad69a` | nonebot | nonebot_plugin_roll | 0.2.3 | linux: verified<br>windows: skipped | linux: verified | linux: verified | qq: verified |
| `nb-4528cf6bfb6a315278ba2dd7` | nonebot | nonebot_plugin_strman | 1.2.0 | linux: failed<br>windows: failed | untested | linux: skipped | untested |
| `nb-5f9a0398ba3644f4256bbe83` | nonebot | nonebot_plugin_guess | 0.1.9 | linux: verified<br>windows: skipped | untested | linux: skipped | untested |
| `nb-7a3163a76a06283efe3bb2c9` | nonebot | nonebot_plugin_morning | 0.3.2 | linux: failed<br>windows: failed | untested | linux: skipped | untested |
| `nb-85b3294c1381385091eb0488` | nonebot | nonebot_plugin_chatrecorder | 0.7.0 | linux: failed<br>windows: skipped | untested | linux: skipped | untested |
| `nb-85f9d46ff59c96d65dd652c8` | nonebot | nonebot_plugin_abbrreply | 1.1.3 | linux: verified<br>windows: skipped | untested | linux: skipped | untested |
| `nb-c24c2083f5b189704248845f` | nonebot | nonebot_plugin_localstore | 0.7.4 | linux: verified<br>windows: skipped | untested | linux: skipped | untested |
| `nb-da100d12327d862b56c82b1a` | nonebot | nonebot_plugin_alias | 0.4.0 | linux: verified<br>windows: skipped | untested | linux: skipped | untested |
