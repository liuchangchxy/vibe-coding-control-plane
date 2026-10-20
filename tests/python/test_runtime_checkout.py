import subprocess
import tempfile
import unittest
from pathlib import Path

from vccp_runtime.runtime_checkout import ensure_runtime_checkout


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], text=True,
                          capture_output=True, check=True).stdout.strip()


class RuntimeCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "source"
        self.source.mkdir()
        git(self.source, "init", "-b", "main")
        git(self.source, "config", "user.email", "test@example.com")
        git(self.source, "config", "user.name", "Test")
        (self.source / "runtime.txt").write_text("first\n")
        git(self.source, "add", "runtime.txt")
        git(self.source, "commit", "-m", "first")
        self.first = git(self.source, "rev-parse", "HEAD")
        (self.source / "runtime.txt").write_text("second\n")
        git(self.source, "commit", "-am", "second")
        self.second = git(self.source, "rev-parse", "HEAD")
        self.root = root

    def tearDown(self):
        self.temp.cleanup()

    def test_creates_a_clean_immutable_checkout_for_each_full_revision(self):
        first_path = ensure_runtime_checkout(self.source, self.root / "runtimes", self.first)
        second_path = ensure_runtime_checkout(self.source, self.root / "runtimes", self.second)

        self.assertEqual(git(first_path, "rev-parse", "HEAD"), self.first)
        self.assertEqual(git(second_path, "rev-parse", "HEAD"), self.second)
        self.assertEqual(git(first_path, "status", "--porcelain"), "")
        self.assertEqual(git(second_path, "status", "--porcelain"), "")
        self.assertEqual(ensure_runtime_checkout(self.source, self.root / "runtimes", self.first),
                         first_path)

    def test_dirty_managed_runtime_is_recreated_at_the_pinned_revision(self):
        runtime = ensure_runtime_checkout(self.source, self.root / "runtimes", self.first)
        (runtime / "runtime.txt").write_text("local edit\n")

        repaired = ensure_runtime_checkout(self.source, self.root / "runtimes", self.first)
        self.assertEqual(repaired, runtime)
        self.assertEqual(git(repaired, "rev-parse", "HEAD"), self.first)
        self.assertEqual(git(repaired, "status", "--porcelain"), "")
        self.assertEqual((repaired / "runtime.txt").read_text(), "first\n")

    def test_stale_managed_runtime_is_recreated_at_the_pinned_revision(self):
        runtime = ensure_runtime_checkout(self.source, self.root / "runtimes", self.first)
        git(runtime, "checkout", self.second)
        repaired = ensure_runtime_checkout(self.source, self.root / "runtimes", self.first)
        self.assertEqual(git(repaired, "rev-parse", "HEAD"), self.first)
        self.assertEqual(git(repaired, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        self.assertEqual(git(repaired, "status", "--porcelain"), "")

    def test_rejects_non_full_or_unavailable_revision(self):
        with self.assertRaisesRegex(ValueError, "full 40-character"):
            ensure_runtime_checkout(self.source, self.root / "runtimes", "main")
        with self.assertRaisesRegex(ValueError, "not present"):
            ensure_runtime_checkout(self.source, self.root / "runtimes", "f" * 40)


if __name__ == "__main__":
    unittest.main()
