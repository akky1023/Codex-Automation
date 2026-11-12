import base64
from collections import defaultdict
from datetime import datetime, timezone
from http.client import HTTPException
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4
import webbrowser
from xml.etree import ElementTree as ET

from . import credentials
from .processes import process_lock
from .store import get_setting, set_setting, settings, update, write


DAV = "{DAV:}"
CAL = "{urn:ietf:params:xml:ns:caldav}"
CONTENT_LINE = re.compile(r'^((?:[^:"]|"[^"]*")*):(.*)$')


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def origin(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("Enter an HTTPS Nextcloud URL without credentials.")
    return parsed.hostname, parsed.port or 443


class Client:
    def __init__(self, server, auth=None):
        self.server = server
        self.origin = origin(server)
        self.auth = auth
        self.opener = build_opener(RejectRedirects())

    def href(self, value, base=None):
        url = urljoin(base or self.server + "/", value)
        if origin(url) != self.origin or urlsplit(url).fragment:
            raise RuntimeError("Nextcloud returned a URL outside its configured origin.")
        return url

    def request(self, method, url, body=None, headers=None, acceptable=()):
        url = self.href(url)
        headers = {"User-Agent": "Codex Queue", **(headers or {})}
        if self.auth:
            token = base64.b64encode(":".join(self.auth).encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=20) as response:
                content = response.read(20 * 1024 * 1024 + 1)
                if len(content) > 20 * 1024 * 1024:
                    raise RuntimeError("Nextcloud response exceeds the size limit.")
                return response.status, response.headers, content
        except HTTPError as error:
            if error.code in acceptable:
                return error.code, error.headers, b""
            if error.code in (401, 403):
                raise RuntimeError("Nextcloud authorization failed. Run cq nextcloud connect.") from None
            raise RuntimeError(f"Nextcloud returned HTTP {error.code} for {method}.") from None
        except (URLError, HTTPException, TimeoutError):
            raise RuntimeError(f"Nextcloud connection failed during {method}.") from None

    def multistatus(self, method, url, document, depth):
        _, _, body = self.request(method, url, document.encode(), {
            "Depth": str(depth), "Content-Type": "application/xml; charset=utf-8",
        })
        root = ET.fromstring(body)
        if root.tag != DAV + "multistatus":
            raise RuntimeError("Expected a Nextcloud DAV multistatus response.")
        for response in root.findall(DAV + "response"):
            path = response.findtext(DAV + "href")
            props = {}
            if not path:
                raise RuntimeError("Invalid Nextcloud DAV response.")
            status = response.findtext(DAV + "status", "").split()
            if status and (len(status) < 2 or status[1] != "200"):
                raise RuntimeError("Nextcloud could not confirm all linked task resources.")
            for part in response.findall(DAV + "propstat"):
                status = part.findtext(DAV + "status", "").split()
                if len(status) > 1 and status[1] == "200":
                    props.update((element.tag, element) for element in part.find(DAV + "prop"))
            yield self.href(path, url), props

    def propfind(self, url, properties, depth=0):
        return self.multistatus(
            "PROPFIND", url,
            f'<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>{properties}</d:prop></d:propfind>',
            depth,
        )

    def calendars(self):
        dav = self.server + "/remote.php/dav/"
        _, principal_props = next(self.propfind(dav, "<d:current-user-principal/>"))
        principal = principal_props[DAV + "current-user-principal"].findtext(DAV + "href")
        _, home_props = next(self.propfind(self.href(principal, dav), "<c:calendar-home-set/>"))
        home = self.href(home_props[CAL + "calendar-home-set"].findtext(DAV + "href"), dav)
        for url, props in self.propfind(home, "<d:displayname/><d:resourcetype/><c:supported-calendar-component-set/>", 1):
            kind = props.get(DAV + "resourcetype")
            supported = props.get(CAL + "supported-calendar-component-set")
            if kind is not None and kind.find(CAL + "calendar") is not None and (
                supported is None or any(item.get("name") == "VTODO" for item in supported)
            ):
                name = props.get(DAV + "displayname")
                yield url, name.text if name is not None else url

    def todos(self, url):
        document = """<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
        <d:prop><d:getetag/><c:calendar-data/></d:prop>
        <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VTODO"/></c:comp-filter></c:filter>
        </c:calendar-query>"""
        for resource, props in self.multistatus("REPORT", url, document, 1):
            if CAL + "calendar-data" not in props or DAV + "getetag" not in props:
                raise RuntimeError("Nextcloud returned incomplete task data.")
            fields = todo_fields(props[CAL + "calendar-data"].text)
            if fields:
                yield resource, props[DAV + "getetag"].text, fields


def todo_lines(body):
    stack = []
    raw = []
    for line in (body or "").splitlines():
        if raw and line.startswith((" ", "\t")):
            raw[-1].append(line)
        else:
            raw.append([line])
    for physical in raw:
        line = physical[0] + "".join(part[1:] for part in physical[1:])
        match = CONTENT_LINE.match(line)
        name, value = match.groups() if match else ("", "")
        key = name.partition(";")[0].upper()
        current = tuple(stack)
        if key == "BEGIN":
            stack.append(value.upper())
        elif key == "END" and stack:
            stack.pop()
        yield physical, key, value, current


def todo_fields(body):
    fields = {}
    count = 0
    for _, key, value, stack in todo_lines(body):
        if stack == ("VCALENDAR",) and key == "BEGIN" and value.upper() == "VTODO":
            count += 1
        if stack == ("VCALENDAR", "VTODO"):
            fields.setdefault(key, value)
    if count != 1 or not fields.get("UID"):
        raise RuntimeError("Expected exactly one VTODO with a UID in each CalDAV resource.")
    return fields


def decode_text(value):
    return re.sub(r"\\([nN,;\\])", lambda match: "\n" if match[1] in "nN" else match[1], value)


def completed_body(body, timestamp):
    lines = []
    for physical, key, _, stack in todo_lines(body):
        if stack == ("VCALENDAR", "VTODO"):
            if key in ("STATUS", "COMPLETED", "PERCENT-COMPLETE"):
                continue
            if key == "END":
                lines += ["STATUS:COMPLETED", "PERCENT-COMPLETE:100", f"COMPLETED:{timestamp}"]
        lines.extend(physical)
    return "\r\n".join(lines) + "\r\n"


def connection(db):
    server = get_setting(db, "nextcloud_server")
    target = get_setting(db, "nextcloud_credential")
    if not server or not target:
        raise RuntimeError("Run cq nextcloud connect first.")
    try:
        auth = credentials.load(target)
    except OSError:
        raise RuntimeError("Nextcloud credentials are unavailable. Run cq nextcloud connect.") from None
    return Client(server, auth), get_setting(db, "nextcloud_connection")


def connect(db):
    server = input("Nextcloud HTTPS URL: ").strip().rstrip("/")
    if urlsplit(server).query or urlsplit(server).fragment:
        raise RuntimeError("Enter the server base URL.")
    credentials.api()
    client = Client(server)
    _, _, response = client.request("POST", server + "/index.php/login/v2", b"")
    flow = json.loads(response)
    login = client.href(flow["login"])
    endpoint = client.href(flow["poll"]["endpoint"])
    data = urlencode({"token": flow["poll"]["token"]}).encode()
    print("Authorize Codex Queue in the browser.")
    webbrowser.open(login)
    deadline = time.monotonic() + 20 * 60
    while time.monotonic() < deadline:
        status, _, response = client.request(
            "POST", endpoint, data,
            {"Content-Type": "application/x-www-form-urlencoded"}, acceptable=(404,),
        )
        if status != 404:
            break
        time.sleep(5)
    else:
        raise RuntimeError("Nextcloud login expired.")
    result = json.loads(response)
    server = client.href(result["server"]).rstrip("/")
    old_target = get_setting(db, "nextcloud_credential")
    identity = uuid4().hex
    same = server == get_setting(db, "nextcloud_server") and result["loginName"] == get_setting(db, "nextcloud_login")
    target = "CodexQueue/Nextcloud/" + identity
    credentials.save(target, result["loginName"], result["appPassword"])
    try:
        with write(db):
            if not same:
                db.execute("DELETE FROM remote_links")
            for key, value in {
                "nextcloud_server": server, "nextcloud_credential": target,
                "nextcloud_login": result["loginName"],
                "nextcloud_connection": get_setting(db, "nextcloud_connection") if same else identity,
                "nextcloud_error": "",
            }.items():
                set_setting(db, key, value)
    except BaseException:
        credentials.delete(target)
        raise
    if old_target:
        credentials.delete(old_target)
    print("Nextcloud connected.")


def disconnect(db):
    target = get_setting(db, "nextcloud_credential")
    if target:
        credentials.delete(target)
    with write(db):
        set_setting(db, "nextcloud_credential", "")
        set_setting(db, "nextcloud_error", "")
    print("Nextcloud disconnected.")


def settle_move(db, task_id):
    row = db.execute("SELECT * FROM remote_tasks WHERE task_id = ?", (task_id,)).fetchone()
    if row is None or (not row["pending_remove"] and row["pending_project"] is None):
        return
    if row["pending_remove"]:
        update(db, "tasks", task_id, {"status": "ABORTED"})
    else:
        update(db, "tasks", task_id, {"status": "QUEUED", "project_id": row["pending_project"]})
    db.execute("UPDATE remote_tasks SET pending_project = NULL, pending_remove = 0 WHERE task_id = ?", (task_id,))


def apply_snapshot(db, identity, links, snapshot, failed):
    if failed:
        db.execute(
            """UPDATE remote_tasks SET blocked = 1 WHERE connection = ? AND task_id IN
               (SELECT id FROM tasks WHERE status IN ('QUEUED', 'RESUME_QUEUED'))""", (identity,),
        )
        return
    existing = {row["uid"]: row for row in db.execute(
        """SELECT r.*, t.status, t.project_id FROM remote_tasks r JOIN tasks t ON t.id = r.task_id
           WHERE r.connection = ?""", (identity,),
    )}
    default = int(settings(db)["priority"])
    for uid, items in snapshot.items():
        row = existing.pop(uid, None)
        if len(items) != 1:
            if row:
                db.execute("UPDATE remote_tasks SET blocked = 1 WHERE task_id = ?", (row["task_id"],))
            continue
        collection, url, etag, fields = items[0]
        remote_done = fields.get("STATUS", "").upper() in ("COMPLETED", "CANCELLED")
        title = decode_text(fields.get("SUMMARY", "")) or "(untitled)"
        prompt = decode_text(fields.get("DESCRIPTION", ""))
        priority = fields.get("PRIORITY", "")
        priority = int(priority) if priority in "123456789" and len(priority) == 1 else default
        project = links[collection]
        if row is None:
            if not remote_done:
                cursor = db.execute(
                    "INSERT INTO tasks(project_id, name, prompt, priority) VALUES (?, ?, ?, ?)",
                    (project, title, prompt, priority),
                )
                db.execute(
                    "INSERT INTO remote_tasks(connection,uid,task_id,list,url,etag) VALUES (?,?,?,?,?,?)",
                    (identity, uid, cursor.lastrowid, collection, url, etag),
                )
            continue
        task_id = row["task_id"]
        status = row["status"]
        if status == "DONE":
            db.execute("UPDATE remote_tasks SET list=?, url=? WHERE task_id=?", (collection, url, task_id))
            continue
        if status in ("REMOVED", "ABORTED"):
            continue
        moved = project != row["project_id"]
        db.execute(
            """UPDATE remote_tasks SET list=?, url=?, etag=?, blocked=0,
               pending_project=?, pending_remove=? WHERE task_id=?""",
            (collection, url, etag if status == "QUEUED" or moved else row["etag"],
             project if moved and not remote_done else None, int(remote_done), task_id),
        )
        if status != "RUNNING":
            settle_move(db, task_id)
        if not remote_done and (status == "QUEUED" or moved):
            update(db, "tasks", task_id, {"name": title, "prompt": prompt, "priority": priority})
    for row in existing.values():
        if row["list"] not in links or row["status"] in ("DONE", "REMOVED", "ABORTED"):
            continue
        db.execute("UPDATE remote_tasks SET pending_remove=1, pending_project=NULL WHERE task_id=?", (row["task_id"],))
        if row["status"] != "RUNNING":
            settle_move(db, row["task_id"])


def writeback(db, client, row, force=False):
    task_id = row["task_id"]
    timestamp = datetime.fromisoformat(row["completed_at"]).replace(tzinfo=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    status, headers, raw = client.request("GET", row["url"], acceptable=(404, 410))
    outcome = "conflict"
    if status == 200:
        body = raw.decode("utf-8")
        fields = todo_fields(body)
        if fields.get("UID") == row["uid"]:
            if fields.get("STATUS", "").upper() == "COMPLETED":
                outcome = "synced"
            elif headers.get("ETag") and (force or headers.get("ETag") == row["etag"]):
                result, _, _ = client.request(
                    "PUT", row["url"], completed_body(body, timestamp).encode(),
                    {"Content-Type": "text/calendar; charset=utf-8", "If-Match": headers["ETag"]},
                    acceptable=(404, 410, 412),
                )
                if result in (200, 201, 204):
                    outcome = "synced"
                elif result == 412:
                    current_status, _, current_raw = client.request("GET", row["url"], acceptable=(404, 410))
                    if current_status == 200:
                        current = todo_fields(current_raw.decode("utf-8"))
                        if current.get("UID") == row["uid"] and current.get("STATUS", "").upper() == "COMPLETED":
                            outcome = "synced"
    with write(db):
        db.execute("UPDATE remote_tasks SET writeback=? WHERE task_id=?", (outcome, task_id))
    return outcome


def sync(db):
    if not get_setting(db, "nextcloud_credential"):
        return True
    with process_lock("sync.lock") as acquired:
        if not acquired:
            return False
        try:
            client, identity = connection(db)
            links = dict(db.execute("SELECT list, project_id FROM remote_links"))
            snapshot = defaultdict(list)
            errors = []
            for collection in links:
                try:
                    for url, etag, fields in client.todos(collection):
                        snapshot[fields["UID"]].append((collection, url, etag, fields))
                except (RuntimeError, ET.ParseError, ValueError) as error:
                    errors.append(str(error))
            with write(db):
                if links != dict(db.execute("SELECT list, project_id FROM remote_links")):
                    raise RuntimeError("Nextcloud list links changed during synchronization.")
                apply_snapshot(db, identity, links, snapshot, errors)
            if errors:
                raise RuntimeError(errors[0])
            for row in db.execute(
                """SELECT r.*, t.completed_at FROM remote_tasks r JOIN tasks t ON t.id=r.task_id
                   WHERE r.connection=? AND t.status='DONE' AND r.writeback IS NULL""", (identity,),
            ).fetchall():
                writeback(db, client, row)
        except (RuntimeError, ET.ParseError, ValueError, KeyError, StopIteration) as error:
            with write(db):
                set_setting(db, "nextcloud_error", str(error) or "Invalid Nextcloud response.")
                db.execute("UPDATE remote_tasks SET blocked=1 WHERE connection=?",
                           (get_setting(db, "nextcloud_connection"),))
            return False
        with write(db):
            set_setting(db, "nextcloud_error", "")
        return True


def add_commands(commands):
    commands.add_parser("sync", help="Synchronize linked Nextcloud Tasks lists")
    parser = commands.add_parser("nextcloud", help="Manage Nextcloud Tasks integration")
    actions = parser.add_subparsers(dest="nc_action", required=True)
    for name in ("connect", "disconnect", "lists"):
        actions.add_parser(name)
    link = actions.add_parser("link")
    link.add_argument("--list", required=True)
    link.add_argument("--project-id", type=int, required=True)
    resolve = actions.add_parser("resolve")
    resolve.add_argument("id", type=int)
    choice = resolve.add_mutually_exclusive_group(required=True)
    choice.add_argument("--complete", action="store_true")
    choice.add_argument("--keep-remote", action="store_true")


def dispatch(db, args):
    if args.command == "sync":
        synced = sync(db)
        if error := get_setting(db, "nextcloud_error"):
            raise RuntimeError(error)
        if not synced:
            raise RuntimeError("Nextcloud sync is already in progress.")
        from .actions import wake
        wake(db)
        return
    action = args.nc_action
    if action in ("connect", "disconnect", "link", "resolve"):
        with process_lock("sync.lock") as acquired:
            if not acquired:
                raise RuntimeError("Nextcloud sync is in progress. Try again after it finishes.")
            try:
                manage_connection(db, args)
            except (ET.ParseError, ValueError, KeyError, StopIteration):
                raise RuntimeError("Nextcloud returned an invalid response.") from None
    else:
        client, _ = connection(db)
        try:
            for url, name in client.calendars():
                print(f"{name}\t{url}")
        except (ET.ParseError, ValueError, KeyError, StopIteration):
            raise RuntimeError("Nextcloud returned an invalid calendar list.") from None


def manage_connection(db, args):
    action = args.nc_action
    if action == "connect":
        connect(db)
        return
    if action == "disconnect":
        disconnect(db)
        return
    client, identity = connection(db)
    if action == "link":
        if args.list not in dict(client.calendars()):
            raise RuntimeError("Unknown Nextcloud list. Use cq nextcloud lists.")
        with write(db):
            if not db.execute("SELECT 1 FROM projects WHERE id=? AND retired=0", (args.project_id,)).fetchone():
                raise RuntimeError(f"Unknown project ID {args.project_id}")
            db.execute(
                "INSERT INTO remote_links(list,project_id) VALUES (?,?) ON CONFLICT(list) DO UPDATE SET project_id=excluded.project_id",
                (args.list, args.project_id),
            )
        print("Nextcloud list linked.")
        return
    row = db.execute(
        """SELECT r.*, t.completed_at FROM remote_tasks r JOIN tasks t ON t.id=r.task_id
           WHERE r.task_id=? AND r.connection=? AND r.writeback='conflict'""",
        (args.id, identity),
    ).fetchone()
    if row is None:
        raise RuntimeError("No Nextcloud completion conflict for that task.")
    if args.complete:
        print(writeback(db, client, row, force=True))
    else:
        with write(db):
            db.execute("UPDATE remote_tasks SET writeback='kept_remote' WHERE task_id=?", (args.id,))
        print("Kept the remote task.")
