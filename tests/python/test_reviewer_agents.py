import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from vccp_runtime.reviewer_agents import ClaudeCodeReviewer, _decision


SHA = "a" * 40
REPO = "owner/repo"
MANIFEST = {"repository": {"implementer_authors": ["builder[bot]"], "base_branch": "main"}}


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


class ReviewerAgentTests(unittest.TestCase):
    def test_decision_parser_accepts_only_final_formal_choices(self):
        self.assertEqual(_decision('Earlier {"event":"BLOCKED"}\nFinal: {"event":"APPROVE","body":"All criteria pass."}'),
                         {"event": "APPROVE", "body": "All criteria pass."})
        self.assertIsNone(_decision('{"event":"APPROVE","body":"  "}'))
        self.assertIsNone(_decision('{"event":"BLOCKED","body":"Need evidence."}'))

    def test_claude_reviewer_uses_exact_head_workspace_and_read_only_tool_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            git(source, "init", "-b", "main")
            git(source, "config", "user.name", "Test")
            git(source, "config", "user.email", "test@example.com")
            (source / "code.py").write_text("print('review me')\n")
            git(source, "add", "code.py")
            git(source, "commit", "-m", "base")
            base = git(source, "rev-parse", "HEAD")
            bare = root / "origin.git"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
            git(source, "remote", "add", "origin", str(bare))
            git(source, "push", "origin", "HEAD:refs/heads/main")
            (source / "code.py").write_text("print('changed')\n")
            git(source, "commit", "-am", "change")
            head = git(source, "rev-parse", "HEAD")
            git(source, "push", "origin", "HEAD:refs/pull/12/head")
            config_root = root / "claude-config"
            handle = "a1b2c3d4"
            session = "session-uuid"
            commands = []
            def runner(args, **kwargs):
                commands.append((list(args), kwargs))
                if len(args) > 1 and args[1] == "--bg":
                    return SimpleNamespace(returncode=0, stdout=f"backgrounded {handle}\n")
                if args[1:3] == ["agents", "--json"]:
                    state = "busy" if not (config_root / "complete").exists() else "completed"
                    return SimpleNamespace(returncode=0, stdout=json.dumps([
                        {"id": handle, "sessionId": session, "state": state, "status": state}]))
                raise AssertionError(args)

            provider = ClaudeCodeReviewer("/usr/bin/claude", FakeAPI(head), MANIFEST,
                                          str(source), str(root / "reviews"),
                                          str(config_root), runner=runner, environ={})
            execution = provider.launch_review(REPO, 7, 12, head)
            launch_args, launch_kwargs = commands[0]
            self.assertEqual(execution["execution_id"], session)
            self.assertEqual(git(root / "reviews" / "owner_repo" / "12" / head, "rev-parse", "HEAD"), head)
            self.assertIn("--permission-mode", launch_args)
            self.assertIn("plan", launch_args)
            self.assertIn("--allowedTools", launch_args)
            self.assertNotIn("GH_TOKEN", launch_kwargs["env"])
            self.assertNotIn("GITHUB_TOKEN", launch_kwargs["env"])
            self.assertEqual(provider.observe_review(session, REPO, 7, 12, head), {"status": "running"})

            transcript = config_root / "projects" / "review" / f"{session}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": '{"event":"REQUEST_CHANGES","body":"Fix the API regression."}'}]}}) + "\n")
            (config_root / "complete").touch()
            self.assertEqual(provider.observe_review(session, REPO, 7, 12, head),
                             {"status": "completed", "event": "REQUEST_CHANGES",
                              "body": "Fix the API regression."})

    def test_claude_reviewer_recovers_one_session_from_its_exact_head_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reviews = root / "reviews"
            source = root / "source"
            source.mkdir()
            git(source, "init", "-b", "main")
            git(source, "config", "user.name", "Test")
            git(source, "config", "user.email", "test@example.com")
            (source / "code.py").write_text("pass\n")
            git(source, "add", "code.py")
            git(source, "commit", "-m", "review head")
            head = git(source, "rev-parse", "HEAD")
            workspace = reviews / "owner_repo" / "12" / head
            workspace.mkdir(parents=True)
            git(workspace, "init", "-b", "detached")
            git(workspace, "config", "user.name", "Test")
            git(workspace, "config", "user.email", "test@example.com")
            (workspace / "code.py").write_text("pass\n")
            git(workspace, "add", "code.py")
            git(workspace, "commit", "-m", "review head")
            sessions = [{"id": "handle", "sessionId": "review-uuid", "status": "busy"}]
            provider = ClaudeCodeReviewer(
                "/usr/bin/claude", FakeAPI(), MANIFEST, str(source), str(reviews),
                str(root / "claude"), runner=lambda *_a, **_k: SimpleNamespace(
                    returncode=0, stdout=json.dumps(sessions)), environ={})

            self.assertEqual(provider.recover_review(REPO, 7, 12, head), "review-uuid")


if __name__ == "__main__":
    unittest.main()
