# T13 WSL Linux representative scan status

Date: 2026-10-10

The full 20-sample representative scan completed on Windows and is recorded in [`t13-representative-windows-full-20261010.json`](t13-representative-windows-full-20261010.json) with `gate=PASS` (10 NoneBot and 10 Koishi samples; four L4 passes, including two per ecosystem).

The first WSL attempt stopped during the Koishi `koishi-game-mini` install while a registry socket remained in `SYN-SENT` to `103.73.220.138:443`. A later Ubuntu 24.04.5 WSL run completed all 20 sample records, but its report is `gate=NOT READY`: 18 sample artifacts passed L0, while the two Koishi L4 candidates (`koishi-game-mini` and `koishi-multi-qa`) both timed out at L0 downloading their exact npm registry tarballs. The other 8 Koishi samples produced 6 L2/L3 passes; the remaining two had upstream/profile failures. No Koishi L4 result was established. See [`full WSL report`](t13-representative-wsl-full-20261010.json).

A separate WSL retry of both L4 candidates also completed with `gate=NOT READY`; both again timed out at L0 before dependency installation or runtime checks. Its report is [`candidate retry report`](t13-representative-wsl-l4-retry-20261010.json). A HEAD request to the registry root succeeded during investigation, but requests for the two package tarballs continued to stall from WSL; Windows direct access returned HTTP 200. This difference is evidence of a WSL egress limitation for those artifact URLs, not evidence that the Koishi plugins fail on Linux. No firewall or network configuration was changed.

T13 Linux execution is complete, but its Linux acceptance gate remains open until both Koishi L4 candidates can be downloaded and validated in WSL. T14's independent production-Core, Host, and Linux stability checks continue in WSL and do not replace the missing T13 L4 evidence.
