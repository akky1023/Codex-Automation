import argparse
from datetime import datetime
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import tempfile

from .actions import queue_control, task_action, wake
from .runtime import register_schedule, setup
from .store import DEFAULTS, get_setting, open_queue, settings, update, write
from .worker import latest_attempt, run_worker


class QueueError(Exception):
    pass


def git_root(path):
    folder = Path(path).resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=folder, capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode:
        raise QueueError(f"No Git repository at {folder}")
    return Path(result.stdout.strip()).resolve()


def changes(args, *keys):
    return {key: getattr(args, key) for key in keys if getattr(args, key) is not None}


def by_id(db, table, row_id):
    active = " AND retired = 0" if table == "projects" else ""
    row = db.execute(f"SELECT * FROM {table} WHERE id = ?{active}", (row_id,)).fetchone()
    if row is None:
        raise QueueError(f"Unknown {table[:-1]} ID {row_id}")
    return row


def show_projects(rows):
    for row in rows:
        print(f"{row['id']}\t{row['name']}\t{row['path']}")


def active_projects(db):
    return db.execute("SELECT * FROM projects WHERE retired = 0 ORDER BY id").fetchall()


def path_project(db, root, create=False):
    key = os.path.normcase(str(root))
    row = db.execute("SELECT * FROM projects WHERE path_key = ?", (key,)).fetchone()
    if row is not None and not row["retired"]:
        return row
    if not create:
        raise QueueError(f"Project is not registered: {root}")
    if row is None:
        cursor = db.execute(
            "INSERT INTO projects(path, path_key, name) VALUES (?, ?, ?)",
            (str(root), key, root.name),
        )
        return by_id(db, "projects", cursor.lastrowid)
    db.execute(
        """UPDATE projects SET retired = 0, name = ?, prompt = '',
                  branch = NULL, base = NULL WHERE id = ?""",
        (root.name, row["id"]),
    )
    return by_id(db, "projects", row["id"])


def project_for_task(db, args):
    if args.project_id is not None:
        return by_id(db, "projects", args.project_id)
    if args.project is not None:
        candidates = [row for row in active_projects(db) if row["name"] == args.project]
        if len(candidates) == 1:
            return candidates[0]
        show_projects(candidates or active_projects(db))
        raise QueueError("Choose a registered project with --project-id ID.")
    try:
        root = git_root(".")
    except QueueError:
        show_projects(active_projects(db))
        raise QueueError("Use --project NAME or --project-id ID outside a Git repository.") from None
    return path_project(db, root, create=True)


def edit_prompt(prompt):
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor and os.name != "nt":
        raise QueueError("Set VISUAL or EDITOR to use --edit.")
    with tempfile.TemporaryDirectory() as folder:
        file = Path(folder) / "prompt.txt"
        file.write_text(prompt, encoding="utf-8")
        if os.name == "nt":
            command = f"{editor or 'notepad.exe'} {subprocess.list2cmdline([str(file)])}"
        else:
            command = [*shlex.split(editor), str(file)]
        if subprocess.run(command).returncode:
            raise QueueError("The editor exited unsuccessfully.")
        return file.read_text(encoding="utf-8-sig")


def project_options(args):
    values = changes(args, "name", "prompt", "branch", "base")
    if args.base in ("inherit", "unset"):
        values["base"] = None if args.base == "inherit" else ""
    return values


def show_project(db, row):
    defaults = settings(db)
    branch = row["branch"] or f"{defaults['branch']} (inherited)"
    base = row["base"]
    if base is None:
        base = f"{defaults['base'] or 'unset'} (inherited)"
    show_projects([row])
    print(f"Branch: {branch}\nBase: {base or 'unset'}\nPrompt: {row['prompt']}")


def manage_project(db, args):
    action = args.project_action
    if action in ("ls", "list"):
        show_projects(active_projects(db))
        return
    with write(db):
        if action in ("rm", "remove"):
            row = by_id(db, "projects", args.id)
            if db.execute(
                "SELECT 1 FROM tasks WHERE project_id = ? AND status NOT IN ('DONE', 'ABORTED', 'REMOVED')",
                (row["id"],),
            ).fetchone():
                raise QueueError("Resolve or remove unfinished tasks before removing this project.")
            db.execute("DELETE FROM remote_links WHERE project_id = ?", (row["id"],))
            update(db, "projects", row["id"], {"retired": 1})
            return
        values = project_options(args)
        if action == "edit":
            row = by_id(db, "projects", args.id)
            if args.path is not None:
                root = git_root(args.path)
                if os.path.normcase(str(root)) != row["path_key"]:
                    if db.execute(
                        """SELECT 1 FROM tasks WHERE project_id = ? AND status IN
                           ('RUNNING', 'INTERRUPTED', 'WAITING_FOR_INPUT', 'NEEDS_REVIEW',
                            'RESUME_QUEUED', 'RATE_LIMITED', 'AUTH_REQUIRED', 'RETRY_WAIT')""",
                        (row["id"],),
                    ).fetchone():
                        raise QueueError("Resolve active or suspended tasks before changing the path.")
                    values.update(path=str(root), path_key=os.path.normcase(str(root)))
        else:
            row = path_project(db, git_root(args.path or "."), create=bool(values) or action == "add")
        update(db, "projects", row["id"], values)
        row = by_id(db, "projects", row["id"])
    show_project(db, row)


def manage_task(db, args):
    action = args.command
    if action in ("ls", "list"):
        if get_setting(db, "enabled") != "1":
            print("Automatic execution paused.")
        if error := get_setting(db, "nextcloud_error"):
            print(error)
        for row in db.execute(
            """SELECT t.*, p.name AS project_name, r.writeback, r.blocked FROM tasks t
               JOIN projects p ON p.id = t.project_id LEFT JOIN remote_tasks r ON r.task_id = t.id
               WHERE t.status <> 'REMOVED'
               ORDER BY (t.status = 'DONE'), t.priority, t.id"""
        ):
            print(f"{row['id']}\t{row['project_name']}\t{row['status']}\t{row['name']}")
            if row["status"] not in ("DONE", "QUEUED"):
                attempt = latest_attempt(db, row["id"])
                if attempt and attempt["reason"]:
                    print(f"  {attempt['reason']}")
                if attempt and attempt["retry_at"]:
                    print(f"  Retry after {datetime.fromtimestamp(attempt['retry_at']).astimezone().isoformat()}")
                if row["status"] == "AUTH_REQUIRED":
                    print("  Run codex login.")
            if row["writeback"] == "conflict":
                print(f"  Nextcloud completion conflict. Run cq nextcloud resolve {row['id']}.")
            if row["blocked"] and row["status"] in ("QUEUED", "RESUME_QUEUED"):
                print("  Waiting for Nextcloud to confirm the task location.")
        return
    with write(db):
        if action == "add":
            project = project_for_task(db, args)
            priority = args.priority if args.priority is not None else int(settings(db)["priority"])
            cursor = db.execute(
                "INSERT INTO tasks(project_id, name, prompt, priority) VALUES (?, ?, ?, ?)",
                (project["id"], args.name, args.prompt or "", priority),
            )
            message = f"Added task {cursor.lastrowid} to {project['name']} (priority {priority})."
        else:
            row = by_id(db, "tasks", args.id)
            if row["status"] in ("RUNNING", "REMOVED"):
                raise QueueError(f"Task {row['id']} is {row['status']}.")
            if action == "edit":
                if db.execute("SELECT 1 FROM remote_tasks WHERE task_id = ?", (row["id"],)).fetchone():
                    raise QueueError("Edit imported tasks in Nextcloud.")
                update(db, "tasks", row["id"], changes(args, "name", "prompt", "priority"))
                message = f"Updated task {row['id']}."
            else:
                update(db, "tasks", row["id"], {"status": "REMOVED"})
                message = f"Removed task {row['id']}."
    print(message)
    wake(db)


def configure(db, args):
    values = changes(args, *DEFAULTS)
    if args.base == "unset":
        values["base"] = ""
    with write(db):
        db.executemany(
            """INSERT INTO settings(key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            values.items(),
        )
        current = settings(db)
    for key, value in current.items():
        print(f"{key.replace('_', '-')}: {value or 'unset'}")


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def task_commands(subparsers):
    add = subparsers.add_parser("add", help="Add a task")
    add.add_argument("name")
    target = add.add_mutually_exclusive_group()
    target.add_argument("--project")
    target.add_argument("--project-id", type=positive)
    prompt = add.add_mutually_exclusive_group()
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file", type=Path, help="Read UTF-8 task instructions from a file")
    add.add_argument("--edit", action="store_true", help="Edit the task prompt before saving")
    add.add_argument("--priority", type=int, choices=range(1, 10))
    edit = subparsers.add_parser("edit", help="Edit a task")
    edit.add_argument("id", type=positive)
    edit.add_argument("--name")
    prompt = edit.add_mutually_exclusive_group()
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file", type=Path, help="Read UTF-8 task instructions from a file")
    edit.add_argument("--priority", type=int, choices=range(1, 10))
    remove = subparsers.add_parser("rm", aliases=["remove"], help="Remove a task")
    remove.add_argument("id", type=positive)
    subparsers.add_parser("ls", aliases=["list"], help="List tasks")


def project_arguments(parser):
    parser.add_argument("--name")
    parser.add_argument("--path")
    parser.add_argument("--prompt")
    parser.add_argument("--branch", choices=["per-task", "current"])
    parser.add_argument("--base")


def make_parser():
    parser = argparse.ArgumentParser(prog="cq", description="Manage the local Codex task queue.")
    commands = parser.add_subparsers(dest="command")
    task_commands(commands)
    task = commands.add_parser("task", help="Manage tasks")
    task_commands(task.add_subparsers(dest="command", required=True))
    project = commands.add_parser("project", help="Manage projects")
    project_arguments(project)
    actions = project.add_subparsers(dest="project_action")
    project_arguments(actions.add_parser("add", help="Register a Git repository"))
    actions.add_parser("ls", aliases=["list"], help="List projects")
    edit = actions.add_parser("edit", help="Edit a project")
    edit.add_argument("id", type=positive)
    project_arguments(edit)
    remove = actions.add_parser("rm", aliases=["remove"], help="Remove a project")
    remove.add_argument("id", type=positive)

    config = commands.add_parser("config", aliases=["settings"], help="Manage queue defaults")
    config.add_argument("--parallel", type=positive)
    config.add_argument("--priority", type=int, choices=range(1, 10))
    config.add_argument("--branch", choices=["per-task", "current"])
    config.add_argument("--base")
    config.add_argument("--on-abort", choices=["pause", "continue"])
    config.add_argument("--sync", choices=["auto", "manual"])
    for name, aliases, help_text in (
        ("start", ["resume"], "Enable automatic execution and request the worker"),
        ("pause", [], "Pause the queue and wait for running tasks"),
        ("stop", [], "Stop the queue and interrupt running tasks"),
        ("setup", [], "Authenticate Codex CLI and register the worker"),
    ):
        commands.add_parser(name, aliases=aliases, help=help_text)
    schedule = commands.add_parser("schedule", help="Register a Windows scheduled task")
    schedule.add_argument("--every-minutes", type=positive, required=True)
    for name in ("retry", "reply", "abort", "logs", "done"):
        action = commands.add_parser(name)
        action.add_argument("id", type=positive)
        if name == "retry":
            action.add_argument("--fresh", action="store_true")
        elif name == "reply":
            action.add_argument("answer")
        elif name == "done":
            action.add_argument("--note")
    worker = commands.add_parser("_worker")
    worker.add_argument("--scheduled", action="store_true")
    worker.add_argument("--data-dir", help=argparse.SUPPRESS)
    from .nextcloud import add_commands
    add_commands(commands)
    return parser


def main(argv=None, interactive=True):
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        import sys
        if interactive and sys.stdin.isatty() and sys.stdout.isatty():
            from .terminal import screen
            return screen()
        parser.print_help()
        return 0
    try:
        if getattr(args, "prompt_file", None) is not None:
            args.prompt = args.prompt_file.read_text(encoding="utf-8-sig")
        if getattr(args, "edit", False):
            args.prompt = edit_prompt(args.prompt or "")
        if args.command == "_worker":
            if args.data_dir:
                os.environ["CQ_WORKER_DATA_DIR"] = args.data_dir
            run_worker(args.scheduled)
            return 0
        with open_queue() as db:
            if args.command == "project":
                manage_project(db, args)
            elif args.command in ("config", "settings"):
                configure(db, args)
            elif args.command in ("start", "resume", "pause", "stop"):
                queue_control(db, args.command)
            elif args.command == "setup":
                setup(db)
            elif args.command == "schedule":
                register_schedule(db, args.every_minutes)
            elif args.command in ("retry", "reply", "abort", "logs", "done"):
                task_action(db, args)
            elif args.command in ("nextcloud", "sync"):
                from .nextcloud import dispatch
                dispatch(db, args)
            else:
                manage_task(db, args)
    except sqlite3.IntegrityError as error:
        if "projects.path_key" in str(error):
            parser.exit(1, "cq: That Git repository is already registered.\n")
        parser.exit(1, f"cq: {error}\n")
    except (QueueError, OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError, UnicodeError) as error:
        parser.exit(1, f"cq: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
