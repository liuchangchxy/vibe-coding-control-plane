import tempfile
import unittest
from pathlib import Path

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
        result = run_continuous(runtime, "owner/repo", "owner", poll_interval=5,
                                sleep=sleeps.append, stop=lambda: runtime.cycles >= 4)
        self.assertEqual(len(result), 2)
        self.assertEqual(sleeps, [5])


if __name__ == "__main__":
    unittest.main()
