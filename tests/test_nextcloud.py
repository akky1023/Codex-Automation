import sqlite3
import io
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest
from unittest.mock import patch

from codex_queue.nextcloud import Client, apply_snapshot, completed_body, todo_fields, writeback
from codex_queue.nextcloud_board import available_projects, board_snapshot, exchange, grilling_active, request_action
from codex_queue.store import SCHEMA
from codex_queue.progress import output_to


class CommunicationProgressTest(unittest.TestCase):
    def test_delayed_request_animates_and_preserves_response(self):
        import threading
        from codex_queue import progress

        class Display(io.StringIO):
            def isatty(self):
                return True

        display = Display()
        updated = threading.Event()
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.status, response.headers = 200, {'ETag': 'one'}

        def read(limit):
            self.assertTrue(updated.wait(2))
            self.assertIn('Communicating with Nextcloud', display.getvalue())
            return b'payload'

        response.read.side_effect = read
        original = progress._draw
        count = 0

        def draw():
            nonlocal count
            original()
            count += 1
            if count >= 2:
                updated.set()

        client = Client('https://example.test')
        with patch.object(client.opener, 'open', return_value=response), output_to(display), patch.object(
            progress, '_draw', side_effect=draw,
        ):
            self.assertEqual((200, {'ETag': 'one'}, b'payload'), client.request('GET', '/tasks'))
        self.assertFalse(progress._active)

    def test_request_failure_ends_plain_log_without_ansi(self):
        from urllib.error import URLError

        display = io.StringIO()
        client = Client('https://example.test')
        with output_to(display), patch.object(client.opener, 'open', side_effect=URLError('offline')):
            with self.assertRaisesRegex(RuntimeError, 'connection failed'):
                client.request('GET', '/tasks')
        self.assertIn('interrupted or failed\n', display.getvalue())
        self.assertNotIn('\r', display.getvalue())
        self.assertNotIn('\x1b', display.getvalue())


BODY = (
    'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\nUID:item\r\n'
    'DESCRIPTION;ALTREP="https://example.test/details":line one\\nline\r\n two\r\n'
    'STATUS:NEEDS-ACTION\r\nBEGIN:VALARM\r\nDESCRIPTION:alarm\r\nEND:VALARM\r\n'
    'END:VTODO\r\nEND:VCALENDAR\r\n'
)


class BoardProjectTest(unittest.TestCase):
    def test_grilling_short_poll_is_limited_to_active_sessions(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        db.execute("INSERT INTO projects(path,path_key,name) VALUES ('one','one','One')")
        db.execute("INSERT INTO ideas(project_id,title,detail) VALUES (1,'Idea','')")
        self.assertFalse(grilling_active(db))
        db.execute("""INSERT INTO grilling_sessions(session_id,idea_id,board_key,status,idea_title,idea_detail,
                   project_id,project_path,project_prompt,project_base,project_retired,nextcloud_sync,
                   config_json,settings_json,branch_mode) VALUES
                   ('session',1,'owner','ACTIVE','Idea','','1','one','','main',0,'on','{}','{}','per-task')""")
        self.assertTrue(grilling_active(db))
        db.execute("UPDATE grilling_sessions SET status='CANCELLED'")
        self.assertFalse(grilling_active(db))

    def test_grilling_session_is_visible_only_to_its_board_owner(self):
        from codex_queue.grilling import start_grilling

        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        db.execute("INSERT INTO projects(path,path_key,name) VALUES ('one','one','One')")
        db.execute("INSERT INTO ideas(project_id,title,detail) VALUES (1,'Idea','')")
        db.commit()
        projects = available_projects(db)
        with TemporaryDirectory() as folder, \
             patch("codex_queue.store.queue_folder", return_value=Path(folder)), \
             patch("codex_queue.processes.queue_folder", return_value=Path(folder)), \
             patch("codex_queue.grilling.generate_round", return_value=([{
                 "title": "Scope", "body": "Which?", "recommendation": "Admins."
             }], "")):
            result = start_grilling(db, 1, "board-owner")
            self.assertEqual("ACTIVE", result["status"])
            self.assertIsNone(board_snapshot(db, projects, "other-owner")["ideas"][0]["grilling"])
            self.assertEqual(result["session_id"], board_snapshot(db, projects, "board-owner")["ideas"][0]["grilling"]["session_id"])
            request = {"kind": "grilling_answer", "target_id": 1, "title": "", "body": "admins",
                       "session_id": result["session_id"], "round_no": result["round"]}
            with self.assertRaisesRegex(RuntimeError, "do not match"):
                request_action(db, request, projects, ("other-owner", 1))

    def test_watch_defers_plan_and_reports_completion_on_next_poll(self):
        self._exercise_deferred_request("plan", fail=False)

    def test_watch_defers_revision_and_reports_failure_on_next_poll(self):
        self._exercise_deferred_request("revise", fail=True)

    def _exercise_deferred_request(self, kind, fail):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "queue.sqlite3"
            db = sqlite3.connect(path, timeout=5)
            db.row_factory = sqlite3.Row
            db.executescript(SCHEMA)
            db.execute("INSERT INTO projects(path,path_key,name) VALUES ('one','one','One')")
            db.execute("INSERT INTO ideas(project_id,title,detail) VALUES (1,'Idea','')")
            if kind == "revise":
                db.execute("INSERT INTO tasks(project_id,name,priority,status) VALUES (1,'Task',1,'QUEUED')")
            db.commit()
            entered, release = Event(), Event()
            calls = []
            target = 1
            incoming = {"instance": "nc", "owner": kind, "requests": [
                {"id": 7, "kind": kind, "target_id": target, "title": "", "body": "change"},
                {"id": 8, "kind": "sync", "target_id": 0, "title": "", "body": ""},
            ]}

            class MockClient:
                def __init__(self):
                    self.posts = []

            client = MockClient()

            def fake_ocs(_client, method, data=None):
                if method == "GET":
                    return incoming
                client.posts.append(data)
                return {}

            @contextmanager
            def worker_connection():
                worker_db = sqlite3.connect(path, timeout=5)
                worker_db.row_factory = sqlite3.Row
                try:
                    yield worker_db
                finally:
                    worker_db.close()

            def slow_action(worker_db, request, projects, key):
                if request["kind"] == "sync":
                    return "同期しました。"
                calls.append(request["kind"])
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("test release timed out")
                if fail:
                    raise RuntimeError("model failed")
                return "To Do created"

            with patch("codex_queue.nextcloud_board.ocs", side_effect=fake_ocs), \
                    patch("codex_queue.nextcloud_board.open_queue", worker_connection), \
                    patch("codex_queue.nextcloud_board.request_action", side_effect=slow_action):
                exchange(db, client, available_projects(db), watch=True)
                self.assertTrue(entered.wait(2))
                self.assertEqual([8], [item["id"] for item in client.posts[0]["results"]])
                self.assertEqual(["Idea"], [idea["title"] for idea in client.posts[0]["snapshot"]["ideas"]])
                exchange(db, client, available_projects(db), watch=True)
                self.assertEqual([8], [item["id"] for item in client.posts[1]["results"]])
                self.assertEqual([kind], calls)
                release.set()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    exchange(db, client, available_projects(db), watch=True)
                    if any(item["id"] == 7 for item in client.posts[-1]["results"]):
                        break
                    time.sleep(.01)
                result = next(item for item in client.posts[-1]["results"] if item["id"] == 7)
                self.assertIn("model failed" if fail else "To Do created", result["outcome"])
                self.assertEqual("同期しました。", next(item["outcome"] for item in client.posts[0]["results"] if item["id"] == 8))
            db.close()

    def test_one_shot_exchange_waits_for_plan_result(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        db.execute("INSERT INTO projects(path,path_key,name) VALUES ('one','one','One')")
        db.execute("INSERT INTO ideas(project_id,title,detail) VALUES (1,'Idea','')")
        db.commit()
        client = type("Client", (), {})()
        client.posts = []
        incoming = {"instance": "nc", "owner": "me", "requests": [
            {"id": 1, "kind": "plan", "target_id": 1, "title": "", "body": ""},
        ]}

        def fake_ocs(_client, method, data=None):
            if method == "GET":
                return incoming
            client.posts.append(data)
        with patch("codex_queue.nextcloud_board.ocs", side_effect=fake_ocs), \
                patch("codex_queue.nextcloud_board.request_action", return_value="planned"):
            exchange(db, client, available_projects(db), watch=False)
        self.assertEqual("planned", client.posts[0]["results"][0]["outcome"])

    def test_board_can_resolve_reviewed_tasks_and_reject_stale_actions(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        with db:
            db.execute("INSERT INTO projects(path,path_key,name) VALUES ('one','one','One')")
            db.execute("INSERT INTO settings VALUES ('enabled','1')")
            db.execute("""INSERT INTO tasks(project_id,name,priority,status) VALUES
                       (1,'Resolved',1,'NEEDS_REVIEW'), (1,'Discarded',1,'NEEDS_REVIEW')""")
            db.execute("""INSERT INTO attempts(task_id,number,path,mode,prompt) VALUES
                       (1,1,'one','current',''), (2,1,'one','current','')""")
        projects = available_projects(db)
        done = {"kind": "done", "target_id": 1, "title": "", "body": ""}
        abort = {"kind": "abort", "target_id": 2, "title": "", "body": ""}
        with patch("codex_queue.actions.wake"):
            self.assertIn("完了", request_action(db, done, projects, ("board", 1)))
            self.assertIn("中止", request_action(db, abort, projects, ("board", 2)))
            with self.assertRaisesRegex(RuntimeError, "status changed|状態が変わりました"):
                request_action(db, {**done, "target_id": 2}, projects, ("board", 3))
        self.assertEqual(["DONE", "ABORTED"], [
            row["status"] for row in db.execute("SELECT status FROM tasks ORDER BY id")
        ])
        self.assertEqual("0", db.execute("SELECT value FROM settings WHERE key='enabled'").fetchone()[0])

    def test_project_sync_defaults_on_and_updates_the_published_snapshot(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        db.execute("INSERT INTO projects(path,path_key,name) VALUES ('one','one','One')")
        db.execute("INSERT INTO ideas(project_id,title,detail) VALUES (1,'Idea','')")
        self.assertEqual({1: "One"}, available_projects(db))
        self.assertEqual(1, len(board_snapshot(db, available_projects(db))["ideas"]))

        db.execute("UPDATE projects SET nextcloud_sync = 'off' WHERE id = 1")
        self.assertEqual({}, available_projects(db, {1}))
        snapshot = board_snapshot(db, available_projects(db))
        self.assertEqual({}, snapshot["projects"])
        self.assertEqual([], snapshot["ideas"])
        self.assertEqual([], snapshot["tasks"])

        db.execute("UPDATE projects SET nextcloud_sync = 'on' WHERE id = 1")
        self.assertEqual({1: "One"}, available_projects(db))


    def test_board_retries_idea_writes_and_publish_without_duplicate_mutations(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        with db:
            db.execute("INSERT INTO projects(path,path_key,name) VALUES ('one','one','One')")
        projects = available_projects(db)
        requests = [
            {"id": 1, "kind": "add", "target_id": 1, "title": "First", "body": ""},
            {"id": 2, "kind": "edit", "target_id": 1, "title": "Latest", "body": "Updated"},
        ]
        published = []

        def sync(_client, method, data=None):
            if method == "GET":
                return {"owner": "test", "instance": "test", "requests": requests}
            published.append(data)
            if len(published) == 2:
                raise OSError("connection dropped")

        with patch("codex_queue.nextcloud_board.ocs", side_effect=sync), patch("codex_queue.nextcloud_board.wake"):
            with patch("codex_queue.nextcloud_board.request_action", side_effect=sqlite3.OperationalError("database locked")):
                with self.assertRaisesRegex(RuntimeError, "database locked"):
                    exchange(db, None, projects)
            self.assertEqual([], published[0]["results"])
            self.assertEqual([], published[0]["snapshot"]["ideas"])
            with self.assertRaisesRegex(OSError, "connection dropped"):
                exchange(db, None, projects)
            exchange(db, None, projects)

        self.assertEqual(published[1], published[2])
        self.assertEqual([1, 2], [result["id"] for result in published[2]["results"]])
        self.assertEqual([(1, "Latest", "Updated")], [
            tuple(row) for row in db.execute("SELECT id, title, detail FROM ideas")
        ])


class CalDAVTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        with self.db:
            self.db.execute("INSERT INTO projects(path,path_key,name) VALUES ('test','test','test')")
            self.db.execute("INSERT INTO remote_links VALUES ('inbox',1)")
            self.links = {"inbox": 1}
            self.snapshot = {"item": [("inbox", "https://example.test/item", '"original"', todo_fields(BODY))]}
            apply_snapshot(self.db, "test", self.links, self.snapshot, False)

    def test_failure_and_duplicate_do_not_withdraw_task(self):
        with self.db:
            apply_snapshot(self.db, "test", self.links, {}, True)
            self.assertEqual(1, self.db.execute("SELECT blocked FROM remote_tasks").fetchone()[0])
            apply_snapshot(self.db, "test", self.links, {"item": self.snapshot["item"] * 2}, False)
            self.assertEqual("QUEUED", self.db.execute("SELECT status FROM tasks").fetchone()[0])
            apply_snapshot(self.db, "test", self.links, {}, False)
            self.assertEqual("ABORTED", self.db.execute("SELECT status FROM tasks").fetchone()[0])

    def test_conflict_and_explicit_completion(self):
        with self.db:
            self.db.execute("UPDATE tasks SET status='DONE', completed_at='2026-11-12 00:00:00'")
        row = self.db.execute(
            "SELECT r.*, t.completed_at FROM remote_tasks r JOIN tasks t ON t.id=r.task_id"
        ).fetchone()

        class MockClient:
            def __init__(self):
                self.updates = []

            def request(self, method, url, body=None, headers=None, acceptable=()):
                if method == "GET":
                    return 200, {"ETag": '"changed"'}, BODY.encode()
                self.updates.append((body, headers))
                return 204, {}, b""

        client = MockClient()
        self.assertEqual("conflict", writeback(self.db, client, row))
        self.assertEqual([], client.updates)
        self.assertEqual("synced", writeback(self.db, client, row, force=True))
        payload, headers = client.updates[0]
        self.assertEqual('"changed"', headers["If-Match"])
        self.assertEqual("COMPLETED", todo_fields(payload.decode())["STATUS"])
        self.assertEqual("DONE", self.db.execute("SELECT status FROM tasks").fetchone()[0])

    def test_done_task_is_not_requeued_and_remote_url_can_follow_a_move(self):
        with self.db:
            self.db.execute("UPDATE tasks SET status='DONE'")
            apply_snapshot(self.db, "test", self.links, {"item": [
                ("inbox", "https://example.test/moved", '"new"', todo_fields(BODY)),
            ]}, False)
        self.assertEqual([("DONE",)], [tuple(row) for row in self.db.execute("SELECT status FROM tasks")])
        self.assertTrue(self.db.execute("SELECT url FROM remote_tasks").fetchone()[0].endswith("/moved"))

    def test_completion_preserves_description_folding_and_alarm(self):
        result = completed_body(BODY, "20261112T000000Z")
        self.assertIn('line\r\n two', result)
        self.assertIn("BEGIN:VALARM\r\nDESCRIPTION:alarm\r\nEND:VALARM", result)
        self.assertEqual(todo_fields(BODY)["DESCRIPTION"], todo_fields(result)["DESCRIPTION"])

    def test_credentials_cannot_follow_cross_origin_links(self):
        client = Client("https://example.test")
        with self.assertRaises(RuntimeError):
            client.href("https://other.test/calendar")
        with self.assertRaises(RuntimeError):
            client.href("http://example.test/calendar")
