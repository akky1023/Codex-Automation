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
from unittest.mock import patch

from codex_queue.nextcloud import apply_snapshot
from codex_queue.store import SCHEMA, open_queue
from codex_queue.worker import classify, run_worker


class PlannerProgressTest(unittest.TestCase):
    def test_grilling_keeps_questions_when_codex_also_returns_a_summary(self):
        from codex_queue.grilling import generate_round

        question = {"title": "Scope", "body": "Which?", "recommendation": "Admins."}
        report = json.dumps({"questions": [question], "summary": "Preliminary findings"})
        script = "import sys; from pathlib import Path; sys.stdin.read(); Path(sys.argv[sys.argv.index('-o')+1]).write_text(" + repr(report) + ", encoding='utf-8')"
        with TemporaryDirectory() as folder, patch("codex_queue.store.queue_folder", return_value=Path(folder)), patch(
            "codex_queue.processes.queue_folder", return_value=Path(folder),
        ), patch("codex_queue.grilling.codex_command", return_value=[sys.executable, "-c", script]), patch(
            "codex_queue.grilling.codex_env", return_value=os.environ.copy(),
        ):
            self.assertEqual(([question], ""), generate_round(None, folder, "Ask a question", {"planner_model": ""}))

    def test_interrupting_stop_wait_keeps_stop_request_and_cleans_display(self):
        import io
        from contextlib import closing, nullcontext
        from codex_queue import progress
        from codex_queue.actions import queue_control
        from codex_queue.store import get_setting

        with closing(sqlite3.connect(':memory:')) as db:
            db.row_factory = sqlite3.Row
            db.executescript(SCHEMA)
            db.execute("INSERT INTO projects(path,path_key,name) VALUES ('repo','repo','Repo')")
            db.execute("INSERT INTO tasks(project_id,name,priority,status) VALUES (1,'Task',1,'RUNNING')")
            db.commit()
            display = io.StringIO()
            with progress.output_to(display), patch(
                'codex_queue.processes.process_lock', return_value=nullcontext(False),
            ), patch('codex_queue.actions.time.sleep', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    queue_control(db, 'stop')
            self.assertEqual('0', get_setting(db, 'enabled'))
            self.assertEqual('1', get_setting(db, 'stop'))
            self.assertEqual('RUNNING', db.execute('SELECT status FROM tasks').fetchone()[0])
            self.assertIn('interrupted or failed', display.getvalue())
            self.assertFalse(progress._active)

    def test_delayed_plan_updates_on_display_thread_and_keeps_caller_thread(self):
        import io
        import threading
        from codex_queue import progress
        from codex_queue.planner import generate_plan

        class Display(io.StringIO):
            def isatty(self):
                return True

        display = Display()
        refreshed = threading.Event()
        caller = threading.get_ident()
        original = progress._draw
        frames = []

        def draw():
            original()
            frames.append(display.getvalue())
            if len(frames) >= 2:
                refreshed.set()

        def codex(command, **kwargs):
            self.assertEqual(caller, threading.get_ident())
            self.assertTrue(refreshed.wait(2))
            Path(command[command.index('-o') + 1]).write_text(json.dumps({
                'question': '', 'todos': [{'title': 'feat: test', 'abstract': 'test', 'plan': 'test', 'model': 'test'}],
            }), encoding='utf-8')
            return subprocess.CompletedProcess(command, 0, '', '')

        with TemporaryDirectory() as folder, progress.output_to(display), patch(
            'codex_queue.planner.queue_folder', return_value=folder,
        ), patch('codex_queue.planner.codex_command', return_value=['codex']), patch(
            'codex_queue.planner.codex_env', return_value={},
        ), patch('codex_queue.planner.subprocess.run', side_effect=codex), patch.object(
            progress, '_draw', side_effect=draw,
        ):
            question, todos = generate_plan(None, folder, 'test', {
                'implementation_model': 'test', 'planner_model': '',
            }, [])
        self.assertEqual('', question)
        self.assertEqual('feat: test', todos[0]['title'])
        self.assertFalse(progress._active)


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
if "read-only" in args:
    models = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
    if "approved" in models["properties"]:
        if "Question from the implementation session" in prompt:
            approved = "ask planner" in prompt and "resume" in args and "plan-session" in args
            answer = "test answer" if approved else "Needs the user's decision."
        else:
            approved = "Independently review" in prompt or "Review merge commit" in prompt
            answer = "Reviewed the committed implementation."
        output.write_text(json.dumps(dict(approved=approved, reason=answer)))
        sys.exit(0)
    assert args[args.index("-m") + 1] == "project-planner"
    print(json.dumps(dict(type="thread.started", thread_id="plan-session")), flush=True)
    models = models["properties"]["todos"]["items"]["properties"]["model"]["enum"]
    assert models == ["small", "large"]
    if "This request revises ToDo" in prompt:
        assert "Prior verification" in prompt
        if "trace from user" not in prompt:
            output.write_text(json.dumps(dict(question="Please provide the failing trace.", todos=[])))
        else:
            output.write_text(json.dumps(dict(question="", todos=[
                dict(title="Handle the failing trace", abstract="Fix regression", plan="Reproduce and verify the fix", model="large"),
            ])))
    elif "clarify first" in prompt:
        output.write_text(json.dumps(dict(question="Which screen?", todos=[])))
    else:
        proposal = dict(question="", todos=[
            dict(title="Add access controls", abstract="Restrict access", plan="Implement and test access", model="small"),
            dict(title="Decline unsafe change", abstract="Assess request", plan="Check policy", model="large"),
        ])
        if "Recommend per-idea" in prompt:
            proposal.update(branch_mode="per-idea", branch_reason="Review the changes together.")
        output.write_text(json.dumps(proposal))
    sys.exit(0)
if "Resolve the conflicts merging" in prompt:
    Path("task-result").write_text("complete and competing change")
    output.write_text(json.dumps(dict(approved=True, reason="Combined both changes.")))
    sys.exit(0)
def event(kind, **data):
    print(json.dumps(dict(type=kind, **data)), flush=True)
if "auth task" in prompt and not authorized:
    event("error", message="Not logged in")
    sys.exit(1)
event("thread.started", thread_id="test-session")
if "Decline unsafe change" in prompt:
    assert args[args.index("-m") + 1] == "large"
    report = dict(status="ABORTED", reason="Refused by policy", question="")
elif "wait for user" in prompt and "test answer" not in prompt:
    report = dict(status="WAITING_FOR_INPUT", reason="Need a decision", question="What next?")
elif "shutdown hang" in prompt:
    report = dict(status="NEEDS_REVIEW", reason="Rendering is still slow", question="")
elif "dirty failure" in prompt:
    Path("partial").write_text("preserve this work")
    report = dict(status="NEEDS_REVIEW", reason="Implementation failed", question="")
elif "sleep task" in prompt and "resume" not in args:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path("child.pid").write_text(str(child.pid))
    time.sleep(60)
elif "limit task" in prompt and "resume" not in args:
    event("turn.failed", error={"codex_error_info": "usageLimitExceeded"})
    sys.exit(1)
else:
    if "Add access controls" in prompt:
        assert args[args.index("-m") + 1] == "small"
        assert "Restrict access" in prompt and "Implement and test access" in prompt
    if "pause task" in prompt:
        Path(__file__).with_name("pause-ready").touch()
        while not Path(__file__).with_name("pause-release").exists():
            time.sleep(0.1)
    Path("task-result").write_text("repaired" if "configured verification failed" in prompt else "complete")
    subprocess.run(["git", "add", "task-result"], check=True)
    subprocess.run(["git", "commit", "-qm", "Complete task"], check=True)
    report = dict(status="COMPLETED", reason="Finished", question="")
output.write_text(json.dumps(dict(report, verification="fake verification")))
event("turn.completed")
if "shutdown hang" in prompt:
    time.sleep(60)
"""


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir=Path.cwd(), prefix="cq-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = Path(__file__).resolve().parents[1] / "src"
        self.env = dict(os.environ, LOCALAPPDATA=str(self.root), CQ_WORKER_DATA_DIR=str(self.root / "CodexQueue"),
                        PYTHONPATH=str(source))
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

    def queue_standalone_task(self):
        if not self.sql("SELECT id FROM projects"):
            self.cq("project", "add")
        self.sql("INSERT INTO tasks(project_id, name, priority) VALUES (1, 'Deliver change', 5)")

    def run_queue(self):
        self.sql("INSERT OR REPLACE INTO settings VALUES ('enabled', '1')")
        self.assertEqual(0, self.worker().wait(timeout=25))

    def test_automatic_repair_planner_review_and_merge(self):
        suite = [sys.executable, "-c",
                 "from pathlib import Path; assert Path('task-result').read_text() == 'repaired'"]
        self.cq("config", "--completion-approval", "planner", "--merge-mode", "auto")
        self.cq("project", "add", "--test-commands", json.dumps([suite]))
        self.queue_standalone_task()
        self.run_queue()
        self.wait(["DONE"])
        self.assertEqual([("OK", 1, "MERGED")],
                         self.sql("SELECT test_result, test_repairs, merge_status FROM attempts"))
        self.assertEqual("repaired", self.git("show", "main:task-result"))
        self.assertEqual(3, len(self.git("rev-list", "--parents", "-1", "main").split()))

    def test_manual_merge_and_project_override(self):
        self.cq("config", "--merge-mode", "off", "--completion-approval", "tests",
                "--test-commands", json.dumps([[sys.executable, "-c", "pass"]]))
        self.cq("project", "add", "--merge-mode", "approval")
        initial = self.git("rev-parse", "main")
        self.queue_standalone_task()
        self.run_queue()
        self.wait(["DONE"])
        self.assertEqual(initial, self.git("rev-parse", "main"))
        self.assertIn("PENDING", self.cq("logs", "1"))
        self.cq("stop")
        self.cq("merge", "1")
        self.assertEqual("complete", self.git("show", "main:task-result"))

    def test_blocked_test_never_publishes(self):
        self.cq("config", "--completion-approval", "tests", "--merge-mode", "auto", "--test-retries", "0",
                "--test-commands", json.dumps([["missing-cq-test-executable"]]))
        initial = self.git("rev-parse", "main")
        self.queue_standalone_task()
        self.run_queue()
        self.wait(["NEEDS_REVIEW"])
        self.assertEqual([("BLOCKED", None)], self.sql("SELECT test_result, merge_status FROM attempts"))
        self.assertEqual(initial, self.git("rev-parse", "main"))

    def test_next_idea_continues_after_dirty_failure(self):
        self.cq("config", "--on-abort", "next-idea")
        self.cq("project", "add", "--planner-model", "project-planner", "--allowed-models", "small,large")
        for idea in ("First", "Second"):
            self.cq("idea", "add", idea)
            self.cq("idea", "plan", str(1 if idea == "First" else 2))
        self.sql("UPDATE tasks SET prompt='dirty failure' WHERE id=1")
        self.run_queue()
        self.wait(["NEEDS_REVIEW", "QUEUED", "DONE", "ABORTED"])
        self.assertEqual("main", self.git("branch", "--show-current"))
        self.assertIn("Codex Queue task 1", self.git("stash", "list"))

    def test_clarification_resumes_original_planning_session(self):
        self.cq("project", "add", "--planner-model", "project-planner", "--allowed-models", "small,large")
        self.cq("idea", "add", "Improve access")
        self.cq("idea", "plan", "1")
        self.sql("UPDATE tasks SET prompt=prompt || ' wait for user, ask planner' WHERE id=1")
        self.run_queue()
        self.wait(["DONE", "ABORTED"])
        self.assertEqual([("plan-session",)], self.sql("SELECT planner_session FROM ideas"))
        self.assertIn("test answer", self.sql("SELECT answer FROM attempts WHERE task_id=1")[0][0])

    def test_merge_conflict_is_held_then_resolved_and_reviewed(self):
        self.cq("config", "--merge-mode", "approval", "--completion-approval", "tests",
                "--test-commands", json.dumps([[sys.executable, "-c", "pass"]]))
        self.queue_standalone_task()
        self.run_queue()
        (self.repo / "task-result").write_text("competing change")
        self.git("add", "task-result")
        self.git("commit", "-qm", "Competing change")
        base = self.git("rev-parse", "main")
        self.cq("merge", "1", code=1)
        self.assertEqual([("CONFLICT",)], self.sql("SELECT merge_status FROM attempts"))
        self.assertEqual(base, self.git("rev-parse", "main"))
        self.assertEqual("", self.git("status", "--porcelain"))
        self.cq("config", "--on-conflict", "resolve")
        self.cq("merge", "1")
        self.assertEqual("complete and competing change", self.git("show", "main:task-result"))
        self.assertEqual([("MERGED",)], self.sql("SELECT merge_status FROM attempts"))

    def test_idea_planning_and_execution(self):
        self.cq("config", "--planner-model", "global-planner", "--allowed-models", "global-model")
        self.cq("project", "add", "--planner-model", "project-planner", "--allowed-models", "small, large")
        self.cq("idea", "add", "Improve access", "--detail", "clarify first")
        self.cq("idea", "plan", "1", code=1)
        self.assertEqual([], self.sql("SELECT id FROM tasks"))
        self.cq("idea", "edit", "1", "--detail", "Apply to the settings screen")
        self.cq("idea", "plan", "1")
        self.cq("idea", "plan", "1", code=1)
        self.assertEqual([(1, "small"), (1, "large")], self.sql(
            "SELECT idea_id, implementation_model FROM tasks ORDER BY id",
        ))
        self.assertIn("Improve access", self.cq("todo", "show", "1"))
        self.assertIn("Implement and test access", self.cq("todo", "show", "1"))
        self.sql("UPDATE tasks SET priority=1 WHERE id=2")
        self.cq("start")
        self.wait(["DONE", "ABORTED"])
        attempts = self.sql("SELECT task_id, branch, start_commit, end_commit FROM attempts ORDER BY id")
        self.assertEqual([(1, "codex/queue-idea-1"), (2, "codex/queue-idea-1")],
                         [attempt[:2] for attempt in attempts])
        self.assertEqual(attempts[0][3], attempts[1][2])
        self.assertNotEqual(attempts[0][2], attempts[0][3])
        self.assertEqual("main", self.git("branch", "--show-current"))
        view = self.cq("status")
        self.assertLess(view.index("\nIdea\n"), view.index("\nToDo\n"))
        self.assertLess(view.index("\nAbort (1)\n"), view.index("\nIdea\n"))
        self.assertLess(view.index("\nToDo\n"), view.index("\nDone\n"))
        self.assertIn("Refused by policy", view)

    def test_next_idea_leaves_previous_queue_branch_for_configured_base(self):
        self.cq("project", "add", "--base", "main", "--planner-model", "project-planner",
                "--allowed-models", "small,large")
        self.cq("idea", "add", "Improve access")
        self.cq("idea", "plan", "1")
        base = self.git("rev-parse", "main")
        self.git("switch", "-qc", "codex/queue-idea-99")
        self.git("commit", "--allow-empty", "-qm", "Previous idea")
        previous = self.git("rev-parse", "HEAD")

        self.run_queue()
        self.wait(["DONE", "ABORTED"])

        self.assertEqual([(base, "main")], self.sql(
            "SELECT start_commit, original_branch FROM attempts WHERE task_id = 1",
        ))
        self.assertEqual(previous, self.git("rev-parse", "codex/queue-idea-99"))
        self.assertEqual("main", self.git("branch", "--show-current"))

    def test_auto_branch_plan_waits_for_approval_and_supports_override(self):
        self.cq("project", "add", "--branch", "auto", "--planner-model", "project-planner",
                "--allowed-models", "small,large")
        self.cq("idea", "add", "Improve access")
        proposal = self.cq("idea", "plan", "1")
        self.assertIn("Suggested branch mode: per-idea", proposal)
        self.assertIn("Review the changes together.", proposal)
        self.assertIn("Add access controls", self.cq("idea", "show", "1"))
        self.assertEqual([], self.sql("SELECT id FROM tasks"))
        (self.root / "fake.py").write_text("raise RuntimeError('Approval must reuse the saved plan')")
        self.cq("idea", "plan", "1", "--approve", "per-task")
        self.assertEqual([("per-task", None)], self.sql("SELECT branch_mode, draft_plan FROM ideas"))
        self.assertEqual([(1,), (2,)], self.sql("SELECT idea_order FROM tasks ORDER BY id"))

    def test_grilling_rounds_require_confirmation_and_create_tasks_once(self):
        from codex_queue.grilling import answer_grilling, confirm_grilling, start_grilling
        from codex_queue.store import open_queue

        self.cq("project", "add")
        self.cq("idea", "add", "Improve access")
        questions = [
            [{"title": "Audience", "body": "Who uses it?", "recommendation": "Yes, admins only."}],
            [{"title": "Location", "body": "Where should it appear?", "recommendation": "Yes, in settings."}],
            [],
        ]
        summary = "Admins can manage access from settings."
        todos = [{"title": "Add access controls", "abstract": "Restrict access", "plan": "Implement and verify access controls", "model": ""}]
        folder = self.root / "CodexQueue"
        with patch("codex_queue.store.queue_folder", return_value=folder), \
             patch("codex_queue.processes.queue_folder", return_value=folder), \
             patch("codex_queue.grilling.generate_round", side_effect=[(questions[0], ""), (questions[1], ""), ([], summary)]) as rounds, \
             patch("codex_queue.grilling.generate_plan", return_value=("", todos)) as planner, \
             open_queue() as db:
            first = start_grilling(db, 1)
            self.assertEqual(("ACTIVE", 1), (first["status"], first["round"]))
            resumed = start_grilling(db, 1)
            self.assertEqual((first["session_id"], first["round"]), (resumed["session_id"], resumed["round"]))
            self.assertEqual([], db.execute("SELECT id FROM tasks").fetchall())
            self.assertEqual(1, rounds.call_count)
            with self.assertRaisesRegex(RuntimeError, "active Grilling"):
                from codex_queue.planner import plan_idea
                plan_idea(db, 1)
            second = answer_grilling(db, first["session_id"], first["round"], "Q1: admins only")
            self.assertEqual(2, second["round"])
            final = answer_grilling(db, second["session_id"], second["round"], "Q1: settings")
            self.assertEqual("CONFIRM", final["status"])
            self.assertIn("Admins can manage access", final["summary"])
            replay = answer_grilling(db, first["session_id"], first["round"], "Q1: admins only")
            self.assertEqual("CONFIRM", replay["status"])
            with self.assertRaisesRegex(RuntimeError, "stale"):
                answer_grilling(db, first["session_id"], first["round"], "different answer")
            self.assertEqual([], db.execute("SELECT id FROM tasks").fetchall())
            created = confirm_grilling(db, final["session_id"], final["round"])
            self.assertEqual("PLANNED", created["status"])
            self.assertEqual([1], created["task_ids"])
            self.assertIn("Admins can manage access", planner.call_args.args[2])
            with self.assertRaisesRegex(RuntimeError, "already created"):
                confirm_grilling(db, final["session_id"], final["round"])
            self.assertEqual(1, db.execute("SELECT count(*) FROM tasks").fetchone()[0])

    def test_grilling_session_is_cancelled_when_idea_is_edited(self):
        from codex_queue.grilling import start_grilling
        from codex_queue.store import open_queue

        self.cq("project", "add")
        self.cq("idea", "add", "Improve access")
        folder = self.root / "CodexQueue"
        with patch("codex_queue.store.queue_folder", return_value=folder), \
             patch("codex_queue.processes.queue_folder", return_value=folder), \
             patch("codex_queue.grilling.generate_round", return_value=([{"title": "Scope", "body": "Which?", "recommendation": "Admins."}], "")), \
             open_queue() as db:
            session = start_grilling(db, 1)
        self.cq("idea", "edit", "1", "--detail", "Updated scope")
        self.assertEqual("CANCELLED", self.sql("SELECT status FROM grilling_sessions WHERE session_id=?", (session["session_id"],))[0][0])

    def test_cancelled_grilling_session_can_be_restarted(self):
        from codex_queue.grilling import cancel_grilling, start_grilling
        from codex_queue.store import open_queue

        self.cq("project", "add")
        self.cq("idea", "add", "Improve access")
        question = [{"title": "Scope", "body": "Who?", "recommendation": "Admins."}]
        folder = self.root / "CodexQueue"
        with patch("codex_queue.store.queue_folder", return_value=folder), \
             patch("codex_queue.processes.queue_folder", return_value=folder), \
             patch("codex_queue.grilling.generate_round", return_value=(question, "")), \
             open_queue() as db:
            first = start_grilling(db, 1)
            cancel_grilling(db, first["session_id"])
            second = start_grilling(db, 1)
            self.assertNotEqual(first["session_id"], second["session_id"])
            self.assertEqual(2, db.execute("SELECT count(*) FROM grilling_sessions WHERE idea_id = 1").fetchone()[0])

    def test_grilling_rechecks_settings_and_reuses_branch_approval(self):
        from codex_queue.grilling import answer_grilling, confirm_grilling, start_grilling
        from codex_queue.planner import plan_idea
        from codex_queue.store import open_queue

        self.cq("project", "add", "--branch", "auto")
        self.cq("idea", "add", "Improve access")
        question = [{"title": "Scope", "body": "Who?", "recommendation": "Admins."}]
        todos = [{"title": "Add access controls", "abstract": "Restrict access", "plan": "Implement controls", "model": ""}]
        folder = self.root / "CodexQueue"
        with patch("codex_queue.store.queue_folder", return_value=folder), \
             patch("codex_queue.processes.queue_folder", return_value=folder), \
             patch("codex_queue.grilling.generate_round", return_value=(question, "")), \
             open_queue() as db:
            session = start_grilling(db, 1)
            db.execute("INSERT INTO settings(key,value) VALUES ('priority','6') ON CONFLICT(key) DO UPDATE SET value='6'")
            db.commit()
            with self.assertRaisesRegex(RuntimeError, "settings changed"):
                answer_grilling(db, session["session_id"], session["round"], "Admins")
            db.execute("UPDATE settings SET value = '5' WHERE key = 'priority'")
            db.commit()
            # Start again after the stale session is cancelled, as a user would after resolving the change.
            db.execute("UPDATE grilling_sessions SET status = 'CANCELLED' WHERE session_id = ?", (session["session_id"],))
            db.commit()
            with patch("codex_queue.grilling.generate_round", return_value=([], "Admins can manage access.")):
                session = start_grilling(db, 1)
            with patch("codex_queue.grilling.generate_plan", return_value=("", todos, "per-task", "Tasks are independent.")) as planner:
                result = confirm_grilling(db, session["session_id"], session["round"])
            self.assertEqual("AWAITING_BRANCH", result["status"])
            self.assertIn("Admins can manage access", planner.call_args.args[2])
            self.assertEqual([], db.execute("SELECT id FROM tasks").fetchall())
            ids = plan_idea(db, 1, "recommended")
            self.assertEqual(1, len(ids))
            self.assertEqual("per-task", db.execute("SELECT branch_mode FROM ideas WHERE id=1").fetchone()[0])

    def test_legacy_unique_grilling_session_schema_migrates_without_losing_rows(self):
        directory = self.root / "legacy-grilling"
        directory.mkdir()
        old_schema = SCHEMA.replace("idea_id INTEGER NOT NULL REFERENCES ideas(id) ON DELETE CASCADE",
                                    "idea_id INTEGER NOT NULL UNIQUE REFERENCES ideas(id) ON DELETE CASCADE")
        old_schema = old_schema.replace(
            "status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'GENERATING', 'CONFIRM', 'PLAN_GENERATING', 'CONFIRMED', 'CANCELLED'))",
            "status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CONFIRM', 'CONFIRMED', 'CANCELLED'))",
        )
        old_schema = old_schema.replace("    board_key TEXT NOT NULL DEFAULT '',\n", "")
        old_schema = old_schema.replace("    project_base TEXT NOT NULL,\n", "")
        old_schema = old_schema.replace("    project_retired INTEGER NOT NULL,\n", "")
        old_schema = old_schema.replace("    nextcloud_sync TEXT NOT NULL,\n", "")
        old_schema = old_schema.replace("    settings_json TEXT NOT NULL,\n", "")
        with sqlite3.connect(directory / "queue.sqlite3") as db:
            db.executescript(old_schema)
            db.execute("INSERT INTO projects(path,path_key,name) VALUES ('repo','repo','Repo')")
            db.execute("INSERT INTO ideas(project_id,title) VALUES (1,'Idea')")
            db.execute("""INSERT INTO grilling_sessions(session_id, idea_id, status, idea_title, idea_detail,
                project_id, project_path, project_prompt, config_json, branch_mode)
                VALUES ('old', 1, 'ACTIVE', 'Idea', '', 1, 'repo', '', '{}', 'per-idea')""")
        db.close()
        with patch("codex_queue.store.queue_folder", return_value=directory), open_queue() as db:
            db.execute("""INSERT INTO grilling_sessions(session_id, idea_id, status, idea_title, idea_detail,
                project_id, project_path, project_prompt, project_base, project_retired, nextcloud_sync,
                config_json, settings_json, branch_mode) VALUES ('new', 1, 'ACTIVE', 'Idea', '', 1,
                'repo', '', '', 0, 'on', '{}', '{}', 'per-idea')""")
            self.assertEqual(["new", "old"], [row[0] for row in db.execute(
                "SELECT session_id FROM grilling_sessions ORDER BY session_id")])
            self.assertEqual("CANCELLED", db.execute(
                "SELECT status FROM grilling_sessions WHERE session_id='old'").fetchone()[0])
            self.assertEqual([], db.execute("PRAGMA foreign_key_check").fetchall())

    def test_legacy_project_branch_constraint_migrates_with_existing_tasks(self):
        directory = self.root / "legacy"
        directory.mkdir()
        with sqlite3.connect(directory / "queue.sqlite3") as db:
            db.executescript(SCHEMA.replace("'per-task', 'per-idea', 'auto', 'current'", "'per-task', 'current'"))
            db.execute("INSERT INTO projects(id, path, path_key, name, branch) VALUES (7, 'repo', 'repo', 'Repo', 'per-task')")
            db.execute("INSERT INTO tasks(project_id, name, priority) VALUES (7, 'Existing', 5)")
            db.execute("UPDATE sqlite_sequence SET seq = 15 WHERE name = 'projects'")
        db.close()
        with patch("codex_queue.store.queue_folder", return_value=directory), open_queue() as db:
            self.assertEqual(("per-task", 7), tuple(db.execute(
                "SELECT p.branch, t.project_id FROM tasks t JOIN projects p ON p.id=t.project_id",
            ).fetchone()))
            self.assertEqual([], db.execute("PRAGMA foreign_key_check").fetchall())
            with db:
                db.execute("UPDATE projects SET branch='auto' WHERE id=7")
                db.execute("INSERT INTO projects(path, path_key, name, branch) VALUES ('next', 'next', 'Next', 'per-idea')")
            self.assertEqual(16, db.execute("SELECT max(id) FROM projects").fetchone()[0])

    def test_automatic_planning_updates_idea_state(self):
        self.cq("project", "add", "--planner-model", "project-planner", "--allowed-models", "small,large")
        self.cq("idea", "add", "Improve access", "--detail", "clarify first")
        self.assertIn("Planned  no", self.cq("idea", "show", "1"))
        self.sql("INSERT INTO settings VALUES ('enabled', '1')")
        self.cq("_worker")
        self.assertEqual([], self.sql("SELECT id FROM tasks"))
        self.sql("UPDATE settings SET value='0' WHERE key='enabled'")
        self.cq("config", "--idea-planning", "auto")
        self.sql("UPDATE settings SET value='1' WHERE key='enabled'")
        self.cq("_worker")
        self.assertIn("Which screen?", self.cq("idea", "show", "1"))
        self.assertEqual([], self.sql("SELECT id FROM tasks"))
        self.cq("idea", "edit", "1", "--detail", "Apply to the settings screen")
        self.wait(["DONE", "ABORTED"])
        self.assertIn("Planned  yes", self.cq("idea", "show", "1"))
        self.assertNotIn("Improve access", self.cq("idea", "ls"))
        self.assertIn("Improve access", self.cq("idea", "ls", "--planned"))
        self.assertIn("\nPlanned\n1  repo  Improve access", self.cq("status"))
        self.assertEqual([(None,)], self.sql("SELECT planning_error FROM ideas"))

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

    def add_task(self, title, priority=5, cwd=None):
        project = Path(cwd or self.repo).resolve()
        self.cq("project", "add", cwd=project)
        self.sql(
            "INSERT INTO tasks(project_id, name, priority) SELECT id, ?, ? FROM projects WHERE path = ?",
            (title, priority, str(project)),
        )

    def test_idea_aliases_and_read_only_tasks(self):
        self.cq("add", "Original", "--detail", "Details")
        self.assertIn("Original", self.cq("idea", "list"))
        self.cq("idea", "edit", "1", "--title", "Updated")
        self.assertIn("Updated", self.cq("list"))
        self.cq("edit", "1", "--detail", "Changed")
        self.assertIn("Changed", self.cq("idea", "show", "1"))
        self.cq("remove", "1")
        self.cq("idea", "add", "Another idea")
        self.cq("idea", "remove", "2")
        self.assertEqual([], self.sql("SELECT id FROM ideas"))
        for action in ("add", "rm", "edit"):
            self.cq("task", action, code=2)
        self.assertEqual([], self.sql("SELECT id FROM tasks"))

    def test_revision_requests_history_before_queuing_replacement(self):
        self.add_task("wait for user")
        self.cq("project", "edit", "1", "--planner-model", "project-planner", "--allowed-models", "small,large")
        self.enable()
        self.cq("_worker")
        self.wait(["WAITING_FOR_INPUT"])
        self.cq("pause")
        self.sql(
            "UPDATE attempts SET report = ? WHERE task_id = 1",
            (json.dumps({"verification": "Prior verification"}),),
        )
        self.assertIn("Please provide the failing trace.", self.cq("todo", "revise", "1", "Fix regression"))
        self.assertEqual([("WAITING_FOR_INPUT",)], self.sql("SELECT status FROM tasks"))
        trace = self.root / "trace.txt"
        trace.write_text("trace from user", encoding="utf-8")
        self.cq("task", "revise", "1", "Fix regression", "--context-file", str(trace))
        self.assertEqual([("SUPERSEDED", None), ("QUEUED", 1)], self.sql(
            "SELECT status, revision_of FROM tasks ORDER BY id",
        ))
        self.assertIn("Revises ToDo  1", self.cq("todo", "show", "2"))
        self.enable()
        self.cq("_worker")
        self.wait(["SUPERSEDED", "DONE"])
        self.assertEqual("main", self.git("branch", "--show-current"))

    def test_priority_and_completion(self):
        self.add_task("second")
        self.add_task("first", priority=1)
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
        self.add_task("first task")
        self.assertIn("Automatic execution enabled", self.cq("start"))
        self.wait(["DONE"])
        self.cq("pause")

    def test_rate_limit_text_from_codex_cli(self):
        message = "You\u2019ve hit your usage limit. Upgrade to Pro or try again at May 18th, 2033 12:33 PM."
        result = classify([{"type": "turn.failed", "error": {"message": message}}], "")
        self.assertEqual(("RATE_LIMITED", datetime(2033, 5, 18, 12, 34).timestamp()), result)

    def test_question_and_resume(self):
        self.add_task("wait for user")
        self.add_task("later")
        self.enable()
        self.cq("_worker")
        self.wait(["WAITING_FOR_INPUT", "QUEUED"])
        self.cq("_worker", "--scheduled")
        self.wait(["WAITING_FOR_INPUT", "QUEUED"])
        self.assertIn("What next?", self.cq("task", "list"))
        status = self.cq("status")
        self.assertLess(status.index("\nWaiting response (1)\n"), status.index("\nToDo\n"))
        self.assertIn("What next?", status)
        self.assertIn('cq reply 1 "..."', status)
        self.cq("pause")
        self.cq("reply", "1", "test answer")
        self.wait(["RESUME_QUEUED", "QUEUED"])
        self.assertNotIn("Waiting response", self.cq("status"))
        self.enable()
        self.cq("_worker")
        self.wait(["DONE", "DONE"])
        self.assertEqual([(1,)], self.sql("SELECT count(*) FROM attempts WHERE task_id=1"))

    def test_limit_waits_for_schedule(self):
        self.add_task("limit task")
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
        self.add_task("auth task")
        self.add_task("later")
        self.enable()
        self.cq("_worker")
        self.wait(["AUTH_REQUIRED", "QUEUED"])
        self.cq("_worker", "--scheduled")
        self.wait(["AUTH_REQUIRED", "QUEUED"])
        (self.root / "authorized").touch()
        self.cq("_worker", "--scheduled")
        self.wait(["DONE", "DONE"])

    def test_pause_waits_without_starting_next_task(self):
        self.add_task("pause task")
        self.add_task("later")
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

    def test_worker_preserves_report_when_codex_hangs_after_completion(self):
        self.add_task("shutdown hang")
        self.enable()
        with patch.dict(os.environ, {"CQ_WORKER_DATA_DIR": str(self.database.parent)}), \
                patch("codex_queue.worker.SHUTDOWN_GRACE_SECONDS", 0.2):
            run_worker(False)
        self.wait(["NEEDS_REVIEW"])
        reason, report = self.sql("SELECT reason, report FROM attempts")[0]
        self.assertEqual("Rendering is still slow", reason)
        self.assertEqual("fake verification", json.loads(report)["verification"])
        run = self.database.parent / "logs" / "1" / "1" / "1"
        exit_result = json.loads((run / "exit.json").read_text())
        self.assertTrue(exit_result["shutdown_timeout"])
        self.assertNotEqual(0, exit_result["code"])

        # Simulate a worker exit after saving the report but before recording the result.
        if os.name == "nt":
            (run / "exit.json").unlink()
            self.sql("UPDATE tasks SET status='RUNNING'")
            self.sql("UPDATE attempts SET reason=NULL, report=NULL")
            self.cq("_worker")
            self.wait(["NEEDS_REVIEW"])
            self.assertEqual([(reason, report)], self.sql("SELECT reason, report FROM attempts"))

    def test_stop_preserves_branch_and_prevents_new_launch(self):
        self.add_task("sleep task")
        self.add_task("later")
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
        self.add_task("sleep task")
        self.add_task("same repository")
        self.add_task("other repository", cwd=other)
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
