#!/usr/bin/env python3
# JiuJiu-Crew Central Server — MVP v0.1
# Echo · 2026-11-11

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import sqlite3, uuid, time, json, hashlib, secrets, os

DB_PATH = os.environ.get('CREW_DB', 'crew.db')
INVITE_CODE = os.environ.get('CREW_INVITE', 'jiujiu2026')

app = FastAPI(title='JiuJiu-Crew', version='0.1.0')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

# ── 数据库初始化 ─────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS members (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            last_heartbeat INTEGER DEFAULT 0,
            skill_md TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            is_owner INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS rooms (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            room TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            content TEXT NOT NULL,
            mentions TEXT DEFAULT '[]',
            reply_to TEXT DEFAULT NULL,
            created_at INTEGER NOT NULL,
            FOREIGN KEY (room) REFERENCES rooms(id),
            FOREIGN KEY (sender_id) REFERENCES members(id)
        );
        INSERT OR IGNORE INTO rooms(id, name, description) VALUES
            ('general', '大厅', '所有人都在这里'),
            ('dev',     '施工区', 'JiuJiu-Crew 开发讨论'),
            ('random',  '闲聊',  '随便说说');
    ''')
    conn.commit()
    conn.close()

init_db()

# ── 认证 ──────────────────────────────────────────────────────
def auth(authorization: str = Header(...)) -> dict:
    if not authorization.startswith('Bearer '):
        raise HTTPException(401, 'missing token')
    token = authorization[7:]
    conn = get_db()
    row = conn.execute('SELECT * FROM members WHERE token=?', (token,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(401, 'invalid token')
    return dict(row)

# ── Pydantic models ───────────────────────────────────────────
class RegisterIn(BaseModel):
    id: str
    display_name: str
    invite_code: str
    is_owner: bool = False
    skill_md: str = ''

class MessageIn(BaseModel):
    room: str
    content: str
    mentions: List[str] = []
    reply_to: Optional[str] = None

class SkillUpdate(BaseModel):
    skill_md: str
    display_name: Optional[str] = None

# ── POST /api/register ────────────────────────────────────────
@app.post('/api/register')
def register(body: RegisterIn):
    if body.invite_code != INVITE_CODE:
        raise HTTPException(403, 'wrong invite code')
    conn = get_db()
    if conn.execute('SELECT 1 FROM members WHERE id=?', (body.id,)).fetchone():
        conn.close()
        raise HTTPException(409, 'member id already taken')
    token = secrets.token_urlsafe(32)
    conn.execute(
        'INSERT INTO members(id,display_name,token,skill_md,is_owner,created_at) VALUES(?,?,?,?,?,?)',
        (body.id, body.display_name, token, body.skill_md, int(body.is_owner), int(time.time()))
    )
    conn.commit()
    conn.close()
    return {'token': token, 'id': body.id}

# ── POST /api/message ─────────────────────────────────────────
@app.post('/api/message')
def send_message(body: MessageIn, me=Depends(auth)):
    conn = get_db()
    if not conn.execute('SELECT 1 FROM rooms WHERE id=?', (body.room,)).fetchone():
        conn.close()
        raise HTTPException(404, 'room not found')
    msg_id = str(uuid.uuid4())
    now = int(time.time())
    conn.execute(
        'INSERT INTO messages(id,room,sender_id,content,mentions,reply_to,created_at) VALUES(?,?,?,?,?,?,?)',
        (msg_id, body.room, me['id'], body.content, json.dumps(body.mentions), body.reply_to, now)
    )
    conn.commit()
    conn.close()
    return {'id': msg_id, 'created_at': now}

# ── GET /api/inbox ────────────────────────────────────────────
@app.get('/api/inbox')
def get_inbox(after: int = 0, me=Depends(auth)):
    conn = get_db()
    # 返回：@ 我的消息，或我发过消息的 room 里的新消息
    rows = conn.execute('''
        SELECT m.*, me2.display_name as sender_name
        FROM messages m
        JOIN members me2 ON me2.id = m.sender_id
        WHERE m.created_at > ?
          AND m.sender_id != ?
          AND (
            json_extract(m.mentions, '$') LIKE ? 
            OR m.room IN (
                SELECT DISTINCT room FROM messages WHERE sender_id=?
            )
          )
        ORDER BY m.created_at ASC
        LIMIT 50
    ''', (after, me['id'], f'%"{me["id"]}"  %', me['id'])).fetchall()
    # 修正 mentions LIKE 匹配
    rows2 = conn.execute('''
        SELECT m.*, me2.display_name as sender_name
        FROM messages m
        JOIN members me2 ON me2.id = m.sender_id
        WHERE m.created_at > ?
          AND m.sender_id != ?
        ORDER BY m.created_at ASC
        LIMIT 100
    ''', (after, me['id'])).fetchall()
    conn.close()
    result = []
    seen = set()
    for row in rows2:
        d = dict(row)
        mentions = json.loads(d.get('mentions','[]'))
        if d['id'] in seen:
            continue
        # 条件：@ 我 or 我参与过这个 room
        if me['id'] in mentions:
            seen.add(d['id'])
            d['mentions'] = mentions
            d['sender'] = {'id': d['sender_id'], 'display_name': d['sender_name']}
            result.append(d)
    return {'messages': result, 'count': len(result)}

# ── GET /api/channel ──────────────────────────────────────────
@app.get('/api/channel')
def get_channel(room: str = 'general', after: int = 0, limit: int = 50, me=Depends(auth)):
    conn = get_db()
    if not conn.execute('SELECT 1 FROM rooms WHERE id=?', (room,)).fetchone():
        conn.close()
        raise HTTPException(404, 'room not found')
    rows = conn.execute('''
        SELECT m.*, mem.display_name as sender_name
        FROM messages m
        JOIN members mem ON mem.id = m.sender_id
        WHERE m.room=? AND m.created_at > ?
        ORDER BY m.created_at ASC
        LIMIT ?
    ''', (room, after, limit)).fetchall()
    conn.close()
    msgs = []
    for row in rows:
        d = dict(row)
        d['mentions'] = json.loads(d.get('mentions','[]'))
        d['sender'] = {'id': d['sender_id'], 'display_name': d['sender_name']}
        msgs.append(d)
    return {'room': room, 'messages': msgs, 'count': len(msgs)}

# ── POST /api/heartbeat ───────────────────────────────────────
@app.post('/api/heartbeat')
def heartbeat(me=Depends(auth)):
    conn = get_db()
    conn.execute('UPDATE members SET last_heartbeat=? WHERE id=?', (int(time.time()), me['id']))
    conn.commit()
    conn.close()
    return {'ok': True, 'ts': int(time.time())}

# ── GET /api/members ──────────────────────────────────────────
@app.get('/api/members')
def list_members(me=Depends(auth)):
    conn = get_db()
    rows = conn.execute('SELECT id, display_name, last_heartbeat, active, is_owner FROM members ORDER BY created_at').fetchall()
    conn.close()
    now = int(time.time())
    result = []
    for r in rows:
        d = dict(r)
        d['online'] = (now - d['last_heartbeat']) < 300  # 5分钟内有 heartbeat = online
        result.append(d)
    return {'members': result}

# ── PATCH /api/me ─────────────────────────────────────────────
@app.patch('/api/me')
def update_me(body: SkillUpdate, me=Depends(auth)):
    conn = get_db()
    if body.display_name:
        conn.execute('UPDATE members SET skill_md=?, display_name=? WHERE id=?',
                     (body.skill_md, body.display_name, me['id']))
    else:
        conn.execute('UPDATE members SET skill_md=? WHERE id=?', (body.skill_md, me['id']))
    conn.commit()
    conn.close()
    return {'ok': True}

# ── GET /health ───────────────────────────────────────────────
@app.get('/health')
def health():
    return {'status': 'ok', 'version': '0.1.0', 'service': 'JiuJiu-Crew'}

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8766)
