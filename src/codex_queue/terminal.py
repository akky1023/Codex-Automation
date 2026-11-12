import argparse
from collections import deque
from datetime import datetime
import json
import os
import re
import shlex
import shutil
import time

from .cli import QueueError, git_root, main, make_parser
from .store import get_setting, open_queue, settings
from .worker import attempt_folder, latest_attempt


def keys():
    if os.name == "nt":
        import msvcrt
        key = msvcrt.getwch()
        if key in ("\x00", "\xe0"):
            return {"H": "up", "P": "down"}.get(msvcrt.getwch(), "")
        return key
    import sys
    import termios
    import tty
    from select import select

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        key = sys.stdin.read(1)
        if key == "\x1b" and select([fd], [], [], 0.08)[0]:
            suffix = os.read(fd, 2)
            return {b"[A": "up", b"[B": "down"}.get(suffix, "")
        return key
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def clear():
    if os.name == "nt":
        os.system("cls")
    else:
        print("\033[2J\033[H", end="")


def choose(title, options, default=0):
    if not options:
        print("No available choices.")
        return None
    index = default
    while True:
        clear()
        print(title, "(Up/Down, Enter, Esc)")
        page = max(3, shutil.get_terminal_size().lines - 4)
        start = min(max(0, index - page // 2), max(0, len(options) - page))
        for i, (label, _) in enumerate(options[start:start + page], start):
            print(f"{'>' if i == index else ' '} {label}")
        key = keys()
        if key == "up":
            index = (index - 1) % len(options)
        elif key == "down":
            index = (index + 1) % len(options)
        elif key in ("\n", "\r"):
            return options[index][1]
        elif key in ("\x1b", "\x04"):
            return None


def tokens(line):
    lexer = shlex.shlex(line, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    if os.name == "nt":
        lexer.escape = ""
    return list(lexer)


def command_text(line):
    return line.lstrip().removeprefix("/")


def completion(parser, line):
    """Return the replaceable suffix and its candidates for the current input."""
    match = re.search(r"[^\s]*$", line)
    start = match.start()
    prefix = match.group()
    if not line[:start].strip() and prefix.startswith("/"):
        start += 1
        prefix = prefix[1:]
    try:
        words = tokens(command_text(line[:start]))
    except ValueError:
        return start, []
    if any(char in prefix for char in ("'", '"')):
        return start, []

    positional = False
    value_expected = False
    for word in words:
        if value_expected:
            value_expected = False
            continue
        if word == "--":
            return start, []
        commands, options = parser_candidates(parser)
        flag, equals, _ = word.partition("=")
        if flag in options:
            value_expected = not equals and options[flag].nargs != 0
        elif not positional and word in commands:
            parser = commands[word]
        else:
            positional = True

    if value_expected:
        return start, []
    commands, options = parser_candidates(parser)
    choices = options if prefix.startswith("-") else commands if not positional else {}
    if not words and not prefix.startswith("-"):
        choices = [*choices, "q"]
    return start, [
        name for name in choices if not name.startswith("_") and name.startswith(prefix)
    ]


def parser_candidates(parser):
    commands = {}
    options = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            commands.update(action.choices)
        elif action.help != argparse.SUPPRESS:
            options.update((name, action) for name in action.option_strings)
    return commands, options


def field(label, default=""):
    result = input(f"{label} [{default}]: ")
    return default if result == "" else result


def multiline(label, default=""):
    print(f"{label} (finish with a line containing only .; enter . first to keep current text)")
    if default:
        print(default)
    lines = []
    while (line := input()) != ".":
        lines.append(line)
    return "\n".join(lines) if lines else default


def confirmed(argv, prompted):
    if prompted and input("Save changes? [y/N] ").strip().lower() not in ("y", "yes"):
        return None
    return argv


def option(command, flag, value):
    return [*command, f"{flag}={value}"]


def task_options(db, statuses=None):
    return [
        (f"{row['id']}  {row['project']}  {row['name']}  {row['status']}", row["id"])
        for row in db.execute(
            """SELECT t.*, p.name AS project FROM tasks t JOIN projects p ON p.id=t.project_id
               WHERE t.status NOT IN ('RUNNING','REMOVED') ORDER BY t.priority,t.id"""
        )
        if statuses is None or row["status"] in statuses
    ]


def project_options(db):
    return [(f"{r['name']}  {r['path']}", r["id"]) for r in db.execute(
        "SELECT * FROM projects WHERE retired=0 ORDER BY id"
    )]


def repo_here():
    try:
        return git_root(".")
    except (QueueError, OSError, ValueError):
        return None


def fill_add(db, argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("name", nargs="?")
    parser.add_argument("--project")
    parser.add_argument("--project-id")
    prompt = parser.add_mutually_exclusive_group()
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file")
    parser.add_argument("--priority")
    parser.add_argument("--edit", action="store_true")
    args = parser.parse_args(argv[1:])
    name = args.name
    command = ["add"]
    if args.project is None and args.project_id is None:
        options = project_options(db)
        root = repo_here()
        default = 0
        if root:
            current = db.execute(
                "SELECT id FROM projects WHERE path_key=? AND retired=0", (os.path.normcase(str(root)),),
            ).fetchone()
            if current:
                default = next(i for i, (_, key) in enumerate(options) if key == current["id"])
            else:
                options.insert(0, (f"{root.name}  {root} (register)", "current"))
        selected = choose("Project", options, default)
        if selected is None:
            return None
        if selected != "current":
            command = option(command, "--project-id", selected)
    elif args.project_id is not None:
        command = option(command, "--project-id", args.project_id)
    else:
        command = option(command, "--project", args.project)
    if name is None:
        name = field("Task title")
        if not name.strip():
            return None
    needs_prompt = args.prompt is None and args.prompt_file is None and not args.edit
    if args.edit:
        command.append("--edit")
    if args.prompt_file is not None:
        command = option(command, "--prompt-file", args.prompt_file)
    elif needs_prompt or args.prompt is not None:
        command = option(command, "--prompt", multiline("Task instructions") if needs_prompt else args.prompt)
    priority = args.priority if args.priority is not None else field("Priority (1-9)", settings(db)["priority"])
    command = option(command, "--priority", priority) + ["--", name]
    return confirmed(command, args.name is None or needs_prompt or args.priority is None)


def fill_task(db, argv):
    action = argv[0]
    parser = argparse.ArgumentParser(prog=f"cq {action}", add_help=False)
    parser.add_argument("id", nargs="?", type=int)
    if action == "edit":
        parser.add_argument("--name")
        prompt = parser.add_mutually_exclusive_group()
        prompt.add_argument("--prompt")
        prompt.add_argument("--prompt-file")
        parser.add_argument("--priority")
    elif action == "reply":
        parser.add_argument("answer", nargs="?")
    args = parser.parse_args(argv[1:])
    target = args.id
    if target is None:
        statuses = {"WAITING_FOR_INPUT"} if action == "reply" else None
        options = task_options(db, statuses)
        if action == "edit":
            imported = {row[0] for row in db.execute("SELECT task_id FROM remote_tasks")}
            options = [item for item in options if item[1] not in imported]
        target = choose("Task", options)
    if target is None:
        return None
    command = [action, str(target)]
    prompted = False
    task = db.execute("SELECT * FROM tasks WHERE id=?", (target,)).fetchone()
    if task is None or task["status"] in ("RUNNING", "REMOVED"):
        return [action, str(target), *([args.answer or ""] if action == "reply" else [])]
    if action == "reply":
        answer = args.answer
        attempt = latest_attempt(db, target)
        if answer is None and attempt and task["status"] == "WAITING_FOR_INPUT":
            print(attempt["reason"])
            answer = multiline("Answer")
            prompted = True
        command += ["--", answer or ""]
    if action == "edit":
        for key, label, read in (
            ("name", "Task title", field), ("prompt", "Task instructions", multiline),
            ("priority", "Priority (1-9)", field),
        ):
            if key == "prompt" and args.prompt_file is not None:
                command = option(command, "--prompt-file", args.prompt_file)
                continue
            value = getattr(args, key)
            current = str(task[key])
            if value is None:
                prompted = True
                value = read(label, current)
                if value == current:
                    continue
            command = option(command, f"--{key}", value)
    return confirmed(command, prompted)


def fill_project(db, argv):
    action = argv[1]
    parser = argparse.ArgumentParser(prog=f"cq project {action}", add_help=False)
    if action != "add":
        parser.add_argument("id", nargs="?", type=int)
    fields = (
        ("path", "Repository path", field), ("name", "Project name", field),
        ("prompt", "Project instructions", multiline), ("branch", "Branch mode (per-task/current)", field),
        ("base", "Base branch (inherit/unset/branch)", field),
    )
    if action in ("add", "edit"):
        for key, _, _ in fields:
            parser.add_argument(f"--{key}")
    args = parser.parse_args(argv[2:])
    command = ["project", action]
    row = None
    if action != "add":
        target = args.id if args.id is not None else choose("Project", project_options(db))
        if target is None:
            return None
        command += [str(target)]
        if action in ("rm", "remove"):
            return command
        row = db.execute("SELECT * FROM projects WHERE id=? AND retired=0", (target,)).fetchone()
        if row is None:
            return command
    defaults = dict(row) if row else {"path": str(repo_here() or "."), "name": "", "prompt": "", "branch": "", "base": None}
    defaults["base"] = "inherit" if defaults["base"] is None else defaults["base"] or "unset"
    prompted = False
    for key, label, read in fields:
        value = getattr(args, key)
        if value is None:
            prompted = True
            current = defaults[key] or ""
            value = read(label, current)
            if value == current:
                continue
        command = option(command, f"--{key}", value)
    return confirmed(command, prompted)


def prepare(argv):
    if not argv or "-h" in argv or "--help" in argv:
        return argv
    if argv[0] == "task" and len(argv) > 1:
        argv = argv[1:]
    with open_queue() as db:
        if argv[0] == "add":
            return fill_add(db, argv)
        if argv[0] in ("edit", "rm", "remove", "reply"):
            return fill_task(db, argv)
        if argv[0] == "project" and len(argv) > 1 and argv[1] in ("add", "edit", "rm", "remove"):
            return fill_project(db, argv)
    return argv


def latest_progress(attempt):
    directory = attempt_folder(attempt)
    if not directory.is_dir():
        return ""
    runs = [p for p in directory.iterdir() if p.is_dir() and p.name.isdecimal()]
    if not runs:
        return ""
    file = max(runs, key=lambda p: int(p.name)) / "events.jsonl"
    try:
        with file.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - 65536))
            for line in reversed(stream.readlines()):
                try:
                    item = json.loads(line).get("item", {})
                except (ValueError, AttributeError):
                    continue
                if isinstance(item, dict):
                    text = item.get("text") or item.get("command")
                    if isinstance(text, str):
                        return " ".join(text.split())[:160]
    except OSError:
        pass
    return ""


def queue_view():
    with open_queue() as db:
        lines = ["Codex Queue", "Automatic execution enabled" if get_setting(db, "enabled") == "1" else "Automatic execution paused"]
        rows = db.execute(
            """SELECT t.*, p.name AS project FROM tasks t JOIN projects p ON p.id=t.project_id
               WHERE t.status NOT IN ('DONE','ABORTED','REMOVED') ORDER BY t.priority,t.id"""
        )
        waiting = []
        for row in rows:
            status = row["status"]
            label = f"{row['id']}  {row['project']}  {row['name']}"
            if status in ("QUEUED", "RESUME_QUEUED"):
                waiting.append(label)
                continue
            attempt = latest_attempt(db, row["id"])
            lines.append(f"{status}  {label}")
            if attempt:
                if status == "RUNNING":
                    lines.append(f"  {attempt['started_at']} UTC  {latest_progress(attempt)}")
                elif attempt["reason"]:
                    lines.append(f"  {attempt['reason']}")
                if attempt["retry_at"]:
                    retry = datetime.fromtimestamp(attempt["retry_at"]).astimezone()
                    lines.append(f"  Retry after {retry.isoformat()}")
        if waiting:
            lines.append("Next: " + waiting[0])
        if error := get_setting(db, "nextcloud_error"):
            lines.append(error)
        return "\n".join(lines)


def screen():
    message = ""
    parser = make_parser()
    try:
        while True:
            if os.name == "nt":
                line = windows_input(parser, message)
            else:
                draw_prompt(queue_view(), message, "", [], 0, show_input=False)
                line = input("cq> ")
            line = command_text(line)
            if line.strip() in ("q", "quit", "exit"):
                return 0
            try:
                argv = prepare(tokens(line))
                if not argv:
                    message = ""
                    continue
                import contextlib
                import io
                live = argv[:2] == ["nextcloud", "connect"] or argv[0] in ("setup", "pause", "stop")
                output = io.StringIO()
                context = contextlib.ExitStack()
                with context:
                    if not live:
                        context.enter_context(contextlib.redirect_stdout(output))
                        context.enter_context(contextlib.redirect_stderr(output))
                    try:
                        main(argv, interactive=False)
                    except SystemExit:
                        pass
                if live:
                    input("Press Enter to return to the queue.")
                message = output.getvalue().strip()
            except (ValueError, OSError, RuntimeError, SystemExit) as error:
                message = str(error)
    except KeyboardInterrupt:
        print()
        return main(["stop"], interactive=False)
    except EOFError:
        return 0


def draw_prompt(view, message, line, candidates, selected, show_input=True):
    clear()
    print(view)
    if message:
        print(message)
    if candidates:
        page = min(5, max(1, shutil.get_terminal_size().lines - 6))
        first = max(0, selected - page + 1)
        print("Suggestions (Up/Down to select, Tab to complete, Esc to hide)")
        for index, candidate in enumerate(candidates[first:first + page], first):
            marker = ">" if index == selected else " "
            print(f"{marker} {candidate}")
    elif not show_input:
        print("add / edit / rm / ls / project / start / pause / stop / sync / q")
    if show_input:
        print("cq> " + line, end="", flush=True)


def windows_input(parser, message):
    import msvcrt

    line = ""
    view = queue_view()
    selected = 0
    start, candidates = completion(parser, line)
    draw_prompt(view, message, line, candidates, selected)
    refresh_at = time.monotonic() + 0.2
    while True:
        redraw = False
        if msvcrt.kbhit():
            key = keys()
            if key in ("\r", "\n"):
                print()
                return line
            if key == "\x03":
                raise KeyboardInterrupt
            if key == "\x1a":
                raise EOFError
            if key == "\x1b" and candidates:
                candidates = []
                redraw = True
            elif key in ("up", "down") and candidates:
                selected = (selected + (1 if key == "down" else -1)) % len(candidates)
                redraw = True
            else:
                previous = line
                if key == "\t" and candidates:
                    line = line[:start] + candidates[selected] + " "
                elif key == "\b" and line:
                    line = line[:-1]
                elif len(key) == 1 and key.isprintable():
                    line += key
                if line != previous:
                    start, candidates = completion(parser, line)
                    selected = 0
                    redraw = True
        else:
            time.sleep(0.03)
        if time.monotonic() >= refresh_at:
            refresh_at = time.monotonic() + 0.2
            current = queue_view()
            if current != view:
                view = current
                redraw = True
        if redraw:
            draw_prompt(view, message, line, candidates, selected)
