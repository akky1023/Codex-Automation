from argparse import Namespace
from getpass import getpass
import json
import sqlite3
from subprocess import SubprocessError
from threading import Lock, Thread
import time
from urllib.request import ProxyHandler, build_opener

from .configuration import current_values, save_values
from .actions import task_action, wake
from .nextcloud import Client, RejectRedirects, connection
from .planner import plan_idea, revise_task
from .grilling import answer_grilling, cancel_grilling, confirm_grilling, start_grilling
from .processes import process_lock
from .store import open_queue, write
from .worker import latest_attempt

_ASYNC_PENDING = "__GRILLING_PENDING__"
BOARD_POLL_SECONDS = 5
GRILLING_POLL_SECONDS = 1


PENDING_OUTCOME = "処理中です。"
_running_lock = Lock()
_running_requests = set()


class LocalBoardClient(Client):
    """Allow HTTP only for the explicitly selected local Docker instance."""

    def __init__(self, port, auth):
        self.server = f"http://127.0.0.1:{port}"
        self.auth = auth
        self.opener = build_opener(ProxyHandler({}), RejectRedirects())

    def href(self, value, base=None):
        if not value.startswith(self.server + "/"):
            raise RuntimeError("Local board request is outside the development instance.")
        return value


def add_board_command(commands):
    parser = commands.add_parser("nextcloud-board", help="Synchronize a Nextcloud Codex Queue board")
    add_board_options(parser)


def add_board_options(parser):
    parser.add_argument("--project-id", type=int, action="append",
                        help="Limit synchronization to these projects (default: all enabled projects)")
    parser.add_argument("--watch", action="store_true", help="Keep checking for board requests")
    parser.add_argument("--dev-port", type=int, help="Use a local HTTP development instance")


def ocs(client, method, data=None):
    url = client.server + "/ocs/v2.php/apps/codex_queue/api/v1/bridge?format=json"
    _, _, body = client.request(
        method, url, None if data is None else json.dumps(data).encode("utf-8"),
        {"OCS-APIRequest": "true", "Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        response = json.loads(body)["ocs"]
        if response["meta"]["statuscode"] not in (100, 200):
            raise ValueError("invalid OCS status")
        return response["data"]
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError("Unexpected response from the Nextcloud board.") from error


def board_snapshot(db, projects, board_key=""):
    marks = ", ".join("?" for _ in projects)
    ideas = [dict(row) for row in db.execute(
        f"""SELECT id, project_id, title, detail, planned_at, planning_error, draft_plan FROM ideas
            WHERE project_id IN ({marks}) ORDER BY id DESC""", tuple(projects),
    )]
    for idea in ideas:
        session = db.execute(
            "SELECT session_id, status, round_no, questions_json, summary FROM grilling_sessions "
            "WHERE idea_id = ? AND board_key = ? AND status != 'CANCELLED' ORDER BY rowid DESC LIMIT 1",
            (idea["id"], board_key),
        ).fetchone()
        idea["grilling"] = None if session is None or session["status"] == "CANCELLED" else {
            "session_id": session["session_id"], "status": session["status"], "round": session["round_no"],
            "questions": json.loads(session["questions_json"]), "summary": session["summary"],
        }
    tasks = []
    for row in db.execute(
        f"""SELECT id, project_id, idea_id, name, abstract, prompt, status, completed_at FROM tasks
            WHERE project_id IN ({marks}) AND status NOT IN ('REMOVED', 'SUPERSEDED')
            ORDER BY priority, id DESC""", tuple(projects),
    ):
        task = dict(row)
        attempt = latest_attempt(db, row["id"])
        if attempt:
            task["reason"] = attempt["reason"]
            task["report"] = attempt["report"]
        tasks.append(task)
    return {"projects": projects, "ideas": ideas, "tasks": tasks,
            "configuration": {"global": current_values(db),
                              "projects": {pid: current_values(db, pid) for pid in projects}}}


def request_action(db, request, projects, key):
    kind, target = request["kind"], int(request["target_id"])
    title, body = request["title"], request["body"]
    if kind == "config":
        try:
            payload = json.loads(body)
            values, expected = payload["changes"], payload["expected"]
        except (ValueError, KeyError, TypeError) as error:
            raise RuntimeError("設定依頼の形式が不正です。") from error
        if not isinstance(values, dict) or not values or not isinstance(expected, dict) or values.keys() != expected.keys():
            raise RuntimeError("変更項目と編集開始時の値を指定してください。")
        with write(db):
            if target < 0 or (target and target not in available_projects(db, set(projects))):
                raise RuntimeError("同期対象の Project が見つかりません。")
            current = current_values(db, target)
            if not values.keys() <= current.keys():
                raise RuntimeError("未知の設定項目です。")
            if any(current[field] != expected[field] for field in values):
                raise RuntimeError("競合しました。最新の値を読み直して編集してください。")
            saved = save_values(db, values, target)
            save_outcome(db, key, "設定を保存しました。")
        if not target and saved.get("idea_planning") == "auto":
            wake(db)
        return "設定を保存しました。"
    if kind == "sync":
        return "同期しました。"
    if kind == "grilling_start":
        idea = db.execute("SELECT project_id FROM ideas WHERE id = ? AND planned_at IS NULL", (target,)).fetchone()
        if idea is None or idea["project_id"] not in projects:
            raise RuntimeError("同期対象のプロジェクトを選んでください。")
        result = start_grilling(db, target, key[0])
        return f"Grilling {result['status']} / round {result['round']}"
    if kind in ("grilling_answer", "grilling_confirm", "grilling_cancel", "approve_branch"):
        session_id = request.get("session_id")
        session = db.execute("SELECT idea_id, board_key, status FROM grilling_sessions WHERE session_id = ?",
                             (session_id,)).fetchone()
        if session is None or session["idea_id"] != target or session["board_key"] != key[0]:
            raise RuntimeError("Grilling session and Idea do not match this board.")
        if kind == "grilling_answer":
            result = answer_grilling(db, session_id, int(request.get("round_no") or 0), body, key[0])
            return f"Grilling {result['status']} / round {result['round']}"
        if kind == "grilling_cancel":
            cancel_grilling(db, session_id, key[0])
            return "Grilling を中止しました。"
        if kind == "grilling_confirm":
            result = confirm_grilling(db, session_id, int(request.get("round_no") or 0), board_key=key[0])
            if result["status"] == "AWAITING_BRANCH":
                return "ブランチ方式の承認待ちです。"
            return "ToDo " + ", ".join(map(str, result["task_ids"])) + " を作成しました。"
        if kind == "approve_branch":
            if session["status"] != "CONFIRMED":
                raise RuntimeError("仕様確認済みの Grilling session が見つかりません。")
            ids = plan_idea(db, target, request.get("branch_mode"))
            return "ToDo " + ", ".join(map(str, ids)) + " を作成しました。"
    if kind == "add":
        if target not in projects:
            raise RuntimeError("同期対象のプロジェクトを選んでください。")
        with write(db):
            db.execute("INSERT INTO ideas(project_id, title, detail) VALUES (?, ?, ?)", (target, title, body))
            save_outcome(db, key, "Idea を追加しました。")
        return "Idea を追加しました。"
    if kind in ("edit", "remove", "plan"):
        with write(db):
            idea = db.execute("SELECT * FROM ideas WHERE id = ?", (target,)).fetchone()
            if idea is None or idea["project_id"] not in projects or idea["planned_at"]:
                raise RuntimeError("未計画の Idea が見つかりません。")
            if kind == "edit":
                db.execute("UPDATE grilling_sessions SET status = 'CANCELLED' WHERE idea_id = ? AND status != 'CANCELLED'",
                           (target,))
                db.execute("UPDATE ideas SET title = ?, detail = ?, planning_error = NULL, draft_plan = NULL WHERE id = ?",
                           (title, body, target))
                save_outcome(db, key, "Idea を更新しました。")
                return "Idea を更新しました。"
            if kind == "remove":
                db.execute("UPDATE grilling_sessions SET status = 'CANCELLED' WHERE idea_id = ? AND status != 'CANCELLED'",
                           (target,))
                db.execute("DELETE FROM ideas WHERE id = ?", (target,))
                save_outcome(db, key, "Idea を削除しました。")
                return "Idea を削除しました。"
        ids = plan_idea(db, target)
        if not ids:
            return f"ブランチ運用の承認待ちです。cq idea show {target} で提案を確認できます。"
        return "To Do " + ", ".join(map(str, ids)) + " を作成しました。"
    task = db.execute("SELECT project_id FROM tasks WHERE id = ?", (target,)).fetchone()
    if task is None or task["project_id"] not in projects:
        raise RuntimeError("同期対象の To Do が見つかりません。")
    if kind == "revise":
        task_id, question = revise_task(db, target, body)
        return question or f"To Do {task_id} を更新しました。"
    if kind in ("reply", "retry", "retry_fresh"):
        task_action(db, Namespace(command="retry" if kind == "retry_fresh" else kind,
                                  id=target, answer=body, fresh=kind == "retry_fresh"))
        return "依頼を処理しました。"
    if kind in ("done", "abort"):
        task_action(db, Namespace(command=kind, id=target, note=body or None), expected_status="NEEDS_REVIEW")
        return f"To Do {target} を{'完了' if kind == 'done' else '中止'}にしました。"
    raise RuntimeError("この操作には対応していません。")


def save_outcome(db, key, outcome):
    db.execute("UPDATE board_receipts SET outcome = ? WHERE board = ? AND request_id = ?", (outcome, *key))


def _run_request(key, request, project_ids):
    try:
        with open_queue() as worker_db:
            projects = available_projects(worker_db, project_ids)
            try:
                outcome = request_action(worker_db, request, projects, key)
            except (OSError, RuntimeError, sqlite3.Error, SubprocessError) as error:
                outcome = f"処理できませんでした。{error}"
            with write(worker_db):
                save_outcome(worker_db, key, outcome[:1800])
    finally:
        with _running_lock:
            _running_requests.discard(key)


def _start_request(db, key, request, project_ids, marker):
    with _running_lock:
        if key in _running_requests:
            return
        outcome = db.execute(
            "SELECT outcome FROM board_receipts WHERE board = ? AND request_id = ?", key,
        ).fetchone()[0]
        if outcome != marker:
            return
        _running_requests.add(key)
    try:
        Thread(target=_run_request, args=(key, request, project_ids), daemon=True).start()
    except BaseException:
        with _running_lock:
            _running_requests.discard(key)
        raise


def exchange(db, client, projects, watch=False):
    incoming = ocs(client, "GET")
    board = json.dumps([incoming["instance"], incoming["owner"]], ensure_ascii=False)
    results = []
    deferred = None
    interrupted = "処理が中断されました。ボードの状態を確認してから、必要なら再度依頼してください。"
    for request in incoming["requests"]:
        request_id = int(request["id"])
        key = (board, request_id)
        kind = request["kind"]
        atomic = kind in ("add", "edit", "remove", "config")
        grilling = kind.startswith("grilling_") or kind == "approve_branch"
        marker = interrupted
        if watch and grilling:
            marker = _ASYNC_PENDING
        elif watch and kind in ("plan", "revise"):
            marker = PENDING_OUTCOME
        try:
            with write(db):
                reserved = db.execute(
                    "INSERT OR IGNORE INTO board_receipts VALUES (?, ?, ?)", (*key, marker),
                ).rowcount
                outcome = db.execute(
                    "SELECT outcome FROM board_receipts WHERE board = ? AND request_id = ?", key,
                ).fetchone()[0]
            if watch and grilling and outcome == _ASYNC_PENDING:
                _start_request(db, key, request, set(projects), marker)
            elif reserved and marker == PENDING_OUTCOME:
                _start_request(db, key, request, set(projects), marker)
            # Idea mutations commit their receipt in the same transaction.
            elif reserved or (atomic and outcome == interrupted):
                try:
                    outcome = request_action(db, request, projects, key)
                except (OSError, RuntimeError, sqlite3.Error, SubprocessError) as error:
                    if atomic and isinstance(error, sqlite3.OperationalError):
                        raise
                    outcome = f"処理できませんでした。{error}"
                with write(db):
                    save_outcome(db, key, outcome[:1800])
            with _running_lock:
                running = key in _running_requests
                outcome = db.execute(
                    "SELECT outcome FROM board_receipts WHERE board = ? AND request_id = ?", key,
                ).fetchone()[0]
            if not running and (outcome == PENDING_OUTCOME or (outcome == _ASYNC_PENDING and not grilling)):
                outcome = interrupted
                with write(db):
                    save_outcome(db, key, outcome)
            if outcome not in (PENDING_OUTCOME, _ASYNC_PENDING):
                results.append({"id": request_id, "outcome": outcome[:1800]})
        except sqlite3.OperationalError as error:
            # Publish completed requests before retrying this request in order.
            deferred = error
            break
    projects = available_projects(db, set(projects))
    ocs(client, "POST", {"snapshot": board_snapshot(db, projects, board), "results": results})
    if any(request["kind"] in ("add", "plan", "edit", "revise", "grilling_start", "grilling_answer", "grilling_confirm", "approve_branch") for request in incoming["requests"]):
        wake(db)
    if deferred is not None:
        raise RuntimeError(f"ボードの同期を再試行します。{deferred}") from deferred


def run_board(db, args):
    project_ids = set(args.project_id) if args.project_id else None
    if project_ids and not project_ids <= {row[0] for row in db.execute("SELECT id FROM projects WHERE retired = 0")}:
        raise RuntimeError("Select active projects with --project-id.")
    local_client = None
    if args.dev_port is not None:
        if not 1 <= args.dev_port <= 65535:
            raise RuntimeError("Invalid development port.")
        username = input("Nextcloud user: ").strip()
        local_client = LocalBoardClient(args.dev_port, (username, getpass("Nextcloud app password: ")))
    with process_lock("nextcloud-board.lock") as acquired:
        if not acquired:
            raise RuntimeError("The Nextcloud board is already synchronizing.")
        while True:
            try:
                client = local_client if local_client is not None else connection(db)[0]
                exchange(db, client, available_projects(db, project_ids), watch=args.watch)
            except (OSError, RuntimeError) as error:
                if not args.watch:
                    raise
                print(f"Nextcloud board: {error}", flush=True)
            if not args.watch:
                return
            try:
                time.sleep(GRILLING_POLL_SECONDS if grilling_active(db, project_ids) else BOARD_POLL_SECONDS)
            except KeyboardInterrupt:
                return


def available_projects(db, project_ids=None):
    return {
        row["id"]: row["name"] for row in db.execute(
            "SELECT id, name FROM projects WHERE retired = 0 AND nextcloud_sync = 'on' ORDER BY id"
        )
        if project_ids is None or row["id"] in project_ids
    }


def grilling_active(db, project_ids=None):
    """Return whether a watched project has a session that needs interaction."""
    projects = available_projects(db, project_ids)
    if not projects:
        return False
    marks = ", ".join("?" for _ in projects)
    row = db.execute(
        f"""SELECT 1 FROM grilling_sessions g JOIN ideas i ON i.id = g.idea_id
            WHERE i.project_id IN ({marks}) AND g.status IN ('ACTIVE', 'GENERATING', 'CONFIRM', 'PLAN_GENERATING')
            LIMIT 1""",
        tuple(projects),
    ).fetchone()
    return row is not None
