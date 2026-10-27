"""Build, validate, and dry-plan the Phase 4 version-two behavior suite."""

from __future__ import annotations

import copy
import hashlib
import tarfile
import json
import re
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
V1_PATH = ROOT / "contracts/lab/automation/v1/fixtures/behavior-suite.phase3-v1.json"
SCHEMA_PATH = ROOT / "contracts/lab/automation/v2/behavior-suite.schema.json"
SUITE_PATH = ROOT / "contracts/lab/automation/v2/fixtures/behavior-suite.phase4-v2.json"
MATRIX_PATH = ROOT / "outputs/acceptance/phase4-p401-migration-matrix-20261026.json"
P402_SUITE_PATH = ROOT / "contracts/lab/automation/v2/fixtures/behavior-suite.phase4-p402-v2r3.json"
P402_MATRIX_PATH = ROOT / "outputs/acceptance/phase4-p402-migration-matrix-20261026-v2r3.json"
CATALOG_ROOT = ROOT / "catalogs/p2-41649744e057d61031d5"
ARTIFACT_CACHE = ROOT.parent / "Ligamina-artifact-cache"
RUNNABLE_IDS = {"nb-16c80a826ca035f6c49ad69a", "ko-163c3bd4cf87c72c24210251"}
P402_HELP_IDS = {"nb-0b37b4bb782b82c00e95a87f", "ko-fb5aa9ada6cacd62ec595c66"}
RUNNABLE_IDS_BY_SUITE = {
    "phase4-entry-behavior-v2": RUNNABLE_IDS,
    "phase4-linux-behavior-p402-v2": RUNNABLE_IDS | P402_HELP_IDS,
    "phase4-linux-behavior-p402-v2r3": RUNNABLE_IDS | P402_HELP_IDS,
}
PREVIOUS_NONEBOT_CANDIDATE = "nb-5f9a0398ba3644f4256bbe83"
PREVIOUS_NONEBOT_FAILURE = {
    "report_path": "outputs/acceptance/phase4-p401-behavior-runs-20261026-v8.json",
    "report_sha256": "18ba1928a64ef06519a5c7af93568a63dec42e791b69a5668eec3172bc500568",
    "case_id": "nb-nonebot-plugin-guess-isolation",
    "failed_attempts": 3,
    "side_executions": 6,
    "classification": "confirmed_third_party_behavior_mismatch",
}


class BehaviorV2Error(ValueError):
    """The Phase 4 behavior suite is incomplete or improperly bound."""


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BehaviorV2Error(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BehaviorV2Error(f"expected JSON object: {path}")
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _archive_member_hash(path: Path, member: str) -> str:
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                matches = [name for name in archive.namelist() if name == member]
                if len(matches) != 1:
                    raise BehaviorV2Error(f"expected one source member {member!r} in {path}")
                return _sha256(archive.read(matches[0]))
        with tarfile.open(path, "r:*") as archive:
            matches = [item for item in archive.getmembers() if item.name == member and item.isfile()]
            if len(matches) != 1:
                raise BehaviorV2Error(f"expected one source member {member!r} in {path}")
            stream = archive.extractfile(matches[0])
            if stream is None:
                raise BehaviorV2Error(f"source member is unreadable: {member}")
            return _sha256(stream.read())
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise BehaviorV2Error(f"cannot inspect frozen source member {member!r}: {exc}") from exc


def _event(event_id: str, account: str, actor: str, kind: str, conversation_id: str, text: str) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "timestamp": "2026-10-26T00:00:00Z",
        "account_id": account,
        "actor_id": actor,
        "conversation": {"kind": kind, "id": conversation_id},
        "message": [{"type": "text", "text": text}],
        "extensions": {"onebot.v11": {"message_id": str(int(hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:7], 16))}},
    }


def _executable_case(entry: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    identity = entry["registry_entry_id"]
    case_id = case["case_id"]
    migration = {
        "status": "executable",
        "v1_case_id": case_id,
        "v1_input": case["stimuli"][0]["input"],
        "v1_assertions": copy.deepcopy(case["assertions"]),
        "v1_oracle": copy.deepcopy(case["oracle"]),
    }
    base = {
        "case_id": case_id,
        "class": case["class"],
        "migration": migration,
        "adapter": None,
        "setup": None,
        "stimulus": None,
        "determinism": None,
        "observation": None,
        "oracle": None,
    }
    if identity == "nb-5f9a0398ba3644f4256bbe83":
        base.update({
            "adapter": {
                "id": "nonebot.onebot.v11",
                "native_dispatch": "OneBot v11 Adapter.json_to_event -> Bot.handle_event",
                "federated_dispatch": "Core event queue -> Rust Host -> Python NoneBot adapter",
            },
            "setup": {
                "plugin_config": {"name": "城市", "limit": 4, "name_list": ["OSLO", "ROME"]},
                "user_authority": {},
                "storage": {"kind": "fresh_directory", "seed": {}},
            },
            "determinism": {"clock": "2026-10-26T00:00:00Z", "random_seed": 4, "settle_timeout_ms": 1500, "scheduler_drain": "no scheduled callbacks; drain until event handler and sends settle"},
            "observation": {"projection": "message_operations", "source": "OneBot send API trace correlated to the dispatched source event"},
        })
        if case["class"] == "positive":
            base["stimulus"] = {"events": [_event("nb-guess-positive", "9001", "101", "group", "501", "/guess")]}
            assertions = [
                {"path": "plugin_observation.response_or_operation_count", "operator": "equals", "value": 1},
                {"path": "plugin_observation.operations.0.params.message.0.text", "operator": "equals", "value": "猜一个城市，你有4次机会"},
                {"path": "plugin_observation.operations.0.target", "operator": "equals", "value": {"kind": "group", "id": "501"}},
                {"path": "plugin_observation.operations.0.source_event_id", "operator": "equals", "value": "nb-guess-positive"},
                {"path": "plugin_observation.receipts.0.status", "operator": "equals", "value": "succeeded"},
            ]
            rationale = "The command handler starts one game and sends the configured game prompt to the command session."
            symbol = "guess.handle"
        elif case["class"] == "negative":
            base["stimulus"] = {"events": [_event("nb-guess-unrelated", "9001", "101", "group", "501", "unrelated text")]}
            assertions = [
                {"path": "plugin_observation.response_or_operation_count", "operator": "equals", "value": 0},
                {"path": "plugin_observation.events.0.event_id", "operator": "equals", "value": "nb-guess-unrelated"},
            ]
            rationale = "The command matcher is not entered by unrelated text when no game command is active."
            symbol = "guess.handle"
        else:
            base["stimulus"] = {"events": [
                _event("nb-guess-a-start", "9001", "101", "group", "501", "/guess"),
                _event("nb-guess-b-start", "9001", "202", "group", "502", "/guess"),
                _event("nb-guess-a-answer", "9001", "101", "group", "501", "OSLO"),
            ]}
            assertions = [
                {"path": "plugin_observation.response_or_operation_count", "operator": "equals", "value": 3},
                {"path": "plugin_observation.operations.2.params.message.0.text", "operator": "contains", "value": "谜底： OSLO"},
                {"path": "plugin_observation.operations.2.target", "operator": "equals", "value": {"kind": "group", "id": "501"}},
                {"path": "plugin_observation.operations.2.source_event_id", "operator": "equals", "value": "nb-guess-a-answer"},
                {"path": "plugin_observation.receipts.2.status", "operator": "equals", "value": "succeeded"},
            ]
            rationale = "Each concurrent command session must retain its own city and turn state while the second session starts."
            symbol = "guess.receive"
        base["oracle"] = {
            "source_member": entry["source_review"]["archive_member"],
            "source_symbol": symbol,
            "source_artifact_sha256": entry["artifact_sha256"],
            "rationale": rationale,
            "review_status": "reviewed",
            "assertions": assertions,
        }
    elif identity == "nb-16c80a826ca035f6c49ad69a":
        base.update({
            "adapter": {
                "id": "nonebot.onebot.v11",
                "native_dispatch": "OneBot v11 Adapter.json_to_event -> Bot.handle_event",
                "federated_dispatch": "Core event queue -> Rust Host -> Python NoneBot adapter",
            },
            "setup": {
                "plugin_config": {},
                "user_authority": {},
                "storage": {"kind": "fresh_directory", "seed": {}},
            },
            "determinism": {"clock": "2026-10-26T00:00:00Z", "random_seed": 4, "settle_timeout_ms": 1500, "scheduler_drain": "no scheduled callbacks; drain until event handler and sends settle"},
            "observation": {"projection": "message_operations", "source": "OneBot send API trace correlated to the dispatched source event"},
        })
        if case["class"] == "positive":
            base["stimulus"] = {"events": [_event("nb-roll-positive", "9001", "101", "group", "501", "/rd 1d6")]}
            assertions = [
                {"path": "plugin_observation.response_or_operation_count", "operator": "equals", "value": 1},
                {"path": "plugin_observation.operations.0.params.message.0.text", "operator": "equals", "value": "你掷出了1个6面骰子, 点数为【1】"},
                {"path": "plugin_observation.operations.0.target", "operator": "equals", "value": {"kind": "group", "id": "501"}},
                {"path": "plugin_observation.operations.0.source_event_id", "operator": "equals", "value": "nb-roll-positive"},
                {"path": "plugin_observation.receipts.0.status", "operator": "equals", "value": "succeeded"},
            ]
            rationale = "With the reviewed seed, one valid 1d6 command produces the source-derived result and replies to its invoking conversation."
            symbol = "roll.handle"
        elif case["class"] == "negative":
            base["stimulus"] = {"events": [_event("nb-roll-invalid-dice", "9001", "101", "group", "501", "/rd 0d6")]}
            assertions = [
                {"path": "plugin_observation.response_or_operation_count", "operator": "equals", "value": 1},
                {"path": "plugin_observation.operations.0.params.message.0.text", "operator": "equals", "value": "错误！你掷出了不存在的骰子, 只有上帝知道结果是多少🤔"},
                {"path": "plugin_observation.operations.0.target", "operator": "equals", "value": {"kind": "group", "id": "501"}},
                {"path": "plugin_observation.operations.0.source_event_id", "operator": "equals", "value": "nb-roll-invalid-dice"},
                {"path": "plugin_observation.receipts.0.status", "operator": "equals", "value": "succeeded"},
            ]
            rationale = "The frozen command rejects a zero-dice expression with its explicit invalid-dice response and performs no dice roll."
            symbol = "roll.handle"
        else:
            base["stimulus"] = {"events": [
                _event("nb-roll-a-start", "9001", "101", "group", "501", "/rd"),
                _event("nb-roll-b-command", "9001", "202", "group", "502", "/rd 1d6"),
                _event("nb-roll-a-resume", "9001", "101", "group", "501", "1d6"),
            ]}
            assertions = [
                {"path": "plugin_observation.response_or_operation_count", "operator": "equals", "value": 3},
                {"path": "plugin_observation.operations.0.params.message.0.text", "operator": "equals", "value": "你还没掷骰子呢：rd [x]d[y]"},
                {"path": "plugin_observation.operations.0.target", "operator": "equals", "value": {"kind": "group", "id": "501"}},
                {"path": "plugin_observation.operations.1.params.message.0.text", "operator": "equals", "value": "你掷出了1个6面骰子, 点数为【1】"},
                {"path": "plugin_observation.operations.1.target", "operator": "equals", "value": {"kind": "group", "id": "502"}},
                {"path": "plugin_observation.operations.1.source_event_id", "operator": "equals", "value": "nb-roll-b-command"},
                {"path": "plugin_observation.operations.2.params.message.0.text", "operator": "equals", "value": "你掷出了1个6面骰子, 点数为【4】"},
                {"path": "plugin_observation.operations.2.target", "operator": "equals", "value": {"kind": "group", "id": "501"}},
                {"path": "plugin_observation.operations.2.source_event_id", "operator": "equals", "value": "nb-roll-a-resume"},
                {"path": "plugin_observation.receipts.0.status", "operator": "equals", "value": "succeeded"},
                {"path": "plugin_observation.receipts.1.status", "operator": "equals", "value": "succeeded"},
                {"path": "plugin_observation.receipts.2.status", "operator": "equals", "value": "succeeded"},
            ]
            rationale = "A pending /rd prompt in group 501 must survive a completed command in group 502, then resume only for the original session."
            symbol = "roll.handle / Matcher.got"
        base["oracle"] = {
            "source_member": entry["source_review"]["archive_member"],
            "source_symbol": symbol,
            "source_artifact_sha256": entry["artifact_sha256"],
            "rationale": rationale,
            "review_status": "reviewed",
            "assertions": assertions,
        }
    elif identity == "ko-163c3bd4cf87c72c24210251":
        base.update({
            "adapter": {
                "id": "koishi.onebot.v11",
                "native_dispatch": "Koishi Context/Session -> Bot.dispatch",
                "federated_dispatch": "Core event queue -> Rust Host -> Koishi OneBot Session dispatch",
            },
            "setup": {
                "plugin_config": {},
                "user_authority": {"onebot.v11:23": 3, "onebot.v11:24": 3},
                "storage": {"kind": "fresh_sqlite", "seed": {"users": [{"platform": "onebot.v11", "id": "23", "authority": 3}, {"platform": "onebot.v11", "id": "24", "authority": 3}]}},
            },
            "determinism": {"clock": "2026-10-26T00:00:00Z", "random_seed": 0, "settle_timeout_ms": 1500, "scheduler_drain": "drain Koishi middleware and pending sends after each Session dispatch"},
            "observation": {"projection": "message_operations", "source": "Koishi Bot.sendMessage trace correlated to dispatched Session.referrer"},
        })
        if case["class"] == "positive":
            base["stimulus"] = {"events": [_event("ko-echo-positive", "9001", "23", "group", "501", "echo alpha")]}
            assertions = [
                {"path": "plugin_observation.response_or_operation_count", "operator": "equals", "value": 1},
                {"path": "plugin_observation.operations.0.params.message.0.text", "operator": "equals", "value": "alpha"},
                {"path": "plugin_observation.operations.0.target", "operator": "equals", "value": {"kind": "group", "id": "501"}},
                {"path": "plugin_observation.operations.0.account_id", "operator": "equals", "value": "9001"},
                {"path": "plugin_observation.operations.0.source_event_id", "operator": "equals", "value": "ko-echo-positive"},
                {"path": "plugin_observation.receipts.0.status", "operator": "equals", "value": "succeeded"},
            ]
            rationale = "The echo command sends only its parsed text to the invoking session and carries that Session target."
            symbol = "echo command action"
        elif case["class"] == "negative":
            base["stimulus"] = {"events": [_event("ko-echo-missing-text", "9001", "23", "group", "501", "echo")]}
            assertions = [
                {"path": "plugin_observation.response_or_operation_count", "operator": "equals", "value": 1},
                {"path": "plugin_observation.operations.0.params.message.0.text", "operator": "equals", "value": "请输入要发送的文本。"},
                {"path": "plugin_observation.operations.0.target", "operator": "equals", "value": {"kind": "group", "id": "501"}},
                {"path": "plugin_observation.receipts.0.status", "operator": "equals", "value": "succeeded"},
            ]
            rationale = "The command's required-text parser returns the localized prompt without forwarding an absent user argument."
            symbol = "echo command action"
        else:
            base["stimulus"] = {"events": [
                _event("ko-echo-group", "9001", "23", "group", "77", "echo group-message"),
                _event("ko-echo-private", "9002", "77", "private", "77", "echo private-message"),
            ]}
            base["setup"]["user_authority"] = {"onebot.v11:23": 3, "onebot.v11:77": 3}
            base["setup"]["storage"]["seed"]["users"] = [
                {"platform": "onebot.v11", "id": "23", "authority": 3},
                {"platform": "onebot.v11", "id": "77", "authority": 3},
            ]
            assertions = [
                {"path": "plugin_observation.response_or_operation_count", "operator": "equals", "value": 2},
                {"path": "plugin_observation.operations.0.account_id", "operator": "equals", "value": "9001"},
                {"path": "plugin_observation.operations.0.target", "operator": "equals", "value": {"kind": "group", "id": "77"}},
                {"path": "plugin_observation.operations.0.params.message.0.text", "operator": "equals", "value": "group-message"},
                {"path": "plugin_observation.operations.1.account_id", "operator": "equals", "value": "9002"},
                {"path": "plugin_observation.operations.1.target", "operator": "equals", "value": {"kind": "private", "id": "77"}},
                {"path": "plugin_observation.operations.1.params.message.0.text", "operator": "equals", "value": "private-message"},
                {"path": "plugin_observation.receipts.0.status", "operator": "equals", "value": "succeeded"},
                {"path": "plugin_observation.receipts.1.status", "operator": "equals", "value": "succeeded"},
            ]
            rationale = "Each echo must use its own account, message, and group/private target where IDs collide; the private conversation and actor IDs obey OneBot v11 identity rules."
            symbol = "echo command action"
        base["oracle"] = {
            "source_member": entry["source_review"]["archive_member"],
            "source_symbol": symbol,
            "source_artifact_sha256": entry["artifact_sha256"],
            "rationale": rationale,
            "review_status": "reviewed",
            "assertions": assertions,
        }
    return base


def _p402_help_case(entry: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    """Review the fixed Help samples against the original host event routes."""
    identity = entry["registry_entry_id"]
    case_id = case["case_id"]
    is_nonebot = identity == "nb-0b37b4bb782b82c00e95a87f"
    member = entry["source_review"]["archive_member"]
    adapter_id = "nonebot.onebot.v11" if is_nonebot else "koishi.onebot.v11"
    base = {
        "case_id": case_id,
        "class": case["class"],
        "migration": {
            "status": "executable",
            "v1_case_id": case["case_id"],
            "v1_input": case["stimuli"][0]["input"],
            "v1_assertions": copy.deepcopy(case["assertions"]),
            "v1_oracle": copy.deepcopy(case["oracle"]),
        },
        "adapter": {
            "id": adapter_id,
            "native_dispatch": "OneBot v11 Adapter.json_to_event -> Bot.handle_event" if is_nonebot else "Koishi Context/Session -> Bot.dispatch",
            "federated_dispatch": "Core event queue -> Rust Host -> Python NoneBot adapter" if is_nonebot else "Core event queue -> Rust Host -> Koishi OneBot Session dispatch",
        },
        "setup": {
            "plugin_config": ({"help_at_sender": False, "help_ignore_plugins": ["phase4_random_seed", "phase4_probe_observer"]} if is_nonebot else {}),
            "user_authority": {},
            "storage": {"kind": "fresh_directory" if is_nonebot else "fresh_sqlite", "seed": {"users": []}},
        },
        "stimulus": None,
        "determinism": {
            "clock": "2026-10-26T00:00:00Z",
            "random_seed": 0,
            "settle_timeout_ms": 1500,
            "scheduler_drain": "drain the command handler, Host route, and pending sends after each event",
        },
        "observation": {
            "projection": "message_operations",
            "source": "Native Bot API or Core-to-Host Gateway operation correlated to the dispatched event id",
        },
    }
    if case["class"] == "positive":
        event = _event("p402-help-positive", "9001", "101" if is_nonebot else "23", "group", "501", "/help")
        expected_count = 1
        assertions = [
            {"path": "plugin_observation.response_or_operation_count", "operator": "equals", "value": expected_count},
            {"path": "plugin_observation.operations.0.target", "operator": "equals", "value": {"kind": "group", "id": "501"}},
            {"path": "plugin_observation.operations.0.account_id", "operator": "equals", "value": "9001"},
            {"path": "plugin_observation.operations.0.source_event_id", "operator": "equals", "value": event["event_id"]},
            {"path": "plugin_observation.receipts.0.status", "operator": "equals", "value": "succeeded"},
            {"path": "plugin_observation.operations.0.params.message.0.text", "operator": "contains", "value": "欢迎使用Nonebot2 Help Menu" if is_nonebot else "help"},
        ]
        rationale = "The published help command responds to the original OneBot session with its own command help output."
        symbol = "handle_first_receive / get_result" if is_nonebot else "help command action"
        if not is_nonebot:
            event["message"] = [{"type": "text", "text": "help help"}]
    elif case["class"] == "negative":
        text = "/help __phase4_missing_plugin__" if is_nonebot else "__phase4_missing_command__"
        event = _event("p402-help-negative", "9001", "101" if is_nonebot else "23", "group", "501", text)
        if is_nonebot:
            expected_text = "__phase4_missing_plugin__ 不存在或未加载，请确认输入了正确的插件名"
            assertions = [
                {"path": "plugin_observation.response_or_operation_count", "operator": "equals", "value": 1},
                {"path": "plugin_observation.operations.0.params.message.0.text", "operator": "equals", "value": expected_text},
                {"path": "plugin_observation.operations.0.target", "operator": "equals", "value": {"kind": "group", "id": "501"}},
                {"path": "plugin_observation.operations.0.source_event_id", "operator": "equals", "value": event["event_id"]},
                {"path": "plugin_observation.receipts.0.status", "operator": "equals", "value": "succeeded"},
            ]
        else:
            assertions = [{"path": "plugin_observation.response_or_operation_count", "operator": "equals", "value": 0}]
        rationale = "An unknown plugin lookup returns the published not-found response." if is_nonebot else "An unresolved command has no permission-approved match and must not emit a Gateway operation."
        symbol = "get_result" if is_nonebot else "inferCommand / help command action"
    else:
        first = _event("p402-help-isolation-a", "9001", "101" if is_nonebot else "23", "group", "77", "/help __phase4_missing_plugin__" if is_nonebot else "help help")
        second = _event("p402-help-isolation-b", "9002", "202" if is_nonebot else "77", "group" if is_nonebot else "private", "77", "/help" if is_nonebot else "__phase4_missing_command__")
        base["stimulus"] = {"events": [first, second]}
        assertions = [
            {"path": "plugin_observation.response_or_operation_count", "operator": "equals", "value": 2 if is_nonebot else 1},
            {"path": "plugin_observation.operations.0.account_id", "operator": "equals", "value": "9001"},
            {"path": "plugin_observation.operations.0.target", "operator": "equals", "value": {"kind": "group", "id": "77"}},
            {"path": "plugin_observation.operations.0.source_event_id", "operator": "equals", "value": first["event_id"]},
            {"path": "plugin_observation.operations.0.params.message.0.text", "operator": "contains", "value": "__phase4_missing_plugin__" if is_nonebot else "help"},
        ]
        if is_nonebot:
            assertions.extend([
                {"path": "plugin_observation.operations.1.account_id", "operator": "equals", "value": "9002"},
                {"path": "plugin_observation.operations.1.target", "operator": "equals", "value": {"kind": "group", "id": "77"}},
                {"path": "plugin_observation.operations.1.source_event_id", "operator": "equals", "value": second["event_id"]},
            ])
        else:
            assertions.append({"path": "plugin_observation.events.1.event_id", "operator": "equals", "value": second["event_id"]})
        rationale = "Fresh Native and Federated dispatches for colliding conversation ids must preserve the source event, account, and recipient for every reply."
        symbol = "get_result / matcher.finish" if is_nonebot else "help command action / session.send"
    if base["stimulus"] is None:
        base["stimulus"] = {"events": [event]}
    base["oracle"] = {
        "source_member": member,
        "source_symbol": symbol,
        "source_artifact_sha256": entry["artifact_sha256"],
        "rationale": rationale,
        "review_status": "reviewed",
        "assertions": assertions,
    }
    return base


def build_phase4_p402_suite() -> tuple[dict[str, Any], dict[str, Any]]:
    """Freeze a separate P4-02 four-entry suite without rewriting P4-01 evidence."""
    suite = _read(SUITE_PATH)
    matrix = _read(MATRIX_PATH)
    old_by_id = {row["registry_entry_id"]: row for row in _read(V1_PATH)["entries"]}
    matrix_by_id = {row["registry_entry_id"]: row for row in matrix["entries"]}
    suite["suite_id"] = "phase4-linux-behavior-p402-v2r3"
    for entry in suite["entries"]:
        entry_id = entry["registry_entry_id"]
        if entry_id not in P402_HELP_IDS:
            continue
        for index, case in enumerate(entry["cases"]):
            reviewed = _p402_help_case(entry, old_by_id[entry_id]["cases"][index])
            entry["cases"][index] = reviewed
            matrix_case = matrix_by_id[entry_id]["cases"][index]
            matrix_case["migration_status"] = "executable"
            matrix_case["reason_code"] = None
            matrix_case["adapter_id"] = reviewed["adapter"]["id"]
            matrix_case["review_note"] = (
                "P4-02 fixed-sample event, setup, output/recipient oracle independently reviewed against the exact frozen artifact; "
                "Koishi command inputs follow the pinned Core profile's empty default prefix, and the negative stimulus is unmatched."
                if entry["ecosystem"] != "nonebot"
                else "P4-02 fixed-sample event, setup, output/recipient oracle independently reviewed against the exact frozen artifact."
            )
        matrix_by_id[entry_id]["migration_status"] = "executable"
    matrix["kind"] = "phase4_p402_v1_to_v2_migration_matrix"
    matrix["executable_entry_ids"] = sorted(RUNNABLE_IDS | P402_HELP_IDS)
    matrix["fixed_sample_expansion"] = {
        "prior_suite_path": SUITE_PATH.relative_to(ROOT).as_posix(),
        "prior_suite_sha256": _sha256(SUITE_PATH.read_bytes()),
        "added_entry_ids": sorted(P402_HELP_IDS),
        "scope": "Linux first four fixed samples only; the legacy four-plugin regression is separately reported and excluded from the 20-entry denominator.",
    }
    return suite, matrix


def build_phase4_suite(v1_path: Path = V1_PATH) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = v1_path.read_bytes()
    v1 = json.loads(raw.decode("utf-8"))
    entries = []
    matrix_entries = []
    for source_entry in v1["entries"]:
        executable = source_entry["registry_entry_id"] in RUNNABLE_IDS
        cases = []
        matrix_cases = []
        for old_case in source_entry["cases"]:
            if executable:
                case = _executable_case(source_entry, old_case)
            else:
                blocked_reason = (
                    "prior_candidate_failed_preserved"
                    if source_entry["registry_entry_id"] == PREVIOUS_NONEBOT_CANDIDATE
                    else "structured_adapter_setup_oracle_review_required"
                )
                case = {
                    "case_id": old_case["case_id"],
                    "class": old_case["class"],
                    "migration": {
                        "status": "blocked",
                        "reason_code": blocked_reason,
                        "v1_case_id": old_case["case_id"],
                        "v1_input": old_case["stimuli"][0]["input"],
                        "v1_assertions": copy.deepcopy(old_case["assertions"]),
                        "v1_oracle": copy.deepcopy(old_case["oracle"]),
                    },
                    "adapter": None,
                    "setup": None,
                    "stimulus": None,
                    "determinism": None,
                    "observation": None,
                    "oracle": None,
                }
            cases.append(case)
            matrix_cases.append({
                "v1_case_id": old_case["case_id"],
                "case_class": old_case["class"],
                "v1_input": old_case["stimuli"][0]["input"],
                "v1_assertions": copy.deepcopy(old_case["assertions"]),
                "v1_oracle": copy.deepcopy(old_case["oracle"]),
                "migration_status": case["migration"]["status"],
                "reason_code": case["migration"].get("reason_code"),
                "v2_case_id": case["case_id"],
                "adapter_id": case["adapter"]["id"] if case["adapter"] else None,
                "source_member_sha256": source_entry["source_review"]["archive_member_sha256"],
                "profile_locks": copy.deepcopy(source_entry["dependency_locks"]),
            })
        entry = {key: copy.deepcopy(source_entry[key]) for key in (
            "registry_entry_id", "ecosystem", "package", "version", "artifact_sha256", "dependency_locks", "source_review"
        )}
        entry["cases"] = cases
        entries.append(entry)
        matrix_entry = {
            "registry_entry_id": source_entry["registry_entry_id"],
            "ecosystem": source_entry["ecosystem"],
            "package": source_entry["package"],
            "version": source_entry["version"],
            "artifact_sha256": source_entry["artifact_sha256"],
            "source_member": source_entry["source_review"]["archive_member"],
            "source_member_sha256": source_entry["source_review"]["archive_member_sha256"],
            "migration_status": "executable" if executable else "blocked",
            "cases": matrix_cases,
        }
        if source_entry["registry_entry_id"] == PREVIOUS_NONEBOT_CANDIDATE:
            matrix_entry["prior_failure"] = copy.deepcopy(PREVIOUS_NONEBOT_FAILURE)
        matrix_entries.append(matrix_entry)
    suite = {
        "schema_version": 2,
        "suite_id": "phase4-entry-behavior-v2",
        "catalog_snapshot_id": v1["catalog_snapshot_id"],
        "reviewed_at_utc": "2026-10-26T00:00:00Z",
        "source_suite": {
            "suite_id": v1["suite_id"],
            "path": "contracts/lab/automation/v1/fixtures/behavior-suite.phase3-v1.json",
            "sha256": _sha256(raw),
        },
        "entries": entries,
        "capability_probes": [
            {
                "capability": "service",
                "status": "required",
                "required_hosts": ["native", "federated"],
                "probe_id": "phase4-service-probe-v1",
                "probe_spec": {
                    "version": 1,
                    "method": "NoneBot Host service.invoke -> Rust CapabilityBroker -> Koishi Host capability",
                    "assertion": "The fresh cross-Host call returns 42, preserves the trace, and emits the expected result through the caller Host.",
                },
            },
            {
                "capability": "storage",
                "status": "required",
                "required_hosts": ["native", "federated"],
                "probe_id": "phase4-storage-probe-v1",
                "probe_spec": {
                    "version": 1,
                    "method": "A framework-loaded probe plugin appends one record per dispatched event under that attempt's Host data directory.",
                    "assertion": "Each Native/Federated side observes one fresh file with exactly the dispatched event count; Workers are distinct and cleanup is confirmed.",
                },
            },
            {
                "capability": "clock",
                "status": "required",
                "required_hosts": ["native", "federated"],
                "probe_id": "phase4-clock-probe-v1",
                "probe_spec": {
                    "version": 1,
                    "method": "The same framework-loaded probe records the event timestamp exposed by NoneBot Event or Koishi Session.",
                    "assertion": "Every Native/Federated observation equals its frozen stimulus timestamp and preserves account, actor, and conversation scope.",
                },
            },
        ],
    }
    matrix = {
        "schema_version": 1,
        "kind": "phase4_p401_v1_to_v2_migration_matrix",
        "source_suite": suite["source_suite"],
        "required_entry_count": 20,
        "required_case_count": 60,
        "executable_entry_ids": sorted(RUNNABLE_IDS),
        "candidate_selection_revision": {
            "authorized_by_user": True,
            "reason": "The original NoneBot candidate failed its isolation oracle in all three fresh Native/Federated pairs; retain that result and substitute another exact locked entry after source review.",
            "previous_candidate": {
                "registry_entry_id": PREVIOUS_NONEBOT_CANDIDATE,
                **copy.deepcopy(PREVIOUS_NONEBOT_FAILURE),
            },
            "selected_candidate": {
                "registry_entry_id": "nb-16c80a826ca035f6c49ad69a",
                "package": "nonebot_plugin_roll",
                "version": "0.2.3",
                "artifact_sha256": "3a5ead2404f26711c9d41604ddcbb2e141df8a950fc53d32536aa7980fab1a2c",
                "source_member": "nonebot_plugin_roll/__init__.py",
                "source_member_sha256": "65835efdf2bfbaeb237df005c459610a068c55a8edd53fcae971e0976d5409d2",
                "linux_profile_id": "nonebot-2.5.0-onebot-2.4.6-py3.12.14-linux-x86_64",
                "linux_dependency_lock_sha256": "792fe11f2fd3581e888b15a23bc0c49f76b3b0d2399fc728dbfcff3d436214df",
            },
        },
        "entries": matrix_entries,
    }
    return suite, matrix


def validate_phase4_suite(suite_path: Path = SUITE_PATH, matrix_path: Path | None = MATRIX_PATH, *, verify_artifacts: bool = True) -> dict[str, Any]:
    from jsonschema import Draft202012Validator, FormatChecker

    suite = _read(suite_path)
    schema = _read(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(suite), key=lambda error: tuple(str(part) for part in error.path))
    if errors:
        raise BehaviorV2Error("v2 suite schema failed: " + "; ".join(error.message for error in errors[:12]))
    v1_raw = V1_PATH.read_bytes()
    if suite["source_suite"]["sha256"] != _sha256(v1_raw):
        raise BehaviorV2Error("v1 source suite hash changed; rebuild and review the migration")
    v1 = json.loads(v1_raw.decode("utf-8"))
    if len(suite["entries"]) != len(v1["entries"]) or [row["registry_entry_id"] for row in suite["entries"]] != [row["registry_entry_id"] for row in v1["entries"]]:
        raise BehaviorV2Error("v2 migration must preserve all 20 v1 identities and order")
    artifacts_lock = _read(CATALOG_ROOT / "artifacts.lock.json")
    profiles_lock = _read(CATALOG_ROOT / "profiles.lock.json")
    artifact_by_id = {row["registry_entry_id"]: row for row in artifacts_lock["entries"]}
    profile_by_id = {row["registry_entry_id"]: row for row in profiles_lock["entries"]}
    artifact_cache_root = ARTIFACT_CACHE
    runnable_ids = RUNNABLE_IDS_BY_SUITE.get(suite["suite_id"])
    if runnable_ids is None:
        raise BehaviorV2Error(f"unsupported Phase 4 suite id: {suite['suite_id']}")
    executable_cases = blocked_cases = 0
    executable_by_id: dict[str, int] = {}
    for entry, old_entry in zip(suite["entries"], v1["entries"], strict=True):
        entry_id = entry["registry_entry_id"]
        if any(entry[key] != old_entry[key] for key in ("ecosystem", "package", "version", "artifact_sha256", "dependency_locks", "source_review")):
            raise BehaviorV2Error(f"entry identity/source/profile changed during migration: {entry_id}")
        if [case["case_id"] for case in entry["cases"]] != [case["case_id"] for case in old_entry["cases"]]:
            raise BehaviorV2Error(f"case identities changed during migration: {entry_id}")
        locked_artifact = artifact_by_id.get(entry_id)
        locked_profile = profile_by_id.get(entry_id)
        if locked_artifact is None or locked_profile is None:
            raise BehaviorV2Error(f"entry is missing from frozen catalog locks: {entry_id}")
        locked_record = next((record.get("artifact") for record in locked_artifact["artifacts"] if record.get("status") == "locked" and record.get("artifact")), None)
        if not locked_record or locked_record.get("computed_digests", {}).get("sha256") != entry["artifact_sha256"]:
            raise BehaviorV2Error(f"archive lock hash mismatch: {entry_id}")
        for platform, bound in entry["dependency_locks"].items():
            lock = locked_profile["profiles"].get(platform, {})
            if lock.get("status") == "locked" and bound.get("dependency_lock_sha256") != lock.get("dependency_lock", {}).get("sha256"):
                raise BehaviorV2Error(f"dependency lock mismatch: {entry_id}/{platform}")
        if verify_artifacts:
            archive = artifact_cache_root / locked_record["cache_path"]
            if not archive.is_file() or _sha256(archive.read_bytes()) != entry["artifact_sha256"]:
                raise BehaviorV2Error(f"cached frozen artifact hash mismatch: {entry_id}")
            member_hash = _archive_member_hash(archive, entry["source_review"]["archive_member"])
            if member_hash != entry["source_review"]["archive_member_sha256"]:
                raise BehaviorV2Error(f"cached source member hash mismatch: {entry_id}")
        for case in entry["cases"]:
            if case["migration"]["v1_case_id"] not in {old["case_id"] for old in old_entry["cases"]}:
                raise BehaviorV2Error(f"case is not bound to v1 identity: {entry_id}/{case['case_id']}")
            if case["migration"]["status"] == "executable":
                executable_cases += 1
                executable_by_id[entry_id] = executable_by_id.get(entry_id, 0) + 1
                if entry_id not in runnable_ids or case["oracle"]["source_artifact_sha256"] != entry["artifact_sha256"]:
                    raise BehaviorV2Error(f"unexpected executable fixture or oracle artifact binding: {entry_id}/{case['case_id']}")
                if entry_id == "nb-16c80a826ca035f6c49ad69a":
                    config = case["setup"]["plugin_config"]
                    if config:
                        raise BehaviorV2Error(f"NoneBot roll fixture configuration must be empty: {entry_id}/{case['case_id']}")
                elif entry_id == "nb-0b37b4bb782b82c00e95a87f":
                    config = case["setup"]["plugin_config"]
                    if (
                        set(config) != {"help_at_sender", "help_ignore_plugins"}
                        or config["help_at_sender"] is not False
                        or config["help_ignore_plugins"] != ["phase4_random_seed", "phase4_probe_observer"]
                    ):
                        raise BehaviorV2Error(f"NoneBot Help config violates its reviewed allowlist: {entry_id}/{case['case_id']}")
                elif entry_id.startswith("nb-"):
                    config = case["setup"]["plugin_config"]
                    if set(config) != {"name", "limit", "name_list"} or not (1 <= config["limit"] <= 7) or len(config["name_list"]) < 2 or any(not isinstance(name, str) or len(name) > 7 for name in config["name_list"]):
                        raise BehaviorV2Error(f"NoneBot plugin configuration violates the reviewed key/value allowlist: {entry_id}/{case['case_id']}")
                elif entry_id == "ko-fb5aa9ada6cacd62ec595c66":
                    config = case["setup"]["plugin_config"]
                    if config != {}:
                        raise BehaviorV2Error(f"Koishi Help fixture configuration must be empty: {entry_id}/{case['case_id']}")
                    if case["setup"]["user_authority"] or case["setup"]["storage"].get("seed", {}).get("users"):
                        raise BehaviorV2Error(f"Koishi Help must not receive unreviewed authority/storage seeds: {entry_id}/{case['case_id']}")
                else:
                    user_authority = case["setup"]["user_authority"]
                    seeded_authority = {
                        f"{user.get('platform')}:{user.get('id')}": user.get("authority")
                        for user in case["setup"]["storage"].get("seed", {}).get("users", [])
                    }
                    if (
                        case["setup"]["plugin_config"] != {}
                        or any(not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= 5 for level in user_authority.values())
                        or seeded_authority != user_authority
                    ):
                        raise BehaviorV2Error(f"Koishi setup violates the reviewed config/authority allowlist: {entry_id}/{case['case_id']}")
                for event in case["stimulus"]["events"]:
                    if not re.fullmatch(r"\d+", event["account_id"]) or not re.fullmatch(r"\d+", event["actor_id"]):
                        raise BehaviorV2Error(f"OneBot fixture account and actor IDs must be numeric: {entry_id}/{case['case_id']}")
                    if event["conversation"]["kind"] == "private" and event["conversation"]["id"] != event["actor_id"]:
                        raise BehaviorV2Error(f"private OneBot conversation ID must equal actor ID: {entry_id}/{case['case_id']}")
            else:
                blocked_cases += 1
    expected_executable_by_id = {entry_id: 3 for entry_id in runnable_ids}
    expected_executable_cases = sum(expected_executable_by_id.values())
    expected_blocked_cases = 60 - expected_executable_cases
    if executable_cases != expected_executable_cases or blocked_cases != expected_blocked_cases or executable_by_id != expected_executable_by_id:
        raise BehaviorV2Error(f"{suite['suite_id']} expected {expected_executable_cases} executable and {expected_blocked_cases} explicitly blocked cases; got {executable_cases}/{blocked_cases}")
    if matrix_path is not None:
        matrix = _read(matrix_path)
        if matrix.get("source_suite") != suite["source_suite"] or len(matrix.get("entries", [])) != 20:
            raise BehaviorV2Error("migration matrix is stale or incomplete")
        if matrix.get("executable_entry_ids") != sorted(runnable_ids):
            raise BehaviorV2Error("migration matrix executable entry identity set differs from the selected suite")
        if sum(len(row.get("cases", [])) for row in matrix["entries"]) != 60:
            raise BehaviorV2Error("migration matrix must preserve all 60 v1 cases")
        if [row.get("registry_entry_id") for row in matrix["entries"]] != [row["registry_entry_id"] for row in suite["entries"]]:
            raise BehaviorV2Error("migration matrix entry identities or order differ from v2 suite")
        for matrix_entry, suite_entry in zip(matrix["entries"], suite["entries"], strict=True):
            if [row.get("v2_case_id") for row in matrix_entry["cases"]] != [case["case_id"] for case in suite_entry["cases"]]:
                raise BehaviorV2Error(f"migration matrix case identities differ: {suite_entry['registry_entry_id']}")
            if any(row.get("migration_status") != case["migration"]["status"] for row, case in zip(matrix_entry["cases"], suite_entry["cases"], strict=True)):
                raise BehaviorV2Error(f"migration matrix dispositions differ: {suite_entry['registry_entry_id']}")
    return {
        "status": "validated",
        "p4_fixture_gate": "blocked",
        "suite_id": suite["suite_id"],
        "entries": len(suite["entries"]),
        "cases": sum(len(entry["cases"]) for entry in suite["entries"]),
        "executable_cases": executable_cases,
        "blocked_cases": blocked_cases,
        "capability_probes": {probe["capability"]: probe["status"] for probe in suite["capability_probes"]},
        "plugins_executed": False,
    }


def dry_plan(suite_path: Path = SUITE_PATH) -> dict[str, Any]:
    """Return exact eligible positions without creating a worker or dispatching an event."""
    validate_phase4_suite(suite_path, matrix_path=None, verify_artifacts=True)
    suite = _read(suite_path)
    runnable_ids = RUNNABLE_IDS_BY_SUITE[suite["suite_id"]]
    positions = []
    for entry in suite.get("entries", []):
        lock = entry.get("dependency_locks", {}).get("linux-x86_64", {})
        for case in entry.get("cases", []):
            status = case.get("migration", {}).get("status")
            if status == "executable" and entry.get("registry_entry_id") in runnable_ids and lock.get("status") == "eligible_not_run":
                positions.append({
                    "registry_entry_id": entry["registry_entry_id"],
                    "package": entry["package"],
                    "version": entry["version"],
                    "artifact_sha256": entry["artifact_sha256"],
                    "platform": "linux-x86_64",
                    "profile_id": lock["profile_id"],
                    "dependency_lock_sha256": lock["dependency_lock_sha256"],
                    "case_id": case["case_id"],
                    "adapter_id": case["adapter"]["id"],
                    "dispatch_count": 0,
                })
    return {"status": "dry_plan", "suite_id": suite.get("suite_id"), "positions": positions, "position_count": len(positions), "dispatch_count": 0, "worker_started": False}
