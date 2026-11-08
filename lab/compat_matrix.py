"""Per-plugin, per-capability compatibility matrix built from committed evidence (P6-04).

    python -m lab.compat_matrix build   # rewrite outputs/compat/compatibility-matrix-v1.{json,md}
    python -m lab.compat_matrix check   # fail if the committed matrix is stale or invalid

Every claim other than ``untested`` cites the evidence file it came from, that file's
SHA-256 and the record inside it. The matrix records the SHA-256 of every source, so
``check`` fails as soon as any evidence file changes without the matrix being rebuilt.
Nothing here runs plugins; it only reads reports under ``outputs/acceptance`` and the
frozen catalogs.

Capabilities (values: verified / failed / skipped / unknown / untested):

- ``load`` per platform: install and native load in an isolated Worker. Koishi/NoneBot
  from the S2-11 full rerun, overridden by the P3-10 recovery matrix; P4-03 recoveries
  are annotations only (feat-028 keeps the P3 verdict). AstrBot from the AB-04 pilot.
- ``mcp_tool``: commands called as MCP tools (TOOL-04, AB-05).
- ``federated_event``: Native/Federated paired behavior (P4-01 v10, P4-02 v2r3).
- ``platform_delivery``: replies delivered to a real chat platform (P6-01 positive round
  plus the scenario round: negative/authority, Core restart, Host timeout; duplicate
  redelivery not covered; earlier P4-05 canaries stayed unknown and are listed as history).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = "outputs/acceptance"
OUTPUT_JSON = "outputs/compat/compatibility-matrix-v1.json"
OUTPUT_MD = "outputs/compat/compatibility-matrix-v1.md"
SCHEMA = "contracts/lab/compat/v1/compatibility-matrix.schema.json"
VALUES = ("verified", "failed", "skipped", "unknown", "untested")
CAPABILITIES = ("load", "mcp_tool", "federated_event", "platform_delivery")

P2_CATALOG = "catalogs/p2-41649744e057d61031d5"
AB_CATALOG = "catalogs/ab-15937e90d26b7caf9d12"
S211_GLOB = "phase2-s211-rerun-census-*-shard*of256-20261014.json"
P310_MATRIX = f"{ACCEPTANCE}/phase3-p310-compatibility-matrix-20261026.md"
P403_RUNS = [
    f"{ACCEPTANCE}/phase4-p403-linux-recovery-run-40479738802.json",
    f"{ACCEPTANCE}/phase4-p403-linux-recovery-run-40513327262.json",
    f"{ACCEPTANCE}/phase4-p403-run-40525975905/phase4-p403-linux-recovery-run-40525975905.json",
]
AB04 = f"{ACCEPTANCE}/ab04-astrbot-linux-pilot-20261108-v4.json"
TOOL04 = f"{ACCEPTANCE}/tool04-tool-surface-linux-20261107-v1.json"
AB05 = f"{ACCEPTANCE}/ab05-astrbot-tool-linux-20261108-v2.json"
P401 = f"{ACCEPTANCE}/phase4-p401-behavior-runs-20261026-v10.json"
P402 = f"{ACCEPTANCE}/phase4-p402-behavior-runs-20261026-v2r3.json"
P601 = f"{ACCEPTANCE}/p6-p601-qq-mcp-field-20261108.json"
P601_SCENARIOS = f"{ACCEPTANCE}/p6-p601-qq-scenarios-20261108.json"
P405_CANARY_GLOB = "phase4-p405-live-20261029/phase4-p405-koishi-live-canary-20261029*.json"
# P6-01 names plugins by package@version / artifact hash; resolved against the catalogs.
P601_PLUGINS = {
    "koishi": ("@koishijs/plugin-echo", "2.2.5"),
    "nonebot": ("nonebot_plugin_roll", "0.2.3"),
    "astrbot": ("astrbot_plugin_teamup", None),
}


class Sources:
    """Loads evidence files once and records their SHA-256."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.hashes: dict[str, str] = {}
        self._json: dict[str, Any] = {}

    def _read(self, rel: str) -> bytes:
        data = (self.root / rel).read_bytes()
        self.hashes[rel] = hashlib.sha256(data).hexdigest()
        return data

    def json(self, rel: str) -> Any:
        if rel not in self._json:
            self._json[rel] = json.loads(self._read(rel).decode("utf-8-sig"))
        return self._json[rel]

    def text(self, rel: str) -> str:
        return self._read(rel).decode("utf-8")

    def glob(self, pattern: str) -> list[str]:
        return sorted(p.relative_to(self.root).as_posix() for p in (self.root / ACCEPTANCE).glob(pattern))

    def ref(self, rel: str, pointer: str) -> dict[str, str]:
        if rel not in self.hashes:
            self._read(rel)
        return {"path": rel, "sha256": self.hashes[rel], "ref": pointer}


def claim(value: str, evidence: list[dict], reason: str | None = None, **extra: Any) -> dict:
    assert value in VALUES, value
    out = {"value": value, "reason": reason, "evidence": evidence}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def entries_from_catalogs(src: Sources) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    p2_lock = {e["registry_entry_id"]: e for e in src.json(f"{P2_CATALOG}/artifacts.lock.json")["entries"]}
    for e in src.json(f"{P2_CATALOG}/catalog.json")["entries"]:
        lock = p2_lock.get(e["registry_entry_id"], {})
        digests = sorted({
            a["artifact"]["computed_digests"]["sha256"]
            for a in lock.get("artifacts") or []
            if a.get("artifact") and (a["artifact"].get("computed_digests") or {}).get("sha256")
        })
        entries[e["registry_entry_id"]] = {
            "registry_entry_id": e["registry_entry_id"], "ecosystem": e["ecosystem"],
            "package": e["package"], "version": e["advertised_version"],
            "artifact_sha256": digests, "catalog_status": lock.get("status"),
        }
    ab_lock = {e["registry_entry_id"]: e for e in src.json(f"{AB_CATALOG}/artifacts.lock.json")["entries"]}
    for e in src.json(f"{AB_CATALOG}/catalog.json")["entries"]:
        lock = ab_lock.get(e["registry_entry_id"], {})
        sha = ((lock.get("artifact") or {}).get("computed_digests") or {}).get("sha256")
        entries[e["registry_entry_id"]] = {
            "registry_entry_id": e["registry_entry_id"], "ecosystem": "astrbot",
            "package": e["package"], "version": e["advertised_version"],
            "artifact_sha256": [sha] if sha else [], "catalog_status": lock.get("status"),
        }
    return entries


def load_from_s211(record: dict) -> tuple[str, str | None]:
    stages = record.get("stage_statuses") or {}
    if record.get("unit_status") == "unknown":
        return "unknown", record.get("reason_code")
    core = [stages.get(name) for name in ("prepare", "install", "native")]
    if all(s == "passed" for s in core) and stages.get("cleanup") == "passed":
        return "verified", None
    if "failed" in core or record.get("unit_status") == "failed":
        return "failed", record.get("reason_code")
    return "skipped", record.get("reason_code")


P310_VALUE = {"passed": "verified", "failed": "failed", "skipped": "skipped", "unknown": "unknown"}


def build(root: Path = ROOT) -> dict:
    src = Sources(root)
    entries = entries_from_catalogs(src)
    caps: dict[str, dict] = {rid: {} for rid in entries}

    # load: S2-11 base layer
    for rel in src.glob(S211_GLOB):
        for i, record in enumerate(src.json(rel)["plugin_reports"]):
            rid, platform = record["registry_entry_id"], record["platform"]
            value, reason = load_from_s211(record)
            caps[rid].setdefault("load", {})[platform] = claim(
                value, [src.ref(rel, f"plugin_reports[{i}] result_id={record['result_id']}")], reason)

    # load: P3-10 recovery matrix overrides
    for line in src.text(P310_MATRIX).splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 9 or cells[0] not in ("linux", "windows"):
            continue
        platform, _eco, rid, _pkg, _ver, status, reason, cleanup, source = cells
        caps[rid].setdefault("load", {})[platform] = claim(
            P310_VALUE[status],
            [src.ref(P310_MATRIX, f"row platform={platform} registry_id={rid}"), src.ref(source, f"registry_entry_id={rid}")],
            None if status == "passed" else reason, cleanup=cleanup)

    # load: P4-03 recoveries are annotations; the P3 verdict stands (feat-028)
    for rel in P403_RUNS:
        for i, record in enumerate(src.json(rel)["plugin_reports"]):
            load = caps[record["registry_entry_id"]].setdefault("load", {}).get("linux")
            if load is not None:
                load.setdefault("annotations", []).append({
                    "recovery_status": record.get("recovery_status"),
                    "evidence": src.ref(rel, f"plugin_reports[{i}]"),
                })

    # load: AstrBot AB-04
    for i, record in enumerate(src.json(AB04)["results"]):
        status = {"passed": "verified", "failed": "failed", "skipped": "skipped"}.get(record["status"], "unknown")
        caps[record["registry_entry_id"]].setdefault("load", {})["linux"] = claim(
            status, [src.ref(AB04, f"results[{i}]")], record.get("reason_code"))

    # mcp_tool: TOOL-04 and AB-05 positions, aggregated per entry
    for rel in (TOOL04, AB05):
        grouped: dict[str, list] = {}
        for i, position in enumerate(src.json(rel)["positions"]):
            grouped.setdefault(position["registry_entry_id"], []).append((i, position))
        for rid, positions in grouped.items():
            statuses = [p["status"] for _, p in positions]
            value = "verified" if all(s == "passed" for s in statuses) else (
                "failed" if "failed" in statuses else "unknown")
            cases = [{
                "case_id": p["case_id"], "class": p["case_class"], "status": p["status"],
                "attempts_passed": sum(a.get("status") == "passed" for a in p.get("attempts") or []),
                "attempts": len(p.get("attempts") or []),
            } for _, p in positions]
            classes = sorted({case["class"] for case in cases})
            scope = None if "positive" in classes else f"no positive case; covered classes: {', '.join(classes)}"
            caps[rid]["mcp_tool"] = {"linux": claim(
                value, [src.ref(rel, f"positions[{i}]") for i, _ in positions], cases=cases, scope=scope)}

    # federated_event: P4-01 v10 and P4-02 v2r3
    behavior: dict[str, list] = {}
    for rel in (P401, P402):
        for i, position in enumerate(src.json(rel)["positions"]):
            behavior.setdefault(position["registry_entry_id"], []).append((rel, i, position))
    for rid, positions in behavior.items():
        executed = [p for _, _, p in positions if p["status"] in ("passed", "failed")]
        statuses = [p["status"] for p in executed]
        if not executed:
            reasons = sorted({p.get("reason_code") or p.get("migration_status") or "not_executable" for _, _, p in positions})
            value, reason = "skipped", ",".join(reasons)
        elif "failed" in statuses:
            failed = [p for p in executed if p["status"] == "failed"]
            slots = [slot for slot in failed[0].get("attempt_slots") or [] if slot.get("status") == "failed"]
            value, reason = "failed", failed[0].get("reason_code") or (slots[0].get("reason_code") if slots else None)
        else:
            value, reason = "verified", None
        cases = [{"case_id": p["case_id"], "class": p["case_class"], "status": p["status"]} for _, _, p in positions]
        caps[rid]["federated_event"] = {"linux": claim(
            value, [src.ref(rel, f"positions[{i}]") for rel, i, _ in positions], reason, cases=cases)}

    # platform_delivery: P6-01 round 5 (positive) and the scenario round, P4-05 canaries as history
    field = src.json(P601)
    scenarios = src.json(P601_SCENARIOS)
    final = next(r for r in field["rounds"] if r.get("outcome") == "passed")
    by_key = {(e["package"], e["version"]): rid for rid, e in entries.items()}
    teamup_sha = field["plugins"]["astrbot"].split("sha256", 1)[1].strip()
    for ecosystem, (package, version) in P601_PLUGINS.items():
        if version is not None:
            rid = by_key[(package, version)]
        else:
            rid = next(r for r, e in entries.items() if e["ecosystem"] == "astrbot" and teamup_sha in e["artifact_sha256"])
        covered = scenarios["coverage_passed_by_ecosystem"][ecosystem]
        if min(covered.get(k, 0) for k in ("positive", "negative", "restart", "timeout")) < 3:
            raise SystemExit(f"P6-01 scenario coverage below 3 for {ecosystem}: {covered}")
        caps[rid]["platform_delivery"] = {"qq": claim(
            "verified", [src.ref(P601, f"rounds[{field['rounds'].index(final)}]"),
                         src.ref(P601_SCENARIOS, f"coverage_passed_by_ecosystem.{ecosystem}")],
            scope="positive, negative/authority, Core restart and Host timeout cases (>=3 each), MCP path through "
                  "one bot in isolated containers, user-observed; duplicate event redelivery not run")}
    for rel in src.glob(P405_CANARY_GLOB):
        report = src.json(rel)
        rid = (report.get("candidate") or {}).get("registry_entry_id")
        if not rid or rid not in caps:
            continue
        delivery = caps[rid].setdefault("platform_delivery", {})
        if "qq" not in delivery:
            delivery["qq"] = claim("unknown", [], str(report.get("result")))
        delivery["qq"].setdefault("history", []).append(
            {"result": report.get("result"), "evidence": src.ref(rel, "result")})

    records = []
    for rid in sorted(entries):
        entry = dict(entries[rid])
        entry["capabilities"] = {name: caps[rid][name] for name in CAPABILITIES if name in caps[rid]}
        records.append(entry)

    summary: dict[str, dict] = {}
    for record in records:
        eco = summary.setdefault(record["ecosystem"], {"entries": 0, **{c: Counter() for c in CAPABILITIES}})
        eco["entries"] += 1
        for name in CAPABILITIES:
            platforms = record["capabilities"].get(name) or {}
            if not platforms:
                eco[name]["untested"] += 1
            for platform, value in platforms.items():
                eco[name][f"{platform}:{value['value']}"] += 1
    summary = {eco: {k: (dict(sorted(v.items())) if isinstance(v, Counter) else v) for k, v in data.items()}
               for eco, data in sorted(summary.items())}

    return {
        "schema_version": 1,
        "kind": "compatibility_matrix",
        "capabilities": list(CAPABILITIES),
        "values": list(VALUES),
        "notes": [
            "untested means no evidence for that capability; absence of a platform key means the same",
            "load verified = prepare, install and native load passed and cleanup was confirmed in an isolated Worker",
            "windows load comes from the S2/P3 Windows Sandbox profile; App Control Enforcement admission (P4-04) is not covered",
            "federated_event and mcp_tool cover only the listed cases; they are not catalog-wide claims",
            "platform_delivery is scoped: see each claim's scope",
        ],
        "sources": dict(sorted(src.hashes.items())),
        "summary": summary,
        "entries": records,
    }


def render_json(matrix: dict) -> str:
    head = {k: v for k, v in matrix.items() if k != "entries"}
    lines = [json.dumps(head, ensure_ascii=False, sort_keys=True, indent=1)[:-2] + ',\n "entries": [']
    body = [json.dumps(e, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for e in matrix["entries"]]
    lines.append(",\n".join(body))
    lines.append("]}\n")
    return "\n".join(lines)


def render_markdown(matrix: dict) -> str:
    out = ["# 兼容性矩阵（自动生成，勿手改）", "",
           "由 `python -m lab.compat_matrix build` 从已提交的证据生成；`check` 在证据变化而矩阵未重建时失败。"
           "每条非 `untested` 结论都引用证据文件、其 SHA-256 与文件内记录，见同目录 JSON。", "",
           "| 能力 | 含义 | 证据来源 |", "| --- | --- | --- |",
           "| `load` | 隔离 Worker 中安装并原生加载，清理已确认（约 L0–L2） | S2-11 全量重跑，P3-10 恢复矩阵覆盖；AstrBot 取 AB-04 |",
           "| `mcp_tool` | 命令作为 MCP 工具的工具级验收 | TOOL-04、AB-05 |",
           "| `federated_event` | Native/Federated 双路径行为（约 L3–L4） | P4-01 v10、P4-02 v2r3 |",
           "| `platform_delivery` | 回复送达真实聊天平台 | P6-01 正例与场景轮（负例/越权、Core 重启、Host 超时，各 ≥3；未覆盖重复投递）；P4-05 canary 作为历史 |", "",
           "取值只有 `verified`、`failed`、`skipped`、`unknown`、`untested`；`scope` 写明覆盖范围。"
           "P4-03 的恢复只作注释，不改变 P3 结论。Windows `load` 来自 S2/P3 的 Windows Sandbox profile，"
           "不覆盖 App Control Enforcement 准入（P4-04）。本矩阵是证据索引，不是新的支持名单。", "",
           "## 按生态与能力的计数", "",
           "| 生态 | 条目 | 能力 | 计数（平台:取值） |", "| --- | --- | --- | --- |"]
    for eco, data in matrix["summary"].items():
        for name in CAPABILITIES:
            counts = ", ".join(f"{k} {v}" for k, v in data[name].items())
            out.append(f"| {eco} | {data['entries']} | `{name}` | {counts} |")
    out += ["", "## 有加载以外结论的插件", "",
            "| 条目 | 生态 | 包 | 版本 | load | mcp_tool | federated_event | platform_delivery |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for e in matrix["entries"]:
        c = e["capabilities"]
        if not any(name in c for name in CAPABILITIES[1:]):
            continue

        def cell(name: str) -> str:
            platforms = c.get(name) or {}
            return "<br>".join(f"{p}: {v['value']}" for p, v in sorted(platforms.items())) or "untested"

        out.append(f"| `{e['registry_entry_id']}` | {e['ecosystem']} | {e['package']} | {e['version']} | "
                   + " | ".join(cell(n) for n in CAPABILITIES) + " |")
    out.append("")
    return "\n".join(out)


def validate(matrix: dict, root: Path = ROOT) -> list[str]:
    from jsonschema import Draft202012Validator

    schema = json.loads((root / SCHEMA).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in Draft202012Validator(schema).iter_errors(matrix)]
    for entry in matrix["entries"]:
        for name, platforms in entry["capabilities"].items():
            for platform, value in platforms.items():
                if value["value"] in ("verified", "failed") and not value["evidence"]:
                    errors.append(f"{entry['registry_entry_id']} {name}/{platform}: {value['value']} without evidence")
                for ref in value["evidence"]:
                    if matrix["sources"].get(ref["path"]) != ref["sha256"]:
                        errors.append(f"{entry['registry_entry_id']} {name}/{platform}: evidence hash not in sources")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("build", "check"))
    args = parser.parse_args(argv)
    matrix = build()
    errors = validate(matrix)
    if errors:
        print("\n".join(errors[:50]), file=sys.stderr)
        return 1
    rendered = {OUTPUT_JSON: render_json(matrix), OUTPUT_MD: render_markdown(matrix)}
    if args.action == "build":
        for rel, text in rendered.items():
            path = ROOT / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        print(json.dumps({"entries": len(matrix["entries"]), "sources": len(matrix["sources"])}))
        return 0
    stale = [rel for rel, text in rendered.items()
             if not (ROOT / rel).exists() or (ROOT / rel).read_text(encoding="utf-8") != text]
    if stale:
        print(f"compatibility matrix is stale: {', '.join(stale)} (run python -m lab.compat_matrix build)", file=sys.stderr)
        return 1
    print(json.dumps({"check": "ok", "entries": len(matrix["entries"]), "sources": len(matrix["sources"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
