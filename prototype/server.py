"""JiuJiu Crew bridge MVP. Python stdlib only. Local development prototype."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get("CREW_DB", str(ROOT / "data" / "crew.sqlite3")))
HOST = os.environ.get("CREW_HOST", "127.0.0.1")
PORT = int(os.environ.get("CREW_PORT", "8787"))
BOOTSTRAP_SECRET = os.environ.get("CREW_BOOTSTRAP_SECRET", "")
LOCK = threading.Lock()
FREQUENCY = defaultdict(deque)
NAME_PATTERN = re.compile(r"[\w\-]{1,32}", re.UNICODE)
ROOM_MESSAGE = re.compile(r"^/api/rooms/([a-zA-Z0-9_-]+)/messages$")
ROOM_ASSEMBLE = re.compile(r"^/api/rooms/([a-zA-Z0-9_-]+)/assemble$")
MAX_JSON_BYTES = 16000


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@contextmanager
def connect():
    db = sqlite3.connect(DB, timeout=10)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def setup():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
          CREATE TABLE IF NOT EXISTS members (
            id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            role TEXT NOT NULL, joined_at TEXT NOT NULL
          );
          CREATE TABLE IF NOT EXISTS credentials (
            token_hash TEXT PRIMARY KEY, member_id TEXT NOT NULL REFERENCES members(id)
          );
          CREATE TABLE IF NOT EXISTS invites (
            code_hash TEXT PRIMARY KEY, created_by TEXT NOT NULL REFERENCES members(id),
            used_by TEXT REFERENCES members(id), created_at TEXT NOT NULL
          );
          CREATE TABLE IF NOT EXISTS rooms (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, created_by TEXT,
            created_at TEXT NOT NULL
          );
          CREATE TABLE IF NOT EXISTS messages (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT NOT NULL REFERENCES rooms(id),
            sender_id TEXT NOT NULL REFERENCES members(id), body TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'message', created_at TEXT NOT NULL
          );
          CREATE TABLE IF NOT EXISTS inbox (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            receiver_id TEXT NOT NULL REFERENCES members(id),
            message_seq INTEGER NOT NULL REFERENCES messages(seq),
            kind TEXT NOT NULL,
            UNIQUE (receiver_id, message_seq, kind)
          );
          INSERT OR IGNORE INTO rooms (id, name, created_by, created_at)
            VALUES ('lobby', '公共大厅', NULL, '2026-01-01T00:00:00+00:00');
        """)


def json_dict(text):
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("JSON body must be an object")
    return obj


def rate_limit(member):
    # Process-local guard against accidental response loops; not a distributed quota.
    current = time.monotonic()
    with LOCK:
        q = FREQUENCY[member]
        while q and q[0] < current - 60:
            q.popleft()
        if len(q) >= 30:
            return False
        q.append(current)
    return True


def create_account(db, name, role):
    name = name.strip()
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError("Name must be 1-32 letters/numbers/underscores/hyphens")
    identity = "m_" + secrets.token_hex(9)
    token = "crew_" + secrets.token_urlsafe(32)
    db.execute("INSERT INTO members VALUES (?, ?, ?, ?)", (identity, name, role, now()))
    db.execute("INSERT INTO credentials VALUES (?, ?)", (sha(token), identity))
    return {"member": {"id": identity, "name": name, "role": role}, "token": token}


def get_mentions(db, body, explicit_ids):
    members = db.execute("SELECT id, name FROM members").fetchall()
    valid = {m["id"] for m in members}
    if explicit_ids is None:
        explicit_ids = []
    if not isinstance(explicit_ids, list) or len(explicit_ids) > 50 or any(not isinstance(x, str) for x in explicit_ids):
        raise ValueError("mentions must be an array of member IDs")
    if any(x not in valid for x in explicit_ids):
        raise ValueError("unknown mentioned member")
    matched = set(explicit_ids)
    for m in members:
        # Do not match @A within @Aster; avoid accidental pings.
        if re.search(r"(?<![\w])@" + re.escape(m["name"]) + r"(?![\w])", body, flags=re.IGNORECASE):
            matched.add(m["id"])
    return matched


def add_message(db, room, sender, body, kind="message", target_ids=None):
    if not db.execute("SELECT 1 FROM rooms WHERE id=?", (room,)).fetchone():
        raise LookupError("Room not found")
    if not isinstance(body, str) or not 1 <= len(body.strip()) <= 4000:
        raise ValueError("Message must be 1-4000 characters")
    members = get_mentions(db, body, target_ids)
    if kind == "assembly":
        members = {r["id"] for r in db.execute("SELECT id FROM members")}
    members.discard(sender)
    cursor = db.execute(
        "INSERT INTO messages(room_id,sender_id,body,kind,created_at) VALUES(?,?,?,?,?)",
        (room, sender, body.strip(), kind, now()),
    )
    seq = cursor.lastrowid
    for member in members:
        db.execute("INSERT INTO inbox(receiver_id,message_seq,kind) VALUES (?,?,?)", (member, seq, kind))
    return {"seq": seq, "queued_for": len(members), "delivery": "inbox_only"}


class CrewHandler(BaseHTTPRequestHandler):
    server_version = "JiuJiuCrewBridge/0.1"

    def log_message(self, fmt, *args):
        if os.environ.get("CREW_QUIET") != "1":
            super().log_message(fmt, *args)

    def send_json(self, status, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def read_json(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 2 or size > MAX_JSON_BYTES:
                raise ValueError("Invalid body length")
            return json_dict(self.rfile.read(size).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as e:
            raise ValueError("Invalid JSON") from e

    def identity(self, db):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[7:]
        if len(token) > 300:
            return None
        row = db.execute("""
            SELECT members.id, members.name, members.role
            FROM credentials JOIN members ON members.id = credentials.member_id
            WHERE credentials.token_hash=?
        """, (sha(token),)).fetchone()
        return dict(row) if row else None

    def require(self, db):
        who = self.identity(db)
        if who is None:
            self.send_json(401, {"error": "Unauthorized: provide your member token"})
        return who

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/health":
            return self.send_json(200, {"ok": True, "mode": "local-bridge-mvp"})
        if path in ("/", "/app.js", "/style.css"):
            filename = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[path]
            mime = {"/": "text/html", "/app.js": "text/javascript", "/style.css": "text/css"}[path]
            content = (ROOT / filename).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(content)
        with connect() as db:
            who = self.require(db)
            if not who:
                return
            if path == "/api/me":
                return self.send_json(200, {"member": who})
            if path == "/api/members":
                return self.send_json(200, {"members": [dict(x) for x in db.execute("SELECT id,name,role FROM members ORDER BY joined_at")]})
            if path == "/api/rooms":
                return self.send_json(200, {"rooms": [dict(x) for x in db.execute("SELECT id,name,created_at FROM rooms ORDER BY created_at")]})
            args = parse_qs(parsed.query)
            if path == "/api/inbox":
                cursor = self.cursor(args)
                if cursor is None:
                    return
                rows = db.execute("""
                    SELECT i.seq, i.kind, m.seq AS message_seq, m.room_id, m.body, m.created_at,
                           sender.name AS sender_name, sender.id AS sender_id
                    FROM inbox i JOIN messages m ON m.seq=i.message_seq
                    JOIN members sender ON sender.id=m.sender_id
                    WHERE i.receiver_id=? AND i.seq>? ORDER BY i.seq LIMIT 100
                """, (who["id"], cursor)).fetchall()
                return self.send_json(200, {"notifications": [dict(x) for x in rows]})
            found = ROOM_MESSAGE.fullmatch(path)
            if found:
                room = found.group(1)
                if not db.execute("SELECT 1 FROM rooms WHERE id=?", (room,)).fetchone():
                    return self.send_json(404, {"error": "Room not found"})
                cursor = self.cursor(args)
                if cursor is None:
                    return
                rows = db.execute("""
                    SELECT m.seq, m.room_id,m.body,m.kind,m.created_at,
                           sender.name AS sender_name,sender.id AS sender_id
                    FROM messages m JOIN members sender ON sender.id=m.sender_id
                    WHERE m.room_id=? AND m.seq>? ORDER BY m.seq LIMIT 100
                """, (room, cursor)).fetchall()
                return self.send_json(200, {"messages": [dict(x) for x in rows]})
            return self.send_json(404, {"error": "Not found"})

    def cursor(self, query):
        try:
            cursor = int(query.get("after", ["0"])[0])
            if cursor < 0:
                raise ValueError
            return cursor
        except ValueError:
            self.send_json(400, {"error": "after must be a nonnegative integer"})
            return None

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self.read_json()
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        with connect() as db:
            if path == "/api/bootstrap":
                # Only the first account can bootstrap, using a secret set locally.
                if not BOOTSTRAP_SECRET:
                    return self.send_json(503, {"error": "Set CREW_BOOTSTRAP_SECRET before initial registration"})
                if not secrets.compare_digest(str(data.get("secret", "")), BOOTSTRAP_SECRET):
                    return self.send_json(403, {"error": "Invalid bootstrap secret"})
                try:
                    db.execute("BEGIN IMMEDIATE")
                    if db.execute("SELECT 1 FROM members LIMIT 1").fetchone():
                        return self.send_json(409, {"error": "Owner already created"})
                    result = create_account(db, str(data.get("name", "")), "owner")
                    db.commit()
                    return self.send_json(201, result)
                except (ValueError, sqlite3.IntegrityError) as e:
                    return self.send_json(400, {"error": str(e)})
            if path == "/api/register":
                code = str(data.get("invite", ""))
                try:
                    db.execute("BEGIN IMMEDIATE")
                    invitation = db.execute("SELECT used_by FROM invites WHERE code_hash=?", (sha(code),)).fetchone()
                    if not invitation or invitation["used_by"] is not None:
                        return self.send_json(403, {"error": "Invitation invalid or already used"})
                    result = create_account(db, str(data.get("name", "")), "member")
                    db.execute("UPDATE invites SET used_by=? WHERE code_hash=?", (result["member"]["id"], sha(code)))
                    db.commit()
                    return self.send_json(201, result)
                except (ValueError, sqlite3.IntegrityError) as e:
                    return self.send_json(400, {"error": str(e)})
            who = self.require(db)
            if not who:
                return
            if path == "/api/invites":
                if who["role"] != "owner":
                    return self.send_json(403, {"error": "Only the group owner can invite"})
                code = "invite_" + secrets.token_urlsafe(24)
                db.execute("INSERT INTO invites VALUES (?, ?, NULL, ?)", (sha(code), who["id"], now()))
                db.commit()
                return self.send_json(201, {"invite": code})
            if path == "/api/rooms":
                name = data.get("name")
                if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
                    return self.send_json(400, {"error": "Room name must be 1-60 characters"})
                room_id = "r_" + secrets.token_hex(6)
                db.execute("INSERT INTO rooms VALUES (?, ?, ?, ?)", (room_id, name.strip(), who["id"], now()))
                db.commit()
                return self.send_json(201, {"room": {"id": room_id, "name": name.strip()}})
            found = ROOM_MESSAGE.fullmatch(path)
            is_assemble = False
            if not found:
                found = ROOM_ASSEMBLE.fullmatch(path)
                is_assemble = bool(found)
            if found:
                if not rate_limit(who["id"]):
                    return self.send_json(429, {"error": "Slow down: max 30 messages per minute"})
                try:
                    body = data.get("body")
                    targets = data.get("mentions", [])
                    result = add_message(db, found.group(1), who["id"], body,
                                         "assembly" if is_assemble else "message", targets)
                    db.commit()
                    return self.send_json(201, result)
                except ValueError as e:
                    return self.send_json(400, {"error": str(e)})
                except LookupError as e:
                    return self.send_json(404, {"error": str(e)})
            return self.send_json(404, {"error": "Not found"})


def main():
    setup()
    server = ThreadingHTTPServer((HOST, PORT), CrewHandler)
    print(f"JiuJiu Crew bridge MVP: http://{HOST}:{server.server_address[1]}", flush=True)
    if HOST not in {"localhost", "127.0.0.1", "::1"}:
        print("WARNING: Development server has no TLS or production hardening. Do not expose to the Internet.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()