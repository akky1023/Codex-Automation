import argparse
from contextlib import contextmanager
from collections import deque
from datetime import datetime
import json
import os
import re
import shlex
import shutil
import time
import unicodedata

from .arguments import ArgumentParser
from .cli import QueueError, git_root, main, make_parser
from .language import language, status_label, tr
from .store import get_setting, open_queue, saved_language, settings
from .worker import attempt_folder, latest_attempt


def keys():
    if os.name == "nt":
        import msvcrt
        key = msvcrt.getwch()
        if key in ("\x00", "\xe0"):
            return {"H": "up", "P": "down", "I": "pageup", "Q": "pagedown"}.get(msvcrt.getwch(), "")
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
        print(tr("No available choices.", "選択できる項目がありません。"))
        return None
    index = default
    while True:
        clear()
        print(title, tr("(Up/Down, Enter, Esc)", "（上下キーで選択、Enter で確定、Esc で戻る）"))
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
    if not line.strip():
        return len(line), []
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
    print(label, tr("(finish with a line containing only .; enter . first to keep current text)",
                    "（. だけの行で入力終了。最初に . を入力すると現在の内容を保持）"))
    if default:
        print(default)
    lines = []
    while (line := input()) != ".":
        lines.append(line)
    return "\n".join(lines) if lines else default


def confirmed(argv, prompted):
    if prompted and input(tr("Save changes? [y/N] ", "変更を保存しますか？ [y/N] ")).strip().lower() not in ("y", "yes"):
        return None
    return argv


def option(command, flag, value):
    return [*command, f"{flag}={value}"]


def task_options(db, statuses=None):
    return [
        (f"{row['id']}  {row['project']}  {row['name']}  {status_label(row['status'])}", row["id"])
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
    parser = ArgumentParser(add_help=False)
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
                options.insert(0, (f"{root.name}  {root} " + tr("(register)", "（新規登録）"), "current"))
        selected = choose(tr("Project", "プロジェクト"), options, default)
        if selected is None:
            return None
        if selected != "current":
            command = option(command, "--project-id", selected)
    elif args.project_id is not None:
        command = option(command, "--project-id", args.project_id)
    else:
        command = option(command, "--project", args.project)
    if name is None:
        name = field(tr("Task title", "タスク名"))
        if not name.strip():
            return None
    needs_prompt = args.prompt is None and args.prompt_file is None and not args.edit
    if args.edit:
        command.append("--edit")
    if args.prompt_file is not None:
        command = option(command, "--prompt-file", args.prompt_file)
    elif needs_prompt or args.prompt is not None:
        command = option(command, "--prompt", multiline(tr("Task instructions", "タスクの指示")) if needs_prompt else args.prompt)
    priority = args.priority if args.priority is not None else field(tr("Priority (1-9)", "優先度（1〜9）"), settings(db)["priority"])
    command = option(command, "--priority", priority) + ["--", name]
    return confirmed(command, args.name is None or needs_prompt or args.priority is None)


def fill_task(db, argv):
    action = argv[0]
    parser = ArgumentParser(prog=f"cq {action}", add_help=False)
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
        target = choose(tr("Task", "タスク"), options)
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
            answer = multiline(tr("Answer", "回答"))
            prompted = True
        command += ["--", answer or ""]
    if action == "edit":
        for key, label, read in (
            ("name", tr("Task title", "タスク名"), field), ("prompt", tr("Task instructions", "タスクの指示"), multiline),
            ("priority", tr("Priority (1-9)", "優先度（1〜9）"), field),
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
    parser = ArgumentParser(prog=f"cq project {action}", add_help=False)
    if action != "add":
        parser.add_argument("id", nargs="?", type=int)
    fields = (
        ("path", tr("Repository path", "リポジトリのパス"), field), ("name", tr("Project name", "プロジェクト名"), field),
        ("prompt", tr("Project instructions", "プロジェクトの共通指示"), multiline), ("branch", tr("Branch mode (per-task/current)", "ブランチ運用（per-task/current）"), field),
        ("base", tr("Base branch (inherit/unset/branch)", "開始元ブランチ（inherit/unset/ブランチ名）"), field),
    )
    if action in ("add", "edit"):
        for key, _, _ in fields:
            parser.add_argument(f"--{key}")
    args = parser.parse_args(argv[2:])
    command = ["project", action]
    row = None
    if action != "add":
        target = args.id if args.id is not None else choose(tr("Project", "プロジェクト"), project_options(db))
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
    try:
        runs = [p for p in directory.iterdir() if p.is_dir() and p.name.isdecimal()]
        if not runs:
            return ""
        file = max(runs, key=lambda p: int(p.name)) / "events.jsonl"
        with file.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - 65536))
            for line in reversed(stream.readlines()):
                try:
                    item = json.loads(line).get("item", {})
                except (ValueError, AttributeError):
                    continue
                if isinstance(item, dict):
                    text = item.get("text")
                    command = item.get("command")
                    if isinstance(command, str) and command.strip():
                        action = {
                            "in_progress": tr("Running", "実行中"),
                            "completed": tr("Finished", "実行済み"),
                            "failed": tr("Failed", "失敗"),
                        }.get(item.get("status"), tr("Command", "コマンド"))
                        text = f"{action}: {command}"
                    if isinstance(text, str) and text.strip():
                        return " ".join(text.split())[:160]
    except OSError:
        pass
    return ""


def queue_view():
    with open_queue() as db:
        lines = ["Codex Queue", tr("Automatic execution enabled", "自動実行は有効です") if get_setting(db, "enabled") == "1" else tr("Automatic execution paused", "自動実行は一時停止中です")]
        rows = db.execute(
            """SELECT t.*, p.name AS project FROM tasks t JOIN projects p ON p.id=t.project_id
               WHERE t.status <> 'REMOVED' ORDER BY (t.status = 'DONE'), t.priority, t.id"""
        )
        running = []
        tasks = []
        for row in rows:
            status = row["status"]
            label = f"{row['id']}  {row['project']}  {row['name']}"
            if status == "RUNNING":
                running.append(label)
                attempt = latest_attempt(db, row["id"])
                if attempt:
                    progress = latest_progress(attempt)
                    if not progress:
                        progress = (
                            tr("Preparing workspace", "作業フォルダを準備中") if attempt["phase"] in ("PREPARING", "CHECKOUT", "READY")
                            else tr("Waiting for Codex output", "Codex の出力を待機中")
                        )
                    running.append(f"  {attempt['started_at']} UTC  {progress}")
                else:
                    running.append(tr("  Preparing workspace", "  作業フォルダを準備中"))
                continue
            tasks.append(f"{status_label(status)}  {label}")
            if status in ("QUEUED", "DONE", "ABORTED"):
                continue
            attempt = latest_attempt(db, row["id"])
            if attempt:
                if attempt["reason"]:
                    tasks.append(f"  {attempt['reason']}")
                if attempt["retry_at"]:
                    retry = datetime.fromtimestamp(attempt["retry_at"]).astimezone()
                    tasks.append("  " + tr("Retry after {time}", "再試行予定 {time}", time=retry.isoformat()))
        lines.extend(["", tr("In progress", "実行中"), *(running or [tr("No tasks running", "実行中のタスクはありません")])])
        lines.extend(["", tr("Tasks", "タスク一覧"), *(tasks or [tr("No other tasks", "ほかのタスクはありません")])])
        if error := get_setting(db, "nextcloud_error"):
            lines.append(error)
        return "\n".join(lines)


def screen():
    message = ""
    try:
        while True:
            language.set(saved_language())
            if os.name == "nt":
                line = windows_input(make_parser(), message)
            else:
                clear()
                print(queue_view())
                if message:
                    print(message)
                print("add / edit / rm / ls / project / start / pause / stop / sync / q")
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
                    input(tr("Press Enter to return to the queue.", "Enter でキューに戻ります。"))
                message = output.getvalue().strip()
            except (ValueError, OSError, RuntimeError, SystemExit) as error:
                message = str(error)
    except KeyboardInterrupt:
        print()
        return main(["stop"], interactive=False)
    except EOFError:
        return 0


@contextmanager
def virtual_terminal():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32")
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = kernel.GetStdHandle(-11)
    mode = wintypes.DWORD()
    enabled = bool(kernel.GetConsoleMode(handle, ctypes.byref(mode))
                   and kernel.SetConsoleMode(handle, mode.value | 0x0004))
    try:
        yield enabled
    finally:
        if enabled:
            kernel.SetConsoleMode(handle, mode.value)


def cell_width(char):
    if unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def fit(text, width, tail=False):
    result = []
    for char in reversed(text) if tail else text:
        size = cell_width(char)
        if size > width:
            break
        width -= size
        result.append(char)
    return "".join(reversed(result) if tail else result)


def wrap_rows(text, width):
    for row in text.expandtabs(4).splitlines():
        row = "".join(char for char in row if unicodedata.category(char) != "Cc")
        if not row or width < 2:
            yield fit(row, width)
            continue
        while row:
            fragment = fit(row, width)
            yield fragment
            row = row[len(fragment):]


def prompt_frame(view, message, line, candidates, selected, size, offset):
    columns, rows = size
    width = max(0, columns - 1)
    body = list(wrap_rows(view, width))
    if message:
        body += ["", *wrap_rows(message, width)]
    footer = []
    if rows >= 4:
        if candidates:
            footer.append(tr("Up/Down select  Tab complete  Esc hide", "上下キーで選択  Tab で補完  Esc で閉じる"))
            page = min(5, rows - 4)
            first = max(0, selected - page + 1)
            for index, candidate in enumerate(candidates[first:first + page], first):
                marker = ">" if index == selected else " "
                footer.append(f"{marker} {candidate}")
        else:
            footer.append(tr("Enter run  / commands  PgUp/PgDn scroll", "Enter で実行  / で候補  PgUp/PgDn でスクロール"))
    separator = ["-" * width] if rows >= 4 else []
    available = max(0, rows - len(footer) - len(separator) - 1)
    max_offset = max(0, len(body) - available)
    offset = min(offset, max_offset)
    visible = body[offset:offset + available]
    if max_offset and footer:
        footer[0] += "  |  PgUp/PgDn" if candidates else f"  |  {offset + 1}-{offset + len(visible)}/{len(body)}"
    prompt = "cq> "
    visible_input = fit(line, max(0, width - len(prompt)), tail=True)
    lines = [*visible, *([""] * (available - len(visible))), *footer, *separator, prompt + visible_input]
    return "\n".join(fit(row, width) for row in lines), available, offset, max_offset


def draw_prompt(view, message, line, candidates, selected, size, offset, vt):
    frame, page, offset, max_offset = prompt_frame(view, message, line, candidates, selected, size, offset)
    if vt:
        body, _, prompt = frame.rpartition("\n")
        upper = body.replace("\n", "\033[K\n") + "\033[K\n" if "\n" in frame else ""
        print(f"\033[H{upper}\033[97;44m{prompt}\033[K\033[0m", end="", flush=True)
    else:
        clear()
        print(frame, end="", flush=True)
    return page, offset, max_offset


def windows_input(parser, message):
    with virtual_terminal() as vt:
        return read_windows_prompt(parser, message, vt)


def read_windows_prompt(parser, message, vt):
    import msvcrt

    line = ""
    view = queue_view()
    selected = 0
    start, candidates = completion(parser, line)
    size = shutil.get_terminal_size()
    page, offset, max_offset = draw_prompt(view, message, line, candidates, selected, size, 0, vt)
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
            elif key in ("pageup", "pagedown"):
                direction = 1 if key == "pagedown" else -1
                new_offset = max(0, min(max_offset, offset + direction * max(1, page - 1)))
                redraw = offset != new_offset
                offset = new_offset
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
            current_size = shutil.get_terminal_size()
            if current != view or current_size != size:
                view = current
                size = current_size
                redraw = True
        if redraw:
            page, offset, max_offset = draw_prompt(view, message, line, candidates, selected, size, offset, vt)
