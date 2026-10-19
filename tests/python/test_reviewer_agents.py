import asyncio
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from vccp_runtime.github_app import GitHubHTTPError
from vccp_runtime.reviewer_agents import ClaudeAgentSDKReviewer, _decision


SHA = "a" * 40
REPO = "owner/repo"
MANIFEST = {"repository": {"implementer_authors": ["builder[bot]"], "base_branch": "main"}}
APPROVAL = {"decision": "APPROVE", "summary": "All criteria pass.", "findings": []}


class FakeAPI:
    def __init__(self, head=SHA): self.head = head
    def get(self, path):
        if path.endswith("/issues/7"):
            return {"number": 7, "body": "Frozen spec criterion: preserve the API."}
        if "/pulls/12/reviews?" in path: return []
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
                       "ANTHROPIC_AUTH_TOKEN=secret_value "
                       '{"ANTHROPIC_AUTH_TOKEN":"secret_value",'
                       '"x-api-key":"another_secret",'
                       '"authorization":"Basic dXNlcjpwYXNz",'
                       '"set-cookie":"cookie_secret"}')
        self.api_error_status = 529
        self.terminal_reason = "api_error"
        self.session_id = "sdk-terminal-session"
        self.exit_code = 1


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
            self.assertEqual(options.max_turns, 45)
            self.assertEqual(options.env["ANTHROPIC_BASE_URL"], environ["ANTHROPIC_BASE_URL"])
            self.assertEqual(options.env["ANTHROPIC_AUTH_TOKEN"], environ["ANTHROPIC_AUTH_TOKEN"])
            self.assertNotIn("GH_TOKEN", options.env)
            self.assertEqual(options.cwd, str(workspace))
            self.assertEqual(options.output_format["type"], "json_schema")
            self.assertIsInstance(inside, FakePermissionAllow)
            self.assertIsInstance(outside, FakePermissionDeny)
            self.assertFalse(hasattr(provider, "_workers"))
            self.assertFalse(hasattr(provider, "_results"))

    def test_structured_changes_requested_requires_blocking_finding(self):
        result = {"decision": "CHANGES_REQUESTED", "summary": "Fix this.", "findings": [
            {"severity": "blocking", "file": "code.py", "line": 2, "description": "Regression."}]}
        self.assertEqual(ClaudeAgentSDKReviewer._to_review(result), {
            "event": "REQUEST_CHANGES", "body": "Fix this.\n\nFindings:\n- [blocking] `code.py:2` — Regression."})
        self.assertIsNone(ClaudeAgentSDKReviewer._to_review({**result, "findings": []}))
        self.assertIsNone(ClaudeAgentSDKReviewer._to_review({**APPROVAL, "findings": [result["findings"][0]]}))

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
        self.assertNotIn("secret_value", raised.exception.sdk_detail)
        self.assertNotIn("another_secret", raised.exception.sdk_detail)
        self.assertNotIn("dXNlcjpwYXNz", raised.exception.sdk_detail)
        self.assertNotIn("cookie_secret", raised.exception.sdk_detail)

    def test_sdk_timeout_is_not_terminal_retry_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, head = self._repo(root)
            module = self._sdk_module(hang=True)
            provider = ClaudeAgentSDKReviewer(None, FakeAPI(head), MANIFEST,
                                               str(source), str(root / "reviews"))
            provider.timeout_seconds = 0.01
            with patch.dict(sys.modules, {"claude_agent_sdk": module}):
                with self.assertRaises(Exception) as raised:
                    provider.launch_review(REPO, 7, 12, head)
            self.assertEqual(raised.exception.execution_id, "sdk-session")
            self.assertFalse(raised.exception.terminal)

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
