import json
import time

from .runtime import request_worker
from .store import get_setting, set_setting, update, write
from .worker import HOLD, attempt_folder, latest_attempt


def wake(db):
    if get_setting(db, "enabled") != "1":
        return
    if db.execute("SELECT 1 FROM tasks WHERE status IN (?,?,?,?,?)", HOLD).fetchone():
        print("Queue is waiting. Run cq ls for details.")
        return
    try:
        requested = request_worker(db)
    except OSError:
        requested = False
    print("Worker requested." if requested else "Worker will check the queue on its next scheduled run.")


def queue_control(db, command):
    if command in ("start", "resume"):
        with write(db):
            if get_setting(db, "stop") == "1" and db.execute(
                "SELECT 1 FROM tasks WHERE status = 'RUNNING'"
            ).fetchone():
                raise RuntimeError("Wait for the stop request to finish before resuming.")
            set_setting(db, "stop", "0")
            set_setting(db, "enabled", "1")
        print("Automatic execution enabled.")
        wake(db)
        return
    with write(db):
        set_setting(db, "enabled", "0")
        if command == "stop":
            set_setting(db, "stop", "1")
    while db.execute("SELECT 1 FROM tasks WHERE status = 'RUNNING'").fetchone():
        from .processes import process_lock
        from .worker import recover
        with process_lock("worker.lock") as acquired:
            if acquired:
                recover(db)
                break
        time.sleep(0.2)
    print("Queue stopped." if command == "stop" else "Queue paused.")


def task_action(db, args):
    action = args.command
    if action == "logs":
        show_logs(db, args.id)
        return
    with write(db):
        task = db.execute("SELECT status FROM tasks WHERE id = ?", (args.id,)).fetchone()
        if task is None:
            raise RuntimeError(f"Unknown task ID {args.id}")
        status = task["status"]
        attempt = latest_attempt(db, args.id)
        if status in ("RUNNING", "DONE", "REMOVED") or attempt is None:
            raise RuntimeError(f"Cannot {action} task {args.id} in state {status}.")
        note = None
        if action == "reply":
            if status != "WAITING_FOR_INPUT" or not attempt["session"]:
                raise RuntimeError("The task has no waiting Codex session.")
            if not args.answer.strip():
                raise RuntimeError("Enter an answer.")
            note = args.answer
            update(db, "attempts", attempt["id"], {"answer": note})
            target = "RESUME_QUEUED"
        elif action == "retry":
            if status == "RATE_LIMITED":
                raise RuntimeError("This task will retry on a scheduled run after the usage limit wait.")
            if status not in ("NEEDS_REVIEW", "INTERRUPTED", "AUTH_REQUIRED", "RETRY_WAIT", "ABORTED"):
                raise RuntimeError(f"Cannot retry a task in state {status}.")
            if args.fresh:
                target = "QUEUED"
            elif attempt["session"] or attempt["phase"] in ("PREPARING", "READY", "REPORTED", "RESTORE"):
                target = "RESUME_QUEUED"
                update(db, "attempts", attempt["id"], {"retry_at": None, "failures": 0})
            else:
                raise RuntimeError("No resumable session. Check the logs and use cq retry --fresh.")
        elif action == "abort":
            if status not in ("NEEDS_REVIEW", "WAITING_FOR_INPUT"):
                raise RuntimeError("Only tasks needing review or input can be aborted.")
            if get_setting(db, "on_abort", "pause") == "pause":
                set_setting(db, "enabled", "0")
            target = "ABORTED"
        else:
            if status == "QUEUED":
                raise RuntimeError("Only a stopped, started task can be marked done.")
            target = "DONE"
            note = args.note
        db.execute(
            """UPDATE tasks SET status = ?, completed_at =
               CASE WHEN ? = 'DONE' THEN CURRENT_TIMESTAMP ELSE NULL END WHERE id = ?""",
            (target, target, args.id),
        )
        db.execute("INSERT INTO task_actions(task_id, action, note) VALUES (?, ?, ?)", (args.id, action, note))
    print(f"Task {args.id}: {target}")
    wake(db)


def show_logs(db, task_id):
    task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        raise RuntimeError(f"Unknown task ID {task_id}")
    print(f"{task['id']}  {task['status']}  {task['name']}")
    if task["completed_at"]:
        print(f"Completed: {task['completed_at']} UTC")
    for attempt in db.execute("SELECT * FROM attempts WHERE task_id = ? ORDER BY number", (task_id,)):
        print(f"Attempt {attempt['number']}  {attempt['started_at']}  {attempt['phase']}")
        print(f"  Repository: {attempt['path']}")
        print(f"  Base: {attempt['base']}  Commit: {attempt['start_commit']}")
        print(f"  Branch: {attempt['branch']}  Final commit: {attempt['end_commit']}")
        print(f"  Session: {attempt['session']}  Logs: {attempt_folder(attempt)}")
        if attempt["reason"]:
            print(f"  {attempt['reason']}")
        if attempt["report"]:
            report = json.loads(attempt["report"])
            print(f"  {report['status']}: {report['reason']}")
            print(f"  Verification: {report['verification']}")
    for row in db.execute("SELECT * FROM task_actions WHERE task_id = ? ORDER BY rowid", (task_id,)):
        print(f"{row['created_at']}  {row['action']}  {row['note'] or ''}")
