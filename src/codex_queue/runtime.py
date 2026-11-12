import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from datetime import datetime
from xml.etree import ElementTree as ET

from .store import get_setting, queue_folder, set_setting, write


def codex_command(db, use_saved=True):
    saved = get_setting(db, "codex_command") if use_saved else None
    if saved:
        return json.loads(saved)
    executable = shutil.which("codex")
    if executable and Path(executable).suffix.lower() not in (".cmd", ".bat"):
        return [str(Path(executable).resolve())]
    if executable:
        folder = Path(executable).resolve().parent
        script = folder / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        node = folder / "node.exe"
        node_path = str(node) if node.is_file() else shutil.which("node")
        if script.is_file() and node_path:
            return [node_path, str(script)]
    raise RuntimeError("Install Codex CLI and make it available on PATH.")


def codex_env(db):
    env = os.environ.copy()
    home = get_setting(db, "codex_home")
    if home:
        env["CODEX_HOME"] = home
    return env


def queue_command():
    return [sys.executable, "-m", "codex_queue.cli"]


def request_worker(db):
    if get_setting(db, "enabled") != "1":
        return False
    subprocess.Popen(
        [*queue_command(), "_worker"], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=queue_folder(),
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )
    return True


def scheduler(action, name, extra=(), check=True):
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    result = subprocess.run(
        [str(system / "schtasks.exe"), f"/{action}", "/TN", name, *extra],
        capture_output=True, text=True, errors="replace",
    )
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result


def task_name(db):
    return get_setting(db, "schedule_name", f"Codex Queue ({os.environ.get('USERNAME', 'user')})")


def register_schedule(db, minutes):
    if os.name != "nt":
        raise RuntimeError("The scheduled worker requires Windows Task Scheduler.")
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    user = subprocess.check_output([str(system / "whoami.exe")], text=True).strip()
    task = ET.Element("Task", version="1.3", xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task")

    def child(parent, tag, value=None, **attrs):
        element = ET.SubElement(parent, tag, attrs)
        element.text = value
        return element

    triggers = child(task, "Triggers")
    periodic = child(triggers, "TimeTrigger")
    child(child(periodic, "Repetition"), "Interval", f"PT{minutes}M")
    child(periodic, "StartBoundary", datetime.now().astimezone().isoformat(timespec="seconds"))
    child(child(triggers, "LogonTrigger"), "UserId", user)
    principal = child(child(task, "Principals"), "Principal", id="Owner")
    child(principal, "UserId", user)
    child(principal, "LogonType", "InteractiveToken")
    child(principal, "RunLevel", "LeastPrivilege")
    config = child(task, "Settings")
    child(config, "MultipleInstancesPolicy", "IgnoreNew")
    child(config, "DisallowStartIfOnBatteries", "false")
    child(config, "StopIfGoingOnBatteries", "false")
    child(config, "StartWhenAvailable", "true")
    child(config, "ExecutionTimeLimit", "PT0S")
    action = child(child(task, "Actions", Context="Owner"), "Exec")
    child(action, "Command", sys.executable)
    child(action, "Arguments", subprocess.list2cmdline([
        *queue_command()[1:], "_worker", "--scheduled", "--data-dir", str(queue_folder()),
    ]))
    child(action, "WorkingDirectory", str(queue_folder()))
    file = queue_folder() / "schedule.xml"
    ET.ElementTree(task).write(file, encoding="utf-16", xml_declaration=True)
    name = task_name(db)
    scheduler("Create", name, ("/XML", str(file), "/F"))
    with write(db):
        set_setting(db, "schedule_name", name)
        set_setting(db, "schedule_minutes", str(minutes))


def setup(db):
    command = codex_command(db, use_saved=False)
    env = codex_env(db)
    if os.environ.get("CODEX_HOME"):
        env["CODEX_HOME"] = os.environ["CODEX_HOME"]
    if subprocess.run([*command, "login", "status"], env=env).returncode:
        subprocess.run([*command, "login"], env=env, check=True)
        subprocess.run([*command, "login", "status"], env=env, check=True)
    with write(db):
        set_setting(db, "codex_command", json.dumps(command))
        set_setting(db, "codex_home", str(Path(env.get("CODEX_HOME") or Path.home() / ".codex").resolve()))
    register_schedule(db, int(get_setting(db, "schedule_minutes", "10")))
    requested_at = time.time()
    scheduler("Run", task_name(db))
    deadline = time.monotonic() + 12
    while float(get_setting(db, "scheduled_heartbeat", "0")) < requested_at:
        if time.monotonic() >= deadline:
            raise RuntimeError("The scheduled task was registered, but its worker did not check in.")
        time.sleep(0.2)
    print("Setup complete. Run cq start to enable automatic execution.")
