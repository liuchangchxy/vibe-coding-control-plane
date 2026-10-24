import asyncio
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from vccp_runtime.adapters import ensure_antigravity_project
from vccp_runtime.github_app import GitHubHTTPError
from vccp_runtime.reviewer_agents import (AntigravityReviewer, ClaudeAgentSDKReviewer,
                                          DEFAULT_REVIEWER_TIMEOUT_SECONDS, ReviewerTaskError,
                                          _decision, reviewer_timeout_seconds)


SHA = "a" * 40
REPO = "owner/repo"
MANIFEST = {"repository": {"implementer_authors": ["builder[bot]"], "base_branch": "main"}}
APPROVAL = {"decision": "APPROVE", "summary": "All criteria pass.", "findings": []}


class FakeAPI:
    def __init__(self, head=SHA): self.head, self.reviews = head, []
    def get(self, path):
        if path.endswith("/issues/7"):
            return {"number": 7, "body": "Frozen spec criterion: preserve the API."}
        if "/pulls/12/reviews?" in path:
            page = int(path.rsplit("page=", 1)[1])
            return self.reviews[(page - 1) * 100:page * 100]
        if "/pulls/12/files?" in path:
            return [{"filename": "code.py", "status": "modified", "additions": 1,
                     "deletions": 1, "patch": "-print('review me')\\n+print('changed')"}]
        if path.endswith("/pulls/12"):
            return {"number": 12, "title": "Change API", "state": "open", "draft": False,
                    "head": {"sha": self.head}, "base": {"ref": "main"},
                    "user": {"login": "builder[bot]"}}
        raise AssertionError(path)


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], text=True,
                          capture_output=True, check=True).stdout.strip()


class FakeSystemMessage:
    def __init__(self, session_id): self.data = {"session_id": session_id}


class FakeResultMessage:
    def __init__(self, data=APPROVAL, subtype="success", session_id="sdk-session"):
        self.structured_output = data
        self.subtype = subtype
        self.session_id = session_id


class ResultError(Exception):
    def __init__(self):
        super().__init__("Claude Code returned an error result")
        self.subtype = "success"
        self.errors = []
        self.result = ("API Error: overloaded (token=sk-ant-sensitive) "
                       "github_pat_11AA11A_" + "G" * 70 + " "
                       "ghp_" + "A" * 36 + " " + "ghs_" + "B" * 36 + " "
                       "ANTHROPIC_AUTH_TOKEN=secret_value "
                       '{"ANTHROPIC_AUTH_TOKEN":"secret_value",'
                       '"x-api-key":"another_secret",'
                       '"authorization":"Basic dXNlcjpwYXNz",'
                       '"set-cookie":"cookie_secret"}')
        self.api_error_status = 529
        self.terminal_reason = "api_error"
        self.session_id = "sdk-terminal-session"
        self.exit_code = 1


class MaxTurnsResultError(ResultError):
    def __init__(self, session_id="sdk-session"):
        super().__init__()
        self.subtype = "error_max_turns"
        self.errors = ["max_turns reached"]
        self.result = ""
        self.api_error_status = None
        self.terminal_reason = "max_turns"
        self.session_id = session_id
        self.data = {"subtype": self.subtype, "terminal_reason": self.terminal_reason,
                     "session_id": session_id, "num_turns": 31}


class FakePermissionAllow:
    pass


class FakePermissionDeny:
    def __init__(self, message=""): self.message = message


class FakeOptions:
    def __init__(self, **kwargs): self.__dict__.update(kwargs)


class ReviewerAgentTests(unittest.TestCase):
    def _sdk_module(self, *, data=APPROVAL, subtype="success", session_id="sdk-session",
                    error=None, hang=False):
        async def fake_query(*, prompt, options):
            self.options = options
            self.prompt = prompt
            yield FakeSystemMessage(session_id)
            if error:
                raise error
            if hang:
                await asyncio.Future()
            yield FakeResultMessage(data, subtype, session_id)
        return types.SimpleNamespace(
            ClaudeAgentOptions=FakeOptions, PermissionResultAllow=FakePermissionAllow,
            PermissionResultDeny=FakePermissionDeny, ResultMessage=FakeResultMessage,
            SystemMessage=FakeSystemMessage, ResultError=ResultError, query=fake_query,
        )

    def _repo(self, root):
        source = root / "source"
        source.mkdir()
        git(source, "init", "-b", "main")
        git(source, "config", "user.name", "Test")
        git(source, "config", "user.email", "test@example.com")
        (source / "code.py").write_text("print('review me')\\n")
        git(source, "add", "code.py")
        git(source, "commit", "-m", "base")
        bare = root / "origin.git"
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        git(source, "remote", "add", "origin", str(bare))
        (source / "code.py").write_text("print('changed')\\n")
        git(source, "commit", "-am", "change")
        head = git(source, "rev-parse", "HEAD")
        git(source, "push", "origin", "HEAD:refs/pull/12/head")
        return source, head

    def test_antigravity_reviewer_uses_shared_project_registration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            projects = root / "projects"
            provider = AntigravityReviewer("language_server", FakeAPI(), MANIFEST,
                                           str(workspace), str(root / "reviews"),
                                           project_config_root=projects)

            project_id = provider._project(workspace)

            self.assertEqual(project_id, ensure_antigravity_project(workspace, projects))
            self.assertTrue((projects / f"{project_id}.json").is_file())

    def test_sdk_review_returns_sdk_session_and_structured_decision_synchronously(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, head = self._repo(root)
            module = self._sdk_module()
            provider = ClaudeAgentSDKReviewer(None, FakeAPI(head), MANIFEST,
                                               str(source), str(root / "reviews"))
            environ = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:15721",
                       "ANTHROPIC_AUTH_TOKEN": "PROXY_MANAGED", "GH_TOKEN": "do-not-pass"}
            with patch.dict(os.environ, environ), \
                    patch.dict(sys.modules, {"claude_agent_sdk": module}):
                execution = provider.launch_review(REPO, 7, 12, head)
                workspace = provider.workspace.path_for(REPO, 12, head)
                options = provider._options(workspace)
                inside = asyncio.run(options.can_use_tool("Read", {"file_path": "code.py"}, None))
                outside = asyncio.run(options.can_use_tool("Read", {"file_path": "/etc/passwd"}, None))

            self.assertEqual(git(workspace, "rev-parse", "HEAD"), head)
            self.assertEqual(execution, {"status": "completed", "execution_id": "sdk-session",
                                         "event": "APPROVE", "body": "All criteria pass."})
            self.assertFalse(hasattr(options, "session_id"))
            self.assertEqual(options.tools, ["Read", "Glob", "Grep"])
            self.assertEqual(options.disallowed_tools, ["Bash", "Write", "Edit"])
            self.assertEqual(options.setting_sources, ["user"])
            self.assertEqual(options.max_turns, 30)
            self.assertEqual(options.env["ANTHROPIC_BASE_URL"], environ["ANTHROPIC_BASE_URL"])
            self.assertEqual(options.env["ANTHROPIC_AUTH_TOKEN"], environ["ANTHROPIC_AUTH_TOKEN"])
            self.assertNotIn("GH_TOKEN", options.env)
            self.assertEqual(options.cwd, str(workspace))
            self.assertEqual(options.output_format["type"], "json_schema")
            self.assertIsInstance(inside, FakePermissionAllow)
            self.assertIsInstance(outside, FakePermissionDeny)
            self.assertFalse(hasattr(provider, "_workers"))
            self.assertFalse(hasattr(provider, "_results"))

    def test_dirty_reviewer_workspace_is_rebuilt_at_the_exact_head(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, head = self._repo(root)
            provider = ClaudeAgentSDKReviewer(None, FakeAPI(head), MANIFEST,
                                               str(source), str(root / "reviews"))
            workspace = provider.workspace.prepare(REPO, 12, head)
            (workspace / "untracked.txt").write_text("disposable drift\n")
            repaired = provider.workspace.prepare(REPO, 12, head)
            self.assertEqual(repaired, workspace)
            self.assertEqual(git(repaired, "rev-parse", "HEAD"), head)
            self.assertEqual(git(repaired, "status", "--porcelain"), "")

    def test_stale_reviewer_workspace_is_rebuilt_at_the_exact_head(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, head = self._repo(root)
            provider = ClaudeAgentSDKReviewer(None, FakeAPI(head), MANIFEST,
                                               str(source), str(root / "reviews"))
            workspace = provider.workspace.prepare(REPO, 12, head)
            base = git(source, "rev-parse", "HEAD~1")
            git(workspace, "checkout", base)
            repaired = provider.workspace.prepare(REPO, 12, head)
            self.assertEqual(repaired, workspace)
            self.assertEqual(git(repaired, "rev-parse", "HEAD"), head)
            self.assertEqual(git(repaired, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")

    def test_reviewer_resumes_same_session_after_per_call_turn_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, head = self._repo(root)
            calls, checkpoints = [], []
            async def segmented_query(*, prompt, options):
                calls.append((prompt, options))
                yield FakeSystemMessage("sdk-session")
                if options.resume is None:
                    raise MaxTurnsResultError()
                yield FakeResultMessage()
            module = self._sdk_module()
            module.query = segmented_query
            provider = ClaudeAgentSDKReviewer(None, FakeAPI(head), MANIFEST,
                                               str(source), str(root / "reviews"))

            with patch.dict(sys.modules, {"claude_agent_sdk": module}):
                result = provider.launch_review(REPO, 7, 12, head, on_checkpoint=checkpoints.append)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["execution_id"], "sdk-session")
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0][1].resume)
        self.assertEqual(calls[0][1].max_turns, 30)
        self.assertEqual(calls[1][1].resume, "sdk-session")
        self.assertEqual(calls[1][1].max_turns, 30)
        self.assertTrue(any(item.get("phase") == "resume_ready" for item in checkpoints))
        self.assertEqual(next(item["total_turns"] for item in checkpoints
                              if item.get("phase") == "resume_ready"), 31)
        self.assertEqual(checkpoints[-1]["phase"], "completed")

    def test_reviewer_total_turn_budget_stops_after_two_resumes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, head = self._repo(root)
            calls = []
            async def segmented_query(*, prompt, options):
                calls.append(options)
                yield FakeSystemMessage("sdk-session")
                raise MaxTurnsResultError()
            module = self._sdk_module()
            module.query = segmented_query
            provider = ClaudeAgentSDKReviewer(None, FakeAPI(head), MANIFEST,
                                               str(source), str(root / "reviews"))

            with patch.dict(sys.modules, {"claude_agent_sdk": module}), \
                    self.assertRaises(ReviewerTaskError) as raised:
                provider.launch_review(REPO, 7, 12, head)

        self.assertEqual(len(calls), 3)
        self.assertEqual([options.resume for options in calls], [None, "sdk-session", "sdk-session"])
        self.assertEqual(raised.exception.exception_type, "ReviewerResumeBudgetExceeded")
        self.assertTrue(raised.exception.terminal)

    def test_reviewer_cumulative_time_budget_applies_across_segments(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, head = self._repo(root)
            calls = []
            async def segmented_query(*, prompt, options):
                calls.append(options)
                yield FakeSystemMessage("sdk-session")
                raise MaxTurnsResultError()
            module = self._sdk_module()
            module.query = segmented_query
            provider = ClaudeAgentSDKReviewer(None, FakeAPI(head), MANIFEST,
                                               str(source), str(root / "reviews"))
            provider.timeout_seconds = 20

            with patch.dict(sys.modules, {"claude_agent_sdk": module}), \
                    patch("vccp_runtime.reviewer_agents.time.time", side_effect=[100.0, 121.0]), \
                    self.assertRaises(ReviewerTaskError) as raised:
                provider.launch_review(REPO, 7, 12, head, started_at=100.0)

        self.assertEqual(len(calls), 1)
        self.assertEqual(raised.exception.stage, "claude_agent_sdk_timeout")

    def test_failed_session_resume_is_not_retried_again(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, head = self._repo(root)
            calls = []
            async def segmented_query(*, prompt, options):
                calls.append(options)
                yield FakeSystemMessage("sdk-session")
                if options.resume is None:
                    raise MaxTurnsResultError()
                raise ResultError()
            module = self._sdk_module()
            module.query = segmented_query
            provider = ClaudeAgentSDKReviewer(None, FakeAPI(head), MANIFEST,
                                               str(source), str(root / "reviews"))

            with patch.dict(sys.modules, {"claude_agent_sdk": module}), \
                    self.assertRaises(ReviewerTaskError) as raised:
                provider.launch_review(REPO, 7, 12, head)

        self.assertEqual(len(calls), 2)
        self.assertEqual(raised.exception.exception_type, "ResultError")
        self.assertTrue(raised.exception.terminal)

    def test_formal_review_appearing_at_turn_boundary_stops_session_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, head = self._repo(root)
            api = FakeAPI(head)
            calls = []
            async def segmented_query(*, prompt, options):
                calls.append(options)
                yield FakeSystemMessage("sdk-session")
                api.reviews = [{"state": "APPROVED", "commit_id": head}]
                raise MaxTurnsResultError()
            module = self._sdk_module()
            module.query = segmented_query
            provider = ClaudeAgentSDKReviewer(None, api, MANIFEST,
                                               str(source), str(root / "reviews"))

            with patch.dict(sys.modules, {"claude_agent_sdk": module}), \
                    self.assertRaises(ReviewerTaskError) as raised:
                provider.launch_review(REPO, 7, 12, head)

        self.assertEqual(len(calls), 1)
        self.assertEqual(raised.exception.stage, "claude_agent_sdk_resume_precondition")

    def test_reviewer_does_not_resume_unknown_sdk_error_or_changed_head(self):
        for scenario in ("unknown_error", "changed_head"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source, head = self._repo(root)
                api = FakeAPI(head)
                calls = []
                async def segmented_query(*, prompt, options):
                    calls.append(options)
                    yield FakeSystemMessage("sdk-session")
                    if scenario == "unknown_error":
                        raise RuntimeError("transport failed")
                    api.head = "b" * 40
                    raise MaxTurnsResultError()
                module = self._sdk_module()
                module.query = segmented_query
                provider = ClaudeAgentSDKReviewer(None, api, MANIFEST,
                                                   str(source), str(root / "reviews"))
                with patch.dict(sys.modules, {"claude_agent_sdk": module}), \
                        self.assertRaises(ReviewerTaskError):
                    provider.launch_review(REPO, 7, 12, head)
                self.assertEqual(len(calls), 1)

    def test_structured_changes_requested_requires_blocking_finding(self):
        result = {"decision": "CHANGES_REQUESTED", "summary": "Fix this.", "findings": [
            {"severity": "blocking", "file": "code.py", "line": 2, "description": "Regression."}]}
        self.assertEqual(ClaudeAgentSDKReviewer._to_review(result), {
            "event": "REQUEST_CHANGES", "body": "Fix this.\n\nFindings:\n- [blocking] `code.py:2` — Regression."})
        self.assertIsNone(ClaudeAgentSDKReviewer._to_review({**result, "findings": []}))
        self.assertIsNone(ClaudeAgentSDKReviewer._to_review({**APPROVAL, "findings": [result["findings"][0]]}))

    def test_structured_siblings_are_recorded_but_never_block_the_pr(self):
        self.assertIn("siblings", ClaudeAgentSDKReviewer._SCHEMA["required"])
        result = {**APPROVAL, "siblings": [
            {"location": "code.py:9",
             "description": "The confirmed parsing pattern also drops empty rows here."}]}

        review = ClaudeAgentSDKReviewer._to_review(result)

        self.assertEqual(review["event"], "APPROVE")
        self.assertIn("Independent siblings (not blocking this PR and not part of this repair):",
                      review["body"])
        self.assertIn("`code.py:9` — The confirmed parsing pattern also drops empty rows here.",
                      review["body"])
        self.assertFalse(ClaudeAgentSDKReviewer._to_review(
            {**result, "siblings": [{"location": "", "description": "x"}]}))
        self.assertFalse(ClaudeAgentSDKReviewer._to_review(
            {**result, "siblings": [{"location": "code.py:9"}]}))

    def test_insufficient_evidence_maps_to_blocked_and_never_to_rejection(self):
        result = {"decision": "INSUFFICIENT_EVIDENCE",
                  "summary": "Criterion 2 cannot be decided: no browser evidence was supplied.",
                  "findings": [], "remediation": "human"}

        self.assertEqual(ClaudeAgentSDKReviewer._to_review(result), {
            "event": "BLOCKED",
            "body": "Criterion 2 cannot be decided: no browser evidence was supplied.",
            "remediation": "human"})
        self.assertFalse(ClaudeAgentSDKReviewer._to_review(
            {**result, "findings": [{"severity": "blocking", "file": "code.py",
                                     "line": 1, "description": "Regression."}]}))
        # A gap that arrives without a usable class is normalized to `human` rather than
        # failing the whole Reviewer execution: the stop must stay explainable.
        self.assertEqual(ClaudeAgentSDKReviewer._to_review(
            {key: value for key, value in result.items() if key != "remediation"}), {
            "event": "BLOCKED",
            "body": "Criterion 2 cannot be decided: no browser evidence was supplied.",
            "remediation": "human"})
        self.assertEqual(ClaudeAgentSDKReviewer._to_review({**result, "remediation": "nobody"}), {
            "event": "BLOCKED",
            "body": "Criterion 2 cannot be decided: no browser evidence was supplied.",
            "remediation": "human"})
        agent = ClaudeAgentSDKReviewer._to_review({**result, "remediation": "agent"})
        self.assertEqual(agent["remediation"], "agent")

    def test_malformed_structured_output_fails_with_session_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, head = self._repo(root)
            module = self._sdk_module(data={"decision": "APPROVE", "summary": " ", "findings": []})
            provider = ClaudeAgentSDKReviewer(None, FakeAPI(head), MANIFEST,
                                               str(source), str(root / "reviews"))
            with patch.dict(sys.modules, {"claude_agent_sdk": module}):
                with self.assertRaises(Exception) as raised:
                    provider.launch_review(REPO, 7, 12, head)
            self.assertEqual(raised.exception.stage, "claude_agent_sdk_result")
            self.assertEqual(raised.exception.execution_id, "sdk-session")
            self.assertTrue(raised.exception.terminal)

    def test_sdk_exception_with_session_but_no_terminal_result_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, head = self._repo(root)
            module = self._sdk_module(error=RuntimeError("transport ended"))
            provider = ClaudeAgentSDKReviewer(None, FakeAPI(head), MANIFEST,
                                               str(source), str(root / "reviews"))
            with patch.dict(sys.modules, {"claude_agent_sdk": module}):
                with self.assertRaises(Exception) as raised:
                    provider.launch_review(REPO, 7, 12, head)
            self.assertEqual(raised.exception.execution_id, "sdk-session")
            self.assertFalse(raised.exception.terminal)

    def test_retry_session_evidence_recognizes_persisted_sdk_result_error_as_terminal(self):
        provider = ClaudeAgentSDKReviewer(None, FakeAPI(), MANIFEST, ".", ".")
        outcome = ("sdk_execution_state_unknown:stage=claude_agent_sdk_execution "
                   "endpoint=none http_status=none exception_type=ResultError process_status=none")

        evidence = provider.retry_session_evidence(
            REPO, 7, 12, SHA, failed_execution_id="sdk-session", failed_outcome=outcome)

        self.assertEqual(evidence, {"status": "terminal_failed", "execution_id": "sdk-session",
                                    "state": "failed"})

    def test_retry_session_evidence_recognizes_persisted_vccp_timeout_as_terminal(self):
        provider = ClaudeAgentSDKReviewer(None, FakeAPI(), MANIFEST, ".", ".")
        outcome = ("sdk_execution_state_unknown:stage=claude_agent_sdk_timeout "
                   "endpoint=none http_status=none exception_type=TimeoutError")

        evidence = provider.retry_session_evidence(
            REPO, 7, 12, SHA, failed_execution_id="sdk-session", failed_outcome=outcome)

        self.assertEqual(evidence, {"status": "terminal_failed", "execution_id": "sdk-session",
                                    "state": "failed"})

    def test_retry_session_evidence_keeps_timeouts_from_other_stages_blocked(self):
        provider = ClaudeAgentSDKReviewer(None, FakeAPI(), MANIFEST, ".", ".")
        for outcome in (
                "sdk_execution_state_unknown:stage=claude_agent_sdk_execution "
                "endpoint=none http_status=none exception_type=TimeoutError",
                "sdk_execution_state_unknown:stage=claude_agent_sdk_timeout "
                "endpoint=none http_status=none exception_type=ProcessError",
                "sdk_execution_state_unknown:stage=runner_restart "
                "endpoint=none http_status=none exception_type=ExecutionInterrupted"):
            evidence = provider.retry_session_evidence(
                REPO, 7, 12, SHA, failed_execution_id="sdk-session", failed_outcome=outcome)
            self.assertEqual(evidence["status"], "unobservable", outcome)

    def test_retry_session_evidence_keeps_other_unknown_sdk_errors_blocked(self):
        provider = ClaudeAgentSDKReviewer(None, FakeAPI(), MANIFEST, ".", ".")
        outcome = ("sdk_execution_state_unknown:stage=claude_agent_sdk_execution "
                   "endpoint=none http_status=none exception_type=RuntimeError process_status=none")

        evidence = provider.retry_session_evidence(
            REPO, 7, 12, SHA, failed_execution_id="sdk-session", failed_outcome=outcome)

        self.assertEqual(evidence["status"], "unobservable")

    def test_sdk_result_error_is_terminal_and_preserves_sanitized_cause(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, head = self._repo(root)
            provider = ClaudeAgentSDKReviewer(None, FakeAPI(head), MANIFEST,
                                               str(source), str(root / "reviews"))
            module = self._sdk_module(error=ResultError())
            with patch.dict(sys.modules, {"claude_agent_sdk": module}):
                with self.assertRaises(Exception) as raised:
                    provider.launch_review(REPO, 7, 12, head)

        self.assertEqual(raised.exception.stage, "claude_agent_sdk_execution")
        self.assertEqual(raised.exception.exception_type, "ResultError")
        self.assertEqual(raised.exception.execution_id, "sdk-terminal-session")
        self.assertTrue(raised.exception.terminal)
        self.assertTrue(raised.exception.execution_started)
        self.assertEqual(raised.exception.http_status, 529)
        self.assertEqual(raised.exception.process_status, 1)
        self.assertIn("terminal_reason=api_error", raised.exception.sdk_detail)
        self.assertIn("API Error: overloaded", raised.exception.sdk_detail)
        self.assertNotIn("sk-ant-sensitive", raised.exception.sdk_detail)
        self.assertNotIn("github_pat_11AA11A_" + "G" * 70, raised.exception.sdk_detail)
        self.assertNotIn("ghp_" + "A" * 36, raised.exception.sdk_detail)
        self.assertNotIn("ghs_" + "B" * 36, raised.exception.sdk_detail)
        self.assertNotIn("secret_value", raised.exception.sdk_detail)
        self.assertNotIn("another_secret", raised.exception.sdk_detail)
        self.assertNotIn("dXNlcjpwYXNz", raised.exception.sdk_detail)
        self.assertNotIn("cookie_secret", raised.exception.sdk_detail)

    def test_reviewer_timeout_default_is_finite_and_replaces_the_hardcoded_value(self):
        provider = ClaudeAgentSDKReviewer(None, FakeAPI(), MANIFEST, ".", ".")

        self.assertEqual(provider.timeout_seconds, DEFAULT_REVIEWER_TIMEOUT_SECONDS)
        self.assertEqual(DEFAULT_REVIEWER_TIMEOUT_SECONDS, 3600)
        self.assertNotEqual(DEFAULT_REVIEWER_TIMEOUT_SECONDS, 1200)

    def test_reviewer_timeout_reads_the_existing_provider_settings(self):
        self.assertEqual(reviewer_timeout_seconds({}), DEFAULT_REVIEWER_TIMEOUT_SECONDS)
        self.assertEqual(reviewer_timeout_seconds(None), DEFAULT_REVIEWER_TIMEOUT_SECONDS)
        self.assertEqual(reviewer_timeout_seconds({"launch_timeout_seconds": 60}),
                         DEFAULT_REVIEWER_TIMEOUT_SECONDS)
        self.assertEqual(reviewer_timeout_seconds({"reviewer_timeout_seconds": 900}), 900)
        for invalid in (0, -1, True, "600", float("inf"), float("nan"), 86401):
            with self.assertRaises(ValueError):
                reviewer_timeout_seconds({"reviewer_timeout_seconds": invalid})

    def test_reviewer_timeout_argument_is_bounded_like_a_configured_value(self):
        self.assertEqual(
            ClaudeAgentSDKReviewer(None, FakeAPI(), MANIFEST, ".", ".", timeout_seconds=90).timeout_seconds, 90)
        with self.assertRaises(ValueError):
            ClaudeAgentSDKReviewer(None, FakeAPI(), MANIFEST, ".", ".", timeout_seconds=0)

    def test_vccp_deadline_tears_down_the_session_before_reporting_timeout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, head = self._repo(root)
            cleanup = []

            async def hanging_query(*, prompt, options):
                yield FakeSystemMessage("sdk-session")
                try:
                    await asyncio.Future()
                finally:
                    import anyio
                    with anyio.CancelScope(shield=True):
                        await anyio.sleep(0.05)
                        cleanup.append("torn_down")

            module = self._sdk_module()
            module.query = hanging_query
            provider = ClaudeAgentSDKReviewer(None, FakeAPI(head), MANIFEST,
                                               str(source), str(root / "reviews"))
            provider.timeout_seconds = 0.05

            with patch.dict(sys.modules, {"claude_agent_sdk": module}), \
                    self.assertRaises(ReviewerTaskError) as raised:
                provider.launch_review(REPO, 7, 12, head)

        self.assertEqual(cleanup, ["torn_down"])
        self.assertEqual(raised.exception.stage, "claude_agent_sdk_timeout")
        self.assertEqual(raised.exception.exception_type, "TimeoutError")
        self.assertEqual(raised.exception.execution_id, "sdk-session")
        self.assertTrue(raised.exception.execution_started)
        self.assertTrue(raised.exception.terminal)

    def test_reviewer_task_rejects_changed_head_before_sdk_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, _ = self._repo(root)
            provider = ClaudeAgentSDKReviewer(None, FakeAPI("b" * 40), MANIFEST,
                                               str(source), str(root / "reviews"))
            with self.assertRaises(ValueError):
                provider.task.build(REPO, 7, 12, SHA, structured=True)

    def test_reviewer_task_failure_reports_github_stage_and_status(self):
        class FailingAPI:
            def get(self, _path): raise GitHubHTTPError(503, "GET")
        with tempfile.TemporaryDirectory() as temp:
            provider = ClaudeAgentSDKReviewer(None, FailingAPI(), MANIFEST, temp,
                                               str(Path(temp) / "reviews"))
            with self.assertRaises(Exception) as raised:
                provider.task.build(REPO, 7, 12, SHA)
        self.assertEqual(raised.exception.stage, "issue")
        self.assertEqual(raised.exception.http_status, 503)
        self.assertNotIn("token", str(raised.exception).casefold())

    def test_legacy_decision_parser_is_preserved_for_antigravity(self):
        self.assertEqual(_decision('Earlier {"event":"BLOCKED"}\\nFinal: {"event":"APPROVE","body":"All criteria pass."}'),
                         {"event": "APPROVE", "body": "All criteria pass."})
        self.assertIsNone(_decision('{"event":"APPROVE","body":"  "}'))


if __name__ == "__main__":
    unittest.main()
