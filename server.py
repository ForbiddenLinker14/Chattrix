# ---------------- server.py ----------------
import os
import json, base64, re
import socket
import sqlite3
import asyncio
import hashlib
from collections import deque
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import socketio
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import aiohttp
from pywebpush import webpush, WebPushException
from dotenv import load_dotenv

import firebase_admin
from firebase_admin import credentials, messaging

# ---------------- Globals ----------------
DB_PATH = "chat.db"
DESTROYED_ROOMS = set()
WAS_DESTROYED_ROOMS = {}  # { room: destroyed_timestamp }
ROOM_USERS = {}  # { room: { username: sid } }
LAST_MESSAGE = {}  # {(room, username): (text, ts)}
# subscriptions = {}  # username -> [subscription objects]
USER_STATUS = {}  # sid -> {"user": username, "active": bool}
FCM_TOKENS = {"<username>": {"<room_id>": ["<token1>", "<token2>", ...]}}

# Push de-duplication: per-endpoint recent payload IDs sent
PUSH_RECENT = {}  # endpoint -> deque[(push_id, ts)]
PUSH_RECENT_MAX = 100
PUSH_RECENT_WINDOW = timedelta(seconds=30)
ROOM_HISTORY = {}  # { room: set([usernames...]) }
USER_LAST_SEEN = {}  # { (user, room): last_seen_timestamp }

# ---------------- Push subscriptions ----------------
# { room: { user: [subscription objects] } }
subscriptions: dict[str, dict[str, list[dict]]] = {}


def normalize_endpoint(endpoint: str) -> str:
    return endpoint.split("?")[0] if endpoint else ""


# ---------------- Env / VAPID ----------------
load_dotenv()
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY")  # required on client to subscribe
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")  # required on server to send push
if not (VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY):
    print("⚠️  Set VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY in your .env")

# ✅ Load .env locally (Render will inject env vars automatically)
load_dotenv()

firebase_creds_b64 = os.getenv("FIREBASE_CREDENTIALS_BASE64")
if not firebase_creds_b64:
    raise RuntimeError("❌ FIREBASE_CREDENTIALS_BASE64 missing in environment!")

# 🔹 Decode Base64 → JSON
firebase_creds = json.loads(base64.b64decode(firebase_creds_b64))

# 🔹 Initialize Firebase Admin SDK
cred = credentials.Certificate(firebase_creds)

if not firebase_admin._apps:  # <-- check before init
    firebase_admin.initialize_app(cred)


# ---------------- Helpers for FCM tokens ----------------
def load_destroyed_rooms():
    """Load all permanently destroyed rooms from DB"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT room FROM destroyed_rooms")
        rows = c.fetchall()
        conn.close()
        return set(row[0] for row in rows)
    except sqlite3.OperationalError:
        # Table doesn't exist yet
        return set()


def save_destroyed_room(room: str):
    """Save destroyed room to DB with current timestamp"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO destroyed_rooms (room, destroyed_at) VALUES (?, ?)",
        (room, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def remove_destroyed_room(room: str):
    """Remove room from destroyed_rooms (when recreated)"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM destroyed_rooms WHERE room = ?", (room,))
    conn.commit()
    conn.close()


def cleanup_old_destroyed_rooms():
    """Remove destroyed rooms that are older than 1.5 minutes and clean up all related data"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        # First, get the rooms that will be deleted (1.5 minutes for testing)
        cutoff_time = (datetime.now(timezone.utc) - timedelta(minutes=1.5)).isoformat()
        c.execute(
            "SELECT room FROM destroyed_rooms WHERE destroyed_at < ?", (cutoff_time,)
        )
        rooms_to_delete = [row[0] for row in c.fetchall()]

        if rooms_to_delete:
            # Delete old destroyed rooms from DB
            c.execute(
                "DELETE FROM destroyed_rooms WHERE destroyed_at < ?", (cutoff_time,)
            )
            conn.commit()

            # Clean up all related data for these rooms
            for room in rooms_to_delete:
                # Clear from all in-memory data structures
                ROOM_HISTORY.pop(room, None)
                ROOM_USERS.pop(room, None)

                # Clear FCM tokens from memory
                for user in list(FCM_TOKENS.keys()):
                    FCM_TOKENS[user].pop(room, None)
                    if not FCM_TOKENS[user]:
                        del FCM_TOKENS[user]

                # Clear WebPush subscriptions
                subscriptions.pop(room, None)

                # Clear various caches
                room_last_seen_keys = [
                    key for key in USER_LAST_SEEN.keys() if key[1] == room
                ]
                for key in room_last_seen_keys:
                    USER_LAST_SEEN.pop(key, None)

                room_message_keys = [
                    key for key in LAST_MESSAGE.keys() if key[0] == room
                ]
                for key in room_message_keys:
                    LAST_MESSAGE.pop(key, None)

                # ✅ IMPORTANT: Remove from DESTROYED_ROOMS set
                if room in DESTROYED_ROOMS:
                    DESTROYED_ROOMS.remove(room)

                print(f"🧹 Completely cleaned up room: {room}")

            print(
                f"✅ Cleaned up {len(rooms_to_delete)} destroyed rooms and all related data"
            )
        else:
            print("✅ No old destroyed rooms to clean up")

        conn.close()
        return len(rooms_to_delete)

    except Exception as e:
        print(f"❌ Error cleaning up destroyed rooms: {e}")
        return 0


def cleanup_old_was_destroyed_rooms():
    """Clean up rooms that were destroyed more than 1 year ago"""
    global WAS_DESTROYED_ROOMS

    now = datetime.now(timezone.utc)
    cutoff_time = now - timedelta(days=365)  # 1 year expiration

    initial_count = len(WAS_DESTROYED_ROOMS)

    # Remove entries older than 1 year
    rooms_to_remove = []
    for room, destroyed_at in WAS_DESTROYED_ROOMS.items():
        if destroyed_at < cutoff_time:
            rooms_to_remove.append(room)

    for room in rooms_to_remove:
        del WAS_DESTROYED_ROOMS[room]

    removed_count = len(rooms_to_remove)
    if removed_count > 0:
        print(f"🧹 Cleaned {removed_count} was_destroyed rooms older than 1 year")

    return removed_count


def load_fcm_tokens():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user, room, token FROM fcm_tokens")
    rows = c.fetchall()
    conn.close()

    fcm_tokens: dict[str, dict[str, list[str]]] = {}
    for user, room, token in rows:
        fcm_tokens.setdefault(user, {}).setdefault(room, []).append(token)
    return fcm_tokens


def save_fcm_token(user: str, room: str, token: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO fcm_tokens (user, room, token, ts)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user, room, token) DO UPDATE SET ts=excluded.ts
        """,
        (user, room, token, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


# ✅ Add this
def register_fcm_token(user: str, room: str, token: str):
    if user not in FCM_TOKENS:
        FCM_TOKENS[user] = {}
    if room not in FCM_TOKENS[user]:
        FCM_TOKENS[user][room] = []
    if token not in FCM_TOKENS[user][room]:
        FCM_TOKENS[user][room].append(token)
        print(f"🔑 Registered FCM token for {user} in room {room}")


def delete_fcm_tokens_for_room(room: str):
    conn = sqlite3.connect("chat.db")
    cur = conn.cursor()
    cur.execute("DELETE FROM fcm_tokens WHERE room = ?", (room,))
    conn.commit()
    conn.close()
    print(f"🗑️ Deleted all FCM tokens from DB for room {room}")


# ---------------- Migration: add UNIQUE(user) if missing ----------------
# def migrate_fcm_tokens():
#     conn = sqlite3.connect(DB_PATH)
#     c = conn.cursor()

#     # Check if UNIQUE constraint exists
#     c.execute("PRAGMA index_list(fcm_tokens)")
#     indexes = c.fetchall()
#     has_unique = any("user" in row[1] for row in indexes)

#     if not has_unique:
#         print("⚡ Migrating fcm_tokens table to add UNIQUE(user)...")
#         c.execute("ALTER TABLE fcm_tokens RENAME TO fcm_tokens_old")
#         c.execute(
#             """
#             CREATE TABLE fcm_tokens (
#                 id INTEGER PRIMARY KEY AUTOINCREMENT,
#                 user TEXT NOT NULL UNIQUE,
#                 token TEXT NOT NULL,
#                 ts TEXT NOT NULL
#             )
#             """
#         )
#         c.execute(
#             "INSERT OR IGNORE INTO fcm_tokens (user, token, ts) "
#             "SELECT user, token, ts FROM fcm_tokens_old"
#         )
#         c.execute("DROP TABLE fcm_tokens_old")
#         conn.commit()

#     conn.close()


# def migrate_db():
#     conn = sqlite3.connect(DB_PATH)
#     c = conn.cursor()
#     c.execute("PRAGMA table_info(messages)")
#     existing = [r[1] for r in c.fetchall()]
#     for col in ("filename", "mimetype", "filedata"):
#         if col not in existing:
#             c.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
#     conn.commit()
#     conn.close()


def count_messages():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM messages")
    n = c.fetchone()[0]
    conn.close()
    return n


def cleanup_old_messages():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    before = count_messages()
    c.execute("DELETE FROM messages WHERE ts < datetime('now', '-48 hours')")
    conn.commit()
    after = count_messages()
    conn.close()
    return before - after


def save_message(room, sender, text=None, filename=None, mimetype=None, filedata=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO messages (room, sender, text, filename, mimetype, filedata, ts) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            room,
            sender,
            text,
            filename,
            mimetype,
            filedata,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def load_messages(room):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT sender, text, filename, mimetype, filedata, ts "
        "FROM messages WHERE room=? ORDER BY id ASC",
        (room,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def clear_room(room):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM messages WHERE room=?", (room,))
    conn.commit()
    conn.close()


# ---------------- DB ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Messages table
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room TEXT NOT NULL,
            sender TEXT NOT NULL,
            text TEXT,
            filename TEXT,
            mimetype TEXT,
            filedata TEXT,
            ts TEXT NOT NULL
        )
        """
    )

    # FCM tokens table
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS fcm_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT NOT NULL,
            room TEXT NOT NULL,
            token TEXT NOT NULL,
            ts TEXT NOT NULL,
            UNIQUE(user, room, token)
        )
        """
    )

    # ✅ ADD THIS: Destroyed rooms table
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS destroyed_rooms (
            room TEXT PRIMARY KEY,
            destroyed_at TEXT NOT NULL
        )
    """
    )

    conn.commit()
    conn.close()


# Load destroyed rooms on startup (add this after init_db() in startup)
DESTROYED_ROOMS = load_destroyed_rooms()


# ---------------- FastAPI + Socket.IO ----------------
app = FastAPI()

# 🔧 CORS fix (important for Android Capacitor apps)
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # or your domain
    allow_credentials=True,
    allow_methods=["*"],  # important: includes DELETE
    allow_headers=["*"],
)

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
    max_http_buffer_size=10 * 1024 * 1024,
    ping_interval=3,
    ping_timeout=5,
)

# Important: use socketio_path="socket.io"
sio_app = socketio.ASGIApp(sio, socketio_path="socket.io")
app.mount("/socket.io", sio_app)

# Serve static files (your web app from "www" folder)
BASE_DIR = os.path.join(os.path.dirname(__file__), "www")


# ---------------- Helpers ----------------
# async def broadcast_users(room):
#     users = [
#         {"name": username, "status": "online"} for username in ROOM_USERS.get(room, {})
#     ]
#     await sio.emit("users_update", {"room": room, "users": users}, room=room)


async def broadcast_users(room):
    users = []
    all_users = ROOM_HISTORY.get(room, set())

    for username in all_users:
        sid = ROOM_USERS.get(room, {}).get(username)
        active = False
        if sid:
            active = USER_STATUS.get(sid, {}).get("active", False)
        users.append({"name": username, "active": active})

    await sio.emit("users_update", {"room": room, "users": users}, room=room)


def normalize_endpoint(endpoint: str) -> str | None:
    if not endpoint:
        return None
    try:
        p = urlparse(endpoint)
        return f"{p.scheme}://{p.netloc}{p.path}"
    except Exception:
        return endpoint.split("?")[0] if endpoint else endpoint


def make_push_id(room: str, sender: str, text: str, timestamp_iso: str) -> str:
    basis = f"{room}|{sender}|{text}|{timestamp_iso}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def should_send_push(endpoint: str, push_id: str, now: datetime) -> bool:
    dq = PUSH_RECENT.setdefault(endpoint, deque())
    for pid, t in dq:
        if pid == push_id and (now - t) <= PUSH_RECENT_WINDOW:
            return False
    dq.append((push_id, now))
    while len(dq) > PUSH_RECENT_MAX:
        dq.popleft()
    while dq and (now - dq[0][1]) > PUSH_RECENT_WINDOW:
        dq.popleft()
    return True


def user_active_foreground(username: str) -> bool:
    for users in ROOM_USERS.values():
        for uname, usid in users.items():
            if uname == username and USER_STATUS.get(usid, {}).get("active") is True:
                return True
    return False


def extract_favicon_from_text(text: str) -> str | None:
    url_regex = r"(https?://[^\s]+)"
    match = re.search(url_regex, text)
    if not match:
        return None
    url = match.group(0)
    parsed = urlparse(url)
    if not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/favicon.ico"


# ---------------- REST: admin-ish ----------------
@app.delete("/clear/{room}")
async def clear_messages(room: str):
    """
    Clear chat history for a specific room.
    """
    clear_room(room)  # delete all messages from DB

    # Notify everyone in the room
    await sio.emit(
        "clear", {"room": room, "message": "Room history cleared."}, room=room
    )

    print(f"🧹 Room {room} history cleared.")
    return JSONResponse({"status": "ok", "message": f"Room {room} cleared."})


@app.delete("/destroy/{room}")
async def destroy_room(room: str):
    # 0. Clear webpush subscriptions
    if room in subscriptions:
        del subscriptions[room]
        print(f"🛑 All webpush subscriptions cleared for room {room}")

    # 0b. Clear in-memory FCM tokens for this room
    for user in list(FCM_TOKENS.keys()):
        if room in FCM_TOKENS[user]:
            del FCM_TOKENS[user][room]
            print(f"🧹 Removed FCM tokens for {user} in room {room}")
        if not FCM_TOKENS[user]:
            del FCM_TOKENS[user]

    # 0c. Clear persisted FCM tokens in DB
    delete_fcm_tokens_for_room(room)

    # 1. Clear DB messages
    clear_room(room)

    # 2. Mark destroyed
    save_destroyed_room(room)
    DESTROYED_ROOMS.add(room)

    # 3. Remove user mapping
    ROOM_USERS.pop(room, None)

    # 4. Notify clients + force disconnect
    await sio.emit(
        "clear",
        {"room": room, "message": "Room destroyed. All messages cleared."},
        room=room,
    )

    # notify everyone who was in the room (in-room clients)
    await sio.emit("room_destroyed", {"room": room}, room=room)

    # --- NEW: also broadcast to all connected clients so clients
    # who have already left the room (but still have it in localStorage)
    # can remove it from their sidebar.
    await sio.emit("room_destroyed", {"room": room})
    namespace = "/"
    if namespace in sio.manager.rooms and room in sio.manager.rooms[namespace]:
        sids = list(sio.manager.rooms[namespace][room])
        for sid in sids:
            await sio.leave_room(sid, room, namespace=namespace)

    # ✅ TRACK WITH TIMESTAMP:
    WAS_DESTROYED_ROOMS[room] = datetime.now(timezone.utc)

    # ✅ NEW: Broadcast to ALL connected clients to clear this room from storage
    await sio.emit("clear_room_from_storage", {"room": room})

    print(f"💥 Room {room} destroyed (history + FCM tokens wiped from memory + DB).")
    return {"status": "ok"}


# ---------------- Socket.IO Events ----------------
@sio.event
async def join(sid, data):
    room = data["room"]
    username = data["sender"]
    last_ts = data.get("lastTs")
    token = data.get("fcmToken")  # 🔑 client should send token when joining

    # ✅ Check if room was permanently destroyed - REJECT JOIN
    if room in DESTROYED_ROOMS:
        await sio.emit("room_permanently_destroyed", {"room": room}, to=sid)
        return {"success": False, "error": "Room was permanently destroyed"}

    # ✅ Simply ensure history exists and add user (no aggressive cleanup)
    ROOM_HISTORY.setdefault(room, set()).add(username)

    if room not in ROOM_USERS:
        ROOM_USERS[room] = {}

    # handle duplicate sessions
    old_sid = ROOM_USERS[room].get(username)
    if old_sid == sid:
        return {"success": True, "message": "Already in room"}
    if old_sid and old_sid != sid:
        try:
            await sio.leave_room(old_sid, room)
        except Exception:
            pass

    # map user → sid and mark active immediately
    ROOM_USERS[room][username] = sid
    USER_STATUS[sid] = {"user": username, "active": True}

    await sio.enter_room(sid, room)
    await broadcast_users(room)

    # 🔑 register token in memory + DB
    if token:
        register_fcm_token(username, room, token)
        save_fcm_token(username, room, token)

    # send missed messages
    for sender_, text, filename, mimetype, filedata, ts in load_messages(room):
        if last_ts and ts <= last_ts:
            continue
        if filename:
            await sio.emit(
                "file",
                {
                    "sender": sender_,
                    "filename": filename,
                    "mimetype": mimetype,
                    "data": filedata,
                    "ts": ts,
                },
                to=sid,
            )
        else:
            await sio.emit(
                "message",
                {"sender": sender_, "text": text, "ts": ts},
                to=sid,
            )

    # broadcast system join
    if not old_sid:
        await sio.emit(
            "message",
            {
                "sender": "System",
                "text": f"{username} joined!",
                "ts": datetime.now(timezone.utc).isoformat(),
            },
            room=room,
        )

    return {"success": True}


@sio.event
async def status(sid, data):
    # Client sends: { active: true|false }
    # Tracks per-connection foreground/visibility status for push suppression
    user = None
    for room, members in ROOM_USERS.items():
        for uname, usid in members.items():
            if usid == sid:
                user = uname
                break
        if user:
            break
    if not user:
        return
    is_active = bool((data or {}).get("active"))
    USER_STATUS[sid] = {"user": user, "active": is_active}
    print(f"📌 Status update: {user} is now {'ACTIVE' if is_active else 'INACTIVE'}")


@sio.event
async def message(sid, data):
    room = data.get("room")
    sender = data.get("sender")
    text = (data.get("text") or "").strip()
    now = datetime.now(timezone.utc)

    if not text or not room or not sender:
        return

    # optional: keep duplicate suppression
    key = (room, sender)
    last = LAST_MESSAGE.get(key)
    if last and last[0] == text and (now - last[1]).total_seconds() < 1.5:
        return
    LAST_MESSAGE[key] = (text, now)

    save_message(room, sender, text=text)
    # broadcast the usual message to the room (already present)
    await sio.emit(
        "message", {"sender": sender, "text": text, "ts": now.isoformat()}, room=room
    )

    # Update unread counts for all users in the room except sender
    for username in ROOM_HISTORY.get(room, set()):
        if username != sender:
            # Mark this message as unread for user
            key = (username, room)
            USER_LAST_SEEN[key] = now.isoformat()

    # --- NEW: tell all connected clients a metadata event (room, sender, short text, ts)
    # so clients can update unread counters for rooms they have joined.
    # This is lightweight and does NOT include file binary data.
    try:
        await sio.emit(
            "room_message_meta",
            {"room": room, "sender": sender, "text": text, "ts": now.isoformat()},
        )
    except Exception as e:
        print("Failed to emit room_message_meta:", e)
    # Web push
    await send_push_to_room(room, sender, text)

    # Android push (FCM)
    await send_fcm_to_room(room, sender, text)


# @sio.event
# async def file(sid, data):
#     room = data.get("room")
#     if not room or room in DESTROYED_ROOMS:
#         return
#     save_message(
#         room,
#         data["sender"],
#         filename=data["filename"],
#         mimetype=data["mimetype"],
#         filedata=data["data"],
#     )
#     await sio.emit(
#         "file",
#         {
#             "sender": data["sender"],
#             "filename": data["filename"],
#             "mimetype": data["mimetype"],
#             "data": data["data"],
#             "ts": datetime.now(timezone.utc).isoformat(),
#         },
#         room=room,
#     )


@sio.event
async def file(sid, data):
    room = data.get("room")
    sender = data.get("sender")
    filename = data.get("filename")
    mimetype = data.get("mimetype")
    filedata = data.get("data")

    if not room or room in DESTROYED_ROOMS:
        return

    save_message(room, sender, filename=filename, mimetype=mimetype, filedata=filedata)

    await sio.emit(
        "file",
        {
            "sender": sender,
            "filename": filename,
            "mimetype": mimetype,
            "data": filedata,
            "ts": datetime.now(timezone.utc).isoformat(),
        },
        room=room,
    )

    # --- If you have a file upload handler that emits "file", also add a meta broadcast there:
    # after await sio.emit("file", {...}, room=room) add:

    try:
        await sio.emit(
            "room_message_meta",
            {
                "room": room,
                "sender": sender,
                "text": filename or "(file)",
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as e:
        print("Failed to emit room_message_meta (file):", e)

    # (This keeps unread-count logic working for file messages too.)

    # ✅ Send push notifications for file uploads
    # message = f"{sender} sent a file: {filename}"
    message = filename
    await send_push_to_room(room, sender, message)
    await send_fcm_to_room(room, sender, message)


# @sio.event
# async def leave(sid, data):
#     room = data.get("room")
#     username = data.get("sender")

#     if room and username and room in ROOM_USERS and username in ROOM_USERS[room]:
#         del ROOM_USERS[room][username]
#         if not ROOM_USERS[room]:
#             del ROOM_USERS[room]

#     await sio.leave_room(sid, room)
#     await broadcast_users(room)
#     await sio.emit("left_room", {"room": room}, to=sid)

#     # 🛑 Also cleanup FCM tokens in memory + DB
#     if username in FCM_TOKENS and room in FCM_TOKENS[username]:
#         tokens = list(FCM_TOKENS[username][room])  # copy
#         for token in tokens:
#             conn = sqlite3.connect(DB_PATH)
#             c = conn.cursor()
#             c.execute(
#                 "DELETE FROM fcm_tokens WHERE user=? AND room=? AND token=?",
#                 (username, room, token),
#             )
#             conn.commit()
#             conn.close()
#             print(f"🗑️ Deleted FCM token for {username} in {room} [{token[:10]}...]")
#         del FCM_TOKENS[username][room]
#         if not FCM_TOKENS[username]:
#             del FCM_TOKENS[username]

#     # 🛑 Also cleanup WebPush subscriptions
#     if room in subscriptions and username in subscriptions[room]:
#         del subscriptions[room][username]
#         if not subscriptions[room]:
#             del subscriptions[room]
#         print(f"🛑 Cleared WebPush subs for {username} in {room}")

#     await sio.emit(
#         "message",
#         {
#             "sender": "System",
#             "text": f"{username} left!",
#             "ts": datetime.now(timezone.utc).isoformat(),
#         },
#         room=room,
#     )


@sio.event
async def leave(sid, data):
    room = data.get("room")
    username = data.get("sender")
    reason = data.get("reason", "leave")  # "leave" | "switch"

    if room and username and room in ROOM_USERS and username in ROOM_USERS[room]:
        del ROOM_USERS[room][username]
        # 🔥 NEW: Remove from ROOM_HISTORY to ensure they disappear from user list
        if room in ROOM_HISTORY and username in ROOM_HISTORY[room]:
            ROOM_HISTORY[room].remove(username)
        if not ROOM_USERS[room]:
            del ROOM_USERS[room]

    await sio.leave_room(sid, room)
    await broadcast_users(room)  # This will now properly update without the left user
    await sio.emit("left_room", {"room": room}, to=sid)

    # 🛑 Only cleanup tokens/subscriptions on a *real leave*
    if reason == "leave":
        # cleanup FCM tokens
        if username in FCM_TOKENS and room in FCM_TOKENS[username]:
            tokens = list(FCM_TOKENS[username][room])
            for token in tokens:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute(
                    "DELETE FROM fcm_tokens WHERE user=? AND room=? AND token=?",
                    (username, room, token),
                )
                conn.commit()
                conn.close()
                print(f"🗑️ Deleted FCM token for {username} in {room} [{token[:10]}...]")
            del FCM_TOKENS[username][room]
            if not FCM_TOKENS[username]:
                del FCM_TOKENS[username]

        # cleanup WebPush subs
        if room in subscriptions and username in subscriptions[room]:
            del subscriptions[room][username]
            if not subscriptions[room]:
                del subscriptions[room]
            print(f"🛑 Cleared WebPush subs for {username} in {room}")

        # broadcast system "left"
        await sio.emit(
            "message",
            {
                "sender": "System",
                "text": f"{username} left!",
                "ts": datetime.now(timezone.utc).isoformat(),
            },
            room=room,
        )


@sio.event
async def disconnect(sid):
    USER_STATUS.pop(sid, None)

    disconnected_user = None
    disconnected_room = None

    for room, users in list(ROOM_USERS.items()):
        for username, user_sid in list(users.items()):
            if user_sid == sid:
                disconnected_user = username
                disconnected_room = room
                # Only remove from active users, keep in ROOM_HISTORY
                del users[username]
                if not users:
                    del ROOM_USERS[room]
                break
        if disconnected_user:
            break

    # Broadcast updated user list to show user as offline
    if disconnected_room:
        await broadcast_users(disconnected_room)

        await sio.emit(
            "message",
            {
                "sender": "System",
                "text": f"{disconnected_user} disconnected.",
                "ts": datetime.now(timezone.utc).isoformat(),
            },
            room=disconnected_room,
        )


# ---------------- Startup ----------------
@app.on_event("startup")
async def startup_tasks():
    init_db()

    global FCM_TOKENS, DESTROYED_ROOMS
    FCM_TOKENS = load_fcm_tokens()
    DESTROYED_ROOMS = load_destroyed_rooms()
    print(f"🔑 Loaded {sum(len(v) for v in FCM_TOKENS.values())} FCM tokens from DB")
    print(f"🚫 Loaded {len(DESTROYED_ROOMS)} permanently destroyed rooms from DB")

    # Clean up old destroyed rooms on startup
    cleaned = cleanup_old_destroyed_rooms()
    if cleaned > 0:
        print(f"✅ Cleaned {cleaned} old destroyed rooms on startup")

    async def loop_cleanup():
        while True:
            deleted_messages = cleanup_old_messages()
            if deleted_messages > 0:
                await sio.emit(
                    "cleanup",
                    {
                        "message": f"{deleted_messages} old messages (48h+) were removed."
                    },
                )

            # Clean up old destroyed rooms (every 2 minutes)
            deleted_rooms = cleanup_old_destroyed_rooms()
            if deleted_rooms > 0:
                print(
                    f"✅ Auto-cleaned {deleted_rooms} destroyed rooms older than 7 days"
                )
            else:
                print(f"✅ No old destroyed rooms to clean up")

            # ✅ NEW: Clean up WAS_DESTROYED_ROOMS
            cleanup_old_was_destroyed_rooms()

            await asyncio.sleep(120)  # Run every 2 minutes

    async def ping_self():
        url = "https://realtime-chat-1mv3.onrender.com"
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        print(f"[KeepAlive] [Running...] {resp.status}")
            except Exception as e:
                print(f"[KeepAlive] Error: {e}")
            await asyncio.sleep(300)

    asyncio.create_task(loop_cleanup())
    asyncio.create_task(ping_self())


# ---------------- Subscribe / Push test ----------------
@app.post("/api/subscribe")
async def subscribe(request: Request):
    body = await request.json()
    subscription = body.get("subscription")
    sender = body.get("sender")
    room = body.get("room")

    if not sender or not subscription or not room:
        return JSONResponse(
            {"error": "sender, room, and subscription required"},
            status_code=400,
        )

    subs_for_room = subscriptions.setdefault(room, {}).setdefault(sender, [])
    endpoint = normalize_endpoint(subscription.get("endpoint"))
    if not endpoint:
        return JSONResponse({"error": "invalid endpoint"}, status_code=400)

    if all(normalize_endpoint(s.get("endpoint")) != endpoint for s in subs_for_room):
        subs_for_room.append(subscription)

    print(
        f"✅ Subscription saved for {sender} in room {room} (total={len(subs_for_room)})"
    )
    return {
        "message": f"Subscribed {sender} to {room}",
        "vapidPublicKey": VAPID_PUBLIC_KEY,
    }


# ---------------- Unsubscribe ----------------
@app.post("/api/unsubscribe")
async def unsubscribe(request: Request):
    body = await request.json()
    sender = body.get("sender")
    room = body.get("room")
    subscription = body.get("subscription")

    if not sender or not room or not subscription:
        return JSONResponse(
            {"error": "sender, room, and subscription required"},
            status_code=400,
        )

    if room not in subscriptions or sender not in subscriptions[room]:
        return {"message": f"No subscription found for {sender} in {room}"}

    subs_for_room = subscriptions[room][sender]
    endpoint = normalize_endpoint(subscription.get("endpoint"))

    new_list = [
        s for s in subs_for_room if normalize_endpoint(s.get("endpoint")) != endpoint
    ]
    if new_list:
        subscriptions[room][sender] = new_list
    else:
        del subscriptions[room][sender]

    if not subscriptions[room]:
        del subscriptions[room]

    print(f"🛑 Unsubscribed {sender} from {room}")
    return {"message": f"Unsubscribed {sender} from {room}"}


# @app.post("/send-push-notification")
# async def send_push_notification():
#     now = datetime.now(timezone.utc)
#     payload = {
#         "title": "Test Message",
#         "body": "This is a test notification.",
#         "timestamp": now.isoformat(),
#     }
#     push_id = make_push_id("TEST", "system", payload["body"], payload["timestamp"])
#     for user, subs in list(subscriptions.items()):
#         for sub in list(subs):
#             try:
#                 endpoint = normalize_endpoint(sub.get("endpoint"))
#                 if not endpoint:
#                     continue
#                 if not should_send_push(endpoint, push_id, now):
#                     continue
#                 webpush(
#                     subscription_info=sub,
#                     data=json.dumps(payload),
#                     vapid_private_key=VAPID_PRIVATE_KEY,
#                     vapid_claims={"sub": "mailto:example@domain.com"},
#                 )
#             except WebPushException as e:
#                 print(f"❌ Push failed for {user}: {e}")
#     return {"status": "ok"}


@app.post("/send-push-notification")
async def send_push_notification():
    now = datetime.now(timezone.utc)

    title = "Test Message"
    body = "This is a test notification."

    payload = {
        "title": title,
        "body": body,
        "timestamp": now.isoformat(),
    }

    push_id = make_push_id("TEST", "system", payload["body"], payload["timestamp"])

    # 🔹 1. Send Web Push (for browsers)
    for user, subs in list(subscriptions.items()):
        for sub in list(subs):
            try:
                endpoint = normalize_endpoint(sub.get("endpoint"))
                if not endpoint:
                    continue
                if not should_send_push(endpoint, push_id, now):
                    continue
                webpush(
                    subscription_info=sub,
                    data=json.dumps(payload),
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": "mailto:example@domain.com"},
                )
                print(f"🌍 Web push sent to {user}")
            except WebPushException as e:
                print(f"❌ Web Push failed for {user}: {e}")

    # 🔹 2. Send FCM Push (for Android)
    tokens = []
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT token FROM fcm_tokens")
    rows = c.fetchall()
    conn.close()

    for (token,) in rows:
        tokens.append(token)

    for token in tokens:
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data={"sender": "system", "message": body},
            token=token,
        )
        try:
            response = messaging.send(message)
            print("📱 FCM push sent:", response)
        except Exception as e:
            print("❌ FCM push failed:", e)

    return {"status": "ok"}


# ---------------- Web Push only ----------------
from pywebpush import WebPushException


async def send_push_to_room(room: str, sender: str, text: str):
    if room not in subscriptions:
        return

    now = datetime.now(timezone.utc)
    payload = {
        "title": "Chattrix",
        "sender": sender,
        # 👇 If text looks like a filename, prepend "sent a file:"
        "text": (
            f"{sender} sent a file: {text}"
            if not text.strip().startswith(("http", "www")) and "." in text
            else text
        ),
        "room": room,
        "url": f"/?room={room}",
        "timestamp": now.isoformat(),
    }
    push_id = make_push_id(room, sender, text, payload["timestamp"])

    for user, subs in list(subscriptions[room].items()):
        # skip sender
        if user == sender:
            continue

            # --- NEW: skip only if user is active in *this same room*
        sid_in_room = ROOM_USERS.get(room, {}).get(user)
        if sid_in_room and USER_STATUS.get(sid_in_room, {}).get("active"):
            continue

        for sub in list(subs):
            try:
                endpoint = normalize_endpoint(sub.get("endpoint"))
                if not endpoint:
                    continue

                # suppress duplicates
                if not should_send_push(endpoint, push_id, now):
                    continue

                webpush(
                    subscription_info=sub,
                    data=json.dumps(payload),
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": "mailto:anitsaha976@gmail.com"},
                )
                print(f"🌍 WebPush sent to {user} in {room}")

            except WebPushException as e:
                print(f"❌ WebPush failed for {user}: {e}")

                # auto-cleanup expired/invalid subscriptions
                if "410" in str(e) or "404" in str(e):
                    subs_for_room = subscriptions.get(room, {}).get(user, [])
                    subs_for_room = [
                        s
                        for s in subs_for_room
                        if normalize_endpoint(s.get("endpoint"))
                        != normalize_endpoint(sub.get("endpoint"))
                    ]
                    if subs_for_room:
                        subscriptions[room][user] = subs_for_room
                    else:
                        del subscriptions[room][user]
                        if not subscriptions[room]:
                            del subscriptions[room]
                    print(
                        f"🗑️ Removed expired WebPush subscription for {user} in {room}"
                    )


async def send_fcm_to_room(room: str, sender: str, text: str):
    if room in DESTROYED_ROOMS:
        print(f"⛔ Skipping FCM: Room {room} is destroyed.")
        return

    now = datetime.now(timezone.utc)

    # ✅ Try to extract favicon from the first link in the text
    favicon_url = extract_favicon_from_text(text)

    for user, rooms in list(FCM_TOKENS.items()):
        if user == sender:
            continue
        if room not in rooms:
            continue

            # --- NEW: skip only if user is active in *this same room*
        sid_in_room = ROOM_USERS.get(room, {}).get(user)
        if sid_in_room and USER_STATUS.get(sid_in_room, {}).get("active"):
            continue

        for token in list(rooms[room]):
            try:
                msg = messaging.Message(
                    notification=messaging.Notification(
                        title=f"Room {room}",
                        body=f"{sender}: {text}",
                        image=(
                            favicon_url if favicon_url else None
                        ),  # ✅ favicon as notification image
                    ),
                    token=token,
                    data={
                        "room": room,
                        "sender": sender,
                        "message": text,
                        "timestamp": now.isoformat(),
                        "favicon": favicon_url
                        or "",  # ✅ pass along favicon in data too
                    },
                    android=messaging.AndroidConfig(
                        priority="high",
                        notification=messaging.AndroidNotification(
                            channel_id="chat_messages",  # 👈 must exist on device
                            sound="default",
                            priority="high",
                            icon="chat_icon",  # fallback if favicon not shown
                        ),
                    ),
                )
                response = messaging.send(msg)
                print(f"📲 FCM sent to {user}: {response} (favicon={favicon_url})")
            except Exception as e:
                print(f"❌ FCM push failed for {user}: {e}")


@app.post("/send-fcm")
async def send_fcm(request: Request):
    body = await request.json()
    user = body.get("user")
    title = body.get("title", "Chat Message")
    message = body.get("message", "")
    room = body.get("room", "")

    if not user or user not in FCM_TOKENS:
        return JSONResponse({"error": "invalid user"}, status_code=400)

    # 🔑 Collect all tokens across rooms for this user
    tokens = []
    for room_tokens in FCM_TOKENS[user].values():
        tokens.extend(room_tokens)

    results = []
    for token in tokens:
        msg = messaging.Message(
            notification=messaging.Notification(title=title, body=message),
            token=token,
            data={"url": f"/?room={room}", "sender": user, "message": message},
        )
        try:
            response = messaging.send(msg)
            print(f"✅ FCM sent to {user} [{token[:10]}...]: {response}")
            results.append({"token": token, "id": response, "status": "ok"})
        except Exception as e:
            print(f"❌ Failed to send FCM to {user} [{token[:10]}...]: {e}")
            results.append({"token": token, "error": str(e)})

    return {"status": "done", "results": results}


@app.post("/api/unregister-fcm")
async def unregister_fcm(request: Request):
    body = await request.json()
    token = body.get("token")
    user = body.get("user")
    room = body.get("room")

    if not token or not user or not room:
        return JSONResponse({"error": "user + token + room required"}, status_code=400)

    if user in FCM_TOKENS and room in FCM_TOKENS[user]:
        if token in FCM_TOKENS[user][room]:
            FCM_TOKENS[user][room].remove(token)
            print(f"🛑 Token removed for {user} in room {room}")

        # cleanup
        if not FCM_TOKENS[user][room]:
            del FCM_TOKENS[user][room]
        if not FCM_TOKENS[user]:
            del FCM_TOKENS[user]

    return {"status": "ok"}


# ---------------- Static / PWA assets ----------------
@app.get("/unread-counts")
async def get_unread_counts(request: Request):
    user = request.query_params.get("user")
    if not user:
        return JSONResponse({"error": "user parameter required"}, status_code=400)

    unread_counts = {}

    # Get all rooms this user has joined
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT room FROM fcm_tokens WHERE user = ? UNION SELECT room FROM messages WHERE sender = ?",
        (user, user),
    )
    rooms = set(row[0] for row in c.fetchall())
    conn.close()

    for room in rooms:
        # Count messages after user's last seen time
        last_seen = USER_LAST_SEEN.get((user, room))
        if last_seen:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute(
                "SELECT COUNT(*) FROM messages WHERE room = ? AND sender != ? AND ts > ?",
                (room, user, last_seen),
            )
            count = c.fetchone()[0]
            conn.close()

            if count > 0:
                unread_counts[room] = count

    return JSONResponse({"unreadCounts": unread_counts})


@app.get("/room-status/{room}")
async def get_room_status(room: str):
    """Check if room is destroyed and get destruction time"""

    # ✅ Check if room is currently destroyed (DESTROYED_ROOMS) - THIS SHOULD COME FIRST
    if room in DESTROYED_ROOMS:
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT destroyed_at FROM destroyed_rooms WHERE room=?", (room,))
            row = c.fetchone()
            conn.close()

            if row:
                destroyed_at = datetime.fromisoformat(row[0])
                now = datetime.now(timezone.utc)
                time_elapsed = (now - destroyed_at).total_seconds()
                time_remaining = max(0, 300 - time_elapsed)  # 5 minutes = 300 seconds

                return JSONResponse(
                    {
                        "destroyed": True,  # Currently destroyed
                        "was_destroyed": True,  # And was destroyed
                        "destroyed_at": row[0],
                        "time_remaining": time_remaining,  # ✅ This has the actual timer value
                    }
                )
        except Exception as e:
            print(f"Error getting room status: {e}")

    # ✅ Check if room was destroyed within last 1 year (WAS_DESTROYED_ROOMS)
    if room in WAS_DESTROYED_ROOMS:
        destroyed_at = WAS_DESTROYED_ROOMS[room]
        now = datetime.now(timezone.utc)

        # If destroyed less than 1 year ago, block auto-join
        if (now - destroyed_at) < timedelta(days=365):
            return JSONResponse(
                {
                    "destroyed": False,  # Not currently destroyed
                    "was_destroyed": True,  # But was destroyed within 1 year
                    "time_remaining": 0,  # ✅ No timer for previously destroyed rooms
                }
            )
        else:
            # Remove if older than 1 year
            del WAS_DESTROYED_ROOMS[room]

    # ✅ Room was never destroyed or destruction expired
    return JSONResponse({"destroyed": False, "was_destroyed": False})


@app.get("/destroyed_rooms")
async def get_destroyed_rooms():
    """
    Return list of rooms that were destroyed (in-memory).
    Clients call this on startup to remove stale rooms from localStorage.
    """
    return JSONResponse({"destroyed": list(DESTROYED_ROOMS)})


# @app.post("/revive-room/{room}")
# async def revive_room(room: str):
#     """Manually revive a destroyed room (for testing)"""
#     if room in DESTROYED_ROOMS:
#         DESTROYED_ROOMS.remove(room)
#         remove_destroyed_room(room)
#         return {"status": "ok", "message": f"Room {room} revived"}
#     else:
#         return {"status": "error", "message": "Room not found in destroyed rooms"}


# --- ✅ Force Update API ---
@app.get("/api/app/version")
async def get_app_version():
    """
    Returns the latest allowed app version info.
    versionName = string version (like '6.2')
    versionCode = integer (like 11)
    """
    return {
        "latest_version": "6.2",  # match Gradle versionName
        "latest_code": 11,  # match Gradle versionCode
        "force_update": True,  # block outdated versions
        "update_url": "https://indusapp.store/3ylw7f8l",  # your update link
    }


# serve /icons/*
app.mount(
    "/icons", StaticFiles(directory=os.path.join(BASE_DIR, "icons")), name="icons"
)


# serve manifest.json
@app.get("/manifest.json")
async def manifest():
    return FileResponse(os.path.join(BASE_DIR, "manifest.json"))


# serve service worker
@app.get("/sw.js")
async def service_worker():
    return FileResponse(os.path.join(BASE_DIR, "sw.js"))


# sitemap
@app.get("/sitemap.xml")
def sitemap():
    base_url = "http://127.0.0.1:8000"  # replace with your domain in production
    static_pages = ["index.html"]
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for page in static_pages:
        url = page.replace("index.html", "")
        loc = f"{base_url}/" if url == "" else f"{base_url}/{url}"
        xml += f"  <url><loc>{loc}</loc></url>\n"
    xml += "</urlset>"
    return Response(content=xml, media_type="application/xml")


# robots.txt
@app.get("/robots.txt")
def robots():
    return Response("User-agent: *\nAllow: /\n", media_type="text/plain")


# serve everything inside www (index.html as root)
app.mount("/", StaticFiles(directory=BASE_DIR, html=True), name="static")

# ---------------- Run ----------------
if __name__ == "__main__":
    import uvicorn

    local_ip = socket.gethostbyname(socket.gethostname())
    print("🚀 Server running at:")
    print("   ➤ Local:   http://127.0.0.1:8000")
    print(f"   ➤ Network: http://{local_ip}:8000")
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
