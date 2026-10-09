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
    canonical_relationship_valid: bool = True
    canonical_link_count: int = 0
    pr_state: str | None = None
    pr_merged: bool = False
    check_runs: tuple[dict, ...] = ()
    active_labels: tuple[str, ...] = ()


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

    def discover_active(self, repo: str) -> list[int]: ...


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
                    repair_count INTEGER NOT NULL DEFAULT 0,
                    repair_budget_known INTEGER NOT NULL DEFAULT 1,
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
                CREATE TABLE IF NOT EXISTS lifecycle (
                    repo TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    phase TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    deadline_at REAL NOT NULL,
                    outcome TEXT,
                    updated_at REAL NOT NULL,
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
                    resulting_head_sha TEXT,
                    pr_number INTEGER,
                    branch TEXT,
                    phase TEXT NOT NULL,
                    launch_outcome TEXT,
                    execution_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    phase_entered_at REAL,
                    deadline_at REAL,
                    implementer_activity_at REAL,
                    progress_deadline_at REAL
                );
                CREATE INDEX IF NOT EXISTS attempts_flow_idx
                    ON attempts(repo, issue_number, kind, repair_ordinal);
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(attempts)")}
            if "phase_entered_at" not in columns:
                connection.execute("ALTER TABLE attempts ADD COLUMN phase_entered_at REAL")
            if "deadline_at" not in columns:
                connection.execute("ALTER TABLE attempts ADD COLUMN deadline_at REAL")
            if "implementer_activity_at" not in columns:
                connection.execute("ALTER TABLE attempts ADD COLUMN implementer_activity_at REAL")
            if "progress_deadline_at" not in columns:
                connection.execute("ALTER TABLE attempts ADD COLUMN progress_deadline_at REAL")
            migrating_bound_head = "resulting_head_sha" not in columns
            if migrating_bound_head:
                connection.execute("ALTER TABLE attempts ADD COLUMN resulting_head_sha TEXT")
            connection.execute("UPDATE attempts SET phase_entered_at=created_at WHERE phase_entered_at IS NULL")
            if migrating_bound_head:
                # Older P2-D1 code stored the bound head in expected_head_sha.
                # Preserve it as resulting evidence and invalidate any lost repair baseline.
                connection.execute("UPDATE attempts SET resulting_head_sha=expected_head_sha "
                                   "WHERE phase='PR_BOUND' AND expected_head_sha IS NOT NULL")
                connection.execute("UPDATE attempts SET expected_head_sha=NULL WHERE phase='PR_BOUND'")
                connection.execute("UPDATE flows SET trusted=0,initial_confirmed=0 WHERE EXISTS "
                                   "(SELECT 1 FROM attempts WHERE attempts.repo=flows.repo "
                                   "AND attempts.issue_number=flows.issue_number AND attempts.kind='repair' "
                                   "AND attempts.phase='PR_BOUND')")
            flow_columns = {row[1] for row in connection.execute("PRAGMA table_info(flows)")}
            migrating_repair_count = "repair_count" not in flow_columns
            if "repair_count" not in flow_columns:
                connection.execute("ALTER TABLE flows ADD COLUMN repair_count INTEGER NOT NULL DEFAULT 0")
            if "repair_budget_known" not in flow_columns:
                connection.execute("ALTER TABLE flows ADD COLUMN repair_budget_known INTEGER NOT NULL DEFAULT 1")
            connection.execute(
                "UPDATE flows SET repair_count=(SELECT COUNT(*) FROM attempts WHERE attempts.repo=flows.repo "
                "AND attempts.issue_number=flows.issue_number AND attempts.kind='repair' "
                "AND attempts.phase!='BUDGET_EXHAUSTED') WHERE repair_count=0"
            )
            if migrating_repair_count:
                connection.execute(
                    "UPDATE flows SET repair_budget_known=CASE WHEN EXISTS "
                    "(SELECT 1 FROM attempts WHERE attempts.repo=flows.repo "
                    "AND attempts.issue_number=flows.issue_number) THEN 1 ELSE 0 END"
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

    def create_initial_attempt(self, repo: str, issue: int, owner: str, ttl: float,
                               recovery_timeout: float):
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
                "AND phase NOT IN ('CLAIM_FAILED','LAUNCH_NOT_STARTED','RETRY_ELIGIBLE') LIMIT 1",
                (repo, issue),
            ).fetchone()
            if flow and (flow["initial_confirmed"] or connection.execute(
                "SELECT 1 FROM attempts WHERE repo=? AND issue_number=? AND kind='repair' LIMIT 1", (repo, issue)
            ).fetchone()):
                connection.commit()
                return None, "flow_already_started"
            if prior and prior["phase"] not in {"CLAIM_FAILED", "LAUNCH_NOT_STARTED", "RETRY_ELIGIBLE"}:
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
                "INSERT INTO attempts(attempt_id,repo,issue_number,owner_id,lease_token,kind,phase,created_at,updated_at,"
                "phase_entered_at,deadline_at) VALUES(?,?,?,?,?,'initial_dispatch','CLAIM_INTENT',?,?,?,?)",
                (attempt_id, repo, issue, owner, token, now, now, now, now + recovery_timeout),
            )
            connection.commit()
            return (attempt_id, token), None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_phase(self, attempt_id: str, phase: str, outcome: str | None = None,
                   execution_id: str | None = None, recovery_timeout: float = 300.0,
                   implementer_progress_timeout: float | None = None):
        connection = self.transaction()
        try:
            row = connection.execute(
                "SELECT repo, issue_number, lease_token, kind, phase FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown attempt")
            now = time.time()
            entered = now if row["phase"] != phase else None
            deadline = now + recovery_timeout if entered is not None and phase in {
                "CLAIM_INTENT", "CLAIMED", "LAUNCH_INTENT", "LAUNCH_UNKNOWN", "LAUNCH_CONFIRMED"
            } else None
            connection.execute(
                "UPDATE attempts SET phase=?, launch_outcome=?, execution_id=?, updated_at=?, "
                "phase_entered_at=COALESCE(?,phase_entered_at), deadline_at=CASE WHEN ? IS NOT NULL THEN ? "
                "WHEN ? THEN NULL ELSE deadline_at END WHERE attempt_id=?",
                (phase, outcome, execution_id, now, entered, deadline, deadline,
                 phase in {"CLAIM_FAILED", "LAUNCH_NOT_STARTED", "BUDGET_EXHAUSTED", "PR_BOUND",
                           "STOPPED_CANCELLED", "TERMINAL_UNRESOLVED", "RETRY_ELIGIBLE"}, attempt_id),
            )
            if phase == "LAUNCH_CONFIRMED" and execution_id and implementer_progress_timeout is not None:
                connection.execute("UPDATE attempts SET implementer_activity_at=?,progress_deadline_at=? "
                                   "WHERE attempt_id=?", (now, now + implementer_progress_timeout, attempt_id))
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

    def admit_repair(self, candidate: RepairCandidate, ttl: float, max_repairs: int,
                     recovery_timeout: float):
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
                "SELECT trusted, initial_confirmed, pr_number, branch, repair_count, repair_budget_known FROM flows "
                "WHERE repo=? AND issue_number=?",
                (repo, candidate.issue_number),
            ).fetchone()
            if flow is None or not flow["trusted"] or not flow["initial_confirmed"]:
                connection.commit()
                return {"status": "untrusted_provenance"}
            if not flow["repair_budget_known"]:
                connection.commit()
                return {"status": "repair_budget_unknown"}
            initial = connection.execute(
                "SELECT phase FROM attempts WHERE repo=? AND issue_number=? "
                "AND kind IN ('initial_dispatch','adopted') "
                "AND phase IN ('LAUNCH_CONFIRMED','PR_BOUND') LIMIT 1", (repo, candidate.issue_number)
            ).fetchone()
            if initial is None:
                connection.commit()
                return {"status": "untrusted_provenance"}
            if flow["pr_number"] is not None and (
                flow["pr_number"] != candidate.pr_number or flow["branch"] != candidate.branch
            ):
                connection.commit()
                return {"status": "wrong_pr_or_branch"}
            recorded_count = connection.execute(
                "SELECT COUNT(*) AS count FROM attempts WHERE repo=? AND issue_number=? AND kind='repair' "
                "AND phase != 'BUDGET_EXHAUSTED'",
                (repo, candidate.issue_number),
            ).fetchone()["count"]
            count = max(flow["repair_count"], recorded_count)
            if count >= max_repairs:
                attempt_id = str(uuid4())
                now = time.time()
                key = compute_repair_key(candidate.repo, candidate.pr_number, candidate.head_sha)
                connection.execute(
                    "INSERT INTO attempts(attempt_id,repo,issue_number,owner_id,lease_token,kind,cause_type,cause_id,"
                    "repair_key,repair_ordinal,expected_head_sha,pr_number,branch,phase,created_at,updated_at,phase_entered_at) "
                    "VALUES(?,?,?,?,?,'repair',?,?,?,?,?,?,?,'BUDGET_EXHAUSTED',?,?,?)",
                    (attempt_id, repo, candidate.issue_number, candidate.owner_id, "", candidate.cause_type,
                     candidate.cause_id, key, count + 1, candidate.head_sha.lower(), candidate.pr_number,
                     candidate.branch, now, now, now),
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
                "repair_key,repair_ordinal,expected_head_sha,pr_number,branch,phase,created_at,updated_at,"
                "phase_entered_at,deadline_at) VALUES(?,?,?,?,?,'repair',?,?,?,?,?,?,?,'CLAIM_INTENT',?,?,?,?)",
                (attempt_id, repo, candidate.issue_number, candidate.owner_id, token, candidate.cause_type,
                 candidate.cause_id, key, count + 1, candidate.head_sha.lower(), candidate.pr_number,
                 candidate.branch, now, now, now, now + recovery_timeout),
            )
            connection.execute(
                "UPDATE flows SET pr_number=COALESCE(pr_number,?), branch=COALESCE(branch,?) "
                "WHERE repo=? AND issue_number=?",
                (candidate.pr_number, candidate.branch, repo, candidate.issue_number),
            )
            connection.execute("UPDATE flows SET repair_count=? WHERE repo=? AND issue_number=?",
                               (count + 1, repo, candidate.issue_number))
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

    def attempts_for_repo(self, repo: str):
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM attempts WHERE repo=? ORDER BY created_at, rowid", (_repo_key(repo),)
            ).fetchall()
            return [dict(row) for row in rows]

    def flow(self, repo: str, issue: int):
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM flows WHERE repo=? AND issue_number=?",
                                     (_repo_key(repo), issue)).fetchone()
            return dict(row) if row else None

    def renew_owner_lease(self, repo: str, issue: int, owner: str, now: float, ttl: float):
        connection = self.transaction()
        try:
            row = connection.execute("SELECT owner_id FROM leases WHERE repo=? AND issue_number=?",
                                     (_repo_key(repo), issue)).fetchone()
            if row is None:
                connection.commit()
                return False
            if row["owner_id"] != owner:
                connection.commit()
                return False
            connection.execute("UPDATE leases SET expires_at=? WHERE repo=? AND issue_number=?",
                               (now + ttl, _repo_key(repo), issue))
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def update_implementer_activity(self, attempt_id: str, activity_at: float, timeout: float):
        with closing(self._connect()) as connection:
            connection.execute("UPDATE attempts SET implementer_activity_at=?,progress_deadline_at=?,updated_at=? "
                               "WHERE attempt_id=? AND phase='LAUNCH_CONFIRMED'",
                               (activity_at, activity_at + timeout, activity_at, attempt_id))
            connection.commit()

    def bind_pr(self, attempt_id: str, pr_number: int, branch: str, head_sha: str, now: float):
        connection = self.transaction()
        try:
            row = connection.execute("SELECT repo,issue_number,kind,phase FROM attempts WHERE attempt_id=?",
                                     (attempt_id,)).fetchone()
            if row is None or row["phase"] in {"STOPPED_CANCELLED", "TERMINAL_UNRESOLVED"}:
                connection.commit()
                return False
            connection.execute("UPDATE flows SET pr_number=?,branch=? WHERE repo=? AND issue_number=?",
                               (pr_number, branch, row["repo"], row["issue_number"]))
            if row["kind"] == "initial_dispatch":
                connection.execute("UPDATE flows SET trusted=1,initial_confirmed=1 WHERE repo=? AND issue_number=?",
                                   (row["repo"], row["issue_number"]))
            connection.execute("UPDATE attempts SET phase='PR_BOUND',pr_number=?,branch=?,resulting_head_sha=?,"
                               "phase_entered_at=?,deadline_at=NULL,updated_at=? WHERE attempt_id=?",
                               (pr_number, branch, head_sha.lower(), now, now, attempt_id))
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ensure_deadline(self, attempt_id: str, recovery_timeout: float):
        connection = self.transaction()
        try:
            connection.execute("UPDATE attempts SET deadline_at=phase_entered_at+? "
                               "WHERE attempt_id=? AND deadline_at IS NULL",
                               (recovery_timeout, attempt_id))
            row = connection.execute("SELECT deadline_at FROM attempts WHERE attempt_id=?",
                                     (attempt_id,)).fetchone()
            connection.commit()
            return row["deadline_at"] if row else None
        finally:
            connection.close()

    def adopt_orphan(self, repo: str, issue: int, owner: str, pr_number: int, branch: str,
                     head_sha: str, now: float, trusted: bool = False):
        connection = self.transaction()
        try:
            existing = connection.execute("SELECT 1 FROM flows WHERE repo=? AND issue_number=?",
                                          (_repo_key(repo), issue)).fetchone()
            if existing:
                connection.execute("UPDATE flows SET trusted=?,initial_confirmed=?,pr_number=?,branch=? "
                                   "WHERE repo=? AND issue_number=?",
                                   (int(trusted), int(trusted), pr_number, branch, _repo_key(repo), issue))
            else:
                connection.execute("INSERT INTO flows(repo,issue_number,trusted,initial_confirmed,pr_number,branch) "
                                   "VALUES(?,?,?,?,?,?)", (_repo_key(repo), issue, int(trusted), int(trusted),
                                                            pr_number, branch))
                if trusted:
                    connection.execute("UPDATE flows SET repair_budget_known=0 WHERE repo=? AND issue_number=?",
                                       (_repo_key(repo), issue))
            attempt_id = str(uuid4())
            connection.execute("INSERT INTO attempts(attempt_id,repo,issue_number,owner_id,lease_token,kind,"
                               "phase,resulting_head_sha,pr_number,branch,created_at,updated_at,phase_entered_at) "
                               "VALUES(?,?,?,?,'','adopted','PR_BOUND',?,?,?,?,?,?)",
                               (attempt_id, _repo_key(repo), issue, owner, head_sha.lower(), pr_number,
                                branch, now, now, now))
            connection.commit()
            return attempt_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def local_terminal(self, attempt_id: str, phase: str, now: float):
        connection = self.transaction()
        try:
            row = connection.execute("SELECT repo,issue_number,lease_token FROM attempts WHERE attempt_id=?",
                                     (attempt_id,)).fetchone()
            if row:
                connection.execute("UPDATE attempts SET phase=?,updated_at=?,phase_entered_at=?,deadline_at=NULL "
                                   "WHERE attempt_id=?", (phase, now, now, attempt_id))
                connection.execute("UPDATE flows SET trusted=0,initial_confirmed=0 WHERE repo=? AND issue_number=?",
                                   (row["repo"], row["issue_number"]))
                connection.execute("DELETE FROM leases WHERE repo=? AND issue_number=? AND token=?",
                                   (row["repo"], row["issue_number"], row["lease_token"]))
            connection.commit()
        finally:
            connection.close()

    def repair_attempts(self, repo: str, issue: int):
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM attempts WHERE repo=? AND issue_number=? AND kind='repair' ORDER BY repair_ordinal",
                (_repo_key(repo), issue),
            ).fetchall()
            return [dict(row) for row in rows]

    def lifecycle_for_repo(self, repo: str):
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM lifecycle WHERE repo=? ORDER BY issue_number",
                                      (_repo_key(repo),)).fetchall()
            return [dict(row) for row in rows]

    def lifecycle_state(self, repo: str, issue: int):
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM lifecycle WHERE repo=? AND issue_number=?",
                                     (_repo_key(repo), issue)).fetchone()
            return dict(row) if row else None

    def set_lifecycle(self, repo: str, issue: int, phase: str, head_sha: str, now: float,
                      timeout: float, outcome: str | None = None):
        connection = self.transaction()
        try:
            prior = connection.execute("SELECT phase,head_sha,deadline_at FROM lifecycle "
                                       "WHERE repo=? AND issue_number=?",
                                       (_repo_key(repo), issue)).fetchone()
            if prior and prior["phase"] == phase and prior["head_sha"] == head_sha:
                deadline = prior["deadline_at"]
            else:
                deadline = now + timeout
            connection.execute("INSERT INTO lifecycle(repo,issue_number,phase,head_sha,deadline_at,outcome,updated_at) "
                               "VALUES(?,?,?,?,?,?,?) ON CONFLICT(repo,issue_number) DO UPDATE SET "
                               "phase=excluded.phase,head_sha=excluded.head_sha,deadline_at=excluded.deadline_at,"
                               "outcome=excluded.outcome,updated_at=excluded.updated_at",
                               (_repo_key(repo), issue, phase, head_sha.lower(), deadline, outcome, now))
            connection.commit()
            return deadline
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete_flow(self, repo: str, issue: int, now: float):
        connection = self.transaction()
        try:
            tokens = connection.execute("SELECT DISTINCT lease_token FROM attempts WHERE repo=? AND issue_number=? "
                                        "AND phase IN ('PR_BOUND','LAUNCH_CONFIRMED') AND lease_token!=''",
                                        (_repo_key(repo), issue)).fetchall()
            connection.execute("UPDATE flows SET trusted=0,initial_confirmed=0 WHERE repo=? AND issue_number=?",
                               (_repo_key(repo), issue))
            connection.execute("UPDATE attempts SET phase='MERGED_SUCCESS',updated_at=?,phase_entered_at=?,"
                               "deadline_at=NULL WHERE repo=? AND issue_number=? AND phase IN "
                               "('PR_BOUND','LAUNCH_CONFIRMED')",
                               (now, now, _repo_key(repo), issue))
            for token in tokens:
                connection.execute("DELETE FROM leases WHERE repo=? AND issue_number=? AND token=?",
                                   (_repo_key(repo), issue, token["lease_token"]))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

class RuntimeCore:
    def __init__(
        self,
        database_path: str | Path,
        manifest: dict,
        workflow: WorkflowPort,
        implementer: ImplementerPort,
        lease_ttl_seconds: float = 300.0,
        recovery_timeout_seconds: float = 900.0,
        implementer_progress_timeout_seconds: float = 1800.0,
    ):
        self.config = RuntimeConfig.from_manifest(manifest)
        self.store = _Store(database_path)
        self.workflow = workflow
        self.implementer = implementer
        self.lease_ttl_seconds = lease_ttl_seconds
        if recovery_timeout_seconds <= 0:
            raise ValueError("recovery_timeout_seconds must be positive")
        self.recovery_timeout_seconds = recovery_timeout_seconds
        if implementer_progress_timeout_seconds <= 0:
            raise ValueError("implementer_progress_timeout_seconds must be positive")
        self.implementer_progress_timeout_seconds = implementer_progress_timeout_seconds

    def observe_implementer_progress(self, repo: str, now: float | None = None):
        """Advance durable Implementer activity only when its conversation artifacts advance."""
        current = time.time() if now is None else float(now)
        items = []
        for row in self.store.attempts_for_repo(repo):
            if row["phase"] != "LAUNCH_CONFIRMED" or not row.get("execution_id"):
                continue
            observer = getattr(self.implementer, "latest_activity", None)
            activity = observer(row["execution_id"]) if observer else None
            baseline = row.get("implementer_activity_at") or row.get("phase_entered_at") or row["created_at"]
            if activity is not None and activity > baseline:
                self.store.update_implementer_activity(row["attempt_id"], activity,
                                                       self.implementer_progress_timeout_seconds)
                items.append({"issue": row["issue_number"], "status": "progress"})
                continue
            deadline = row.get("progress_deadline_at") or (baseline + self.implementer_progress_timeout_seconds)
            if current >= deadline:
                try:
                    snapshot = self.workflow.observe(repo, row["issue_number"])
                    if not (snapshot.issue_open and snapshot.coordination_state == "agent-working"
                            and snapshot.frozen_spec and not snapshot.canceled and not snapshot.terminal_labels):
                        self.store.local_terminal(row["attempt_id"], "STOPPED_CANCELLED", current)
                        items.append({"issue": row["issue_number"], "status": "cancelled_or_terminal"})
                        continue
                    changed = self.workflow.transition_coordination_state(repo, row["issue_number"],
                        "agent-working", "infra-blocked", snapshot.revision)
                    if changed:
                        self.store.local_terminal(row["attempt_id"], "TERMINAL_UNRESOLVED", current)
                        items.append({"issue": row["issue_number"], "status": "implementer_stalled"})
                    else:
                        items.append({"issue": row["issue_number"], "status": "stale_watchdog_evidence"})
                except Exception as error:
                    items.append({"issue": row["issue_number"], "status": "workflow_unavailable",
                                  "detail": str(error)})
            else:
                items.append({"issue": row["issue_number"], "status": "no_new_activity"})
        return {"status": "observed", "items": items}

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
        created, reason = self.store.create_initial_attempt(repo_key, issue_number, owner_id,
                                                            self.lease_ttl_seconds,
                                                            self.recovery_timeout_seconds)
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

        self.store.mark_phase(attempt_id, "CLAIMED", recovery_timeout=self.recovery_timeout_seconds)
        self.store.mark_phase(attempt_id, "LAUNCH_INTENT", recovery_timeout=self.recovery_timeout_seconds)
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

        admission = self.store.admit_repair(candidate, self.lease_ttl_seconds, self.config.max_automated_repairs,
                                            self.recovery_timeout_seconds)
        status = admission["status"]
        if status == "repair_budget_unknown":
            transitioned = False
            try:
                transitioned = self.workflow.transition_coordination_state(
                    repo, candidate.issue_number, observed.coordination_state, "needs-human", observed.revision
                )
            except Exception:
                pass
            return {**admission, "reason": "repair_history_or_budget_provenance_unknown",
                    "needs_human": True, "needs_human_transitioned": transitioned}
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

        self.store.mark_phase(attempt_id, "CLAIMED", recovery_timeout=self.recovery_timeout_seconds)
        self.store.mark_phase(attempt_id, "LAUNCH_INTENT", recovery_timeout=self.recovery_timeout_seconds)
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
        self.store.mark_phase(request.attempt_id, phase, outcome, execution_id,
                              recovery_timeout=self.recovery_timeout_seconds,
                              implementer_progress_timeout=self.implementer_progress_timeout_seconds)
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

    def reconcile_once(self, repo: str, owner_id: str, now: float | None = None):
        """Reconcile only durable attempts and GitHub's already-active issues; never launches."""
        repo_key = _repo_key(repo)
        if not isinstance(owner_id, str) or not owner_id.strip():
            return {"status": "invalid_owner", "items": []}
        current_time = time.time() if now is None else float(now)
        try:
            discovered = self.workflow.discover_active(repo_key)
        except Exception as error:
            return {"status": "recovery_discovery_unavailable", "detail": str(error), "items": []}
        attempts = self.store.attempts_for_repo(repo_key)
        issue_numbers = set(discovered)
        issue_numbers.update(row["issue_number"] for row in attempts)
        output = []
        active_phases = {"CLAIM_INTENT", "CLAIMED", "LAUNCH_INTENT", "LAUNCH_UNKNOWN", "LAUNCH_CONFIRMED"}
        for issue in sorted(issue_numbers):
            try:
                snapshot = self.workflow.observe(repo_key, issue)
            except Exception as error:
                output.append({"issue": issue, "status": "workflow_unavailable", "detail": str(error)})
                continue
            rows = [row for row in attempts if row["issue_number"] == issue]
            if any(row["phase"] == "MERGED_SUCCESS" for row in rows):
                output.append({"issue": issue, "status": "merged_success"})
                continue
            active_rows = [row for row in rows if row["phase"] in active_phases]
            latest = active_rows[-1] if active_rows else None
            local_fence_attempts = [row for row in rows
                                    if row["phase"] in active_phases | {"PR_BOUND"}]
            if snapshot.canceled or not snapshot.issue_open or not snapshot.frozen_spec or snapshot.terminal_labels:
                if local_fence_attempts:
                    try:
                        fresh = self.workflow.observe(repo_key, issue)
                        if fresh.revision == snapshot.revision and (fresh.canceled or not fresh.issue_open
                                or not fresh.frozen_spec or fresh.terminal_labels):
                            for row in local_fence_attempts:
                                self.store.local_terminal(row["attempt_id"], "STOPPED_CANCELLED", current_time)
                    except Exception:
                        pass
                output.append({"issue": issue, "status": "cancelled_or_terminal"})
                continue
            if snapshot.coordination_state not in {"agent-working", "changes-requested"}:
                if latest and snapshot.coordination_state == "agent-ready":
                    if latest["phase"] == "CLAIM_INTENT" and snapshot.canonical_relationship_valid \
                            and snapshot.canonical_link_count == 0 and snapshot.open_linked_pr_count == 0:
                        self.store.mark_phase(latest["attempt_id"], "RETRY_ELIGIBLE")
                        output.append({"issue": issue, "status": "retry_eligible"})
                    elif latest["phase"] in {"LAUNCH_UNKNOWN", "LAUNCH_CONFIRMED"} \
                            and snapshot.canonical_relationship_valid and snapshot.canonical_link_count == 0 \
                            and snapshot.open_linked_pr_count == 0:
                        output.append({"issue": issue, "status": "unresolved_claim_contradiction"})
                    else:
                        self._recovery_terminal(repo_key, issue, snapshot, "needs-human")
                        self.store.local_terminal(latest["attempt_id"], "TERMINAL_UNRESOLVED", current_time)
                        output.append({"issue": issue, "status": "contradictory_claim_evidence"})
                continue
            if latest and latest["owner_id"] != owner_id:
                output.append({"issue": issue, "status": "owner_mismatch"})
                continue
            flow = self.store.flow(repo_key, issue)
            has_repair_history = any(r["kind"] == "repair" and r["phase"] != "BUDGET_EXHAUSTED" for r in rows)
            if snapshot.coordination_state == "changes-requested" and (
                    not flow or not flow.get("trusted") or not has_repair_history):
                self._recovery_terminal(repo_key, issue, snapshot, "needs-human")
                for row in local_fence_attempts:
                    self.store.local_terminal(row["attempt_id"], "TERMINAL_UNRESOLVED", current_time)
                output.append({"issue": issue, "status": "untrusted_repair_history"})
                continue
            if not latest and rows:
                phase = rows[-1]["phase"]
                if phase in {"STOPPED_CANCELLED", "TERMINAL_UNRESOLVED"}:
                    output.append({"issue": issue, "status": "stopped_terminal"})
                    continue
                if phase == "PR_BOUND":
                    output.append({"issue": issue, "status": "already_bound", "pr": rows[-1]["pr_number"]})
                    continue
                terminal = "needs-human"
                self._recovery_terminal(repo_key, issue, snapshot, terminal)
                self.store.local_terminal(rows[-1]["attempt_id"], "TERMINAL_UNRESOLVED", current_time)
                output.append({"issue": issue, "status": "stale_or_contradictory_local_history",
                               "terminal": terminal})
                continue
            if latest:
                self.store.renew_owner_lease(repo_key, issue, owner_id, current_time, self.lease_ttl_seconds)
                latest["deadline_at"] = self.store.ensure_deadline(latest["attempt_id"],
                                                                    self.recovery_timeout_seconds)
                if not snapshot.canonical_relationship_valid:
                    self._recovery_terminal(repo_key, issue, snapshot, "needs-human")
                    self.store.local_terminal(latest["attempt_id"], "TERMINAL_UNRESOLVED", current_time)
                    output.append({"issue": issue, "status": "ambiguous_relationship"})
                    continue
                if snapshot.canonical_link_count > 1 or snapshot.open_linked_pr_count > 1:
                    self._recovery_terminal(repo_key, issue, snapshot, "needs-human")
                    self.store.local_terminal(latest["attempt_id"], "TERMINAL_UNRESOLVED", current_time)
                    output.append({"issue": issue, "status": "ambiguous_pr"})
                    continue
                if snapshot.pr_open and snapshot.pr_number is not None and snapshot.pr_branch and snapshot.pr_head_sha:
                    if latest["kind"] == "repair":
                        same_target = (latest["pr_number"] == snapshot.pr_number
                                       and latest["branch"] == snapshot.pr_branch)
                        if not same_target:
                            self._recovery_terminal(repo_key, issue, snapshot, "needs-human")
                            self.store.local_terminal(latest["attempt_id"], "TERMINAL_UNRESOLVED", current_time)
                            output.append({"issue": issue, "status": "repair_target_changed"})
                            continue
                        if (latest["expected_head_sha"] or "").lower() == snapshot.pr_head_sha.lower():
                            if current_time >= (latest["deadline_at"] or float("inf")):
                                self._recovery_terminal(repo_key, issue, snapshot, "infra-blocked")
                                self.store.local_terminal(latest["attempt_id"], "TERMINAL_UNRESOLVED", current_time)
                                output.append({"issue": issue, "status": "repair_head_timeout"})
                            else:
                                output.append({"issue": issue, "status": "waiting_for_repair_push"})
                            continue
                    if not self._recovery_pr_still_current(repo_key, issue, snapshot):
                        output.append({"issue": issue, "status": "stale_recovery_evidence"})
                        continue
                    self.store.bind_pr(latest["attempt_id"], snapshot.pr_number, snapshot.pr_branch,
                                       snapshot.pr_head_sha, current_time)
                    output.append({"issue": issue, "status": "pr_bound", "pr": snapshot.pr_number,
                                   "head": snapshot.pr_head_sha})
                    continue
                if current_time >= (latest["deadline_at"] or float("inf")):
                    terminal = "needs-human" if snapshot.coordination_state == "changes-requested" else "infra-blocked"
                    self._recovery_terminal(repo_key, issue, snapshot, terminal)
                    self.store.local_terminal(latest["attempt_id"], "TERMINAL_UNRESOLVED", current_time)
                    output.append({"issue": issue, "status": "recovery_timeout", "terminal": terminal})
                else:
                    output.append({"issue": issue, "status": "unresolved"})
                continue

            # No local attempt is authoritative for this active workflow: treat it as orphaned.
            if snapshot.coordination_state == "changes-requested":
                if not flow or not flow.get("trusted") or not any(r["kind"] == "repair" for r in rows):
                    self._recovery_terminal(repo_key, issue, snapshot, "needs-human")
                    output.append({"issue": issue, "status": "untrusted_repair_history"})
                else:
                    output.append({"issue": issue, "status": "repair_history_present"})
                continue
            if not snapshot.canonical_relationship_valid or snapshot.canonical_link_count > 1 \
                    or snapshot.open_linked_pr_count > 1:
                self._recovery_terminal(repo_key, issue, snapshot, "needs-human")
                output.append({"issue": issue, "status": "ambiguous_pr"})
            elif snapshot.pr_open and snapshot.pr_number is not None and snapshot.pr_branch and snapshot.pr_head_sha:
                if not self._recovery_pr_still_current(repo_key, issue, snapshot):
                    output.append({"issue": issue, "status": "stale_recovery_evidence"})
                    continue
                # GitHub evidence is sufficient to restore provenance: one native
                # closing relationship, canonical author/base, current authorization,
                # and a fresh unchanged PR snapshot. Local launch receipts are not required.
                adopted = self.store.adopt_orphan(repo_key, issue, owner_id, snapshot.pr_number,
                                                  snapshot.pr_branch, snapshot.pr_head_sha, current_time,
                                                  trusted=True)
                output.append({"issue": issue, "status": "orphan_adopted_trusted", "attempt_id": adopted,
                               "pr": snapshot.pr_number})
            else:
                self._recovery_terminal(repo_key, issue, snapshot, "infra-blocked")
                output.append({"issue": issue, "status": "orphan_without_pr"})
        return {"status": "reconciled", "items": output}

    def _recovery_terminal(self, repo: str, issue: int, snapshot: WorkflowSnapshot, target: str):
        if snapshot.coordination_state not in {"agent-ready", "agent-working", "changes-requested"}:
            return False
        try:
            fresh = self.workflow.observe(repo, issue)
            if (fresh.revision != snapshot.revision or fresh.coordination_state != snapshot.coordination_state
                    or fresh.canceled or fresh.terminal_labels or not fresh.issue_open or not fresh.frozen_spec):
                return False
            return self.workflow.transition_coordination_state(
                repo, issue, fresh.coordination_state, target, fresh.revision)
        except Exception:
            return False

    def _recovery_pr_still_current(self, repo: str, issue: int, expected: WorkflowSnapshot):
        try:
            current = self.workflow.observe(repo, issue)
            return (current.revision == expected.revision and current.issue_open and current.frozen_spec
                    and not current.canceled and not current.terminal_labels
                    and current.coordination_state in {"agent-working", "changes-requested"}
                    and current.canonical_relationship_valid and current.canonical_link_count == 1
                    and current.open_linked_pr_count == 1 and current.pr_open
                    and current.pr_linked_issue == issue
                    and current.pr_number == expected.pr_number and current.pr_branch == expected.pr_branch
                    and current.pr_head_sha == expected.pr_head_sha)
        except Exception:
            return False
