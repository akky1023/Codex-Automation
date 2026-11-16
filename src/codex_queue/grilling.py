"""Persistent, read-only discovery rounds for turning an Idea into a ToDo plan."""
import json
import subprocess
import uuid

from .planner import generate_plan, idea_record, planning_config, plan_context
from .processes import process_lock
from .runtime import NO_WINDOW, codex_command, codex_env
from .store import get_setting, settings, write


def _snapshot(db, idea):
    return {
        "idea_title": idea["title"], "idea_detail": idea["detail"],
        "project_id": idea["project_id"], "project_path": idea["path"],
        "project_prompt": idea["project_prompt"],
        "project_base": idea["project_base"] or "", "project_retired": idea["project_retired"],
        "nextcloud_sync": idea["nextcloud_sync"],
        "config_json": json.dumps(planning_config(db, idea), sort_keys=True),
        "settings_json": json.dumps({
            **{key: settings(db)[key] for key in ("branch", "base", "priority", "idea_planning")},
            "codex_command": get_setting(db, "codex_command", ""),
        }, sort_keys=True),
        "branch_mode": plan_context(db, idea)["mode"],
    }


def _assert_current(db, session):
    idea = idea_record(db, session["idea_id"])
    actual = _snapshot(db, idea)
    expected = {key: session[key] for key in actual}
    if actual != expected or idea["planned_at"]:
        raise RuntimeError("The Idea, project, or planner settings changed during Grilling. Start a new session.")
    return idea


def grilling_prompt(idea, session):
    return (
        "Interview the user about this Idea until every material design decision is settled. "
        "Inspect the repository read-only before asking. Treat the decisions as a dependency tree. "
        "Ask the whole currently answerable frontier in one round, number questions, and include a recommended answer. "
        "Word each question so yes accepts the recommendation. Defer dependent questions until later rounds. "
        "Use prior answers and repository facts; never ask the user for facts discoverable in the repository. "
        "When every branch is settled, return no questions and provide a concise agreed specification. "
        "Do not implement or create ToDos. Return Japanese if the Idea is Japanese, otherwise use its language.\n\n"
        f"# Project instructions\n{idea['project_prompt']}\n\n"
        f"# Idea\n{idea['title']}\n\n{idea['detail']}\n\n"
        f"# Previous rounds and answers\n{json.dumps(json.loads(session['answers_json']), ensure_ascii=False)}"
    )


def generate_round(db, path, prompt, config):
    schema = {"type": "object", "properties": {
        "questions": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "body": {"type": "string"}, "recommendation": {"type": "string"},
        }, "required": ["title", "body", "recommendation"], "additionalProperties": False}},
        "summary": {"type": "string"},
    }, "required": ["questions", "summary"], "additionalProperties": False}
    command = [*codex_command(db), "-a", "never", "exec", "-s", "read-only"]
    if config["planner_model"]:
        command += ["-m", config["planner_model"]]
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from .store import queue_folder
    with TemporaryDirectory(prefix="grill-", dir=queue_folder()) as directory:
        schema_path, output = Path(directory) / "schema.json", Path(directory) / "round.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")
        result = subprocess.run([*command, "--output-schema", str(schema_path), "-o", str(output), "-"],
            input=prompt, cwd=path, env=codex_env(db), encoding="utf-8", errors="replace",
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"Grilling exited with code {result.returncode}.")
        try:
            report = json.loads(output.read_text(encoding="utf-8"))
            questions, summary = report["questions"], report["summary"].strip()
            if not isinstance(questions, list) or (not questions and not summary):
                raise ValueError
            for item in questions:
                if any(not isinstance(item[key], str) or not item[key].strip()
                       for key in ("title", "body", "recommendation")):
                    raise ValueError
            if questions and summary:
                raise ValueError
            return questions, summary
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise RuntimeError("Invalid Grilling response.") from error


def _render(session):
    questions = json.loads(session["questions_json"])
    if session["status"] == "CONFIRM":
        return {"session_id": session["session_id"], "status": "CONFIRM", "round": session["round_no"],
                "summary": session["summary"]}
    return {"session_id": session["session_id"], "status": session["status"], "round": session["round_no"],
            "questions": questions}


def start_grilling(db, idea_id, board_key=""):
    with write(db):
        idea = idea_record(db, idea_id)
        if idea["planned_at"]:
            raise RuntimeError(f"Idea {idea_id} already has a plan.")
        existing = db.execute("SELECT * FROM grilling_sessions WHERE idea_id = ? AND status != 'CANCELLED' ORDER BY rowid DESC LIMIT 1",
                              (idea_id,)).fetchone()
        created = False
        if existing and existing["status"] != "CANCELLED":
            if existing["board_key"] != board_key:
                raise RuntimeError("This Idea already has a Grilling session on another board or CLI.")
            session_id = existing["session_id"]
        else:
            created = True
            snapshot = _snapshot(db, idea)
            session_id = str(uuid.uuid4())
            db.execute("""INSERT INTO grilling_sessions(session_id, idea_id, board_key, status, idea_title, idea_detail,
                project_id, project_path, project_prompt, project_base, project_retired, nextcloud_sync,
                config_json, settings_json, branch_mode)
                VALUES (?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, idea_id, board_key, snapshot["idea_title"], snapshot["idea_detail"], snapshot["project_id"],
                 snapshot["project_path"], snapshot["project_prompt"], snapshot["project_base"], snapshot["project_retired"],
                 snapshot["nextcloud_sync"], snapshot["config_json"], snapshot["settings_json"], snapshot["branch_mode"]))
        session = db.execute("SELECT * FROM grilling_sessions WHERE session_id = ?", (session_id,)).fetchone()
        idea = _assert_current(db, session)
        if session["status"] == "PLAN_GENERATING":
            with process_lock(f"grilling-{idea_id}.lock") as acquired:
                if acquired:
                    db.execute("UPDATE grilling_sessions SET status = 'CONFIRM' WHERE session_id = ? AND status = 'PLAN_GENERATING'",
                               (session_id,))
                    session = db.execute("SELECT * FROM grilling_sessions WHERE session_id = ?", (session_id,)).fetchone()
                else:
                    return _render(session)
        if session["status"] == "ACTIVE" and created:
            db.execute("UPDATE grilling_sessions SET status = 'GENERATING' WHERE session_id = ?", (session_id,))
            session = db.execute("SELECT * FROM grilling_sessions WHERE session_id = ?", (session_id,)).fetchone()
        elif session["status"] != "GENERATING":
            return _render(session)
    return _advance(db, session, idea)


def _advance(db, session, idea):
    with process_lock(f"grilling-{session['idea_id']}.lock") as acquired:
        if not acquired:
            current = db.execute("SELECT * FROM grilling_sessions WHERE session_id = ?", (session["session_id"],)).fetchone()
            return _render(current)
        session = db.execute("SELECT * FROM grilling_sessions WHERE session_id = ?", (session["session_id"],)).fetchone()
        if session is None or session["status"] != "GENERATING":
            raise RuntimeError("The Grilling session changed before generation started.")
        idea = _assert_current(db, session)
        config = json.loads(session["config_json"])
        questions, summary = generate_round(db, session["project_path"], grilling_prompt(idea, session), config)
        with write(db):
            current = db.execute("SELECT * FROM grilling_sessions WHERE session_id = ?", (session["session_id"],)).fetchone()
            if current is None or current["status"] != "GENERATING":
                raise RuntimeError("The Grilling session changed while its response was generated. Refresh the session.")
            _assert_current(db, current)
            status = "CONFIRM" if not questions else "ACTIVE"
            db.execute("""UPDATE grilling_sessions SET status = ?, round_no = round_no + 1,
                questions_json = ?, summary = ?, updated_at = CURRENT_TIMESTAMP WHERE session_id = ?""",
                (status, json.dumps(questions, ensure_ascii=False), summary, session["session_id"]))
            result = db.execute("SELECT * FROM grilling_sessions WHERE session_id = ?", (session["session_id"],)).fetchone()
            return _render(result)


def answer_grilling(db, session_id, round_no, answer, board_key=""):
    answer = answer.strip()
    if not answer:
        raise RuntimeError("Enter an answer for this Grilling round.")
    with write(db):
        session = db.execute("SELECT * FROM grilling_sessions WHERE session_id = ?", (session_id,)).fetchone()
        if session is None:
            raise RuntimeError("Unknown Grilling session.")
        if session["board_key"] != board_key:
            raise RuntimeError("This Grilling session belongs to another board.")
        previous = next((item for item in json.loads(session["answers_json"]) if item["round"] == round_no), None)
        if previous and previous["answer"] == answer and session["status"] in ("GENERATING", "CONFIRM", "PLAN_GENERATING", "CONFIRMED"):
            return _render(session)
        if round_no < session["round_no"]:
            if previous and previous["answer"] == answer:
                return _render(session)
            raise RuntimeError("This Grilling round is stale.")
        if round_no != session["round_no"] or session["status"] != "ACTIVE":
            raise RuntimeError("This Grilling round is not accepting answers.")
        idea = _assert_current(db, session)
        answers = json.loads(session["answers_json"])
        answers.append({"round": round_no, "questions": json.loads(session["questions_json"]), "answer": answer})
        db.execute("UPDATE grilling_sessions SET status = 'GENERATING', answers_json = ?, updated_at = CURRENT_TIMESTAMP WHERE session_id = ?",
                   (json.dumps(answers, ensure_ascii=False), session_id))
        session = db.execute("SELECT * FROM grilling_sessions WHERE session_id = ?", (session_id,)).fetchone()
    return _advance(db, session, idea)


def cancel_grilling(db, session_id, board_key=""):
    with write(db):
        row = db.execute("SELECT board_key FROM grilling_sessions WHERE session_id = ?", (session_id,)).fetchone()
        if row is None or row["board_key"] != board_key:
            raise RuntimeError("Unknown Grilling session.")
        cursor = db.execute("UPDATE grilling_sessions SET status = 'CANCELLED', updated_at = CURRENT_TIMESTAMP "
                            "WHERE session_id = ? AND status IN ('ACTIVE', 'GENERATING', 'CONFIRM')", (session_id,))
        if not cursor.rowcount:
            raise RuntimeError("No active Grilling session to cancel.")


def confirm_grilling(db, session_id, round_no, board_key=""):
    session = db.execute("SELECT idea_id FROM grilling_sessions WHERE session_id = ?", (session_id,)).fetchone()
    if session is None:
        raise RuntimeError("Unknown Grilling session.")
    with process_lock(f"grilling-{session['idea_id']}.lock") as acquired:
        if not acquired:
            raise RuntimeError("The Grilling session is still processing. Refresh and try again.")
        return _confirm_grilling_locked(db, session_id, round_no, board_key)


def _confirm_grilling_locked(db, session_id, round_no, board_key):
    session = db.execute("SELECT * FROM grilling_sessions WHERE session_id = ?", (session_id,)).fetchone()
    if session is None:
        raise RuntimeError("Unknown Grilling session.")
    if session["board_key"] != board_key:
        raise RuntimeError("This Grilling session belongs to another board.")
    if session["status"] == "CONFIRMED":
        raise RuntimeError("This Grilling session has already created its ToDos.")
    if session["status"] != "CONFIRM" or round_no != session["round_no"]:
        raise RuntimeError("Confirm the current Grilling specification first.")
    with write(db):
        current = db.execute("SELECT * FROM grilling_sessions WHERE session_id = ?", (session_id,)).fetchone()
        if current is None or current["status"] != "CONFIRM":
            raise RuntimeError("The Grilling session is no longer awaiting confirmation.")
        _assert_current(db, current)
        idea = idea_record(db, current["idea_id"])
        db.execute("UPDATE grilling_sessions SET status = 'PLAN_GENERATING' WHERE session_id = ?", (session_id,))
    from .planner import planning_prompt
    prompt = planning_prompt(
        idea, planning_config(db, idea)["allowed_models"].split(",") if planning_config(db, idea)["allowed_models"] else [],
        planning_config(db, idea)["implementation_model"])
    try:
        return _create_confirmed_plan(db, session, idea, prompt)
    except Exception:
        with write(db):
            db.execute("UPDATE grilling_sessions SET status = 'CONFIRM' WHERE session_id = ? AND status = 'PLAN_GENERATING'",
                       (session_id,))
        raise


def _create_confirmed_plan(db, session, idea, prompt):
    config = planning_config(db, idea)
    models = config["allowed_models"].split(",") if config["allowed_models"] else []
    prompt += "\n\n# User-confirmed specification\n" + session["summary"]
    auto = session["branch_mode"] == "auto"
    if auto:
        question, todos, recommendation, reason = generate_plan(db, idea["path"], prompt, config, models, review_branch=True)
    else:
        question, todos = generate_plan(db, idea["path"], prompt, config, models)
    if question:
        raise RuntimeError(question)
    with write(db):
        current = db.execute("SELECT * FROM grilling_sessions WHERE session_id = ?", (session["session_id"],)).fetchone()
        if current is None or current["status"] != "PLAN_GENERATING":
            raise RuntimeError("The Grilling session changed while its ToDo plan was generated.")
        _assert_current(db, current)
        if idea_record(db, idea["id"])["planned_at"]:
            raise RuntimeError("The Idea was planned by another request.")
        if auto:
            context = plan_context(db, idea)
            draft = {"context": context, "todos": todos, "branch_mode": recommendation, "reason": reason}
            db.execute("UPDATE ideas SET draft_plan = ?, planning_error = NULL WHERE id = ?",
                       (json.dumps(draft, ensure_ascii=False), idea["id"]))
            db.execute("UPDATE grilling_sessions SET status = 'CONFIRMED', approved = 1 WHERE session_id = ?",
                       (session["session_id"],))
            return {"status": "AWAITING_BRANCH", "branch_mode": recommendation, "reason": reason, "todos": todos}
        task_ids = []
        priority = int(settings(db)["priority"])
        for position, task in enumerate(todos, 1):
            cursor = db.execute("""INSERT INTO tasks(project_id, idea_id, name, abstract, prompt, priority,
                implementation_model, idea_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (idea["project_id"], idea["id"], task["title"].strip(), task["abstract"].strip(), task["plan"].strip(),
                 priority, task["model"] if models else None, position))
            task_ids.append(cursor.lastrowid)
        db.execute("UPDATE ideas SET planned_at = CURRENT_TIMESTAMP, planning_error = NULL, branch_mode = ? WHERE id = ?",
                   (session["branch_mode"], idea["id"]))
        db.execute("UPDATE grilling_sessions SET status = 'CONFIRMED', approved = 1 WHERE session_id = ?",
                   (session["session_id"],))
        return {"status": "PLANNED", "task_ids": task_ids}
