"""One-shot exact-head CI, formal Review, and native merge observation."""
from __future__ import annotations

import time

from .core import RepairCandidate


_PHASES = {
    "WAITING_CI": ("waiting_ci", "infra-blocked"),
    "WAITING_REVIEW": ("waiting_review", "needs-human"),
    "WAITING_MERGE": ("waiting_merge", "infra-blocked"),
}


class LifecycleDriver:
    def __init__(self, core, manifest: dict, timeouts: dict | None = None):
        self.core = core
        self.manifest = manifest
        required_checks = (manifest.get("reviewer") or {}).get("required_checks")
        if not isinstance(required_checks, list) or not required_checks:
            raise ValueError("reviewer.required_checks must contain consumer-defined checks")
        names = []
        for check in required_checks:
            if not isinstance(check, dict) or not isinstance(check.get("name"), str) \
                    or not check["name"].strip() or not isinstance(check.get("accepted_conclusions"), list) \
                    or not check["accepted_conclusions"] \
                    or any(not isinstance(value, str) or not value.strip()
                           for value in check["accepted_conclusions"]):
                raise ValueError("each required check needs a name and accepted conclusions")
            names.append(check["name"])
            for field in ("implementation_failure_conclusions", "infrastructure_failure_conclusions"):
                values = check.get(field, [])
                if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip()
                                                       for value in values):
                    raise ValueError(f"{field} must contain non-empty conclusion names")
        if len(set(names)) != len(names):
            raise ValueError("reviewer.required_checks names must be unique")
        self.timeouts = {"waiting_ci": 3600.0, "waiting_review": 86400.0,
                         "waiting_merge": 86400.0, **(timeouts or {})}
        if any(not isinstance(value, (int, float)) or value <= 0 for value in self.timeouts.values()):
            raise ValueError("lifecycle timeouts must be positive numbers")

    def _terminal(self, repo, issue, snapshot, label, now, outcome):
        transitioned = False
        if snapshot.coordination_state in {"agent-working", "changes-requested"} \
                and not snapshot.terminal_labels and snapshot.issue_open:
            try:
                transitioned = self.core.workflow.transition_coordination_state(
                    repo, issue, snapshot.coordination_state, label, snapshot.revision)
            except Exception:
                transitioned = False
        rows = [row for row in self.core.store.attempts_for_repo(repo)
                if row["issue_number"] == issue and row["phase"] not in {"MERGED_SUCCESS", "STOPPED_CANCELLED"}]
        for row in rows:
            self.core.store.local_terminal(row["attempt_id"], "TERMINAL_UNRESOLVED", now)
        self.core.store.set_lifecycle(repo, issue, label.upper().replace("-", "_"),
                                      snapshot.pr_head_sha or "", now, 1, outcome)
        return {"issue": issue, "status": label, "transitioned": transitioned, "outcome": outcome}

    def _set_waiting(self, repo, issue, phase, head, now):
        duration = self.timeouts[_PHASES[phase][0]]
        deadline = self.core.store.set_lifecycle(repo, issue, phase, head, now, duration)
        return deadline

    def _checks(self, snapshot):
        """Return pending, success, implementation, infrastructure, or ambiguous."""
        required = self.manifest["reviewer"]["required_checks"]
        pending = False
        failures = set()
        evidence = []
        for spec in required:
            runs = [run for run in snapshot.check_runs
                    if run.get("name") == spec["name"]
                    and str(run.get("head_sha", "")).casefold() == (snapshot.pr_head_sha or "").casefold()]
            if not runs:
                pending = True
                continue
            run = max(runs, key=lambda item: (str(item.get("started_at") or ""),
                                              int(item.get("id") or 0)))
            if run.get("status") != "completed" or not run.get("conclusion"):
                pending = True
                continue
            conclusion = run["conclusion"]
            if conclusion in spec["accepted_conclusions"]:
                continue
            impl = spec.get("implementation_failure_conclusions", [])
            infra = spec.get("infrastructure_failure_conclusions", [])
            is_impl, is_infra = conclusion in impl, conclusion in infra
            if is_impl == is_infra:
                failures.add("ambiguous")
            else:
                failures.add("implementation" if is_impl else "infrastructure")
            evidence.append(f"{spec['name']}:{run.get('id', conclusion)}:{conclusion}")
        if pending:
            return "pending", evidence
        if not failures:
            return "success", evidence
        if len(failures) != 1 or "ambiguous" in failures:
            return "ambiguous", evidence
        return next(iter(failures)), evidence

    def advance_once(self, repo: str, owner_id: str, now: float | None = None):
        """Observe and advance only locally bound work; never schedules agent-ready Issues."""
        if not isinstance(owner_id, str) or not owner_id.strip():
            return {"status": "invalid_owner", "items": []}
        current = time.time() if now is None else float(now)
        repo_key = repo.casefold()
        attempts = self.core.store.attempts_for_repo(repo_key)
        issues = sorted({row["issue_number"] for row in attempts
                         if row["pr_number"] and row["phase"] in {
                             "PR_BOUND", "LAUNCH_CONFIRMED", "MERGED_SUCCESS"}})
        active_repair_phases = {"CLAIM_INTENT", "CLAIMED", "LAUNCH_INTENT", "LAUNCH_UNKNOWN",
                                "LAUNCH_CONFIRMED"}
        issues_with_active_repair = {row["issue_number"] for row in attempts
                                     if row["kind"] == "repair" and row["phase"] in active_repair_phases}
        merged_results = []

        # A merged canonical PR outranks Issue auto-closure/cancellation fencing.
        for issue in issues:
            if issue in issues_with_active_repair:
                continue
            try:
                snapshot = self.core.workflow.observe(repo_key, issue)
            except Exception:
                continue
            bound_prs = {row["pr_number"] for row in attempts if row["issue_number"] == issue
                         and row["pr_number"] is not None}
            if snapshot.pr_merged and snapshot.pr_number in bound_prs:
                try:
                    labels_cleared = self.core.workflow.clear_active_coordination_labels(repo_key, issue)
                except Exception:
                    labels_cleared = False
                self.core.store.complete_flow(repo_key, issue, current)
                self.core.store.set_lifecycle(repo_key, issue, "MERGED_SUCCESS",
                                              snapshot.pr_head_sha or "", current, 1,
                                              "merged" if labels_cleared else "merged_cleanup_pending")
                merged_results.append({"issue": issue, "status": "merged_success",
                                       "head": snapshot.pr_head_sha,
                                       "active_labels_cleared": labels_cleared})

        # Reuse D1 only when durable dispatch attempts need recovery/binding. Calling it
        # for a first exact-head Reviewer rejection would incorrectly classify the
        # intentionally not-yet-created repair ledger as orphaned history.
        if any(row["phase"] in {"CLAIM_INTENT", "CLAIMED", "LAUNCH_INTENT", "LAUNCH_UNKNOWN",
                                "LAUNCH_CONFIRMED"} for row in attempts):
            self.core.reconcile_once(repo_key, owner_id, current)
        attempts = self.core.store.attempts_for_repo(repo_key)
        issues = sorted({row["issue_number"] for row in attempts if row["phase"] == "PR_BOUND"})
        results = list(merged_results)
        for issue in issues:
            rows = [row for row in attempts if row["issue_number"] == issue]
            in_flight_repairs = [row for row in rows if row["kind"] == "repair"
                                 and row["phase"] in active_repair_phases]
            if in_flight_repairs:
                latest_repair = in_flight_repairs[-1]
                results.append({"issue": issue, "status": "waiting_for_repair",
                                "attempt_id": latest_repair["attempt_id"],
                                "phase": latest_repair["phase"]})
                continue
            bound = max(rows, key=lambda row: (row["updated_at"], row["created_at"]))
            state = self.core.store.lifecycle_state(repo_key, issue)
            if state and state["phase"] == "MERGED_SUCCESS":
                results.append({"issue": issue, "status": "merged_success", "head": state["head_sha"],
                                "active_labels_cleared": state["outcome"] != "merged_cleanup_pending"})
                continue
            try:
                snapshot = self.core.workflow.observe(repo_key, issue)
            except Exception as error:
                results.append({"issue": issue, "status": "workflow_unavailable", "detail": str(error)})
                continue
            if not snapshot.canonical_relationship_valid or snapshot.pr_number != bound["pr_number"]:
                results.append({"issue": issue, "status": "unresolved_canonical_pr"})
                continue
            if snapshot.pr_merged:
                try:
                    labels_cleared = self.core.workflow.clear_active_coordination_labels(repo_key, issue)
                except Exception:
                    labels_cleared = False
                self.core.store.complete_flow(repo_key, issue, current)
                self.core.store.set_lifecycle(repo_key, issue, "MERGED_SUCCESS",
                                              snapshot.pr_head_sha or "", current, 1,
                                              "merged" if labels_cleared else "merged_cleanup_pending")
                results.append({"issue": issue, "status": "merged_success", "head": snapshot.pr_head_sha,
                                "active_labels_cleared": labels_cleared})
                continue
            if snapshot.pr_state == "closed":
                results.append(self._terminal(repo_key, issue, snapshot, "needs-human", current,
                                              "pr_closed_without_merge"))
                continue
            if not snapshot.pr_open or not snapshot.pr_head_sha:
                results.append({"issue": issue, "status": "pr_unavailable"})
                continue
            head = snapshot.pr_head_sha.lower()
            if not snapshot.issue_open or not snapshot.frozen_spec or snapshot.terminal_labels or snapshot.canceled:
                self.core.reconcile_once(repo_key, owner_id, current)
                results.append({"issue": issue, "status": "cancelled_or_terminal"})
                continue
            if state and state["phase"] in {"WAITING_REVIEW", "WAITING_MERGE"} \
                    and state["head_sha"].casefold() != head:
                deadline = self._set_waiting(repo_key, issue, "WAITING_CI", head, current)
                results.append({"issue": issue, "status": "WAITING_CI", "head": head,
                                "deadline_at": deadline})
                continue
            check_state, evidence = self._checks(snapshot)
            if check_state == "implementation":
                if not self.core.store.flow(repo_key, issue).get("trusted"):
                    results.append(self._terminal(repo_key, issue, snapshot, "needs-human", current,
                                                  "untrusted_flow_requires_repair"))
                    continue
                if snapshot.coordination_state != "agent-working":
                    results.append(self._terminal(repo_key, issue, snapshot, "needs-human", current,
                                                  "ci_repair_state_not_active"))
                    continue
                candidate = RepairCandidate(repo_key, issue, snapshot.pr_number, head, snapshot.pr_branch,
                                            "implementation_failure", f"ci:{head}:" + ";".join(sorted(evidence)),
                                            owner_id)
                results.append({"issue": issue, "trigger": "ci_repair", **self.core.dispatch_repair(candidate)})
                continue
            if check_state == "infrastructure":
                results.append(self._terminal(repo_key, issue, snapshot, "infra-blocked", current,
                                              "explicit_infrastructure_failure"))
                continue
            if check_state == "ambiguous":
                results.append(self._terminal(repo_key, issue, snapshot, "needs-human", current,
                                              "ambiguous_ci_failure"))
                continue
            if check_state == "pending":
                phase = "WAITING_CI"
            elif snapshot.formal_review_state == "CHANGES_REQUESTED" \
                    and snapshot.formal_review_head_sha \
                    and snapshot.formal_review_head_sha.casefold() == head \
                    and snapshot.coordination_state == "changes-requested":
                if not self.core.store.flow(repo_key, issue).get("trusted"):
                    results.append(self._terminal(repo_key, issue, snapshot, "needs-human", current,
                                                  "untrusted_flow_requires_repair"))
                    continue
                candidate = RepairCandidate(repo_key, issue, snapshot.pr_number, head, snapshot.pr_branch,
                                            "reviewer_rejection", snapshot.formal_review_id or "", owner_id)
                results.append({"issue": issue, "trigger": "reviewer_repair", **self.core.dispatch_repair(candidate)})
                continue
            elif snapshot.formal_review_state == "APPROVED" \
                    and snapshot.formal_review_head_sha \
                    and snapshot.formal_review_head_sha.casefold() == head:
                phase = "WAITING_MERGE"
            else:
                phase = "WAITING_REVIEW"
            deadline = self._set_waiting(repo_key, issue, phase, head, current)
            if current >= deadline:
                label = _PHASES[phase][1]
                results.append(self._terminal(repo_key, issue, snapshot, label, current,
                                              f"{phase.lower()}_timeout"))
                continue
            results.append({"issue": issue, "status": phase, "head": head, "deadline_at": deadline})
        return {"status": "ok", "items": results}
