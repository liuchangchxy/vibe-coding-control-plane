"""SQLite-backed generic execution orchestration with injected collaborators.

This module deliberately contains no GitHub, process, shell, or AgentAPI client.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import re
import sqlite3
import time
from typing import Protocol
from uuid import uuid4


MAX_AUTOMATED_REPAIRS = 3
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_REPAIR_CAUSES = {"implementation_failure", "reviewer_rejection"}


class LaunchDisposition(StrEnum):
    CONFIRMED = "confirmed"
    DEFINITELY_NOT_STARTED = "definitely_not_started"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RuntimeConfig:
    max_automated_repairs: int

    @classmethod
    def from_manifest(cls, manifest: dict) -> "RuntimeConfig":
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 2:
            raise ValueError("generic runtime requires manifest schema_version 2")
        contract = manifest.get("issue_contract")
        limit = contract.get("max_automated_repairs") if isinstance(contract, dict) else None
        if limit != MAX_AUTOMATED_REPAIRS:
            raise ValueError("issue_contract.max_automated_repairs must be 3")
        return cls(max_automated_repairs=limit)


@dataclass(frozen=True)
class WorkflowSnapshot:
    repo: str
    issue_number: int
    revision: str
    issue_open: bool
    coordination_state: str
    frozen_spec: bool
    terminal_labels: tuple[str, ...] = ()
    canceled: bool = False
    open_linked_pr_count: int = 0
    pr_number: int | None = None
    pr_open: bool = False
    pr_linked_issue: int | None = None
    pr_head_sha: str | None = None
    pr_branch: str | None = None
    formal_review_state: str | None = None
    formal_review_id: str | None = None
    formal_review_head_sha: str | None = None


@dataclass(frozen=True)
class RepairCandidate:
    repo: str
    issue_number: int
    pr_number: int
    head_sha: str
    branch: str
    cause_type: str
    cause_id: str
    owner_id: str


@dataclass(frozen=True)
class LaunchResult:
    disposition: LaunchDisposition
    execution_id: str | None = None


@dataclass(frozen=True)
class LaunchRequest:
    repo: str
    issue_number: int
    attempt_id: str
    attempt_kind: str
    repair_cause_type: str | None = None
    repair_cause_id: str | None = None
    repair_key: str | None = None
    repair_ordinal: int | None = None
    expected_head_sha: str | None = None
    pr_number: int | None = None
    branch: str | None = None


class WorkflowPort(Protocol):
    """Fresh facts and compare-and-set coordination transitions only."""

    def observe(self, repo: str, issue_number: int) -> WorkflowSnapshot: ...

    def transition_coordination_state(
        self, repo: str, issue_number: int, expected_state: str, new_state: str, expected_revision: str
    ) -> bool: ...


class ImplementerPort(Protocol):
    """Launch must distinguish confirmed, definitely-not-started, and unknown outcomes."""

    def launch(self, request: LaunchRequest) -> LaunchResult: ...


def _repo_key(repo: str) -> str:
    if not isinstance(repo, str) or "/" not in repo or any(not part.strip() for part in repo.split("/", 1)):
        raise ValueError("repo must be an OWNER/REPO identity")
    return repo.strip().casefold()


def compute_repair_key(repo: str, pr_number: int, head_sha: str) -> str:
    if not isinstance(pr_number, int) or pr_number < 1:
        raise ValueError("pr_number must be a positive integer")
    if not isinstance(head_sha, str) or not _SHA_RE.fullmatch(head_sha.strip()):
        raise ValueError("repair head SHA must be a full 40-character commit SHA")
    return f"repair:{_repo_key(repo)}:pr:{pr_number}:head:{head_sha.strip().lower()}"


class _Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=30)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS flows (
                    repo TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    trusted INTEGER NOT NULL DEFAULT 0,
                    initial_confirmed INTEGER NOT NULL DEFAULT 0,
                    pr_number INTEGER,
                    branch TEXT,
                    PRIMARY KEY (repo, issue_number)
                );
                CREATE TABLE IF NOT EXISTS leases (
                    repo TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    owner_id TEXT NOT NULL,
                    token TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY (repo, issue_number)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY,
                    repo TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    owner_id TEXT NOT NULL,
                    lease_token TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    cause_type TEXT,
                    cause_id TEXT,
                    repair_key TEXT UNIQUE,
                    repair_ordinal INTEGER,
                    expected_head_sha TEXT,
                    pr_number INTEGER,
                    branch TEXT,
                    phase TEXT NOT NULL,
                    launch_outcome TEXT,
                    execution_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS attempts_flow_idx
                    ON attempts(repo, issue_number, kind, repair_ordinal);
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def transaction(self):
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        return connection

    @staticmethod
    def _acquire_lease(connection: sqlite3.Connection, repo: str, issue: int, owner: str, ttl: float):
        row = connection.execute(
            "SELECT owner_id, token FROM leases WHERE repo=? AND issue_number=?", (repo, issue)
        ).fetchone()
        now = time.time()
        if row:
            if row["owner_id"] != owner:
                return None
            connection.execute(
                "UPDATE leases SET expires_at=? WHERE repo=? AND issue_number=?",
                (now + ttl, repo, issue),
            )
            return row["token"]
        token = str(uuid4())
        connection.execute(
            "INSERT INTO leases(repo, issue_number, owner_id, token, expires_at) VALUES(?,?,?,?,?)",
            (repo, issue, owner, token, now + ttl),
        )
        return token

    def create_initial_attempt(self, repo: str, issue: int, owner: str, ttl: float):
        connection = self.transaction()
        try:
            token = self._acquire_lease(connection, repo, issue, owner, ttl)
            if token is None:
                connection.commit()
                return None, "lease_held"
            flow = connection.execute(
                "SELECT trusted, initial_confirmed FROM flows WHERE repo=? AND issue_number=?", (repo, issue)
            ).fetchone()
            prior = connection.execute(
                "SELECT phase FROM attempts WHERE repo=? AND issue_number=? AND kind='initial_dispatch' "
                "AND phase NOT IN ('CLAIM_FAILED','LAUNCH_NOT_STARTED') LIMIT 1",
                (repo, issue),
            ).fetchone()
            if flow and (flow["initial_confirmed"] or connection.execute(
                "SELECT 1 FROM attempts WHERE repo=? AND issue_number=? AND kind='repair' LIMIT 1", (repo, issue)
            ).fetchone()):
                connection.commit()
                return None, "flow_already_started"
            if prior and prior["phase"] not in {"CLAIM_FAILED", "LAUNCH_NOT_STARTED"}:
                connection.commit()
                return None, "initial_attempt_already_recorded"
            connection.execute(
                "INSERT INTO flows(repo, issue_number, trusted, initial_confirmed) VALUES(?,?,0,0) "
                "ON CONFLICT(repo, issue_number) DO UPDATE SET trusted=0, initial_confirmed=0",
                (repo, issue),
            )
            attempt_id = str(uuid4())
            now = time.time()
            connection.execute(
                "INSERT INTO attempts(attempt_id,repo,issue_number,owner_id,lease_token,kind,phase,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'initial_dispatch','CLAIM_INTENT',?,?)",
                (attempt_id, repo, issue, owner, token, now, now),
            )
            connection.commit()
            return (attempt_id, token), None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_phase(self, attempt_id: str, phase: str, outcome: str | None = None, execution_id: str | None = None):
        connection = self.transaction()
        try:
            row = connection.execute(
                "SELECT repo, issue_number, lease_token, kind FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown attempt")
            now = time.time()
            connection.execute(
                "UPDATE attempts SET phase=?, launch_outcome=?, execution_id=?, updated_at=? WHERE attempt_id=?",
                (phase, outcome, execution_id, now, attempt_id),
            )
            if row["kind"] == "initial_dispatch":
                if phase == "LAUNCH_CONFIRMED":
                    connection.execute(
                        "UPDATE flows SET trusted=1, initial_confirmed=1 WHERE repo=? AND issue_number=?",
                        (row["repo"], row["issue_number"]),
                    )
                elif phase in {"LAUNCH_NOT_STARTED", "LAUNCH_UNKNOWN"}:
                    connection.execute(
                        "UPDATE flows SET trusted=0, initial_confirmed=0 WHERE repo=? AND issue_number=?",
                        (row["repo"], row["issue_number"]),
                    )
            if phase == "LAUNCH_UNKNOWN" and row["kind"] == "repair":
                connection.execute(
                    "UPDATE flows SET trusted=0 WHERE repo=? AND issue_number=?",
                    (row["repo"], row["issue_number"]),
                )
            if phase == "CLAIM_FAILED":
                connection.execute(
                    "DELETE FROM leases WHERE repo=? AND issue_number=? AND token=?",
                    (row["repo"], row["issue_number"], row["lease_token"]),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def admit_repair(self, candidate: RepairCandidate, ttl: float, max_repairs: int):
        repo = _repo_key(candidate.repo)
        connection = self.transaction()
        try:
            duplicate = connection.execute(
                "SELECT attempt_id, phase, repair_ordinal FROM attempts WHERE repair_key=?", (
                    compute_repair_key(candidate.repo, candidate.pr_number, candidate.head_sha),
                ),
            ).fetchone()
            if duplicate:
                connection.commit()
                return {"status": "duplicate", "attempt_id": duplicate["attempt_id"], "ordinal": duplicate["repair_ordinal"]}
            flow = connection.execute(
                "SELECT trusted, initial_confirmed, pr_number, branch FROM flows WHERE repo=? AND issue_number=?",
                (repo, candidate.issue_number),
            ).fetchone()
            if flow is None or not flow["trusted"] or not flow["initial_confirmed"]:
                connection.commit()
                return {"status": "untrusted_provenance"}
            if flow["pr_number"] is not None and (
                flow["pr_number"] != candidate.pr_number or flow["branch"] != candidate.branch
            ):
                connection.commit()
                return {"status": "wrong_pr_or_branch"}
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM attempts WHERE repo=? AND issue_number=? AND kind='repair' "
                "AND phase != 'BUDGET_EXHAUSTED'",
                (repo, candidate.issue_number),
            ).fetchone()["count"]
            if count >= max_repairs:
                attempt_id = str(uuid4())
                now = time.time()
                key = compute_repair_key(candidate.repo, candidate.pr_number, candidate.head_sha)
                connection.execute(
                    "INSERT INTO attempts(attempt_id,repo,issue_number,owner_id,lease_token,kind,cause_type,cause_id,"
                    "repair_key,repair_ordinal,expected_head_sha,pr_number,branch,phase,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,'repair',?,?,?,?,?,?,?,'BUDGET_EXHAUSTED',?,?)",
                    (attempt_id, repo, candidate.issue_number, candidate.owner_id, "", candidate.cause_type,
                     candidate.cause_id, key, count + 1, candidate.head_sha.lower(), candidate.pr_number,
                     candidate.branch, now, now),
                )
                connection.commit()
                return {"status": "budget_exhausted", "attempt_id": attempt_id, "ordinal": count + 1}
            token = self._acquire_lease(connection, repo, candidate.issue_number, candidate.owner_id, ttl)
            if token is None:
                connection.commit()
                return {"status": "lease_held"}
            attempt_id = str(uuid4())
            now = time.time()
            key = compute_repair_key(candidate.repo, candidate.pr_number, candidate.head_sha)
            connection.execute(
                "INSERT INTO attempts(attempt_id,repo,issue_number,owner_id,lease_token,kind,cause_type,cause_id,"
                "repair_key,repair_ordinal,expected_head_sha,pr_number,branch,phase,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'repair',?,?,?,?,?,?,?,'CLAIM_INTENT',?,?)",
                (attempt_id, repo, candidate.issue_number, candidate.owner_id, token, candidate.cause_type,
                 candidate.cause_id, key, count + 1, candidate.head_sha.lower(), candidate.pr_number,
                 candidate.branch, now, now),
            )
            connection.execute(
                "UPDATE flows SET pr_number=COALESCE(pr_number,?), branch=COALESCE(branch,?) "
                "WHERE repo=? AND issue_number=?",
                (candidate.pr_number, candidate.branch, repo, candidate.issue_number),
            )
            connection.commit()
            return {"status": "admitted", "attempt_id": attempt_id, "ordinal": count + 1, "repair_key": key, "lease_token": token}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def attempt(self, attempt_id: str):
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            return dict(row) if row else None

    def repair_attempts(self, repo: str, issue: int):
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM attempts WHERE repo=? AND issue_number=? AND kind='repair' ORDER BY repair_ordinal",
                (_repo_key(repo), issue),
            ).fetchall()
            return [dict(row) for row in rows]

class RuntimeCore:
    def __init__(
        self,
        database_path: str | Path,
        manifest: dict,
        workflow: WorkflowPort,
        implementer: ImplementerPort,
        lease_ttl_seconds: float = 300.0,
    ):
        self.config = RuntimeConfig.from_manifest(manifest)
        self.store = _Store(database_path)
        self.workflow = workflow
        self.implementer = implementer
        self.lease_ttl_seconds = lease_ttl_seconds

    @staticmethod
    def _eligible_initial(snapshot: WorkflowSnapshot, repo: str, issue: int) -> bool:
        return (
            _repo_key(snapshot.repo) == _repo_key(repo)
            and snapshot.issue_number == issue
            and snapshot.issue_open
            and snapshot.coordination_state == "agent-ready"
            and snapshot.frozen_spec
            and "infra-blocked" not in snapshot.terminal_labels
            and "needs-human" not in snapshot.terminal_labels
            and not snapshot.canceled
            and snapshot.open_linked_pr_count == 0
        )

    @staticmethod
    def _eligible_repair(snapshot: WorkflowSnapshot, candidate: RepairCandidate) -> bool:
        required_state = "changes-requested" if candidate.cause_type == "reviewer_rejection" else "agent-working"
        return (
            _repo_key(snapshot.repo) == _repo_key(candidate.repo)
            and snapshot.issue_number == candidate.issue_number
            and snapshot.issue_open
            and snapshot.coordination_state == required_state
            and snapshot.frozen_spec
            and "infra-blocked" not in snapshot.terminal_labels
            and "needs-human" not in snapshot.terminal_labels
            and not snapshot.canceled
            and snapshot.pr_open
            and snapshot.pr_number == candidate.pr_number
            and snapshot.pr_linked_issue == candidate.issue_number
            and bool(snapshot.pr_branch)
            and snapshot.pr_branch == candidate.branch
            and bool(snapshot.pr_head_sha)
            and snapshot.pr_head_sha.lower() == candidate.head_sha.lower()
            and bool(_SHA_RE.fullmatch(candidate.head_sha))
            and (
                candidate.cause_type != "reviewer_rejection"
                or (
                    snapshot.formal_review_state == "CHANGES_REQUESTED"
                    and snapshot.formal_review_id == candidate.cause_id
                    and snapshot.formal_review_head_sha is not None
                    and snapshot.formal_review_head_sha.lower() == snapshot.pr_head_sha.lower()
                )
            )
        )

    def dispatch_initial(self, repo: str, issue_number: int, owner_id: str):
        if not isinstance(issue_number, int) or issue_number < 1 or not isinstance(owner_id, str) or not owner_id.strip():
            return {"status": "invalid_dispatch_identity"}
        repo_key = _repo_key(repo)
        try:
            observed = self.workflow.observe(repo_key, issue_number)
        except Exception as error:
            return {"status": "workflow_unavailable", "detail": str(error)}
        if not self._eligible_initial(observed, repo_key, issue_number):
            return {"status": "ineligible"}
        created, reason = self.store.create_initial_attempt(repo_key, issue_number, owner_id, self.lease_ttl_seconds)
        if created is None:
            return {"status": reason}
        attempt_id, _token = created

        try:
            fresh = self.workflow.observe(repo_key, issue_number)
            if not self._eligible_initial(fresh, repo_key, issue_number):
                self.store.mark_phase(attempt_id, "CLAIM_FAILED", outcome="workflow_changed")
                return {"status": "claim_failed", "attempt_id": attempt_id}
            transitioned = self.workflow.transition_coordination_state(
                repo_key, issue_number, "agent-ready", "agent-working", fresh.revision
            )
        except Exception as error:
            self.store.mark_phase(attempt_id, "CLAIM_FAILED", outcome="claim_error")
            return {"status": "claim_failed", "attempt_id": attempt_id, "detail": str(error)}
        if not transitioned:
            self.store.mark_phase(attempt_id, "CLAIM_FAILED", outcome="claim_rejected")
            return {"status": "claim_failed", "attempt_id": attempt_id}

        self.store.mark_phase(attempt_id, "CLAIMED")
        self.store.mark_phase(attempt_id, "LAUNCH_INTENT")
        return self._launch(
            LaunchRequest(repo_key, issue_number, attempt_id, "initial_dispatch"),
        )

    def dispatch_repair(self, candidate: RepairCandidate):
        if candidate.cause_type not in _REPAIR_CAUSES or not isinstance(candidate.cause_id, str) or not candidate.cause_id.strip():
            return {"status": "unsupported_repair_cause"}
        if (
            not isinstance(candidate.pr_number, int)
            or candidate.pr_number < 1
            or not isinstance(candidate.issue_number, int)
            or candidate.issue_number < 1
            or not isinstance(candidate.owner_id, str)
            or not candidate.owner_id.strip()
            or not isinstance(candidate.branch, str)
            or not candidate.branch.strip()
            or not isinstance(candidate.head_sha, str)
            or not _SHA_RE.fullmatch(candidate.head_sha)
        ):
            return {"status": "invalid_repair_target"}
        repo = _repo_key(candidate.repo)
        try:
            observed = self.workflow.observe(repo, candidate.issue_number)
        except Exception as error:
            return {"status": "workflow_unavailable", "detail": str(error)}
        if not self._eligible_repair(observed, candidate):
            return {"status": "stale_or_ineligible_repair"}

        admission = self.store.admit_repair(candidate, self.lease_ttl_seconds, self.config.max_automated_repairs)
        status = admission["status"]
        if status == "budget_exhausted":
            transitioned = False
            try:
                transitioned = self.workflow.transition_coordination_state(
                    repo, candidate.issue_number, observed.coordination_state, "needs-human", observed.revision
                )
            except Exception:
                pass
            return {**admission, "needs_human": True, "needs_human_transitioned": transitioned}
        if status != "admitted":
            return admission

        attempt_id = admission["attempt_id"]
        try:
            fresh = self.workflow.observe(repo, candidate.issue_number)
            if not self._eligible_repair(fresh, candidate):
                self.store.mark_phase(attempt_id, "CLAIM_FAILED", outcome="repair_facts_changed")
                return {"status": "stale_or_ineligible_repair", "attempt_id": attempt_id}
            if candidate.cause_type == "reviewer_rejection":
                transitioned = self.workflow.transition_coordination_state(
                    repo, candidate.issue_number, "changes-requested", "agent-working", fresh.revision
                )
                if not transitioned:
                    self.store.mark_phase(attempt_id, "CLAIM_FAILED", outcome="claim_rejected")
                    return {"status": "claim_failed", "attempt_id": attempt_id}
        except Exception as error:
            self.store.mark_phase(attempt_id, "CLAIM_FAILED", outcome="claim_error")
            return {"status": "claim_failed", "attempt_id": attempt_id, "detail": str(error)}

        self.store.mark_phase(attempt_id, "CLAIMED")
        self.store.mark_phase(attempt_id, "LAUNCH_INTENT")
        launch_request = LaunchRequest(
            repo=repo,
            issue_number=candidate.issue_number,
            attempt_id=attempt_id,
            attempt_kind="repair",
            repair_cause_type=candidate.cause_type,
            repair_cause_id=candidate.cause_id,
            repair_key=admission["repair_key"],
            repair_ordinal=admission["ordinal"],
            expected_head_sha=candidate.head_sha.lower(),
            pr_number=candidate.pr_number,
            branch=candidate.branch,
        )
        return self._launch(launch_request, admission)

    def _launch(self, request: LaunchRequest, extra: dict | None = None):
        try:
            result = self.implementer.launch(request)
        except Exception as error:
            result = LaunchResult(LaunchDisposition.UNKNOWN)
            detail = str(error)
        else:
            detail = None
        if result.disposition == LaunchDisposition.CONFIRMED and result.execution_id:
            phase = "LAUNCH_CONFIRMED"
            outcome = "confirmed"
            status = "launched"
            execution_id = result.execution_id
        elif result.disposition == LaunchDisposition.DEFINITELY_NOT_STARTED:
            phase = "LAUNCH_NOT_STARTED"
            outcome = "definitely_not_started"
            status = "launch_not_started"
            execution_id = None
        else:
            phase = "LAUNCH_UNKNOWN"
            outcome = "unknown"
            status = "launch_unresolved"
            execution_id = None
        self.store.mark_phase(request.attempt_id, phase, outcome, execution_id)
        response = {
            "status": status,
            "attempt_id": request.attempt_id,
            "phase": phase,
            "execution_id": execution_id,
            "repair_key": request.repair_key,
            "ordinal": request.repair_ordinal,
        }
        if detail:
            response["detail"] = detail
        if extra:
            response.update({key: value for key, value in extra.items() if key not in response})
        return response
