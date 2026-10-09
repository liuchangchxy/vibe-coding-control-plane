import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO

from vccp_runtime import runner
from vccp_runtime.runner import RuntimeAlreadyRunning, RuntimeLock, run_continuous


class RuntimeRunnerTests(unittest.TestCase):
    def test_process_lock_excludes_second_instance_and_releases(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "runtime.db")
            first = RuntimeLock("owner/repo", path).acquire()
            try:
                with self.assertRaises(RuntimeAlreadyRunning):
                    RuntimeLock("OWNER/REPO", path).acquire()
            finally:
                first.release()
            second = RuntimeLock("owner/repo", path).acquire()
            second.release()

    def test_continuous_loop_uses_injected_sleep_and_stop(self):
        class Fake:
            def __init__(self):
                self.cycles = 0
                self.core = self
                self.workflow = self
                self.lifecycle = self

            def reconcile_once(self, *args):
                return {"status": "ok", "items": []}

            def observe_implementer_progress(self, *args):
                return {"status": "observed", "items": []}

            def advance_once(self, *args):
                self.cycles += 1
                return {"status": "ok", "items": []}

            def discover_ready(self, repo):
                return []

            def dispatch_initial(self, *args):
                raise AssertionError("no ready Issue")

        runtime = Fake()
        sleeps = []
        observed = []
        result = run_continuous(runtime, "owner/repo", "owner", poll_interval=5,
                                sleep=sleeps.append, stop=lambda: runtime.cycles >= 4,
                                on_cycle=lambda item: observed.append(item["dispatches"]))
        self.assertIsNone(result)
        self.assertEqual(observed, [[], []])
        self.assertEqual(sleeps, [5])

    def test_cli_acquires_lock_before_building_runtime(self):
        events = []

        class Lock:
            def __init__(self, repo, database):
                events.append(("identity", repo, database))
            def __enter__(self):
                events.append("lock")
                return self
            def __exit__(self, *_):
                events.append("release")

        runtime = object()
        def build(*_):
            events.append("build")
            return runtime
        def cycle(*_):
            events.append("cycle")
            return {"status": "ok"}

        with patch.object(runner, "_load", side_effect=[{}, {"database_path": "state.db", "owner_id": "owner"}]), \
             patch.object(runner, "RuntimeLock", Lock), \
             patch.object(runner, "build_runtime", side_effect=build), \
             patch.object(runner, "run_cycle", side_effect=cycle), redirect_stdout(StringIO()):
            runner.main(["--manifest", "manifest.json", "--runtime-config", "runtime.json",
                         "--repo", "owner/repo", "--one-cycle"])
        self.assertEqual(events, [("identity", "owner/repo", "state.db"), "lock", "build", "cycle", "release"])


if __name__ == "__main__":
    unittest.main()
