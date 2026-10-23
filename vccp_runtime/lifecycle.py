"""One-shot exact-head CI, formal Review, and native merge observation."""
from __future__ import annotations

import hashlib
import time
import re

from .core import RepairCandidate, _ci_cause_is_current


_PHASES = {
    "WAITING_CI": ("waiting_ci", "infra-blocked"),
    "WAITING_REVIEW": ("waiting_review", "needs-human"),
    "WAITING_MERGE": ("waiting_merge", "infra-blocked"),
}


class LifecycleDriver:
    def __init__(self, core, manifest: dict, timeouts: dict | None = None,
                 local_reviewer=None):
        self.core = core
        self.manifest = manifest
        self.local_reviewer = local_reviewer
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
        if transitioned or self.core._terminal_label_observed(repo, issue, label):
            self._summarize_terminal_stop(repo, issue, snapshot, label, now, outcome)
        return {"issue": issue, "status": label, "transitioned": transitioned, "outcome": outcome}

    def _summarize_terminal_stop(self, repo, issue, snapshot, label, now, outcome):
        """Leave one short GitHub comment explaining a newly reached terminal stop.

        Only the pass that actually moved the Issue into ``label`` writes, so the comment
        describes a state GitHub already shows. Deduplication and the retry-once rule live
        in the durable notice ledger inside ``RuntimeCore.publish_terminal_stop``.
        """
        reason, _, summary = str(outcome or "").partition(": ")
        self.core.publish_terminal_stop(repo, issue, label, reason.strip(), summary.strip(),
                                        snapshot.pr_number, snapshot.pr_head_sha, now)

    def _evidence_gap_route(self, repo, issue, snapshot, head, summary, remediation,
                            owner_id, now):
        """Send an agent-fixable evidence gap back to the same PR, else stop for a human.

        The Implementer already owns the PR and branch, so this reuses the ordinary repair
        admission (same PR, same head, existing budget) instead of inventing a second
        dispatch path. A gap is not a code defect, so no ``CHANGES_REQUESTED`` is written.
        """
        if remediation == "agent" and snapshot.pr_branch:
            flow = self.core.store.flow(repo, issue)
            if (flow and flow.get("trusted") and flow.get("repair_budget_known")
                    and flow.get("pr_number") in (None, snapshot.pr_number)
                    and snapshot.coordination_state == "agent-working"):
                digest = hashlib.sha256(f"{head}\n{summary}".encode("utf-8")).hexdigest()[:16]
                candidate = RepairCandidate(
                    repo, issue, snapshot.pr_number, head, snapshot.pr_branch,
                    "evidence_remediation", f"reviewer-evidence:{digest}", owner_id, summary or None)
                dispatch = self.core.dispatch_repair(candidate)
                # The same exact gap was already routed once and its remediation already
                # finished, yet the Reviewer still cannot decide. Re-dispatching would loop
                # forever, so the stop becomes visible instead.
                duplicate = (self.core.store.receipt(dispatch["attempt_id"])
                             if dispatch.get("status") == "duplicate" and dispatch.get("attempt_id")
                             else None)
                if duplicate is not None and duplicate.get("source") == "evidence_remediation":
                    return self._terminal(repo, issue, snapshot, "needs-human", now,
                                          "evidence_remediation_did_not_close_gap"
                                          + (f": {summary}" if summary else ""))
                # Any admitted attempt counts as routed, including a duplicate replay and
                # a launch whose outcome the next reconcile pass must resolve.
                if (dispatch.get("needs_human") or dispatch.get("attempt_id")
                        or dispatch.get("status") == "lease_held"):
                    return {"trigger": "evidence_remediation", **dispatch}
        return self._terminal(repo, issue, snapshot, "needs-human", now,
                              "reviewer_insufficient_evidence"
                              + (f": {summary}" if summary else ""))

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

    @staticmethod
    def _ci_grant_is_current(grant, snapshot):
        candidate = RepairCandidate(grant["repo"], grant["issue_number"], grant["pr_number"],
                                    grant["head_sha"], snapshot.pr_branch or "",
                                    grant["cause_type"], grant["cause_id"], "lifecycle")
        return _ci_cause_is_current(candidate, snapshot)

    def _merged_bound_head(self, repo, issue, snapshot, bound):
        """Return ``(merged_head, recoverable_row)`` for a bound canonical PR, else ``(None, None)``.

        The Issue timeline stops reporting the closing relationship the moment GitHub
        auto-closes the Issue, which lands within seconds of the merge, so a merged PR
        that used to be canonical disappears from the snapshot entirely. A closed Issue
        therefore re-reads the PR a durable attempt already bound.

        That re-read is only evidence of *which* PR merged, never of whether it was
        authorized to: the head it reports must be the exact head this Issue's durable
        ``WAITING_MERGE`` already holds, which is only ever written after the required
        checks and a formal APPROVE were both observed at that head. A missing
        ``WAITING_MERGE`` or any other head fails closed.

        ``recoverable_row`` names the single fenced attempt this proof belongs to, so only
        the attempt whose own bound PR/branch the proof established can be lifted out of a
        cancellation fence. A snapshot that still shows the canonical PR merged is not an
        exact-head fence-lifting proof, so it never reports a row.
        """
        if snapshot.pr_merged and snapshot.pr_number in {row["pr_number"] for row in bound}:
            return snapshot.pr_head_sha, None
        if snapshot.issue_open:
            return None, None
        state = self.core.store.lifecycle_state(repo, issue)
        if not state or state["phase"] != "WAITING_MERGE" or not state["head_sha"]:
            return None, None
        authorized = state["head_sha"].casefold()
        for row in sorted(bound, key=lambda item: item["pr_number"]):
            merged = self.core.workflow.merged_closing_pr(repo, issue, row["pr_number"])
            if merged and merged.get("headRefName") == row["branch"] \
                    and merged["headRefOid"].casefold() == authorized:
                recovered = row if row["phase"] == "STOPPED_CANCELLED" else None
                return merged["headRefOid"], recovered
        return None, None

    def _merge_observation_after_fence(self, repo, row):
        """Whether a fenced attempt may still be offered to merge observation.

        ``reconcile_once`` can fence a bound attempt to ``STOPPED_CANCELLED`` when its own
        observation of an Issue auto-close arrives before the lifecycle pass observed the
        merge that caused it. Re-offering every cancelled attempt would resurrect ordinary
        cancellations, so re-entry requires both halves of the proof already on record: a
        bound PR/branch, and a durable ``WAITING_MERGE`` carrying the exact head the required
        checks and the formal APPROVE were observed at. Whether that PR really merged is
        still decided only by ``_merged_bound_head``.
        """
        if row["phase"] != "STOPPED_CANCELLED" or not row["pr_number"] or not row["branch"]:
            return False
        state = self.core.store.lifecycle_state(repo, row["issue_number"])
        return bool(state and state["phase"] == "WAITING_MERGE" and state["head_sha"])

    @staticmethod
    def _reviewer_approved_exact_head(result, head):
        """Whether the Reviewer's own result proves a formal APPROVE at exactly ``head``."""
        if not isinstance(result, dict) or not isinstance(head, str) or not head:
            return False
        if str(result.get("head_sha") or "").casefold() != head.casefold():
            return False
        if result.get("status") == "formal_review_exists":
            return result.get("state") == "APPROVED"
        if result.get("status") != "review_decision_submitted" or result.get("event") != "APPROVE":
            return False
        if result.get("decision_status") == "review_submitted":
            return True
        return (result.get("decision_status") in {"already_reviewed", "formal_review_exists"}
                and result.get("state") == "APPROVED")

    def advance_once(self, repo: str, owner_id: str, now: float | None = None):
        """Observe and advance only locally bound work; never schedules agent-ready Issues."""
        if not isinstance(owner_id, str) or not owner_id.strip():
            return {"status": "invalid_owner", "items": []}
        current = time.time() if now is None else float(now)
        repo_key = repo.casefold()
        attempts = self.core.store.attempts_for_repo(repo_key)
        attempts = [row for row in attempts if not (row["phase"] == "LAUNCH_CONFIRMED"
                    and self.core.store.receipt(row["attempt_id"]))]
        issues = sorted({row["issue_number"] for row in attempts
                         if row["pr_number"] and (row["phase"] in {
                             "PR_BOUND", "LAUNCH_CONFIRMED", "MERGED_SUCCESS"}
                             or self._merge_observation_after_fence(repo_key, row))})
        issues = sorted(set(issues) | {row["issue_number"]
                                      for row in self.core.store.active_continuations(repo_key)})
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
            bound = [row for row in attempts if row["issue_number"] == issue
                     and row["pr_number"] is not None]
            merged_head, recoverable = self._merged_bound_head(repo_key, issue, snapshot, bound)
            if merged_head is not None:
                try:
                    labels_cleared = self.core.workflow.clear_active_coordination_labels(repo_key, issue)
                except Exception:
                    labels_cleared = False
                self.core.store.complete_flow(repo_key, issue, current,
                                              recovered_attempt_ids=(
                                                  [recoverable["attempt_id"]] if recoverable else []))
                self.core.store.set_lifecycle(repo_key, issue, "MERGED_SUCCESS",
                                              merged_head, current, 1,
                                              "merged" if labels_cleared else "merged_cleanup_pending")
                merged_results.append({"issue": issue, "status": "merged_success",
                                       "head": merged_head,
                                       "active_labels_cleared": labels_cleared})

        # Reuse D1 only when durable dispatch attempts need recovery/binding. Calling it
        # for a first exact-head Reviewer rejection would incorrectly classify the
        # intentionally not-yet-created repair ledger as orphaned history.
        if any(row["phase"] in {"CLAIM_INTENT", "CLAIMED", "LAUNCH_INTENT", "LAUNCH_UNKNOWN",
                                "LAUNCH_CONFIRMED"} for row in attempts):
            self.core.reconcile_once(repo_key, owner_id, current)
        attempts = self.core.store.attempts_for_repo(repo_key)
        issues = sorted({row["issue_number"] for row in attempts if row["phase"] == "PR_BOUND"})
        issues = sorted(set(issues) | {row["issue_number"]
                                      for row in self.core.store.active_continuations(repo_key)})
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
            if (snapshot.canonical_relationship_valid and snapshot.canonical_link_count == 1
                    and snapshot.open_linked_pr_count == 1 and snapshot.pr_open
                    and snapshot.pr_linked_issue == issue and snapshot.pr_branch and snapshot.pr_head_sha):
                flow = self.core.store.flow(repo_key, issue)
                if flow and not (flow.get("trusted") and flow.get("initial_confirmed")):
                    self.core.store.normalize_legacy_flow(repo_key, issue, snapshot.pr_number,
                        snapshot.pr_branch, snapshot.pr_head_sha.lower())
            # A continuation is a durable, exact-head rediscovery token for a
            # terminalized flow. Re-enter only the original PR and formal cause.
            pending = [row for row in self.core.store.active_continuations(repo_key)
                       if row["issue_number"] == issue and row["pr_number"] == snapshot.pr_number
                       and row["head_sha"] == (snapshot.pr_head_sha or "").lower()]
            if pending:
                grant = pending[-1]
                cause_current = (
                    grant["cause_type"] == "reviewer_rejection"
                    and snapshot.formal_review_state == "CHANGES_REQUESTED"
                    and snapshot.formal_review_id == grant["cause_id"]
                    and snapshot.formal_review_head_sha == snapshot.pr_head_sha
                    and snapshot.coordination_state == "changes-requested"
                ) or (
                    grant["cause_type"] == "implementation_failure"
                    and snapshot.coordination_state == "agent-working"
                    and self._ci_grant_is_current(grant, snapshot)
                )
                flow = self.core.store.flow(repo_key, issue)
                if (cause_current and snapshot.canonical_relationship_valid
                        and snapshot.canonical_link_count == 1 and snapshot.open_linked_pr_count == 1
                        and snapshot.pr_open and snapshot.pr_linked_issue == issue
                        and snapshot.pr_branch and flow and flow.get("trusted")
                        and flow.get("initial_confirmed") and flow.get("repair_budget_known")
                        and flow.get("pr_number") == snapshot.pr_number
                        and flow.get("branch") == snapshot.pr_branch):
                    candidate = RepairCandidate(repo_key, issue, snapshot.pr_number,
                                                snapshot.pr_head_sha.lower(), snapshot.pr_branch,
                                                grant["cause_type"], grant["cause_id"], owner_id)
                    results.append({"issue": issue, "trigger": "authorized_repair_continuation",
                                    **self.core.dispatch_repair(candidate)})
                    continue
                # A grant is only an admission token. If its cause went stale,
                # let the ordinary exact-head CI/review/merge lifecycle proceed.
            if state and state["phase"] in {"WAITING_REVIEW", "WAITING_MERGE"} \
                    and state["head_sha"].casefold() != head:
                deadline = self._set_waiting(repo_key, issue, "WAITING_CI", head, current)
                results.append({"issue": issue, "status": "WAITING_CI", "head": head,
                                "deadline_at": deadline})
                continue
            check_state, evidence = self._checks(snapshot)
            if check_state == "implementation":
                flow = self.core.store.flow(repo_key, issue)
                if flow and not flow.get("repair_budget_known"):
                    results.append(self._terminal(repo_key, issue, snapshot, "needs-human", current,
                                                  "repair_history_or_budget_provenance_unknown"))
                    continue
                if not flow or not flow.get("trusted"):
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
                prior = self.core.store.lifecycle_state(repo_key, issue)
                match = re.fullmatch(r"infrastructure-retry:(\d+)",
                    (prior or {}).get("outcome") or "")
                retries = int(match.group(1)) if match else 0
                if retries >= 2:
                    results.append(self._terminal(repo_key, issue, snapshot, "infra-blocked", current,
                                                  "explicit_infrastructure_failure"))
                else:
                    deadline = self._set_waiting(repo_key, issue, "WAITING_CI", head, current)
                    self.core.store.set_lifecycle(repo_key, issue, "WAITING_CI", head, current,
                                                  self.timeouts["waiting_ci"],
                                                  outcome=f"infrastructure-retry:{retries + 1}")
                    results.append({"issue": issue, "status": "WAITING_CI", "head": head,
                                    "deadline_at": deadline, "infrastructure_retry": retries + 1})
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
                flow = self.core.store.flow(repo_key, issue)
                if flow and not flow.get("repair_budget_known"):
                    results.append(self._terminal(repo_key, issue, snapshot, "needs-human", current,
                                                  "repair_history_or_budget_provenance_unknown"))
                    continue
                if not flow or not flow.get("trusted"):
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
            result = {"issue": issue, "status": phase, "head": head, "deadline_at": deadline}
            if phase == "WAITING_REVIEW" and self.local_reviewer is not None:
                result["reviewer"] = self.local_reviewer.advance(
                    repo_key, issue, snapshot.pr_number, head)
                if result["reviewer"].get("status") == "reviewer_insufficient_evidence":
                    # The Reviewer could not decide an acceptance criterion. Who can close
                    # the gap decides the route: the same PR Implementer stays inside the
                    # Frozen Spec, while anything only a person can supply stops for a human.
                    summary = " ".join(str(result["reviewer"].get("summary") or "").split())[:2000]
                    route = self._evidence_gap_route(repo_key, issue, snapshot, head, summary,
                                                     result["reviewer"].get("remediation"), owner_id,
                                                     current)
                    results.append({"issue": issue, **route})
                    continue
                if self._reviewer_approved_exact_head(result["reviewer"], head):
                    # The Reviewer submitted APPROVE into this very pass, so GitHub can
                    # satisfy native Auto-merge and auto-close the Issue before the next
                    # observation. Record the exact-head authorization now: it is the only
                    # durable proof merge observation and fence recovery can rely on.
                    deadline = self._set_waiting(repo_key, issue, "WAITING_MERGE", head, current)
                    result.update(status="WAITING_MERGE", deadline_at=deadline)
            results.append(result)
        return {"status": "ok", "items": results}
