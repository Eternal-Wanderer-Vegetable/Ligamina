"""Durable, lease-based stage queue for catalog runs."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable


JOB_STATUSES = ("queued", "leased", "passed", "failed", "skipped", "unknown")
TERMINAL_STATUSES = ("passed", "failed", "skipped", "unknown")


class QueueError(RuntimeError):
    """The durable queue rejected an invalid or stale operation."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _job_id(run_id: str, unit_id: str) -> str:
    return hashlib.sha256(f"{run_id}\0{unit_id}".encode("utf-8")).hexdigest()


class StageQueue:
    """SQLite-backed queue with atomic claims, leases, attempts, and an append-only journal."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser().absolute()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL,
                    manifest_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('queued','running','interrupted','completed')),
                    manifest_json TEXT NOT NULL,
                    fencing_required INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    unit_id TEXT NOT NULL,
                    result_id TEXT NOT NULL,
                    registry_entry_id TEXT NOT NULL,
                    ecosystem TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('queued','leased','passed','failed','skipped','unknown')),
                    attempts_used INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    always_run INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(run_id, unit_id)
                );
                CREATE TABLE IF NOT EXISTS job_dependencies (
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    depends_on_job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    PRIMARY KEY(job_id, depends_on_job_id),
                    CHECK (job_id != depends_on_job_id)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    attempt_number INTEGER NOT NULL,
                    worker_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('leased','passed','failed','skipped','unknown')),
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    result_json TEXT,
                    UNIQUE(job_id, attempt_number)
                );
                CREATE TABLE IF NOT EXISTS journal (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    job_id TEXT REFERENCES jobs(job_id) ON DELETE SET NULL,
                    at REAL NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_claim_order ON jobs(run_id, status, created_at, job_id);
                CREATE INDEX IF NOT EXISTS jobs_lease_expiry ON jobs(status, lease_expires_at);
                CREATE INDEX IF NOT EXISTS attempts_job_order ON attempts(job_id, attempt_number);
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                PRAGMA foreign_keys=ON;
                """
            )
            run_columns = {row["name"] for row in connection.execute("PRAGMA table_info(runs)")}
            if "fencing_required" not in run_columns:
                connection.execute("ALTER TABLE runs ADD COLUMN fencing_required INTEGER NOT NULL DEFAULT 0")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @staticmethod
    def _journal(
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        job_id: str | None = None,
        at: float,
    ) -> None:
        connection.execute(
            "INSERT INTO journal(run_id,job_id,at,event_type,payload_json) VALUES(?,?,?,?,?)",
            (run_id, job_id, at, event_type, _canonical(payload)),
        )

    def create_run(
        self,
        run_id: str,
        snapshot_id: str,
        manifest_sha256: str,
        manifest: dict[str, Any],
        *,
        fencing_required: bool = False,
        now: float | None = None,
    ) -> bool:
        if (
            not run_id
            or not snapshot_id
            or len(manifest_sha256) != 64
            or any(character not in "0123456789abcdef" for character in manifest_sha256)
            or not isinstance(manifest, dict)
        ):
            raise QueueError("run_id, snapshot_id, and a SHA-256 manifest identity are required")
        at = time.time() if now is None else float(now)
        encoded = _canonical(manifest)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if existing:
                    if (
                        existing["snapshot_id"] != snapshot_id
                        or existing["manifest_sha256"] != manifest_sha256
                        or existing["manifest_json"] != encoded
                        or bool(existing["fencing_required"]) != fencing_required
                    ):
                        raise QueueError("run_id already exists with a different frozen manifest")
                    connection.commit()
                    return False
                connection.execute(
                    "INSERT INTO runs(run_id,snapshot_id,manifest_sha256,state,manifest_json,fencing_required,created_at,updated_at) "
                    "VALUES(?,?,?,'queued',?,?,?,?)",
                    (run_id, snapshot_id, manifest_sha256, encoded, int(fencing_required), at, at),
                )
                self._journal(connection, run_id, "run_created", {"snapshot_id": snapshot_id, "manifest_sha256": manifest_sha256}, at=at)
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise

    def enqueue_jobs(
        self,
        run_id: str,
        jobs: Iterable[dict[str, Any]],
        *,
        now: float | None = None,
    ) -> dict[str, int]:
        at = time.time() if now is None else float(now)
        job_list = list(jobs)
        by_unit: dict[str, dict[str, Any]] = {}
        for item in job_list:
            self._validate_job(item)
            if item["unit_id"] in by_unit and _canonical(by_unit[item["unit_id"]]) != _canonical(item):
                raise QueueError(f"duplicate unit_id has conflicting job definitions: {item['unit_id']}")
            by_unit[item["unit_id"]] = item

        created = 0
        existing_count = 0
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
                    raise QueueError(f"unknown run_id: {run_id}")
                unit_to_id = {unit_id: _job_id(run_id, unit_id) for unit_id in by_unit}
                for item in by_unit.values():
                    missing = sorted(set(item.get("dependencies", [])) - set(unit_to_id))
                    for dependency in missing:
                        if connection.execute(
                            "SELECT 1 FROM jobs WHERE run_id=? AND unit_id=?", (run_id, dependency)
                        ).fetchone() is None:
                            raise QueueError(f"job {item['unit_id']} references missing dependency {dependency}")
                for item in by_unit.values():
                    job_id = unit_to_id[item["unit_id"]]
                    expected_dependencies = {
                        unit_to_id.get(dependency, _job_id(run_id, dependency))
                        for dependency in item.get("dependencies", [])
                    }
                    existing = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                    payload = _canonical(item["payload"])
                    if existing:
                        expected_fields = {
                            "registry_entry_id": item["registry_entry_id"],
                            "ecosystem": item["ecosystem"],
                            "platform": item["platform"],
                            "profile_id": item["profile_id"],
                            "stage": item["stage"],
                            "case_id": item["case_id"],
                            "max_attempts": item.get("max_attempts", 2),
                            "always_run": int(bool(item.get("always_run", False))),
                            "payload_json": payload,
                        }
                        if any(existing[key] != value for key, value in expected_fields.items()):
                            raise QueueError(f"job identity was reused with different inputs: {item['unit_id']}")
                        actual_dependencies = {
                            row["depends_on_job_id"]
                            for row in connection.execute(
                                "SELECT depends_on_job_id FROM job_dependencies WHERE job_id=?", (job_id,)
                            )
                        }
                        if actual_dependencies != expected_dependencies:
                            raise QueueError(f"job identity was reused with different dependencies: {item['unit_id']}")
                        existing_count += 1
                    else:
                        connection.execute(
                            "INSERT INTO jobs(job_id,run_id,unit_id,result_id,registry_entry_id,ecosystem,platform,profile_id,stage,case_id,status,max_attempts,always_run,payload_json,created_at,updated_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,'queued',?,?,?,?,?)",
                            (
                                job_id,
                                run_id,
                                item["unit_id"],
                                hashlib.sha256(f"{run_id}\0{item['unit_id']}".encode("utf-8")).hexdigest(),
                                item["registry_entry_id"],
                                item["ecosystem"],
                                item["platform"],
                                item["profile_id"],
                                item["stage"],
                                item["case_id"],
                                item.get("max_attempts", 2),
                                int(bool(item.get("always_run", False))),
                                payload,
                                at,
                                at,
                            ),
                        )
                        created += 1
                    for dependency in item.get("dependencies", []):
                        dependency_id = unit_to_id.get(dependency, _job_id(run_id, dependency))
                        connection.execute(
                            "INSERT OR IGNORE INTO job_dependencies(job_id,depends_on_job_id) VALUES(?,?)",
                            (job_id, dependency_id),
                        )
                if created:
                    self._journal(connection, run_id, "jobs_enqueued", {"created": created, "existing": existing_count}, at=at)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return {"created": created, "existing": existing_count}

    @staticmethod
    def _validate_job(item: dict[str, Any]) -> None:
        required = ("unit_id", "registry_entry_id", "ecosystem", "platform", "profile_id", "stage", "case_id", "payload")
        if not isinstance(item, dict) or any(not isinstance(item.get(key), str) or not item.get(key) for key in required[:-1]):
            raise QueueError("job requires non-empty string identity fields")
        if not isinstance(item.get("payload"), dict):
            raise QueueError("job payload must be an object")
        dependencies = item.get("dependencies", [])
        if not isinstance(dependencies, list) or any(not isinstance(value, str) or not value for value in dependencies):
            raise QueueError("job dependencies must be a list of unit IDs")
        max_attempts = item.get("max_attempts", 2)
        if not isinstance(max_attempts, int) or max_attempts < 1:
            raise QueueError("max_attempts must be a positive integer")
        _canonical(item["payload"])

    @staticmethod
    def _decode_job(row: sqlite3.Row, dependencies: list[str]) -> dict[str, Any]:
        return {
            "job_id": row["job_id"],
            "run_id": row["run_id"],
            "unit_id": row["unit_id"],
            "result_id": row["result_id"],
            "registry_entry_id": row["registry_entry_id"],
            "ecosystem": row["ecosystem"],
            "platform": row["platform"],
            "profile_id": row["profile_id"],
            "stage": row["stage"],
            "case_id": row["case_id"],
            "status": row["status"],
            "attempts_used": row["attempts_used"],
            "max_attempts": row["max_attempts"],
            "lease_owner": row["lease_owner"],
            "lease_expires_at": row["lease_expires_at"],
            "always_run": bool(row["always_run"]),
            "payload": json.loads(row["payload_json"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "dependencies": dependencies,
        }

    def _dependencies(self, connection: sqlite3.Connection, job_id: str) -> list[sqlite3.Row]:
        return connection.execute(
            "SELECT jobs.job_id,jobs.unit_id,jobs.status,jobs.result_json "
            "FROM job_dependencies JOIN jobs ON jobs.job_id=job_dependencies.depends_on_job_id "
            "WHERE job_dependencies.job_id=? ORDER BY jobs.unit_id",
            (job_id,),
        ).fetchall()

    def _recover_expired(self, connection: sqlite3.Connection, run_id: str | None, now: float) -> int:
        query = (
            "SELECT jobs.*,runs.fencing_required FROM jobs "
            "JOIN runs ON runs.run_id=jobs.run_id "
            "WHERE jobs.status='leased' AND jobs.lease_expires_at <= ?"
        )
        params: tuple[Any, ...] = (now,)
        if run_id is not None:
            query += " AND jobs.run_id=?"
            params += (run_id,)
        expired = connection.execute(query, params).fetchall()
        for row in expired:
            attempt = connection.execute(
                "SELECT * FROM attempts WHERE job_id=? AND attempt_number=?",
                (row["job_id"], row["attempts_used"]),
            ).fetchone()
            result = {
                "status": "unknown",
                "reason_code": "worker_lost",
                "reason": "lease expired before a terminal result was recorded",
                "requires_reconciliation": bool(row["fencing_required"]),
            }
            if attempt:
                connection.execute(
                    "UPDATE attempts SET status='unknown',ended_at=?,result_json=? WHERE attempt_id=? AND status='leased'",
                    (now, _canonical(result), attempt["attempt_id"]),
                )
            final_unknown = bool(row["fencing_required"]) or row["attempts_used"] >= row["max_attempts"]
            connection.execute(
                "UPDATE jobs SET status=?,lease_owner=NULL,lease_expires_at=NULL,result_json=?,updated_at=? WHERE job_id=?",
                ("unknown" if final_unknown else "queued", _canonical(result) if final_unknown else None, now, row["job_id"]),
            )
            self._journal(
                connection,
                row["run_id"],
                "lease_expired",
                {
                    "unit_id": row["unit_id"],
                    "attempt_id": attempt["attempt_id"] if attempt else None,
                    "retry_queued": not final_unknown,
                    "reconciliation_required": bool(row["fencing_required"]),
                },
                job_id=row["job_id"],
                at=now,
            )
            if row["fencing_required"]:
                connection.execute("UPDATE runs SET state='interrupted',updated_at=? WHERE run_id=?", (now, row["run_id"]))
        return len(expired)

    def _skip_blocked(self, connection: sqlite3.Connection, run_id: str, now: float) -> int:
        total_changed = 0
        while True:
            rows = connection.execute("SELECT * FROM jobs WHERE run_id=? AND status='queued' ORDER BY created_at,unit_id", (run_id,)).fetchall()
            changed = 0
            for row in rows:
                dependencies = self._dependencies(connection, row["job_id"])
                if not dependencies or any(dependency["status"] not in TERMINAL_STATUSES for dependency in dependencies):
                    continue
                unsuccessful = [dependency for dependency in dependencies if dependency["status"] != "passed"]
                if unsuccessful and not row["always_run"]:
                    source = unsuccessful[0]
                    result = {
                        "status": "skipped",
                        "reason_code": "upstream_stage_not_passed",
                        "reason": f"dependency {source['unit_id']} ended as {source['status']}",
                        "dependency_unit_ids": [dependency["unit_id"] for dependency in unsuccessful],
                    }
                    connection.execute(
                        "UPDATE jobs SET status='skipped',result_json=?,updated_at=? WHERE job_id=?",
                        (_canonical(result), now, row["job_id"]),
                    )
                    self._journal(connection, run_id, "job_skipped_upstream", result, job_id=row["job_id"], at=now)
                    changed += 1
            total_changed += changed
            if changed == 0:
                return total_changed

    @staticmethod
    def _refresh_run_state(connection: sqlite3.Connection, run_id: str, now: float) -> str:
        run = connection.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise QueueError(f"unknown run_id: {run_id}")
        if run["state"] == "interrupted":
            return "interrupted"
        counts = connection.execute(
            "SELECT COUNT(*) AS total,SUM(CASE WHEN status IN ('passed','failed','skipped','unknown') THEN 1 ELSE 0 END) AS terminal "
            "FROM jobs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        state = "completed" if counts["total"] > 0 and counts["total"] == counts["terminal"] else "running"
        if counts["total"] == 0:
            state = "queued"
        connection.execute("UPDATE runs SET state=?,updated_at=? WHERE run_id=?", (state, now, run_id))
        return state

    def claim(
        self,
        run_id: str,
        worker_id: str,
        *,
        lease_seconds: float = 120,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        if not worker_id or lease_seconds <= 0:
            raise QueueError("worker_id and a positive lease_seconds are required")
        at = time.time() if now is None else float(now)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = connection.execute("SELECT state,fencing_required FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if run is None:
                    raise QueueError(f"unknown run_id: {run_id}")
                if run["state"] == "interrupted":
                    raise QueueError("run must be resumed before workers can claim jobs")
                if run["state"] == "completed":
                    connection.commit()
                    return None
                recovered = self._recover_expired(connection, run_id, at)
                if recovered and run["fencing_required"]:
                    connection.commit()
                    return None
                self._skip_blocked(connection, run_id, at)
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE run_id=? AND status='queued' ORDER BY created_at,unit_id",
                    (run_id,),
                ).fetchall()
                selected = None
                for row in rows:
                    dependencies = self._dependencies(connection, row["job_id"])
                    if dependencies and any(item["status"] not in TERMINAL_STATUSES for item in dependencies):
                        continue
                    if dependencies and not row["always_run"] and any(item["status"] != "passed" for item in dependencies):
                        continue
                    selected = row
                    break
                if selected is None:
                    self._refresh_run_state(connection, run_id, at)
                    connection.commit()
                    return None
                attempt_number = selected["attempts_used"] + 1
                attempt_id = secrets.token_hex(24)
                expires = at + float(lease_seconds)
                connection.execute(
                    "UPDATE jobs SET status='leased',attempts_used=?,lease_owner=?,lease_expires_at=?,updated_at=? WHERE job_id=? AND status='queued'",
                    (attempt_number, worker_id, expires, at, selected["job_id"]),
                )
                connection.execute(
                    "INSERT INTO attempts(attempt_id,job_id,attempt_number,worker_id,status,started_at) VALUES(?,?,?,?,'leased',?)",
                    (attempt_id, selected["job_id"], attempt_number, worker_id, at),
                )
                self._journal(
                    connection,
                    run_id,
                    "job_claimed",
                    {"unit_id": selected["unit_id"], "attempt_id": attempt_id, "worker_id": worker_id, "lease_expires_at": expires},
                    job_id=selected["job_id"],
                    at=at,
                )
                deps = [item["unit_id"] for item in self._dependencies(connection, selected["job_id"])]
                refreshed = connection.execute("SELECT * FROM jobs WHERE job_id=?", (selected["job_id"],)).fetchone()
                job = self._decode_job(refreshed, deps)
                job["attempt_id"] = attempt_id
                job["generation"] = attempt_number
                connection.commit()
                return job
            except BaseException:
                connection.rollback()
                raise

    def heartbeat(
        self,
        job_id: str,
        worker_id: str,
        *,
        attempt_id: str | None = None,
        lease_seconds: float = 120,
        now: float | None = None,
    ) -> float:
        if lease_seconds <= 0:
            raise QueueError("lease_seconds must be positive")
        at = time.time() if now is None else float(now)
        expires = at + lease_seconds
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT jobs.*,runs.fencing_required FROM jobs JOIN runs ON runs.run_id=jobs.run_id WHERE jobs.job_id=?", (job_id,)).fetchone()
            if row is None or row["status"] != "leased" or row["lease_owner"] != worker_id or row["lease_expires_at"] <= at:
                connection.rollback()
                raise QueueError("lease is absent, expired, or owned by another worker")
            current_attempt = connection.execute(
                "SELECT attempt_id FROM attempts WHERE job_id=? AND attempt_number=? AND status='leased'",
                (job_id, row["attempts_used"]),
            ).fetchone()
            if current_attempt is None or (row["fencing_required"] and attempt_id != current_attempt["attempt_id"]) or (
                attempt_id is not None and attempt_id != current_attempt["attempt_id"]
            ):
                connection.rollback()
                raise QueueError("attempt fencing token is stale or missing")
            connection.execute("UPDATE jobs SET lease_expires_at=?,updated_at=? WHERE job_id=?", (expires, at, job_id))
            self._journal(connection, row["run_id"], "lease_renewed", {"worker_id": worker_id, "lease_expires_at": expires}, job_id=job_id, at=at)
            connection.commit()
        return expires

    def finish(
        self,
        job_id: str,
        worker_id: str,
        result: dict[str, Any],
        *,
        attempt_id: str | None = None,
        retryable: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        if not isinstance(result, dict) or result.get("status") not in TERMINAL_STATUSES:
            raise QueueError("stage result must use a terminal v1 status")
        at = time.time() if now is None else float(now)
        encoded = _canonical(result)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT jobs.*,runs.fencing_required FROM jobs JOIN runs ON runs.run_id=jobs.run_id WHERE jobs.job_id=?", (job_id,)).fetchone()
                if row is None or row["status"] != "leased" or row["lease_owner"] != worker_id:
                    raise QueueError("job lease is absent or owned by another worker")
                if row["lease_expires_at"] <= at:
                    raise QueueError("job lease expired before result commit")
                attempt = connection.execute(
                    "SELECT * FROM attempts WHERE job_id=? AND attempt_number=?",
                    (job_id, row["attempts_used"]),
                ).fetchone()
                if attempt is None or (row["fencing_required"] and attempt_id != attempt["attempt_id"]) or (
                    attempt_id is not None and attempt_id != attempt["attempt_id"]
                ):
                    raise QueueError("attempt fencing token is stale or missing")
                attempt_id = attempt["attempt_id"]
                if attempt is None or attempt["status"] != "leased":
                    raise QueueError("current attempt is not leased")
                connection.execute(
                    "UPDATE attempts SET status=?,ended_at=?,result_json=? WHERE attempt_id=?",
                    (result["status"], at, encoded, attempt_id),
                )
                will_retry = retryable and result["status"] in {"failed", "unknown"} and row["attempts_used"] < row["max_attempts"]
                job_status = "queued" if will_retry else result["status"]
                final_result = None if will_retry else encoded
                connection.execute(
                    "UPDATE jobs SET status=?,lease_owner=NULL,lease_expires_at=NULL,result_json=?,updated_at=? WHERE job_id=?",
                    (job_status, final_result, at, job_id),
                )
                self._journal(
                    connection,
                    row["run_id"],
                    "attempt_finished",
                    {"unit_id": row["unit_id"], "attempt_id": attempt_id, "status": result["status"], "retry_queued": will_retry},
                    job_id=job_id,
                    at=at,
                )
                self._skip_blocked(connection, row["run_id"], at)
                run_state = self._refresh_run_state(connection, row["run_id"], at)
                connection.commit()
                return {"job_id": job_id, "unit_id": row["unit_id"], "attempt_id": attempt_id, "attempt_status": result["status"], "retry_queued": will_retry, "job_status": job_status, "run_state": run_state}
            except BaseException:
                connection.rollback()
                raise

    def interrupt_run(self, run_id: str, *, now: float | None = None) -> None:
        at = time.time() if now is None else float(now)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
                connection.rollback()
                raise QueueError(f"unknown run_id: {run_id}")
            connection.execute("UPDATE runs SET state='interrupted',updated_at=? WHERE run_id=?", (at, run_id))
            self._journal(connection, run_id, "run_interrupted", {}, at=at)
            connection.commit()

    def resume_run(self, run_id: str, *, now: float | None = None) -> dict[str, Any]:
        at = time.time() if now is None else float(now)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                connection.rollback()
                raise QueueError(f"unknown run_id: {run_id}")
            recovered = self._recover_expired(connection, run_id, at)
            fenced = bool(run["fencing_required"])
            unresolved = False
            if fenced:
                unresolved = connection.execute(
                    "SELECT 1 FROM jobs WHERE run_id=? AND status='unknown' "
                    "AND json_extract(result_json,'$.requires_reconciliation')=1 "
                    "AND COALESCE(json_extract(result_json,'$.reconciliation.cleanup_confirmed'),0)!=1 LIMIT 1",
                    (run_id,),
                ).fetchone() is not None
            if not (fenced and unresolved):
                connection.execute("UPDATE runs SET state='running',updated_at=? WHERE run_id=? AND state!='completed'", (at, run_id))
            self._skip_blocked(connection, run_id, at)
            state = "interrupted" if fenced and unresolved else self._refresh_run_state(connection, run_id, at)
            self._journal(connection, run_id, "run_resumed", {"expired_leases_recovered": recovered, "reconciliation_required": bool(fenced and unresolved), "state": state}, at=at)
            connection.commit()
        return {"run_id": run_id, "state": state, "expired_leases_recovered": recovered, "reconciliation_required": bool(fenced and unresolved)}

    def record_reconciliation(
        self,
        run_id: str,
        unit_id: str,
        attempt_id: str,
        evidence: dict[str, Any],
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Record cleanup evidence and authorize retry only after a fenced lease is resolved."""
        at = time.time() if now is None else float(now)
        if not isinstance(evidence, dict) or not isinstance(evidence.get("cleanup_confirmed"), bool):
            raise QueueError("reconciliation requires an explicit cleanup_confirmed boolean")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = connection.execute("SELECT fencing_required,state FROM runs WHERE run_id=?", (run_id,)).fetchone()
                row = connection.execute("SELECT * FROM jobs WHERE run_id=? AND unit_id=?", (run_id, unit_id)).fetchone()
                if run is None or row is None:
                    raise QueueError("unknown run or unit during reconciliation")
                if not run["fencing_required"] or run["state"] != "interrupted":
                    raise QueueError("reconciliation is only valid for an interrupted fenced run")
                attempt = connection.execute(
                    "SELECT * FROM attempts WHERE job_id=? AND attempt_number=?",
                    (row["job_id"], row["attempts_used"]),
                ).fetchone()
                if row["status"] != "unknown" or attempt is None or attempt["attempt_id"] != attempt_id or attempt["status"] != "unknown":
                    raise QueueError("reconciliation attempt identity is stale")
                if row["result_json"]:
                    prior = json.loads(row["result_json"])
                    if prior.get("reconciliation"):
                        raise QueueError("attempt has already been reconciled")
                resolved = {**(json.loads(row["result_json"]) if row["result_json"] else {}), "reconciliation": evidence}
                connection.execute(
                    "UPDATE attempts SET result_json=? WHERE attempt_id=?",
                    (_canonical(resolved), attempt_id),
                )
                retry_queued = evidence["cleanup_confirmed"] and row["attempts_used"] < row["max_attempts"]
                status = "queued" if retry_queued else "unknown"
                connection.execute(
                    "UPDATE jobs SET status=?,result_json=?,updated_at=? WHERE job_id=?",
                    (status, None if retry_queued else _canonical(resolved), at, row["job_id"]),
                )
                self._journal(
                    connection,
                    run_id,
                    "attempt_reconciled",
                    {"unit_id": unit_id, "attempt_id": attempt_id, "cleanup_confirmed": evidence["cleanup_confirmed"], "retry_queued": retry_queued},
                    job_id=row["job_id"],
                    at=at,
                )
                connection.commit()
                return {"unit_id": unit_id, "attempt_id": attempt_id, "retry_queued": retry_queued, "job_status": status}
            except BaseException:
                connection.rollback()
                raise

    def complete_reconciliation(self, run_id: str, *, now: float | None = None) -> None:
        """Resume fenced claims only after every expired attempt has explicit cleanup evidence."""
        at = time.time() if now is None else float(now)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute("SELECT state,fencing_required FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None or not run["fencing_required"] or run["state"] != "interrupted":
                connection.rollback()
                raise QueueError("run is not awaiting fenced reconciliation")
            unresolved = connection.execute(
                "SELECT unit_id FROM jobs WHERE run_id=? AND status='unknown' AND json_extract(result_json,'$.requires_reconciliation')=1 "
                "AND COALESCE(json_extract(result_json,'$.reconciliation.cleanup_confirmed'),0)!=1",
                (run_id,),
            ).fetchall()
            if unresolved:
                connection.rollback()
                raise QueueError("one or more expired attempts have no cleanup reconciliation")
            connection.execute("UPDATE runs SET state='running',updated_at=? WHERE run_id=?", (at, run_id))
            self._journal(connection, run_id, "reconciliation_completed", {"unresolved_worker_resources": 0}, at=at)
            connection.commit()

    def run(self, run_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise QueueError(f"unknown run_id: {run_id}")
            return {
                "run_id": row["run_id"],
                "snapshot_id": row["snapshot_id"],
                "manifest_sha256": row["manifest_sha256"],
                "state": row["state"],
                "manifest": json.loads(row["manifest_json"]),
                "fencing_required": bool(row["fencing_required"]),
            }

    def get_job(self, run_id: str, unit_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM jobs WHERE run_id=? AND unit_id=?", (run_id, unit_id)).fetchone()
            if row is None:
                raise QueueError(f"unknown job: {unit_id}")
            dependencies = [item["unit_id"] for item in self._dependencies(connection, row["job_id"])]
            return self._decode_job(row, dependencies)

    def list_jobs(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM jobs WHERE run_id=? ORDER BY unit_id", (run_id,)).fetchall()
            return [
                self._decode_job(row, [item["unit_id"] for item in self._dependencies(connection, row["job_id"])])
                for row in rows
            ]

    def list_attempts(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT attempts.*,jobs.unit_id FROM attempts JOIN jobs ON jobs.job_id=attempts.job_id "
                "WHERE jobs.run_id=? ORDER BY jobs.unit_id,attempts.attempt_number",
                (run_id,),
            ).fetchall()
            return [
                {
                    "attempt_id": row["attempt_id"],
                    "unit_id": row["unit_id"],
                    "attempt_number": row["attempt_number"],
                    "worker_id": row["worker_id"],
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "result": json.loads(row["result_json"]) if row["result_json"] else None,
                }
                for row in rows
            ]

    def journal(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM journal WHERE run_id=? ORDER BY sequence", (run_id,)).fetchall()
            return [
                {"sequence": row["sequence"], "job_id": row["job_id"], "at": row["at"], "event_type": row["event_type"], "payload": json.loads(row["payload_json"])}
                for row in rows
            ]

    def counts(self, run_id: str) -> dict[str, int]:
        with closing(self._connect()) as connection:
            counts = {status: 0 for status in JOB_STATUSES}
            for row in connection.execute("SELECT status,COUNT(*) AS count FROM jobs WHERE run_id=? GROUP BY status", (run_id,)):
                counts[row["status"]] = row["count"]
            counts["total"] = sum(counts[status] for status in JOB_STATUSES)
            return counts
