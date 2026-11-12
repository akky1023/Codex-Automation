"""Exercise the queue through separate CLI processes with a stand-in Codex."""
import json
from datetime import datetime
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from codex_queue.nextcloud import apply_snapshot
from codex_queue.worker import classify


FAKE_CODEX = """
import json
from pathlib import Path
import subprocess
import sys
import time

args = sys.argv[1:]
authorized = Path(__file__).with_name("authorized").exists()
if args == ["login", "status"]:
    sys.exit(0 if authorized else 1)
prompt = sys.stdin.read()
output = Path(args[args.index("-o") + 1])
def event(kind, **data):
    print(json.dumps(dict(type=kind, **data)), flush=True)
if "auth task" in prompt and not authorized:
    event("error", message="Not logged in")
    sys.exit(1)
event("thread.started", thread_id="test-session")
if "wait for user" in prompt and "test answer" not in prompt:
    report = dict(status="WAITING_FOR_INPUT", reason="Need a decision", question="What next?")
elif "sleep task" in prompt and "resume" not in args:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path("child.pid").write_text(str(child.pid))
    time.sleep(60)
elif "limit task" in prompt and "resume" not in args:
    event("turn.failed", error={"codex_error_info": "usageLimitExceeded"})
    sys.exit(1)
else:
    if "pause task" in prompt:
        Path(__file__).with_name("pause-ready").touch()
        while not Path(__file__).with_name("pause-release").exists():
            time.sleep(0.1)
    Path("task-result").write_text("complete")
    subprocess.run(["git", "add", "task-result"], check=True)
    subprocess.run(["git", "commit", "-qm", "Complete task"], check=True)
    report = dict(status="COMPLETED", reason="Finished", question="")
output.write_text(json.dumps(dict(report, verification="fake verification")))
event("turn.completed")
"""


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir=Path.cwd(), prefix="cq-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = Path(__file__).resolve().parents[1] / "src"
        self.env = dict(os.environ, LOCALAPPDATA=str(self.root), PYTHONPATH=str(source))
        self.database = self.root / "CodexQueue" / "queue.sqlite3"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Queue Test")
        self.git("config", "user.email", "queue@example.invalid")
        self.git("commit", "--allow-empty", "-qm", "Initial")
        self.cq("config")
        fake = self.root / "fake.py"
        fake.write_text(FAKE_CODEX, encoding="utf-8")
        self.sql("INSERT INTO settings VALUES ('codex_command', ?)", (json.dumps([sys.executable, str(fake)]),))

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", *args], cwd=cwd or self.repo, text=True).strip()

    def sql(self, sql, params=()):
        db = sqlite3.connect(self.database)
        try:
            with db:
                return db.execute(sql, params).fetchall()
        finally:
            db.close()

    def command(self, *args):
        return [sys.executable, "-m", "codex_queue.cli", *args]

    def cq(self, *args, code=0, cwd=None):
        result = subprocess.run(self.command(*args), cwd=cwd or self.repo, env=self.env,
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(code, result.returncode, result.stdout + result.stderr)
        return result.stdout

    def wait(self, expected):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            actual = self.sql("SELECT status FROM tasks ORDER BY id")
            if actual == [(status,) for status in expected]:
                return
            if any(status == "NEEDS_REVIEW" for status, in actual) and "NEEDS_REVIEW" not in expected:
                self.fail(str(self.sql("SELECT reason FROM attempts")))
            time.sleep(0.1)
        self.fail(f"Expected {expected}, got {actual}: {self.sql('SELECT reason FROM attempts')}")

    def worker(self, *args):
        worker = subprocess.Popen(self.command("_worker", *args), cwd=self.repo, env=self.env)
        self.addCleanup(self.clean_worker, worker)
        return worker

    def clean_worker(self, worker):
        if worker.poll() is None:
            worker.kill()
            worker.wait()

    def enable(self):
        self.sql("INSERT INTO settings VALUES ('enabled', '1') ON CONFLICT(key) DO UPDATE SET value='1'")
        self.sql("INSERT INTO settings VALUES ('stop', '0') ON CONFLICT(key) DO UPDATE SET value='0'")

    def test_priority_and_completion(self):
        self.cq("task", "add", "second")
        self.cq("add", "first", "--priority", "1")
        self.cq("_worker")
        self.wait(["QUEUED", "QUEUED"])
        self.enable()
        self.cq("_worker")
        self.wait(["DONE", "DONE"])
        self.assertEqual([(2,), (1,)], self.sql("SELECT task_id FROM attempts ORDER BY id"))
        self.assertEqual("main", self.git("branch", "--show-current"))
        self.assertIn("fake verification", self.cq("logs", "1"))
        self.cq("_worker", "--scheduled")
        self.assertEqual([(2,)], self.sql("SELECT count(*) FROM attempts"))
        self.sql("UPDATE tasks SET status='RUNNING' WHERE id=1")
        self.sql("UPDATE attempts SET phase='RESTORE' WHERE task_id=1")
        self.cq("_worker")
        self.wait(["DONE", "DONE"])
        self.assertEqual([(2,)], self.sql("SELECT count(*) FROM attempts"))

    def test_start_requests_detached_worker(self):
        self.cq("add", "first task")
        self.assertIn("Automatic execution enabled", self.cq("start"))
        self.wait(["DONE"])
        self.cq("pause")

    def test_rate_limit_text_from_codex_cli(self):
        message = "You\u2019ve hit your usage limit. Upgrade to Pro or try again at May 18th, 2033 12:33 PM."
        result = classify([{"type": "turn.failed", "error": {"message": message}}], "")
        self.assertEqual(("RATE_LIMITED", datetime(2033, 5, 18, 12, 34).timestamp()), result)

    def test_question_and_resume(self):
        self.cq("add", "wait for user")
        self.cq("add", "later")
        self.enable()
        self.cq("_worker")
        self.wait(["WAITING_FOR_INPUT", "QUEUED"])
        self.cq("_worker", "--scheduled")
        self.wait(["WAITING_FOR_INPUT", "QUEUED"])
        self.assertIn("What next?", self.cq("list"))
        status = self.cq("status")
        self.assertIn("Replies needed (1): 1", status)
        self.assertIn("What next?", status)
        self.assertIn('cq reply 1 "..."', status)
        self.cq("pause")
        self.cq("reply", "1", "test answer")
        self.wait(["RESUME_QUEUED", "QUEUED"])
        self.assertIn("No replies needed", self.cq("status"))
        self.enable()
        self.cq("_worker")
        self.wait(["DONE", "DONE"])
        self.assertEqual([(1,)], self.sql("SELECT count(*) FROM attempts WHERE task_id=1"))

    def test_limit_waits_for_schedule(self):
        self.cq("add", "limit task")
        self.enable()
        self.cq("_worker")
        self.wait(["RATE_LIMITED"])
        self.cq("_worker")
        self.wait(["RATE_LIMITED"])
        self.sql("UPDATE attempts SET retry_at=?", (time.time() + 300,))
        self.cq("_worker", "--scheduled")
        self.wait(["RATE_LIMITED"])
        self.sql("UPDATE attempts SET retry_at=NULL")
        self.cq("_worker", "--scheduled")
        self.wait(["DONE"])

    def test_authentication_recovers_on_schedule(self):
        self.cq("add", "auth task")
        self.cq("add", "later")
        self.enable()
        self.cq("_worker")
        self.wait(["AUTH_REQUIRED", "QUEUED"])
        self.cq("_worker", "--scheduled")
        self.wait(["AUTH_REQUIRED", "QUEUED"])
        (self.root / "authorized").touch()
        self.cq("_worker", "--scheduled")
        self.wait(["DONE", "DONE"])

    def test_pause_waits_without_starting_next_task(self):
        self.cq("add", "pause task")
        self.cq("add", "later")
        self.enable()
        worker = self.worker()
        deadline = time.monotonic() + 8
        while not (self.root / "pause-ready").exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue((self.root / "pause-ready").exists())
        pause = subprocess.Popen(self.command("pause"), cwd=self.repo, env=self.env)
        self.addCleanup(self.clean_worker, pause)
        deadline = time.monotonic() + 8
        while self.sql("SELECT value FROM settings WHERE key='enabled'") == [("1",)] and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual([("0",)], self.sql("SELECT value FROM settings WHERE key='enabled'"))
        self.assertIsNone(pause.poll())
        self.wait(["RUNNING", "QUEUED"])
        (self.root / "pause-release").touch()
        self.assertEqual(0, pause.wait(8))
        self.assertEqual(0, worker.wait(8))
        self.wait(["DONE", "QUEUED"])

    def test_stop_preserves_branch_and_prevents_new_launch(self):
        self.cq("add", "sleep task")
        self.cq("add", "later")
        self.enable()
        worker = self.worker()
        self.wait(["RUNNING", "QUEUED"])
        deadline = time.monotonic() + 8
        while not (self.repo / "child.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue((self.repo / "child.pid").exists())
        self.assertEqual(0, self.worker().wait(6))
        self.cq("stop")
        self.assertEqual(0, worker.wait(8))
        self.wait(["INTERRUPTED", "QUEUED"])
        self.assertEqual("codex/queue-1-1", self.git("branch", "--show-current"))
        self.assertTrue((self.repo / "child.pid").exists())
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32")
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x00100000, False, int((self.repo / "child.pid").read_text()))
            if handle:
                try:
                    self.assertEqual(0, kernel.WaitForSingleObject(handle, 5000))
                finally:
                    kernel.CloseHandle(handle)

    def second_repo(self):
        other = self.root / "other"
        other.mkdir()
        self.git("init", "-q", "-b", "main", cwd=other)
        self.git("config", "user.name", "Queue Test", cwd=other)
        self.git("config", "user.email", "queue@example.invalid", cwd=other)
        self.git("commit", "--allow-empty", "-qm", "Initial", cwd=other)
        self.cq("project", "add", cwd=other)
        return other

    def test_parallel_excludes_running_repository(self):
        other = self.second_repo()
        self.cq("config", "--parallel", "2")
        self.cq("add", "sleep task")
        self.cq("add", "same repository")
        self.cq("add", "other repository", cwd=other)
        self.enable()
        worker = self.worker()
        self.wait(["RUNNING", "QUEUED", "DONE"])
        self.cq("stop")
        self.assertEqual(0, worker.wait(8))
        self.wait(["INTERRUPTED", "QUEUED", "DONE"])

    def test_nextcloud_move_waits_for_original_process(self):
        self.cq("project", "add")
        other = self.second_repo()
        self.cq("config", "--sync", "manual")
        self.sql("INSERT INTO settings VALUES ('nextcloud_connection', 'remote')")
        self.sql("INSERT INTO settings VALUES ('nextcloud_credential', 'test')")
        self.sql("INSERT INTO remote_links VALUES ('source', 1), ('target', 2)")
        links = {"source": 1, "target": 2}
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        try:
            with db:
                apply_snapshot(db, "remote", links, {"uid": [
                    ("source", "source/task.ics", '"1"', {"SUMMARY": "sleep task"}),
                ]}, False)
            self.enable()
            worker = self.worker()
            self.wait(["RUNNING"])
            deadline = time.monotonic() + 8
            while not (self.repo / "child.pid").exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertTrue((self.repo / "child.pid").exists())
            with db:
                apply_snapshot(db, "remote", links, {"uid": [
                    ("target", "target/task.ics", '"2"', {"SUMMARY": "Moved task"}),
                ]}, False)
            self.assertEqual(0, worker.wait(12))
            self.wait(["DONE"])
            self.assertEqual([(2,)], self.sql("SELECT project_id FROM tasks"))
            paths = self.sql("SELECT path FROM attempts ORDER BY number")
            self.assertEqual([self.repo.resolve(), other.resolve()], [Path(path).resolve() for path, in paths])
            self.assertEqual("codex/queue-1-1", self.git("branch", "--show-current"))
            self.assertEqual("main", self.git("branch", "--show-current", cwd=other))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
