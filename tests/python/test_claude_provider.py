"""P5-A2 Claude Code provider and cross-provider guard compatibility."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vccp_runtime.adapters import (ClaudeCodeImplementer, WorkspaceWriteGuard, build_runtime,
                                   generic_prompt)
from vccp_runtime.activation import qualify_activation
from vccp_runtime.core import LaunchDisposition, LaunchRequest
from vccp_runtime.providers import ANTIGRAVITY_PROVIDER, CLAUDE_CODE_PROVIDER


REPO = "acme/alpha"
ISSUE = 7
CLAUDE_EXE = "claude.exe"
SESSION_ID = "f0120bc6-946e-4fcd-a444-dceed2cb5fab"
HANDLE = "f0120bc6"
SHA = "a" * 40
MANIFEST = {
    "schema_version": 2,
    "issue_contract": {"max_automated_repairs": 3, "frozen_spec_label": "frozen-spec",
                       "active_coordination_labels": ["agent-ready", "agent-working", "changes-requested"],
                       "terminal_coordination_labels": ["infra-blocked", "needs-human"]},
    "repository": {"base_branch": "main", "implementer_authors": ["agent"]},
    "reviewer": {"required_checks": [{"name": "Unit Tests", "accepted_conclusions": ["success"]}]},
}


def init_git_repo(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=VCCP Test", "-c",
                    "user.email=vccp-test@example.invalid", "commit", "--allow-empty", "-m", "baseline"],
                   check=True, capture_output=True, text=True)
    return path


def absolute_git_dir(workspace):
    return Path(subprocess.run(["git", "-C", str(workspace), "rev-parse", "--absolute-git-dir"],
                               capture_output=True, text=True, check=True).stdout.strip())


class FakeClaudeCLI:
    """Deterministic stand-in for the installed Claude Code CLI surface."""

    def __init__(self, session_id=SESSION_ID, handle=HANDLE, launch=None, sessions=None):
        self.session_id, self.handle = session_id, handle
        self.launch = launch
        self.sessions = sessions
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        if len(args) > 1 and args[1] == "--bg":
            if isinstance(self.launch, Exception):
                raise self.launch
            if self.launch is not None:
                return self.launch
            return SimpleNamespace(returncode=0, stderr="",
                                   stdout=f"backgrounded ❯ {self.handle}\n  claude agents\n")
        if len(args) > 2 and args[1] == "agents" and args[2] == "--json":
            records = self.sessions if self.sessions is not None else [
                {"id": self.handle, "sessionId": self.session_id, "cwd": kwargs.get("cwd"),
                 "state": "working", "status": "busy"}]
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(records))
        raise AssertionError(f"unexpected claude command: {args}")

    @property
    def launch_argv(self):
        return next(args for args, _ in self.calls if len(args) > 1 and args[1] == "--bg")

    @property
    def launch_env(self):
        return next(kwargs["env"] for args, kwargs in self.calls if len(args) > 1 and args[1] == "--bg")


class ClaudeLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = init_git_repo(Path(self.temp.name) / "repo")

    def tearDown(self):
        self.temp.cleanup()

    def implementer(self, cli=None, **kwargs):
        self.cli = cli or FakeClaudeCLI()
        implementer = ClaudeCodeImplementer(
            CLAUDE_EXE, str(self.workspace), "app-gh.exe", "app-git-push.exe", runner=self.cli,
            environ=kwargs.pop("environ", {}), session_lookup_interval=0, **kwargs)
        # Production wiring supplies the consumer manifest the shared prompt is built from.
        implementer._policy = MANIFEST
        return implementer

    def request(self, attempt_id="attempt-1"):
        return LaunchRequest(REPO, ISSUE, attempt_id, "initial_dispatch")

    def test_confirmed_launch_returns_the_stable_session_identity(self):
        result = self.implementer().launch(self.request())
        self.assertEqual((result.disposition, result.execution_id),
                         (LaunchDisposition.CONFIRMED, SESSION_ID))
        self.assertEqual(self.cli.launch_argv[1], "--bg")

    def test_prompt_is_the_shared_generic_prompt(self):
        request = self.request()
        self.implementer().launch(request)
        env = self.cli.launch_env
        self.assertEqual(self.cli.launch_argv[2],
                         generic_prompt(request, str(self.workspace), MANIFEST, env["VCCP_APP_GH"],
                                        env["VCCP_APP_GIT_PUSH"]))
        self.assertIn("Frozen Issue: #7", self.cli.launch_argv[2])
        self.assertIn("Closes #<issue_number>", self.cli.launch_argv[2])

    def test_repair_prompt_preserves_runtime_repair_authority(self):
        request = LaunchRequest(REPO, ISSUE, "attempt-repair", "repair",
                                repair_cause_type="reviewer_rejection", repair_cause_id="review-9",
                                repair_ordinal=2, expected_head_sha=SHA, pr_number=44, branch="fix/44")
        self.implementer().launch(request)
        prompt = self.cli.launch_argv[2]
        self.assertIn("existing PR #44", prompt)
        self.assertIn(SHA, prompt)
        self.assertIn("ordinal 2 is authoritative", prompt)

    def test_inherited_github_tokens_and_claude_session_identity_are_stripped(self):
        environ = {"GH_TOKEN": "secret", "GITHUB_TOKEN": "secret", "GITHUB_APP_PRIVATE_KEY": "key",
                   "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "parent-session",
                   "CLAUDE_CODE_CHILD_SESSION": "1", "KEEP": "yes"}
        self.implementer(environ=environ).launch(self.request())
        env = self.cli.launch_env
        for stripped in ("GH_TOKEN", "GITHUB_TOKEN", "GITHUB_APP_PRIVATE_KEY", "CLAUDECODE",
                         "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION"):
            self.assertNotIn(stripped, env)
        self.assertEqual(env["KEEP"], "yes")
        self.assertEqual(env["VCCP_APP_GH"], "app-gh.exe")
        self.assertTrue(Path(env["GH_CONFIG_DIR"]).is_dir())
        self.assertTrue(Path(env["VCCP_APP_GIT_PUSH"]).is_file())

    def test_guard_failure_before_invocation_is_definitely_not_started(self):
        cli = FakeClaudeCLI()

        class FailedGuard:
            def install(self, *args, **kwargs):
                raise RuntimeError("cannot install guard")

        result = self.implementer(cli, write_guard=FailedGuard()).launch(self.request())
        self.assertEqual(result.disposition, LaunchDisposition.DEFINITELY_NOT_STARTED)
        self.assertEqual(cli.calls, [])

    def test_unresolvable_executable_is_definitely_not_started(self):
        result = self.implementer(FakeClaudeCLI(launch=FileNotFoundError("no claude"))).launch(self.request())
        self.assertEqual(result.disposition, LaunchDisposition.DEFINITELY_NOT_STARTED)

    def test_post_invocation_ambiguity_is_unknown(self):
        ambiguous = {
            "non-zero exit": FakeClaudeCLI(launch=SimpleNamespace(returncode=1, stdout="", stderr="boom")),
            "missing acknowledgement": FakeClaudeCLI(launch=SimpleNamespace(returncode=0, stdout="nothing here")),
            "unresolvable session": FakeClaudeCLI(sessions=[]),
            "timeout": FakeClaudeCLI(launch=TimeoutError("still running")),
        }
        for label, cli in ambiguous.items():
            with self.subTest(case=label):
                # Each case is its own Runtime attempt: one provider installs one guard.
                result = self.implementer(cli).launch(self.request(f"attempt-{label.replace(' ', '-')}"))
                self.assertEqual(result.disposition, LaunchDisposition.UNKNOWN)
                self.assertIsNone(result.execution_id)

    def test_activity_reads_only_the_stored_session(self):
        root = Path(self.temp.name) / "claude-config"
        transcript = root / "projects" / "some-workspace" / f"{SESSION_ID}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n", encoding="utf-8")
        expected = transcript.stat().st_mtime
        implementer = self.implementer(environ={"CLAUDE_CONFIG_DIR": str(root)})
        self.assertEqual(implementer.latest_activity(SESSION_ID), expected)

    def test_unobservable_or_foreign_execution_fails_closed(self):
        root = Path(self.temp.name) / "claude-config"
        transcript = root / "projects" / "some-workspace" / f"{SESSION_ID}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n", encoding="utf-8")
        foreign = FakeClaudeCLI(sessions=[{"id": "deadbeef",
                                           "sessionId": "deadbeef-0000-0000-0000-000000000000"}])
        self.assertIsNone(self.implementer(foreign, environ={"CLAUDE_CONFIG_DIR": str(root)})
                          .latest_activity(SESSION_ID))
        malformed = FakeClaudeCLI(sessions="not-a-list")
        self.assertIsNone(self.implementer(malformed, environ={"CLAUDE_CONFIG_DIR": str(root)})
                          .latest_activity(SESSION_ID))

    def test_activity_observation_never_launches_or_resumes(self):
        self.implementer().latest_activity(SESSION_ID)
        self.assertTrue(all(args[1] != "--bg" and args[1] != "--resume" for args, _ in self.cli.calls))


class CrossProviderGuardTests(unittest.TestCase):
    """One Runtime attempt must be able to host several providers' guards."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = init_git_repo(Path(self.temp.name) / "repo")

    def tearDown(self):
        self.temp.cleanup()

    def test_providers_in_one_attempt_get_isolated_guard_roots(self):
        guard = WorkspaceWriteGuard()
        first = guard.install(str(self.workspace), "attempt-1", shutil.which("git"),
                              provider=ANTIGRAVITY_PROVIDER)
        second = guard.install(str(self.workspace), "attempt-1", shutil.which("git"),
                               provider=CLAUDE_CODE_PROVIDER)
        first_root = Path(first.environment["GH_CONFIG_DIR"]).parent
        second_root = Path(second.environment["GH_CONFIG_DIR"]).parent
        self.assertEqual((first_root.name, second_root.name),
                         (ANTIGRAVITY_PROVIDER, CLAUDE_CODE_PROVIDER))
        self.assertEqual(first_root.parent, second_root.parent)
        for installed in (first, second):
            self.assertTrue(Path(installed.git_push_command).is_file())
            self.assertEqual(list(Path(installed.environment["GH_CONFIG_DIR"]).iterdir()), [])
        self.assertNotEqual(Path(first.git_push_command), Path(second.git_push_command))
        index = int(first.environment["GIT_CONFIG_COUNT"]) - 1
        self.assertEqual(first.environment[f"GIT_CONFIG_KEY_{index}"], "core.hooksPath")
        self.assertNotEqual(first.environment[f"GIT_CONFIG_VALUE_{index}"],
                            second.environment[f"GIT_CONFIG_VALUE_{index}"])

    def test_same_provider_cannot_install_twice_for_one_attempt(self):
        guard = WorkspaceWriteGuard()
        guard.install(str(self.workspace), "attempt-1", shutil.which("git"), provider=CLAUDE_CODE_PROVIDER)
        with self.assertRaises(FileExistsError):
            guard.install(str(self.workspace), "attempt-1", shutil.which("git"),
                          provider=CLAUDE_CODE_PROVIDER)

    def test_provider_scoped_guards_still_block_direct_push(self):
        remote = Path(self.temp.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(self.workspace), "remote", "add", "origin", str(remote)],
                       check=True, capture_output=True)
        guard = WorkspaceWriteGuard().install(str(self.workspace), "attempt-1", shutil.which("git"),
                                              provider=CLAUDE_CODE_PROVIDER)
        env = dict(os.environ)
        env.update(guard.environment)
        blocked = subprocess.run(["git", "push", "origin", "HEAD"], cwd=self.workspace, env=env,
                                 capture_output=True, text=True, timeout=10)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("Direct git push is disabled", blocked.stderr)

    def test_hostile_existing_hook_configuration_fails_closed(self):
        hostile = Path(self.temp.name) / "hostile-hooks"
        hostile.mkdir()
        (hostile / "pre-push").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "config", "--local", "core.hooksPath", str(hostile)],
                       check=True, capture_output=True)
        result = ClaudeCodeImplementer(CLAUDE_EXE, str(self.workspace), "app-gh.exe", "app-git-push.exe",
                                       runner=FakeClaudeCLI(), environ={}).launch(
            LaunchRequest(REPO, ISSUE, "attempt-hostile", "initial_dispatch"))
        self.assertEqual(result.disposition, LaunchDisposition.DEFINITELY_NOT_STARTED)
        self.assertEqual((hostile / "pre-push").read_text(encoding="utf-8"), "#!/bin/sh\nexit 0\n")

    def test_unmarked_managed_hook_directory_fails_closed(self):
        previous = absolute_git_dir(self.workspace) / "vccp-control" / "previous" / "hooks"
        previous.mkdir(parents=True)
        (previous / "pre-push").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.workspace), "config", "--local", "core.hooksPath", str(previous)],
                       check=True, capture_output=True)
        with self.assertRaises(RuntimeError):
            WorkspaceWriteGuard().install(str(self.workspace), "attempt-1", shutil.which("git"),
                                          provider=CLAUDE_CODE_PROVIDER)


class FakeGitHub:
    def __init__(self):
        self.issue = {"number": ISSUE, "state": "open", "updated_at": "r1",
                      "labels": [{"name": "agent-ready"}, {"name": "frozen-spec"}]}

    def get(self, path):
        return self.issue

    def graphql(self, query, variables):
        return {"repository": {"issue": {"timelineItems": {"nodes": [],
                                                           "pageInfo": {"hasNextPage": False}}}}}


class RuntimeWriter:
    """Coordination-label writer for the injected production graph."""

    def __init__(self, api):
        self.api = api

    def replace_labels(self, repo, issue, remove, add):
        self.api.issue["labels"] = [item for item in self.api.issue["labels"]
                                    if item["name"] not in remove]
        self.api.issue["labels"].extend({"name": label} for label in add)
        self.api.issue["updated_at"] += "+"
        return True


class ProductionFallbackTests(unittest.TestCase):
    """Real production adapters: AntiGravity proves not-started, Claude actually launches."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = init_git_repo(Path(self.temp.name) / "repo")
        self.claude = FakeClaudeCLI()
        self.antigravity_calls = []

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, args, **kwargs):
        if args[0] == "language_server.exe":
            self.antigravity_calls.append(list(args))
            raise FileNotFoundError("language_server.exe is not installed")
        return self.claude(args, **kwargs)

    def legacy_config(self, **overrides):
        return {"database_path": str(Path(self.temp.name) / "runtime.sqlite"),
                "workspace": str(self.workspace), "owner_id": "owner",
                "app_gh_executable": "app-gh.exe", "app_git_push_executable": "app-git-push.exe",
                "github_read_token_env": "TOKEN", **overrides}

    def providers(self):
        return [{"key": ANTIGRAVITY_PROVIDER, "type": ANTIGRAVITY_PROVIDER,
                 "executable": "language_server.exe"},
                {"key": CLAUDE_CODE_PROVIDER, "type": CLAUDE_CODE_PROVIDER, "executable": CLAUDE_EXE}]

    def wired(self, runner=None):
        api = FakeGitHub()
        return build_runtime(MANIFEST, self.legacy_config(providers=self.providers()),
                             api=api, writer=RuntimeWriter(api), runner=runner or self.runner)

    def test_not_started_primary_reaches_a_real_claude_launch(self):
        runtime = self.wired()
        result = runtime.core.dispatch_initial(REPO, ISSUE, "owner")
        self.assertEqual(result["phase"], "LAUNCH_CONFIRMED")
        row = runtime.core.store.attempt(result["attempt_id"])
        self.assertEqual((row["implementer_provider"], row["execution_id"]),
                         (CLAUDE_CODE_PROVIDER, SESSION_ID))
        self.assertEqual(len(self.antigravity_calls), 1)
        self.assertEqual(self.claude.launch_argv[1], "--bg")
        attempt_guards = absolute_git_dir(self.workspace) / "vccp-control" / row["attempt_id"]
        self.assertEqual(sorted(path.name for path in attempt_guards.iterdir()),
                         [ANTIGRAVITY_PROVIDER, CLAUDE_CODE_PROVIDER])
        env = self.claude.launch_env
        self.assertIn(f"{os.sep}{CLAUDE_CODE_PROVIDER}{os.sep}", env["VCCP_APP_GIT_PUSH"])
        for primary_artifact in (str(attempt_guards / ANTIGRAVITY_PROVIDER),):
            self.assertNotIn(primary_artifact, env["VCCP_APP_GIT_PUSH"])
            self.assertNotIn(primary_artifact, env["GH_CONFIG_DIR"])

    def test_restart_observes_only_the_stored_claude_execution(self):
        runtime = self.wired()
        result = runtime.core.dispatch_initial(REPO, ISSUE, "owner")
        baseline = runtime.core.store.attempt(result["attempt_id"])["implementer_activity_at"]
        config_root = Path(self.temp.name) / "claude-config"
        transcript = config_root / "projects" / "workspace" / f"{SESSION_ID}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n", encoding="utf-8")
        activity = baseline + 30
        os.utime(transcript, (activity, activity))
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config_root)}):
            restarted = self.wired()
            self.assertEqual(restarted.router.latest_activity_for(CLAUDE_CODE_PROVIDER, SESSION_ID), activity)
            self.assertIsNone(restarted.router.latest_activity_for(ANTIGRAVITY_PROVIDER, SESSION_ID))
            self.assertIsNone(restarted.router.latest_activity_for(None, SESSION_ID))
            observed = restarted.core.observe_implementer_progress(REPO, activity + 1)
        self.assertEqual(observed["items"][0]["status"], "progress")
        self.assertEqual(restarted.core.store.attempt(result["attempt_id"])["implementer_activity_at"], activity)

    def test_unknown_primary_never_invokes_claude(self):
        def runner(args, **kwargs):
            self.antigravity_calls.append(list(args))
            return SimpleNamespace(returncode=1, stdout="", stderr="explicit agentapi failure")

        runtime = self.wired(runner=runner)
        result = runtime.core.dispatch_initial(REPO, ISSUE, "owner")
        row = runtime.core.store.attempt(result["attempt_id"])
        self.assertEqual(result["phase"], "LAUNCH_UNKNOWN")
        self.assertEqual((row["implementer_provider"], row["execution_id"]), (ANTIGRAVITY_PROVIDER, None))
        self.assertEqual(self.claude.calls, [])


class ProviderConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = init_git_repo(Path(self.temp.name) / "repo")
        self.claude_executable = Path(self.temp.name) / "claude.exe"
        self.claude_executable.write_bytes(b"fake claude")

    def tearDown(self):
        self.temp.cleanup()

    def config(self, **overrides):
        return {"database_path": str(Path(self.temp.name) / "db.sqlite"),
                "workspace": str(self.workspace), "owner_id": "owner",
                "app_gh_executable": "app-gh.exe", "app_git_push_executable": "push.exe",
                "github_read_token_env": "TOKEN", **overrides}

    def build(self, local):
        api = FakeGitHub()
        return build_runtime(MANIFEST, local, api=api, writer=RuntimeWriter(api),
                             runner=FakeClaudeCLI())

    def test_explicit_antigravity_and_claude_configuration_constructs(self):
        runtime = self.build(self.config(providers=[
            {"key": ANTIGRAVITY_PROVIDER, "type": ANTIGRAVITY_PROVIDER, "executable": "language_server.exe"},
            {"key": CLAUDE_CODE_PROVIDER, "type": CLAUDE_CODE_PROVIDER,
             "executable": str(self.claude_executable)}]))
        self.assertEqual(runtime.router.primary, ANTIGRAVITY_PROVIDER)
        self.assertIsInstance(runtime.router._by_key[CLAUDE_CODE_PROVIDER], ClaudeCodeImplementer)
        self.assertEqual(runtime.router._by_key[CLAUDE_CODE_PROVIDER].provider, CLAUDE_CODE_PROVIDER)

    def test_claude_only_configuration_needs_no_antigravity_fields(self):
        local = self.config(providers=[{"key": CLAUDE_CODE_PROVIDER, "type": CLAUDE_CODE_PROVIDER,
                                        "executable": str(self.claude_executable)}])
        runtime = self.build(local)
        self.assertEqual(runtime.router.primary, CLAUDE_CODE_PROVIDER)
        self.assertNotIn("antigravity_executable", local)

    def test_missing_provider_key_is_rejected_at_configuration(self):
        with self.assertRaisesRegex(ValueError, "key must be a lowercase identifier"):
            self.build(self.config(providers=[{"type": CLAUDE_CODE_PROVIDER,
                                               "executable": str(self.claude_executable)}]))


class FakeCredentialProvider:
    api_url = "https://api.github.com"
    target_repository = REPO

    def probe(self, repo):
        return {"installed": True, "permissions_sufficient": True, "missing_permissions": []}

    def get_token(self, repo):
        raise AssertionError("qualification must not mint a token")

    def request(self, *args, **kwargs):
        raise AssertionError("qualification must not write")


class RepositoryReadAPI:
    def get(self, path):
        return {"full_name": REPO}


class ReadinessWriter:
    def replace_labels(self, *args):
        raise AssertionError("readiness must not mutate labels")


class ReadinessPushBackend:
    def push(self, *args, **kwargs):
        raise AssertionError("readiness must not push")


class ActivationProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "workspace").mkdir()
        subprocess.run(["git", "init", str(self.root / "workspace")], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root / "workspace"), "remote", "add", "origin",
                        f"https://github.com/{REPO}.git"], check=True, capture_output=True)
        (self.root / "state").mkdir()
        (self.root / "key.dpapi").write_bytes(b"ciphertext")
        self.claude_executable = self.root / "claude.exe"
        self.claude_executable.write_bytes(b"fake claude")
        self.antigravity_executable = self.root / "language_server.exe"
        self.antigravity_executable.write_bytes(b"fake ag")

    def tearDown(self):
        self.temp.cleanup()

    def config(self, **overrides):
        return {"database_path": str(self.root / "state" / "runtime.sqlite"),
                "workspace": str(self.root / "workspace"), "target_repository": REPO,
                "enrollment": {"authorized_repository": REPO}, "owner_id": "owner",
                "github_app": {"app_id": "123456", "expected_app_slug": "app",
                               "credential_source": {"type": "windows_dpapi_file",
                                                     "path": str(self.root / "key.dpapi")}}, **overrides}

    def qualify(self, config):
        with patch("vccp_runtime.activation.DPAPIFileCredentialSource") as source:
            source.return_value.load_private_key.return_value = object()
            return qualify_activation(MANIFEST, config, REPO, credential_provider=FakeCredentialProvider(),
                                      api=RepositoryReadAPI(), writer=ReadinessWriter(),
                                      push_backend=ReadinessPushBackend())

    def test_legacy_antigravity_only_activation_still_passes(self):
        result = self.qualify(self.config(antigravity_executable=str(self.antigravity_executable)))
        self.assertEqual((result["status"], result["blockers"]), ("ready", []))

    def test_claude_only_activation_passes_without_antigravity(self):
        result = self.qualify(self.config(providers=[
            {"key": CLAUDE_CODE_PROVIDER, "type": CLAUDE_CODE_PROVIDER,
             "executable": str(self.claude_executable)}]))
        self.assertEqual((result["status"], result["blockers"]), ("ready", []))

    def test_missing_claude_capability_blocks_readiness_clearly(self):
        result = self.qualify(self.config(providers=[
            {"key": CLAUDE_CODE_PROVIDER, "type": CLAUDE_CODE_PROVIDER,
             "executable": str(self.root / "absent.exe")}]))
        self.assertEqual(result["status"], "not_ready")
        self.assertIn("required executable is unavailable: claude_code", result["blockers"])

    def test_provider_without_executable_blocks_readiness_clearly(self):
        result = self.qualify(self.config(providers=[{"key": CLAUDE_CODE_PROVIDER,
                                                      "type": CLAUDE_CODE_PROVIDER}]))
        self.assertEqual(result["status"], "not_ready")
        self.assertIn("claude_code provider requires a configured executable", result["blockers"])


if __name__ == "__main__":
    unittest.main()
