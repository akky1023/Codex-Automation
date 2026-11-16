from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from math import isfinite
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from .nextcloud import settle_move, sync
from .planner import plan_next_idea
from .processes import process_lock, spawn, stop_tree, wait_for_previous_job
from .runtime import NO_WINDOW, codex_command, codex_env
from .store import get_setting, open_queue, queue_folder, set_setting, settings, update, write


HOLD = ("NEEDS_REVIEW", "WAITING_FOR_INPUT", "RATE_LIMITED", "AUTH_REQUIRED", "RETRY_WAIT")
RETRY = ("RESUME_QUEUED", "RATE_LIMITED", "AUTH_REQUIRED", "RETRY_WAIT", "INTERRUPTED")
REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["COMPLETED", "ABORTED", "WAITING_FOR_INPUT", "NEEDS_REVIEW"]},
        "reason": {"type": "string"},
        "verification": {"type": "string"},
        "question": {"type": "string"},
    },
    "required": ["status", "reason", "verification", "question"],
    "additionalProperties": False,
}
RECONNECTING = "Reconnecting... waiting for network (Connection failed:"
SHUTDOWN_GRACE_SECONDS = 30
RESET_TIME = re.compile(
    r"try again at ([A-Za-z]+) (\d{1,2})(?:st|nd|rd|th)?, (\d{4}) "
    r"(\d{1,2}):(\d{2}) (AM|PM)", re.IGNORECASE,
)
MONTHS = (
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
)


def git(path, *args):
    result = subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"git {args[0]} failed")
    return result.stdout.rstrip("\r\n")


def branch(path):
    return git(path, "symbolic-ref", "--short", "HEAD")


def clean(path):
    return not git(path, "status", "--porcelain", "--untracked-files=all")


def attempt_folder(attempt, run=None):
    folder = queue_folder() / "logs" / str(attempt["task_id"]) / str(attempt["number"])
    return folder if run is None else folder / str(run)


def latest_attempt(db, task_id):
    return db.execute(
        "SELECT * FROM attempts WHERE task_id = ? ORDER BY number DESC LIMIT 1", (task_id,)
    ).fetchone()


def row_attempt(db, attempt_id):
    return db.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()


def record(db, attempt, status, reason=None, **values):
    with write(db):
        if status == "DONE" and move_requested(db, attempt):
            status = "INTERRUPTED"
            reason = "Nextcloud task moved or was removed."
            values["phase"] = "INTERRUPTED"
        update(db, "attempts", attempt["id"], {"reason": reason, **values})
        if status == "ABORTED" and get_setting(db, "on_abort", "pause") == "pause":
            set_setting(db, "enabled", "0")
        db.execute(
            """UPDATE tasks SET status = ?, completed_at =
               CASE WHEN ? = 'DONE' THEN CURRENT_TIMESTAMP ELSE NULL END WHERE id = ?""",
            (status, status, attempt["task_id"]),
        )
        if status != "DONE":
            settle_move(db, attempt["task_id"])
        else:
            db.execute(
                "UPDATE remote_tasks SET pending_project=NULL, pending_remove=0 WHERE task_id=?",
                (attempt["task_id"],),
            )


def prompt_for(task):
    details = task["prompt"]
    if task["idea_id"] is not None:
        details = (
            f"## Abstract\n{task['abstract']}\n\n"
            f"## Original idea\n{task['idea_title']}\n\n{task['idea_detail']}\n\n"
            f"## Implementation plan\n{details}"
        )
    return (
        f"# Project instructions\n{task['project_prompt']}\n\n"
        f"# Task\n{task['name']}\n\n{details}\n\n"
        "# Execution and reporting\n"
        "Task-specific requirements override project defaults. Follow repository instructions. "
        "Keep your assigned branch checked out. Commit task changes in reviewable commits, "
        "without merging or pushing. Leave the worktree clean on completion. "
        "Report COMPLETED only when the task is complete. Include the reason and verification. "
        "If user input is required, report WAITING_FOR_INPUT and put a concrete question in "
        "question. Otherwise leave question empty. Report ABORTED and explain the reason "
        "when implementation is refused, for example for security reasons. "
        "Report unresolved execution failures as NEEDS_REVIEW."
    )


def claim(db, scheduled, attempted, authenticated, remote_ready):
    with write(db):
        if get_setting(db, "enabled") != "1":
            return None
        config = settings(db)
        rows = db.execute(
            """SELECT t.*, p.path, p.prompt AS project_prompt, p.base, p.branch,
                      p.implementation_model AS project_model,
                      i.title AS idea_title, i.detail AS idea_detail, i.branch_mode AS idea_branch_mode
               FROM tasks t JOIN projects p ON p.id = t.project_id
               LEFT JOIN ideas i ON i.id = t.idea_id
               WHERE t.status NOT IN ('DONE', 'ABORTED', 'REMOVED', 'SUPERSEDED')
               ORDER BY t.priority, t.id"""
        ).fetchall()
        running = {row["path"] for row in rows if row["status"] == "RUNNING"}
        if len(running) >= int(config["parallel"]):
            return None
        if any(row["status"] in HOLD[:2] for row in rows):
            return None
        if any(row["id"] in attempted and row["status"] in ("RATE_LIMITED", "AUTH_REQUIRED") for row in rows):
            return None
        held = any(row["status"] in HOLD for row in rows)
        for row in sorted(rows, key=lambda item: item["status"] not in RETRY):
            status = row["status"]
            if row["id"] in attempted or row["path"] in running:
                continue
            if held and status not in HOLD:
                continue
            if status not in RETRY and status != "QUEUED":
                continue
            configured = row["branch"] or config["branch"]
            mode = row["idea_branch_mode"] or ("current" if configured == "current" else "per-task")
            if mode == "per-idea" and db.execute(
                """SELECT 1 FROM tasks WHERE idea_id = ? AND id != ?
                   AND (coalesce(idea_order, id), id) < (?, ?)
                   AND status NOT IN ('DONE', 'REMOVED', 'SUPERSEDED') LIMIT 1""",
                (row["idea_id"], row["id"], row["idea_order"] or row["id"], row["id"]),
            ).fetchone():
                continue
            remote = db.execute("SELECT * FROM remote_tasks WHERE task_id=?", (row["id"],)).fetchone()
            if remote and (not remote_ready or remote["blocked"] or not get_setting(db, "nextcloud_credential")
                           or remote["connection"] != get_setting(db, "nextcloud_connection")):
                continue
            attempt = latest_attempt(db, row["id"])
            if status in HOLD:
                if not scheduled or attempt["retry_at"] and attempt["retry_at"] > time.time():
                    continue
                if status == "AUTH_REQUIRED" and not authenticated:
                    continue
                if status == "RATE_LIMITED" and attempt["retry_at"] is None and running:
                    continue
            if status == "QUEUED" or attempt is None:
                number = 1 if attempt is None else attempt["number"] + 1
                base = config["base"] if row["base"] is None else row["base"]
                model = row["implementation_model"]
                if model is None:
                    model = config["implementation_model"] if row["project_model"] is None else row["project_model"]
                cursor = db.execute(
                    """INSERT INTO attempts(task_id, number, path, mode, base, prompt, implementation_model)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (row["id"], number, row["path"], mode, base, prompt_for(row), model),
                )
                attempt = row_attempt(db, cursor.lastrowid)
            update(db, "tasks", row["id"], {"status": "RUNNING"})
            return attempt
    return None


def idea_checkout(db, attempt, idea_id, current):
    path = attempt["path"]
    target = f"codex/queue-idea-{idea_id}"
    ref = f"refs/heads/{target}"
    previous = db.execute(
        """SELECT a.original_branch, a.start_commit, a.end_commit, t.status
           FROM attempts a JOIN tasks t ON t.id = a.task_id
           WHERE t.idea_id = ? AND a.path = ? AND a.branch = ? AND a.mode = 'per-idea'
             AND a.id != ? ORDER BY a.id DESC LIMIT 1""",
        (idea_id, path, target, attempt["id"]),
    ).fetchone()
    try:
        tip = git(path, "rev-parse", "--verify", f"{ref}^{{commit}}")
    except RuntimeError:
        tip = None
    if tip and previous is None:
        raise RuntimeError(f"The Idea branch {target} already exists outside the queue.")
    if previous and not tip:
        raise RuntimeError(f"The recorded Idea branch {target} is missing.")
    if current.startswith("codex/queue-") and current != target:
        raise RuntimeError("Switch away from the previous queue branch first.")
    original = previous["original_branch"] if current == target else current
    if tip:
        checkpoint = (previous["end_commit"] if previous["status"] == "DONE" else None) or previous["start_commit"]
        git(path, "merge-base", "--is-ancestor", checkpoint, tip)
        return original, target, target, tip
    base = attempt["base"] or current
    commit = git(path, "rev-parse", "--verify", f"refs/heads/{base}^{{commit}}")
    return original, target, base, commit


def switch_idea_branch(path, current, attempt):
    target, start = attempt["branch"], attempt["start_commit"]
    if not clean(path):
        raise RuntimeError("The worktree has uncommitted or untracked changes.")
    if current == target:
        if git(path, "rev-parse", "HEAD") != start:
            raise RuntimeError("The Idea branch changed before execution.")
        return
    if current != attempt["original_branch"]:
        raise RuntimeError(f"Check out {attempt['original_branch']} to continue preparing the Idea branch.")
    if attempt["base"] == target:
        if git(path, "rev-parse", "--verify", f"refs/heads/{target}^{{commit}}") != start:
            raise RuntimeError("The Idea branch changed before execution.")
        git(path, "switch", target)
    else:
        git(path, "switch", "-c", target, start)


def prepare(db, attempt):
    path = attempt["path"]
    if not Path(path).samefile(git(path, "rev-parse", "--show-toplevel")):
        raise RuntimeError("The recorded path is no longer the root of the Git repository.")
    current = branch(path)
    if attempt["phase"] == "PREPARING":
        if not clean(path):
            raise RuntimeError("The worktree has uncommitted or untracked changes.")
        task = db.execute("SELECT idea_id, revision_of FROM tasks WHERE id = ?", (attempt["task_id"],)).fetchone()
        prior = latest_attempt(db, task["revision_of"]) if task["revision_of"] is not None else None
        continuing = (
            attempt["mode"] == "per-idea" and current == f"codex/queue-idea-{task['idea_id']}"
            or attempt["mode"] == "per-task" and prior and current == prior["branch"]
        )
        configured_base = attempt["base"]
        if (attempt["mode"] in ("per-idea", "per-task") and not continuing
                and configured_base and configured_base != current and not configured_base.startswith("codex/queue-")):
            git(path, "switch", "--no-guess", "--", configured_base)
            current = configured_base
        if attempt["mode"] == "per-idea":
            original, target, base, commit = idea_checkout(db, attempt, task["idea_id"], current)
            create = False
        else:
            if current.startswith("codex/queue-") and not continuing:
                raise RuntimeError("Switch away from the previous queue branch first.")
            original = prior["original_branch"] if continuing else current
            base = current if continuing or attempt["mode"] == "current" else attempt["base"] or current
            if prior and prior["end_commit"] and attempt["mode"] == "per-task":
                base = prior["branch"]
            commit = git(path, "rev-parse", "--verify", f"refs/heads/{base}^{{commit}}")
            create = attempt["mode"] == "per-task"
            target = f"codex/queue-{attempt['task_id']}-{attempt['number']}" if create else current
        with write(db):
            update(db, "attempts", attempt["id"], {
                "original_branch": original, "branch": target, "start_commit": commit,
                "base": base, "phase": "CHECKOUT",
            })
        if attempt["mode"] == "per-idea":
            switch_idea_branch(path, current, row_attempt(db, attempt["id"]))
        elif target != current:
            if create:
                git(path, "switch", "-c", target, commit)
            else:
                git(path, "switch", target)
        with write(db):
            update(db, "attempts", attempt["id"], {"phase": "READY"})
    elif attempt["mode"] == "per-idea" and attempt["phase"] == "CHECKOUT":
        switch_idea_branch(path, current, attempt)
        with write(db):
            update(db, "attempts", attempt["id"], {"phase": "READY"})
    elif current != attempt["branch"]:
        raise RuntimeError(f"Check out the recorded branch {attempt['branch']} to resume.")
    else:
        git(path, "merge-base", "--is-ancestor", attempt["start_commit"], "HEAD")
    return row_attempt(db, attempt["id"])


def finalize(db, attempt):
    path = attempt["path"]
    report = json.loads(attempt["report"]) if attempt["report"] else {}
    aborted = report.get("status") == "ABORTED"
    if not clean(path):
        if aborted:
            record(db, attempt, "ABORTED", report["reason"], phase="FINISHED")
            return
        raise RuntimeError("Codex left uncommitted or untracked changes.")
    current = branch(path)
    if attempt["phase"] == "RESTORE":
        if current == attempt["branch"]:
            if git(path, "rev-parse", "HEAD") != attempt["end_commit"]:
                raise RuntimeError("The task branch moved after completion.")
        elif current != attempt["original_branch"] or git(
            path, "rev-parse", "--verify", f"refs/heads/{attempt['branch']}^{{commit}}"
        ) != attempt["end_commit"]:
            raise RuntimeError("The recorded completed branch could not be verified.")
    else:
        if current != attempt["branch"]:
            raise RuntimeError("The task branch changed during execution.")
        end = git(path, "rev-parse", "HEAD")
        git(path, "merge-base", "--is-ancestor", attempt["start_commit"], end)
        with write(db):
            update(db, "attempts", attempt["id"], {"end_commit": end, "phase": "RESTORE"})
    if attempt["mode"] in ("per-task", "per-idea") and current != attempt["original_branch"]:
        git(path, "switch", attempt["original_branch"])
    record(db, attempt, "ABORTED" if aborted else "DONE",
           report["reason"] if aborted else None, phase="FINISHED")


def classify(events, stderr):
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        error = event.get("error", {})
        if not isinstance(error, dict):
            continue
        info = error.get("codex_error_info", error.get("codexErrorInfo"))
        key = info if isinstance(info, str) else next(iter(info), "") if isinstance(info, dict) else ""
        key = key.replace("_", "").lower()
        if key == "usagelimitexceeded" or error.get("type") == "usage_limit_reached":
            detail = next(iter(info.values()), {}) if isinstance(info, dict) else {}
            if not isinstance(detail, dict):
                detail = {}
            resets = detail.get("resets_at", detail.get("resetsAt", error.get("resets_at")))
            valid_reset = type(resets) in (int, float) and 0 < resets < 253402300799 and isfinite(resets)
            return "RATE_LIMITED", resets if valid_reset else None
        if key == "unauthorized":
            return "AUTH_REQUIRED", None
        if key in {"streamdisconnected", "responsestreamdisconnected", "responseconnectionfailed",
                   "httpconnectionfailed", "internalservererror", "serveroverloaded",
                   "responsetoomanyfailedattempts"}:
            return "RETRY_WAIT", None
    messages = []
    for event in events:
        if isinstance(event, dict):
            messages.append(event.get("message", ""))
            error = event.get("error")
            if isinstance(error, dict):
                messages.append(error.get("message", ""))
    messages += stderr.splitlines()
    for message in messages:
        if not isinstance(message, str):
            continue
        message = message.removeprefix("ERROR: ").strip().replace("\u2019", "'")
        if message.startswith(("You've hit your usage limit", "Usage limit reached.")):
            return "RATE_LIMITED", reset_time(message)
        if message.startswith(("Not logged in", "Your access token could not be refreshed",
                               "unexpected status 401 Unauthorized")):
            return "AUTH_REQUIRED", None
        if message.startswith((RECONNECTING, "stream disconnected before completion:", "error sending request for url")):
            return "RETRY_WAIT", None
    return "NEEDS_REVIEW", None


def reset_time(message):
    match = RESET_TIME.search(message)
    if not match:
        return None
    month, day, year, hour, minute, period = match.groups()
    if not 1 <= int(hour) <= 12:
        return None
    try:
        local = datetime(
            int(year), MONTHS.index(month[:3].lower()) + 1, int(day),
            int(hour) % 12 + (12 if period.upper() == "PM" else 0), int(minute),
        )
        # Codex renders the local time to the minute. Wait through that minute.
        return max(local.timestamp(), local.replace(fold=1).timestamp()) + 60
    except (ValueError, OSError, OverflowError):
        return None


def run_attempt(attempt_id):
    with open_queue() as db:
        attempt = row_attempt(db, attempt_id)
        try:
            if attempt["phase"] in ("REPORTED", "RESTORE"):
                finalize(db, attempt)
                return
            attempt = prepare(db, attempt)
            execute(db, attempt)
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
            record(db, attempt, "NEEDS_REVIEW", str(error))


def move_requested(db, attempt):
    return db.execute(
        """SELECT 1 FROM remote_tasks WHERE task_id=?
           AND (pending_remove=1 OR pending_project IS NOT NULL)""",
        (attempt["task_id"],),
    ).fetchone() is not None


def read_report(path):
    report = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(report, dict)
        or report.keys() != set(REPORT_SCHEMA["required"])
        or any(not isinstance(value, str) for value in report.values())
        or report["status"] not in REPORT_SCHEMA["properties"]["status"]["enum"]
    ):
        raise ValueError("Invalid Codex final report.")
    return report


def write_run_json(path, value):
    marker = path.with_suffix(".tmp")
    marker.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    marker.replace(path)


def execute(db, attempt):
    folder = attempt_folder(attempt)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "prompt.txt").write_text(attempt["prompt"], encoding="utf-8")
    run = max((int(p.name) for p in folder.iterdir() if p.is_dir() and p.name.isdecimal()), default=0) + 1
    folder = attempt_folder(attempt, run)
    folder.mkdir()
    schema = folder / "schema.json"
    schema.write_text(json.dumps(REPORT_SCHEMA), encoding="utf-8")
    output = folder / "final.json"
    stdout = folder / "events.jsonl"
    stderr = folder / "stderr.log"
    command = [*codex_command(db), "-a", "never", "exec", "-s", "workspace-write"]
    if attempt["implementation_model"]:
        command += ["-m", attempt["implementation_model"]]
    if attempt["session"]:
        command += ["resume", attempt["session"]]
    elif attempt["phase"] != "READY":
        raise RuntimeError("The previous execution has no saved Codex session.")
    command += ["--json", "--output-schema", str(schema), "-o", str(output), "-"]
    prompt = attempt["prompt"]
    if attempt["session"]:
        prompt = (
            "Resume the same task. Inspect previous changes and verification before continuing.\n\n"
            + prompt
        )
        if attempt["answer"] is not None:
            prompt += "\n\n# User answer\n" + attempt["answer"]
    (folder / "prompt.txt").write_text(prompt, encoding="utf-8")
    env = codex_env(db)
    process = close_job = None
    interruption = None
    events = []
    completed = failed = started = False
    report = shutdown_deadline = None
    shutdown_timeout = False
    reconnects = 0
    with (folder / "prompt.txt").open("rb") as source, stdout.open("wb") as out, stderr.open("wb") as err:
        try:
            with write(db):
                if move_requested(db, attempt):
                    update(db, "attempts", attempt["id"], {"reason": "Nextcloud task moved or was removed."})
                    settle_move(db, attempt["task_id"])
                    return
                if get_setting(db, "enabled") != "1":
                    update(db, "tasks", attempt["task_id"], {"status": "INTERRUPTED"})
                    update(db, "attempts", attempt["id"], {"reason": "Queue paused before Codex started."})
                    return
                update(db, "attempts", attempt["id"], {"phase": "EXECUTING"})
                process, close_job = spawn(
                    command, attempt["id"], cwd=attempt["path"], env=env, stdin=source, stdout=out, stderr=err,
                )
            with stdout.open("rb") as reader:
                while True:
                    exited = process.poll() is not None
                    while line := reader.readline():
                        if not line.endswith(b"\n") and not exited:
                            reader.seek(-len(line), os.SEEK_CUR)
                            break
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(event, dict):
                            continue
                        kind = event.get("type")
                        if kind == "thread.started" and event.get("thread_id"):
                            with write(db):
                                update(db, "attempts", attempt["id"], {"session": event["thread_id"]})
                        elif kind == "turn.completed":
                            completed = True
                        elif kind == "turn.started" or kind.startswith("item."):
                            started = True
                            reconnects = 0
                        elif kind in ("turn.failed", "error"):
                            events.append(event)
                            failed |= kind == "turn.failed"
                            if str(event.get("message", "")).startswith(RECONNECTING):
                                reconnects += 1
                    if exited:
                        break
                    if move_requested(db, attempt):
                        interruption = "Nextcloud task moved or was removed."
                        break
                    if get_setting(db, "stop") == "1":
                        interruption = "Stopped by the user."
                        break
                    if reconnects >= 3:
                        break
                    if completed and not failed:
                        if report is None:
                            try:
                                report = read_report(output)
                            except (FileNotFoundError, ValueError):
                                pass  # The CLI may still be writing the report.
                            else:
                                write_run_json(folder / "report-ready.json", report)
                                shutdown_deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
                        if shutdown_deadline is not None and time.monotonic() >= shutdown_deadline:
                            shutdown_timeout = process.poll() is None
                            if shutdown_timeout:
                                break
                    time.sleep(0.15)
        finally:
            if process is not None:
                if close_job or process.poll() is None:
                    stop_tree(process, close_job)
                process.wait()
    if interruption or move_requested(db, attempt):
        record(db, attempt, "INTERRUPTED", interruption or "Nextcloud task moved or was removed.")
        return
    write_run_json(folder / "exit.json", {"code": process.returncode, "shutdown_timeout": shutdown_timeout})
    process_result(db, attempt, folder, process.returncode, events, completed, failed, started,
                   report=report if shutdown_timeout else None)


def process_result(db, attempt, folder, code, events, completed, failed, started, *, report=None):
    if (code and report is None) or not completed or failed:
        status, retry_at = classify(events, (folder / "stderr.log").read_text(encoding="utf-8", errors="replace"))
        failures = attempt["failures"] + 1
        if status == "RETRY_WAIT":
            if failures >= 3:
                status = "NEEDS_REVIEW"
            else:
                retry_at = time.time() + 30 * 2 ** (failures - 1)
        latest = row_attempt(db, attempt["id"])
        phase = latest["phase"]
        if status in HOLD[2:] and not latest["session"]:
            if not started and not completed:
                phase = "READY"
            else:
                status = "NEEDS_REVIEW"
        record(db, attempt, status, f"Codex exit {code}; see logs.", phase=phase,
               retry_at=retry_at, failures=failures if status == "RETRY_WAIT" else 0)
        return
    if report is None:
        report = read_report(folder / "final.json")
    status = report["status"]
    report_json = json.dumps(report, ensure_ascii=False)
    if status in ("COMPLETED", "ABORTED"):
        with write(db):
            update(db, "attempts", attempt["id"], {"phase": "REPORTED", "report": report_json})
        finalize(db, row_attempt(db, attempt["id"]))
    else:
        waiting = (
            status == "WAITING_FOR_INPUT" and report["question"].strip()
            and row_attempt(db, attempt["id"])["session"]
        )
        record(db, attempt, "WAITING_FOR_INPUT" if waiting else "NEEDS_REVIEW",
               report["question"] if waiting else report["reason"], report=report_json)


def recover(db):
    for task in db.execute("SELECT id FROM tasks WHERE status = 'RUNNING'").fetchall():
        attempt = latest_attempt(db, task["id"])
        wait_for_previous_job(attempt["id"])
        if attempt["phase"] == "EXECUTING":
            folder = attempt_folder(attempt)
            runs = [path for path in folder.iterdir() if path.is_dir() and path.name.isdecimal()] if folder.is_dir() else []
            if runs:
                run = max(runs, key=lambda path: int(path.name))
                try:
                    try:
                        exit_result = json.loads((run / "exit.json").read_text(encoding="utf-8"))
                    except FileNotFoundError:
                        if os.name != "nt":
                            raise  # Without a Windows job, the previous process may still be running.
                        exit_result = None
                    report = None
                    if exit_result is None or exit_result.get("shutdown_timeout"):
                        report = read_report(run / "report-ready.json")
                    events = [json.loads(line) for line in (run / "events.jsonl").read_bytes().splitlines()]
                    types = {event["type"] for event in events if isinstance(event, dict)}
                    if exit_result is None:
                        moved = move_requested(db, attempt)
                        if moved or get_setting(db, "stop") == "1":
                            reason = "Nextcloud task moved or was removed." if moved else "Stopped by the user."
                            record(db, attempt, "INTERRUPTED", reason)
                            continue
                        if "turn.completed" not in types or "turn.failed" in types:
                            raise ValueError("The saved report has no successful turn.")
                    for event in reversed(events):
                        if isinstance(event, dict) and event.get("type") == "thread.started" and event.get("thread_id"):
                            with write(db):
                                update(db, "attempts", attempt["id"], {"session": event["thread_id"]})
                            break
                    started = "turn.started" in types or any(kind.startswith("item.") for kind in types)
                    process_result(
                        db, attempt, run, exit_result["code"] if exit_result else None, events,
                        "turn.completed" in types, "turn.failed" in types, started,
                        report=report,
                    )
                    continue
                except (OSError, ValueError, KeyError, RuntimeError):
                    pass
        if attempt["phase"] in ("REPORTED", "RESTORE"):
            try:
                finalize(db, attempt)
                continue
            except (RuntimeError, OSError) as error:
                record(db, attempt, "NEEDS_REVIEW", str(error))
                continue
        record(db, attempt, "NEEDS_REVIEW", "The worker exited before the result was saved.")


def run_worker(scheduled):
    if scheduled:
        with open_queue() as db, write(db):
            set_setting(db, "scheduled_heartbeat", str(time.time()))
    with process_lock("worker.lock") as acquired:
        if not acquired:
            return
        with open_queue() as db, ThreadPoolExecutor(sys.maxsize) as pool, ThreadPoolExecutor(1) as sync_pool:
            recover(db)
            attempted = set()
            running = {}
            authenticated = False
            if scheduled and db.execute("SELECT 1 FROM tasks WHERE status='AUTH_REQUIRED'").fetchone():
                try:
                    authenticated = subprocess.run(
                        [*codex_command(db), "login", "status"], env=codex_env(db),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, creationflags=NO_WINDOW,
                    ).returncode == 0
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    pass
            next_sync = 0
            sync_future = None
            remote_ready = True
            if get_setting(db, "sync", "auto") == "auto":
                remote_ready = sync(db)
                next_sync = time.monotonic() + 30
            while True:
                for task_id, future in list(running.items()):
                    if future.done():
                        future.result()
                        del running[task_id]
                        task = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
                        if task["status"] == "QUEUED":
                            attempted.discard(task_id)
                            next_sync = 0
                auto_sync = get_setting(db, "sync", "auto") == "auto" and get_setting(db, "nextcloud_credential")
                if sync_future and sync_future.done():
                    remote_ready = sync_future.result()
                    sync_future = None
                if auto_sync and time.monotonic() >= next_sync and sync_future is None:
                    sync_future = sync_pool.submit(sync_in_background)
                    next_sync = time.monotonic() + 30
                if not running and get_setting(db, "enabled") == "1":
                    if plan_next_idea(db):
                        continue
                attempt = claim(
                    db, scheduled, attempted, authenticated,
                    not auto_sync or (remote_ready and sync_future is None),
                )
                if attempt is not None:
                    task_id = attempt["task_id"]
                    attempted.add(task_id)
                    running[task_id] = pool.submit(run_attempt, attempt["id"])
                elif not running:
                    if sync_future:
                        remote_ready = sync_future.result()
                        sync_future = None
                        continue
                    if auto_sync:
                        sync(db)
                    return
                else:
                    time.sleep(0.2)


def sync_in_background():
    with open_queue() as db:
        return sync(db)
