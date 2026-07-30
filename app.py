from flask import Flask, request, render_template_string, session, redirect, jsonify
import sqlite3
import random
import time
import os
import uuid
from datetime import timedelta

app = Flask(__name__)
app.secret_key = "chat_secret"

# Without this, Flask issues a browser-session-only cookie that dies the moment
# the tab/browser is closed, logging everyone out constantly. Make it last 90 days.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=90)

# DB_PATH env var lets you point this at a persistent disk mount (e.g. on Render,
# a paid instance with a Disk attached at /var/data) so data survives redeploys.
# Falls back to a local file for plain/free hosting where storage resets each deploy.
DB = os.environ.get("DB_PATH", "chat.db")
ONLINE_THRESHOLD_SECONDS = 15

@app.before_request
def make_session_permanent():
    session.permanent = True

# ---------------- DB ----------------
def init_db():
    db_dir = os.path.dirname(DB)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        email TEXT UNIQUE,
        name TEXT,
        username TEXT UNIQUE,
        user_id TEXT UNIQUE
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS friends (
        user TEXT,
        friend TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender TEXT,
        receiver TEXT,
        message TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS read_state (
        user TEXT,
        peer TEXT,
        last_id INTEGER,
        PRIMARY KEY (user, peer)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS presence (
        username TEXT PRIMARY KEY,
        last_seen REAL
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        created_by TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS group_members (
        group_id INTEGER,
        username TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS call_invites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room TEXT UNIQUE,
        caller TEXT,
        callee TEXT,
        kind TEXT,
        status TEXT,
        created_at REAL
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room TEXT,
        sender TEXT,
        target TEXT,
        data TEXT,
        created_at REAL
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS group_calls (
        room TEXT PRIMARY KEY,
        group_id INTEGER,
        kind TEXT,
        started_by TEXT,
        status TEXT,
        created_at REAL
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS call_participants (
        room TEXT,
        username TEXT,
        joined_at REAL
    )
    """)

    # migration: add photo column to users if it doesn't exist yet
    c.execute("PRAGMA table_info(users)")
    existing_cols = [r[1] for r in c.fetchall()]
    if "photo" not in existing_cols:
        c.execute("ALTER TABLE users ADD COLUMN photo TEXT")

    # migration: add group_id to messages (NULL = normal 1:1 message)
    c.execute("PRAGMA table_info(messages)")
    msg_cols = [r[1] for r in c.fetchall()]
    if "group_id" not in msg_cols:
        c.execute("ALTER TABLE messages ADD COLUMN group_id INTEGER")
    if "media_type" not in msg_cols:
        c.execute("ALTER TABLE messages ADD COLUMN media_type TEXT")
    if "media_data" not in msg_cols:
        c.execute("ALTER TABLE messages ADD COLUMN media_data TEXT")

    # migration: add photo column to groups
    c.execute("PRAGMA table_info(groups)")
    group_cols = [r[1] for r in c.fetchall()]
    if "photo" not in group_cols:
        c.execute("ALTER TABLE groups ADD COLUMN photo TEXT")

    # migration: add target column to signals (for group-call mesh routing)
    c.execute("PRAGMA table_info(signals)")
    signal_cols = [r[1] for r in c.fetchall()]
    if "target" not in signal_cols:
        c.execute("ALTER TABLE signals ADD COLUMN target TEXT")

    conn.commit()
    conn.close()

init_db()


def get_profile_photo(username):
    """Fetch the currently stored profile photo (URL or data URI) for a username."""
    if not username:
        return None

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT photo FROM users WHERE username=?", (username,))
    row = c.fetchone()
    conn.close()

    return row[0] if row else None


def get_chat_partners(me):
    """Everyone who should show up in the chat list: friends + anyone who has
    ever messaged `me`, even if they were never added as a friend."""
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("SELECT friend FROM friends WHERE user=?", (me,))
    friends = set(r[0] for r in c.fetchall())

    c.execute("SELECT DISTINCT sender FROM messages WHERE receiver=?", (me,))
    incoming = set(r[0] for r in c.fetchall())

    partners = friends | incoming

    order = {}
    for p in partners:
        c.execute("""
            SELECT MAX(id) FROM messages
            WHERE (sender=? AND receiver=?) OR (sender=? AND receiver=?)
        """, (me, p, p, me))
        order[p] = c.fetchone()[0] or 0

    conn.close()

    return sorted(partners, key=lambda p: (-order[p], p))


def with_photos(usernames):
    """Turn a list of usernames into [{username, photo}, ...] for template rendering."""
    if not usernames:
        return []

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute(f"SELECT username, photo FROM users WHERE username IN ({','.join('?' * len(usernames))})", usernames)
    photo_map = dict(c.fetchall())
    conn.close()

    return [{"username": u, "photo": photo_map.get(u)} for u in usernames]


def touch_presence(username):
    """Record that `username` was just active - called on every poll from a logged-in client."""
    if not username:
        return

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""
        INSERT INTO presence (username, last_seen) VALUES (?, ?)
        ON CONFLICT(username) DO UPDATE SET last_seen=excluded.last_seen
    """, (username, time.time()))
    conn.commit()
    conn.close()


def get_online_map(usernames):
    """Return {username: bool} - whether each was seen within the online threshold."""
    if not usernames:
        return {}

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute(
        f"SELECT username, last_seen FROM presence WHERE username IN ({','.join('?' * len(usernames))})",
        usernames
    )
    seen = dict(c.fetchall())
    conn.close()

    now = time.time()
    return {u: bool(seen.get(u) and (now - seen[u]) < ONLINE_THRESHOLD_SECONDS) for u in usernames}


def get_user_groups(username):
    """Groups the user belongs to, with member counts, newest activity first."""
    if not username:
        return []

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("""
        SELECT g.id, g.name, g.photo FROM groups g
        JOIN group_members gm ON gm.group_id = g.id
        WHERE gm.username = ?
    """, (username,))
    groups = c.fetchall()

    result = []
    for gid, name, photo in groups:
        c.execute("SELECT COUNT(*) FROM group_members WHERE group_id=?", (gid,))
        member_count = c.fetchone()[0]
        c.execute("SELECT MAX(id) FROM messages WHERE group_id=?", (gid,))
        last_id = c.fetchone()[0] or 0
        result.append({"id": gid, "name": name, "photo": photo, "member_count": member_count, "_order": last_id})

    conn.close()

    result.sort(key=lambda g: (-g["_order"], g["name"]))
    for g in result:
        del g["_order"]
    return result


def is_group_member(group_id, username):
    if not username:
        return False
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT 1 FROM group_members WHERE group_id=? AND username=?", (group_id, username))
    row = c.fetchone()
    conn.close()
    return bool(row)


def get_group_info(group_id):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT id, name, photo, created_by FROM groups WHERE id=?", (group_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    c.execute("SELECT username FROM group_members WHERE group_id=?", (group_id,))
    members = [r[0] for r in c.fetchall()]
    conn.close()
    return {"id": row[0], "name": row[1], "photo": row[2], "created_by": row[3], "members": members}


def mark_read(user, peer):
    """Remember that `user` has seen all messages in the user<->peer thread so far."""
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("""
        SELECT MAX(id) FROM messages
        WHERE (sender=? AND receiver=?) OR (sender=? AND receiver=?)
    """, (user, peer, peer, user))

    last_id = c.fetchone()[0] or 0

    c.execute("""
        INSERT INTO read_state (user, peer, last_id) VALUES (?, ?, ?)
        ON CONFLICT(user, peer) DO UPDATE SET last_id=excluded.last_id
    """, (user, peer, last_id))

    conn.commit()
    conn.close()


# ---------------- GOOGLE LOGIN ----------------
@app.route("/google-login", methods=["POST"])
def google_login():
    data = request.get_json()
    email = data["email"]
    name = data["name"]
    photo = data["photo"]

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("SELECT user_id, username FROM users WHERE email=?", (email,))
    user = c.fetchone()

    if user is None:
        while True:
            new_id = str(random.randint(100000, 999999))
            c.execute("SELECT 1 FROM users WHERE user_id=?", (new_id,))
            if not c.fetchone():
                break

        c.execute("""
        INSERT INTO users (email, name, username, user_id, photo)
        VALUES (?, ?, ?, ?, ?)
        """, (email, name, None, new_id, photo))

        user_id, username = new_id, None
        conn.commit()
    else:
        user_id, username = user
        # only backfill from Google if the user has no photo of their own yet
        c.execute("""
            UPDATE users SET photo=? WHERE email=? AND (photo IS NULL OR photo='')
        """, (photo, email))
        conn.commit()

    conn.close()

    session["email"] = email
    session["name"] = name
    session["photo"] = photo
    session["user_id"] = user_id
    session["username"] = username

    return jsonify({"ok": True})


# ---------------- SET USERNAME ----------------
@app.route("/set-username", methods=["GET", "POST"])
def set_username():
    if not session.get("email"):
        return redirect("/")

    if request.method == "POST":
        username = request.form["username"]
        email = session["email"]

        conn = sqlite3.connect(DB)
        c = conn.cursor()

        c.execute("UPDATE users SET username=? WHERE email=?", (username, email))

        conn.commit()
        conn.close()

        session["username"] = username
        return redirect("/")

    return render_template_string(AUTH_SHELL,
        title="Придумай имя",
        body="""
        <p class="auth-sub">Оно будет видно друзьям в списке чатов и в поиске.</p>
        <form method="POST" class="auth-form">
            <input name="username" placeholder="username" autocomplete="off" required>
            <button type="submit">Продолжить</button>
        </form>
        """
    )


# ---------------- SET PROFILE PHOTO ----------------
@app.route("/set-photo", methods=["POST"])
def set_photo():
    email = session.get("email")
    if not email:
        return jsonify({"ok": False, "error": "not logged in"}), 401

    data = request.get_json(silent=True) or {}
    photo = data.get("photo", "")

    if not isinstance(photo, str) or not photo.startswith("data:image/"):
        return jsonify({"ok": False, "error": "invalid image"}), 400

    if len(photo) > 1_500_000:  # safety cap, resized client-side so real uploads are far smaller
        return jsonify({"ok": False, "error": "image too large"}), 400

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("UPDATE users SET photo=? WHERE email=?", (photo, email))
    conn.commit()
    conn.close()

    return jsonify({"ok": True})


# ---------------- SEARCH ----------------
@app.route("/search")
def search():
    q = request.args.get("q", "")

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("""
    SELECT username, user_id, photo FROM users
    WHERE username LIKE ? OR user_id LIKE ?
    """, (f"%{q}%", f"%{q}%"))

    res = c.fetchall()
    conn.close()

    return jsonify({"results": res})


# ---------------- ADD FRIEND ----------------
@app.route("/add-friend", methods=["POST"])
def add_friend():
    data = request.get_json()

    me = session.get("username")
    other = data["username"]

    if not me or not other:
        return jsonify({"ok": False})

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("INSERT INTO friends (user, friend) VALUES (?, ?)", (me, other))

    conn.commit()
    conn.close()

    return jsonify({"ok": True})


# ---------------- UNREAD COUNTS (for notifications) ----------------
@app.route("/unread-counts")
def unread_counts():
    email = session.get("email")
    if not email:
        return jsonify({})

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("SELECT username FROM users WHERE email=?", (email,))
    row = c.fetchone()

    if not row or not row[0]:
        conn.close()
        return jsonify({})

    me = row[0]

    touch_presence(me)

    c.execute("SELECT DISTINCT sender FROM messages WHERE receiver=?", (me,))
    senders = [r[0] for r in c.fetchall()]

    result = {}

    for sender in senders:
        c.execute("SELECT last_id FROM read_state WHERE user=? AND peer=?", (me, sender))
        row = c.fetchone()
        last_id = row[0] if row else 0

        c.execute("""
            SELECT message FROM messages
            WHERE sender=? AND receiver=? AND id>?
            ORDER BY id
        """, (sender, me, last_id))

        unread_msgs = c.fetchall()

        if unread_msgs:
            c.execute("SELECT photo FROM users WHERE username=?", (sender,))
            photo_row = c.fetchone()

            result[sender] = {
                "count": len(unread_msgs),
                "last": unread_msgs[-1][0],
                "photo": photo_row[0] if photo_row else None
            }

    conn.close()

    return jsonify(result)


# ---------------- PRESENCE (who's online) ----------------
@app.route("/presence", methods=["POST"])
def presence():
    email = session.get("email")
    if not email:
        return jsonify({})

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT username FROM users WHERE email=?", (email,))
    row = c.fetchone()
    conn.close()

    me = row[0] if row else None
    if me:
        touch_presence(me)

    data = request.get_json(silent=True) or {}
    usernames = data.get("usernames", [])
    if not isinstance(usernames, list):
        usernames = []
    usernames = [u for u in usernames if isinstance(u, str)][:200]

    return jsonify(get_online_map(usernames))


# ---------------- GROUPS ----------------
def current_username():
    email = session.get("email")
    if not email:
        return None
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT username FROM users WHERE email=?", (email,))
    row = c.fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def mark_group_read(user, group_id):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT MAX(id) FROM messages WHERE group_id=?", (group_id,))
    last_id = c.fetchone()[0] or 0
    c.execute("""
        INSERT INTO read_state (user, peer, last_id) VALUES (?, ?, ?)
        ON CONFLICT(user, peer) DO UPDATE SET last_id=excluded.last_id
    """, (user, f"group:{group_id}", last_id))
    conn.commit()
    conn.close()


@app.route("/create-group", methods=["POST"])
def create_group():
    me = current_username()
    if not me:
        return jsonify({"ok": False, "error": "not logged in"}), 401

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    members = data.get("members", [])

    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    if not isinstance(members, list):
        members = []

    all_members = sorted(set([me] + [m for m in members if isinstance(m, str) and m]))

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT INTO groups (name, created_by) VALUES (?, ?)", (name, me))
    group_id = c.lastrowid
    for member in all_members:
        c.execute("INSERT INTO group_members (group_id, username) VALUES (?, ?)", (group_id, member))
    conn.commit()
    conn.close()

    return jsonify({"ok": True, "group_id": group_id})


@app.route("/group/<int:group_id>")
def group_chat(group_id):
    me = current_username()
    if not me:
        return redirect("/")

    info = get_group_info(group_id)
    if not info or me not in info["members"]:
        return redirect("/")

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT sender, message, media_type, media_data FROM messages WHERE group_id=? ORDER BY id", (group_id,))
    messages = c.fetchall()
    conn.close()

    mark_group_read(me, group_id)

    chats = with_photos(get_chat_partners(me))
    groups = get_user_groups(me)

    return render_template_string(HTML,
        friends=chats,
        groups=groups,
        peer=None,
        group=info,
        messages=messages,
        my_id=session.get("user_id"),
        me=me,
        photo=get_profile_photo(me)
    )


@app.route("/group-messages/<int:group_id>")
def group_messages(group_id):
    me = current_username()
    if not me:
        return jsonify([])

    info = get_group_info(group_id)
    if not info or me not in info["members"]:
        return jsonify([])

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT sender, message, media_type, media_data FROM messages WHERE group_id=? ORDER BY id", (group_id,))
    msgs = c.fetchall()
    conn.close()

    mark_group_read(me, group_id)

    return jsonify(msgs)


@app.route("/send-group/<int:group_id>", methods=["POST"])
def send_group(group_id):
    me = current_username()
    if not me:
        return redirect("/")

    info = get_group_info(group_id)
    if not info or me not in info["members"]:
        return jsonify({"ok": False}), 403

    msg = request.form.get("msg", "")
    if not msg:
        return jsonify({"ok": False})

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT INTO messages (sender, receiver, message, group_id) VALUES (?, ?, ?, ?)",
              (me, None, msg, group_id))
    conn.commit()
    conn.close()

    return jsonify({"ok": True})


MEDIA_SIZE_LIMIT_GROUP = 15_000_000

@app.route("/send-group-media/<int:group_id>", methods=["POST"])
def send_group_media(group_id):
    me = current_username()
    if not me:
        return jsonify({"ok": False}), 401

    info = get_group_info(group_id)
    if not info or me not in info["members"]:
        return jsonify({"ok": False}), 403

    data = request.get_json(silent=True) or {}
    media_type = data.get("media_type")
    media_data = data.get("media_data", "")
    caption = data.get("caption", "")

    if media_type not in ("image", "video") or not isinstance(media_data, str) or not media_data.startswith("data:"):
        return jsonify({"ok": False, "error": "invalid media"}), 400
    if len(media_data) > MEDIA_SIZE_LIMIT_GROUP:
        return jsonify({"ok": False, "error": "file too large"}), 400

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""
        INSERT INTO messages (sender, receiver, message, group_id, media_type, media_data)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (me, None, caption, group_id, media_type, media_data))
    conn.commit()
    conn.close()

    return jsonify({"ok": True})


@app.route("/group/<int:group_id>/rename", methods=["POST"])
def group_rename(group_id):
    me = current_username()
    if not me:
        return jsonify({"ok": False}), 401

    info = get_group_info(group_id)
    if not info or me not in info["members"]:
        return jsonify({"ok": False}), 403

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("UPDATE groups SET name=? WHERE id=?", (name, group_id))
    conn.commit()
    conn.close()

    return jsonify({"ok": True})


@app.route("/group/<int:group_id>/photo", methods=["POST"])
def group_photo(group_id):
    me = current_username()
    if not me:
        return jsonify({"ok": False}), 401

    info = get_group_info(group_id)
    if not info or me not in info["members"]:
        return jsonify({"ok": False}), 403

    data = request.get_json(silent=True) or {}
    photo = data.get("photo", "")

    if not isinstance(photo, str) or not photo.startswith("data:image/"):
        return jsonify({"ok": False, "error": "invalid image"}), 400
    if len(photo) > 1_500_000:
        return jsonify({"ok": False, "error": "image too large"}), 400

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("UPDATE groups SET photo=? WHERE id=?", (photo, group_id))
    conn.commit()
    conn.close()

    return jsonify({"ok": True})


@app.route("/group/<int:group_id>/invite", methods=["POST"])
def group_invite(group_id):
    me = current_username()
    if not me:
        return jsonify({"ok": False}), 401

    info = get_group_info(group_id)
    if not info or me not in info["members"]:
        return jsonify({"ok": False}), 403

    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    if not username:
        return jsonify({"ok": False, "error": "username required"}), 400

    if username in info["members"]:
        return jsonify({"ok": False, "error": "already a member"}), 400

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT 1 FROM users WHERE username=?", (username,))
    if not c.fetchone():
        conn.close()
        return jsonify({"ok": False, "error": "user not found"}), 404

    c.execute("INSERT INTO group_members (group_id, username) VALUES (?, ?)", (group_id, username))
    conn.commit()
    conn.close()

    return jsonify({"ok": True})


@app.route("/group/<int:group_id>/leave", methods=["POST"])
def group_leave(group_id):
    me = current_username()
    if not me:
        return jsonify({"ok": False}), 401

    info = get_group_info(group_id)
    if not info or me not in info["members"]:
        return jsonify({"ok": False}), 403

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("DELETE FROM group_members WHERE group_id=? AND username=?", (group_id, me))
    conn.commit()
    conn.close()

    return jsonify({"ok": True})


@app.route("/group/<int:group_id>/delete", methods=["POST"])
def group_delete(group_id):
    me = current_username()
    if not me:
        return jsonify({"ok": False}), 401

    info = get_group_info(group_id)
    if not info:
        return jsonify({"ok": False}), 404
    if info["created_by"] != me:
        return jsonify({"ok": False, "error": "only the creator can delete this group"}), 403

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("DELETE FROM groups WHERE id=?", (group_id,))
    c.execute("DELETE FROM group_members WHERE group_id=?", (group_id,))
    c.execute("DELETE FROM messages WHERE group_id=?", (group_id,))
    conn.commit()
    conn.close()

    return jsonify({"ok": True})


# ---------------- CALLS (1:1 audio/video via WebRTC) ----------------
RING_TIMEOUT_SECONDS = 30

@app.route("/call/start", methods=["POST"])
def call_start():
    me = current_username()
    if not me:
        return jsonify({"ok": False}), 401

    data = request.get_json(silent=True) or {}
    callee = data.get("callee")
    kind = data.get("kind")

    if kind not in ("audio", "video") or not callee:
        return jsonify({"ok": False, "error": "bad request"}), 400

    room = uuid.uuid4().hex

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""
        INSERT INTO call_invites (room, caller, callee, kind, status, created_at)
        VALUES (?, ?, ?, ?, 'ringing', ?)
    """, (room, me, callee, kind, time.time()))
    conn.commit()
    conn.close()

    return jsonify({"ok": True, "room": room})


@app.route("/call/incoming")
def call_incoming():
    me = current_username()
    if not me:
        return jsonify(None)

    cutoff = time.time() - RING_TIMEOUT_SECONDS

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""
        SELECT room, caller, kind FROM call_invites
        WHERE callee=? AND status='ringing' AND created_at > ?
        ORDER BY created_at DESC LIMIT 1
    """, (me, cutoff))
    row = c.fetchone()
    conn.close()

    if not row:
        return jsonify(None)

    conn2 = sqlite3.connect(DB)
    c2 = conn2.cursor()
    c2.execute("SELECT photo FROM users WHERE username=?", (row[1],))
    photo_row = c2.fetchone()
    conn2.close()

    return jsonify({"room": row[0], "caller": row[1], "kind": row[2], "caller_photo": photo_row[0] if photo_row else None})


@app.route("/call/respond", methods=["POST"])
def call_respond():
    me = current_username()
    if not me:
        return jsonify({"ok": False}), 401

    data = request.get_json(silent=True) or {}
    room = data.get("room")
    action = data.get("action")

    if action not in ("accept", "decline"):
        return jsonify({"ok": False}), 400

    new_status = "accepted" if action == "accept" else "declined"

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("UPDATE call_invites SET status=? WHERE room=? AND callee=?", (new_status, room, me))
    conn.commit()
    conn.close()

    return jsonify({"ok": True})


@app.route("/call/status/<room>")
def call_status(room):
    me = current_username()
    if not me:
        return jsonify(None)

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT caller, callee, kind, status FROM call_invites WHERE room=?", (room,))
    row = c.fetchone()
    conn.close()

    if not row or me not in (row[0], row[1]):
        return jsonify(None)

    return jsonify({"caller": row[0], "callee": row[1], "kind": row[2], "status": row[3]})


@app.route("/call/end", methods=["POST"])
def call_end():
    me = current_username()
    if not me:
        return jsonify({"ok": False}), 401

    data = request.get_json(silent=True) or {}
    room = data.get("room")

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("UPDATE call_invites SET status='ended' WHERE room=? AND (caller=? OR callee=?)", (room, me, me))
    conn.commit()
    conn.close()

    return jsonify({"ok": True})


def room_access_ok(c, room, me):
    """Check whether `me` may read/write signals in this room - either a 1:1 call they're part of, or a group call for a group they belong to."""
    c.execute("SELECT caller, callee FROM call_invites WHERE room=?", (room,))
    row = c.fetchone()
    if row:
        return me in (row[0], row[1])

    c.execute("SELECT group_id FROM group_calls WHERE room=?", (room,))
    row = c.fetchone()
    if row:
        c.execute("SELECT 1 FROM group_members WHERE group_id=? AND username=?", (row[0], me))
        return bool(c.fetchone())

    return False


@app.route("/signals/<room>", methods=["GET", "POST"])
def signals(room):
    me = current_username()
    if not me:
        return jsonify({"ok": False}) if request.method == "POST" else jsonify([])

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    if not room_access_ok(c, room, me):
        conn.close()
        return jsonify({"ok": False}) if request.method == "POST" else jsonify([])

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        payload = data.get("data", "")
        target = data.get("target")  # None = 1:1 (implicit "the other person"); set = group mesh routing
        c.execute("INSERT INTO signals (room, sender, target, data, created_at) VALUES (?, ?, ?, ?, ?)",
                  (room, me, target, payload, time.time()))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    after = request.args.get("after", 0, type=int)
    c.execute("""
        SELECT id, sender, data FROM signals
        WHERE room=? AND sender!=? AND id>? AND (target IS NULL OR target=?)
        ORDER BY id
    """, (room, me, after, me))
    rows = c.fetchall()
    conn.close()

    return jsonify([{"id": r[0], "from": r[1], "data": r[2]} for r in rows])


# ---------------- GROUP CALLS (multi-party mesh via WebRTC) ----------------
@app.route("/group-call/start", methods=["POST"])
def group_call_start():
    me = current_username()
    if not me:
        return jsonify({"ok": False}), 401

    data = request.get_json(silent=True) or {}
    group_id = data.get("group_id")
    kind = data.get("kind")

    info = get_group_info(group_id)
    if not info or me not in info["members"]:
        return jsonify({"ok": False}), 403
    if kind not in ("audio", "video"):
        return jsonify({"ok": False}), 400

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("SELECT room FROM group_calls WHERE group_id=? AND status='active'", (group_id,))
    existing = c.fetchone()
    if existing:
        conn.close()
        return jsonify({"ok": True, "room": existing[0]})

    room = uuid.uuid4().hex
    c.execute("""
        INSERT INTO group_calls (room, group_id, kind, started_by, status, created_at)
        VALUES (?, ?, ?, ?, 'active', ?)
    """, (room, group_id, kind, me, time.time()))
    conn.commit()
    conn.close()

    return jsonify({"ok": True, "room": room})


@app.route("/group-call/status/<int:group_id>")
def group_call_status(group_id):
    me = current_username()
    if not me:
        return jsonify({"active": False})

    info = get_group_info(group_id)
    if not info or me not in info["members"]:
        return jsonify({"active": False})

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT room, kind FROM group_calls WHERE group_id=? AND status='active'", (group_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"active": False})

    c.execute("SELECT COUNT(*) FROM call_participants WHERE room=?", (row[0],))
    count = c.fetchone()[0]
    conn.close()

    return jsonify({"active": True, "room": row[0], "kind": row[1], "participant_count": count})


@app.route("/group-call/join", methods=["POST"])
def group_call_join():
    me = current_username()
    if not me:
        return jsonify({"ok": False}), 401

    data = request.get_json(silent=True) or {}
    room = data.get("room")

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("SELECT group_id, kind, status FROM group_calls WHERE room=?", (room,))
    row = c.fetchone()
    if not row or row[2] != "active":
        conn.close()
        return jsonify({"ok": False}), 404

    if not is_group_member(row[0], me):
        conn.close()
        return jsonify({"ok": False}), 403

    c.execute("SELECT username FROM call_participants WHERE room=?", (room,))
    existing_participants = [r[0] for r in c.fetchall() if r[0] != me]

    c.execute("INSERT INTO call_participants (room, username, joined_at) VALUES (?, ?, ?)", (room, me, time.time()))
    conn.commit()
    conn.close()

    return jsonify({"ok": True, "kind": row[1], "participants": existing_participants})


@app.route("/group-call/participants/<room>")
def group_call_participants(room):
    me = current_username()
    if not me:
        return jsonify([])

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    if not room_access_ok(c, room, me):
        conn.close()
        return jsonify([])

    c.execute("SELECT username FROM call_participants WHERE room=?", (room,))
    participants = [r[0] for r in c.fetchall() if r[0] != me]
    conn.close()

    return jsonify(participants)


@app.route("/group-call/leave", methods=["POST"])
def group_call_leave():
    me = current_username()
    if not me:
        return jsonify({"ok": False}), 401

    data = request.get_json(silent=True) or {}
    room = data.get("room")

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("DELETE FROM call_participants WHERE room=? AND username=?", (room, me))

    c.execute("SELECT COUNT(*) FROM call_participants WHERE room=?", (room,))
    remaining = c.fetchone()[0]
    if remaining == 0:
        c.execute("UPDATE group_calls SET status='ended' WHERE room=?", (room,))

    conn.commit()
    conn.close()

    return jsonify({"ok": True})


@app.route("/chat/<user>")
def chat(user):
    email = session.get("email")
    if not email:
        return redirect("/")

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("SELECT username FROM users WHERE email=?", (email,))
    row = c.fetchone()

    if not row or not row[0]:
        return redirect("/")

    me = row[0]

    c.execute("""
    SELECT sender, message, media_type, media_data FROM messages
    WHERE (sender=? AND receiver=?)
    OR (sender=? AND receiver=?)
    ORDER BY id
    """, (me, user, user, me))

    messages = c.fetchall()

    conn.close()

    mark_read(me, user)

    chats = with_photos(get_chat_partners(me))

    return render_template_string(HTML,
        friends=chats,
        groups=get_user_groups(me),
        peer=user,
        peer_photo=get_profile_photo(user),
        group=None,
        messages=messages,
        my_id=session.get("user_id"),
        me=me,
        photo=get_profile_photo(me)
    )

# ---------------- LIVE MESSAGES (AJAX) ----------------
@app.route("/messages/<user>")
def messages(user):
    email = session.get("email")

    if not email:
        return jsonify([])

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("SELECT username FROM users WHERE email=?", (email,))
    row = c.fetchone()

    if not row or not row[0]:
        return jsonify([])

    me = row[0]

    c.execute("""
        SELECT sender, message, media_type, media_data FROM messages
        WHERE (sender=? AND receiver=?)
        OR (sender=? AND receiver=?)
        ORDER BY id
    """, (me, user, user, me))

    msgs = c.fetchall()
    conn.close()

    mark_read(me, user)

    return jsonify(msgs)

# ---------------- SEND ----------------
@app.route("/send/<user>", methods=["POST"])
def send(user):
    email = session.get("email")

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("SELECT username FROM users WHERE email=?", (email,))
    row = c.fetchone()

    if not row or not row[0]:
        return redirect("/")

    me = row[0]
    msg = request.form["msg"]

    c.execute("""
    INSERT INTO messages (sender, receiver, message)
    VALUES (?, ?, ?)
    """, (me, user, msg))

    conn.commit()
    conn.close()

    return jsonify({"ok": True})


MEDIA_SIZE_LIMIT = 15_000_000  # ~15MB of base64 text - keeps SQLite/server reasonably happy

@app.route("/send-media/<user>", methods=["POST"])
def send_media(user):
    me = current_username()
    if not me:
        return jsonify({"ok": False}), 401

    data = request.get_json(silent=True) or {}
    media_type = data.get("media_type")
    media_data = data.get("media_data", "")
    caption = data.get("caption", "")

    if media_type not in ("image", "video") or not isinstance(media_data, str) or not media_data.startswith("data:"):
        return jsonify({"ok": False, "error": "invalid media"}), 400
    if len(media_data) > MEDIA_SIZE_LIMIT:
        return jsonify({"ok": False, "error": "file too large"}), 400

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""
        INSERT INTO messages (sender, receiver, message, media_type, media_data)
        VALUES (?, ?, ?, ?, ?)
    """, (me, user, caption, media_type, media_data))
    conn.commit()
    conn.close()

    return jsonify({"ok": True})


# ---------------- LOGOUT ----------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


# ---------------- HOME ----------------
@app.route("/")
def home():
    email = session.get("email")

    if not email:
        return render_template_string(HTML, friends=[], groups=[], peer=None, group=None, my_id="LOGIN FIRST", me=None, photo=None)

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("SELECT username, user_id FROM users WHERE email=?", (email,))
    row = c.fetchone()

    conn.close()

    if not row:
        session.clear()
        return redirect("/")

    username, user_id = row

    session["username"] = username or None
    session["user_id"] = user_id

    chats = with_photos(get_chat_partners(username)) if username else []
    groups = get_user_groups(username) if username else []

    return render_template_string(
        HTML,
        friends=chats,
        groups=groups,
        peer=None,
        group=None,
        my_id=user_id,
        me=username,
        photo=get_profile_photo(username)
    )


# ---------------- SETTINGS ----------------
@app.route("/settings")
def settings():
    if not session.get("email"):
        return redirect("/")

    return render_template_string(SETTINGS_HTML,
        email=session.get("email"),
        username=session.get("username"),
        user_id=session.get("user_id"),
        photo=get_profile_photo(session.get("username"))
    )


# ---------------- SHARED STYLE ----------------
BASE_STYLE = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@500&display=swap');

:root{
    --bg:#F4F5F8;
    --surface:#FFFFFF;
    --surface-2:#ECEEF3;
    --text:#191C22;
    --text-dim:#6B7280;
    --border:#E2E4EA;
    --primary:#4F46E5;
    --primary-dim:#EEF0FF;
    --accent:#0EA5A5;
    --bubble-me:#4F46E5;
    --bubble-me-text:#FFFFFF;
    --bubble-them:#FFFFFF;
    --radius-lg:20px;
    --radius-md:14px;
    --shadow:0 1px 2px rgba(20,20,43,0.04), 0 8px 24px -12px rgba(20,20,43,0.10);
}

body.dark{
    --bg:#101218;
    --surface:#171A21;
    --surface-2:#1E212A;
    --text:#EDEEF3;
    --text-dim:#8C90A0;
    --border:#272B36;
    --primary:#818CF8;
    --primary-dim:#232242;
    --accent:#2DD4BF;
    --bubble-me:#4F46E5;
    --bubble-me-text:#FFFFFF;
    --bubble-them:#1E212A;
    --shadow:0 1px 2px rgba(0,0,0,0.2), 0 8px 24px -12px rgba(0,0,0,0.5);
}

*{ box-sizing:border-box; }

body{
    margin:0;
    font-family:'Inter', Arial, sans-serif;
    background:var(--bg);
    color:var(--text);
    transition:background .25s ease, color .25s ease;
    -webkit-font-smoothing:antialiased;
}

.wordmark{
    font-family:'Space Grotesk', sans-serif;
    font-weight:700;
    letter-spacing:-0.02em;
    display:flex;
    align-items:center;
    gap:8px;
}

.wordmark .dot{
    width:9px;
    height:9px;
    border-radius:3px;
    background:var(--primary);
    display:inline-block;
    transform:rotate(45deg);
}

.mono{ font-family:'JetBrains Mono', monospace; }

button{ font-family:inherit; cursor:pointer; transition:transform .12s ease, background .2s ease, opacity .2s ease; }
button:active{ transform:scale(0.96); }
input{ font-family:inherit; transition:border-color .2s ease; }

a{ transition:transform .15s ease, background .2s ease; }

@keyframes fadeInUp{
    from{ opacity:0; transform:translateY(10px); }
    to{ opacity:1; transform:translateY(0); }
}

@keyframes popIn{
    from{ opacity:0; transform:scale(.7); }
    to{ opacity:1; transform:scale(1); }
}

@keyframes slideDownFade{
    from{ opacity:0; transform:translate(-50%,-12px); }
    to{ opacity:1; transform:translate(-50%,0); }
}

@keyframes pulseOnce{
    0%{ transform:scale(1); }
    35%{ transform:scale(1.18); }
    100%{ transform:scale(1); }
}

@media (prefers-reduced-motion: reduce){
    *{ animation-duration:0.001ms !important; animation-iteration-count:1 !important; transition-duration:0.001ms !important; }
}

#offline-banner{
    position:fixed;
    top:0;
    left:0;
    right:0;
    z-index:9999999;
    background:#EF4444;
    color:white;
    text-align:center;
    font-size:13px;
    font-weight:600;
    padding:9px 12px;
    transform:translateY(-100%);
    transition:transform .25s ease;
}
#offline-banner.show{ transform:translateY(0); }
</style>
"""

# ---------------- AUTH SHELL (login / username creation) ----------------
AUTH_SHELL = BASE_STYLE + """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Relay</title>
<style>
.auth-wrap{
    min-height:100vh;
    display:flex;
    align-items:center;
    justify-content:center;
    padding:24px;
}
.auth-card{
    width:100%;
    max-width:380px;
    background:var(--surface);
    border:1px solid var(--border);
    border-radius:var(--radius-lg);
    box-shadow:var(--shadow);
    padding:36px 32px;
    text-align:center;
    animation:fadeInUp .45s ease both;
}
.auth-card .wordmark{
    justify-content:center;
    font-size:22px;
    margin-bottom:6px;
}
.auth-card h2{
    font-family:'Space Grotesk', sans-serif;
    font-size:19px;
    margin:18px 0 4px;
}
.auth-sub{
    color:var(--text-dim);
    font-size:14px;
    margin:0 0 20px;
}
.auth-form{
    display:flex;
    flex-direction:column;
    gap:10px;
}
.auth-form input{
    padding:13px 14px;
    border-radius:var(--radius-md);
    border:1px solid var(--border);
    background:var(--surface-2);
    color:var(--text);
    font-size:15px;
    outline:none;
}
.auth-form input:focus{
    border-color:var(--primary);
}
.auth-form button{
    padding:13px 14px;
    border-radius:var(--radius-md);
    border:none;
    background:var(--primary);
    color:white;
    font-weight:600;
    font-size:15px;
}
.auth-form button:hover{ opacity:0.92; }
</style>
</head>
<body>
<div id="offline-banner">Нет доступа к интернету</div>
<div class="auth-wrap">
    <div class="auth-card">
        <div class="wordmark"><span class="dot"></span>Relay</div>
        <h2>{{title}}</h2>
        {{body|safe}}
    </div>
</div>
<script>
if(localStorage.getItem("theme") === "dark"){
    document.body.classList.add("dark");
}

function updateOnlineStatus(){
    var banner = document.getElementById("offline-banner");
    if(!banner) return;
    banner.classList.toggle("show", !navigator.onLine);
}
window.addEventListener("online", updateOnlineStatus);
window.addEventListener("offline", updateOnlineStatus);
updateOnlineStatus();
</script>
</body>
</html>
"""

# ---------------- SETTINGS ----------------
SETTINGS_HTML = BASE_STYLE + """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Настройки — Relay</title>
<style>
.settings-wrap{
    min-height:100vh;
    display:flex;
    align-items:flex-start;
    justify-content:center;
    padding:40px 20px;
}
.settings-card{
    width:100%;
    max-width:440px;
    background:var(--surface);
    border:1px solid var(--border);
    border-radius:var(--radius-lg);
    box-shadow:var(--shadow);
    padding:28px;
    animation:fadeInUp .45s ease both;
}
.settings-card h2{
    font-family:'Space Grotesk', sans-serif;
    font-size:20px;
    margin:0 0 20px;
}
.profile-row{
    display:flex;
    align-items:center;
    gap:14px;
    padding-bottom:20px;
    margin-bottom:16px;
    border-bottom:1px solid var(--border);
}
.avatar{
    width:56px;
    height:56px;
    border-radius:50%;
    object-fit:cover;
    background:var(--primary-dim);
}
.avatar-edit{
    position:relative;
    width:56px;
    height:56px;
    flex-shrink:0;
    cursor:pointer;
}
.avatar-edit .cam{
    position:absolute;
    bottom:-2px;
    right:-2px;
    width:22px;
    height:22px;
    border-radius:50%;
    background:var(--primary);
    color:white;
    display:flex;
    align-items:center;
    justify-content:center;
    font-size:11px;
    border:2px solid var(--surface);
}
.avatar-edit input[type="file"]{ display:none; }
.avatar-edit .uploading{
    position:absolute;
    inset:0;
    border-radius:50%;
    background:rgba(0,0,0,0.45);
    color:white;
    display:none;
    align-items:center;
    justify-content:center;
    font-size:10px;
    text-align:center;
}
.avatar-edit.busy .uploading{ display:flex; }
.profile-row .name{
    font-weight:600;
    font-size:16px;
}
.profile-row .id{
    color:var(--text-dim);
    font-size:13px;
}
.field{
    display:flex;
    justify-content:space-between;
    padding:10px 0;
    font-size:14px;
    border-bottom:1px solid var(--border);
}
.field span:first-child{ color:var(--text-dim); }
.settings-links{
    margin-top:20px;
    display:flex;
    flex-direction:column;
    gap:8px;
}
.settings-links a{
    text-decoration:none;
    color:var(--text);
    background:var(--surface-2);
    padding:13px 14px;
    border-radius:var(--radius-md);
    font-size:14px;
    font-weight:500;
    display:flex;
    align-items:center;
    gap:10px;
}
.settings-links a:hover{ transform:translateX(3px); }
.settings-links a.danger{ color:#EF4444; }
</style>
</head>
<body>
<div id="offline-banner">Нет доступа к интернету</div>
<div class="settings-wrap">
    <div class="settings-card">
        <h2>Настройки</h2>

        <div class="profile-row">
            <label class="avatar-edit" id="avatarEdit">
                {% if photo %}
                    <img class="avatar" src="{{photo}}">
                {% else %}
                    <div class="avatar"></div>
                {% endif %}
                <span class="cam">✎</span>
                <span class="uploading">...</span>
                <input type="file" accept="image/*" id="photoInput">
            </label>
            <div>
                <div class="name">{{username or "—"}}</div>
                <div class="id mono">ID {{user_id}}</div>
            </div>
        </div>

        <div class="field"><span>Email</span><span>{{email}}</span></div>
        <div class="field"><span>Username</span><span>{{username or "не задан"}}</span></div>

        <div class="settings-links">
            <a href="/set-username">✏️&nbsp; Изменить username</a>
            <a href="/">⬅&nbsp; Назад к чатам</a>
            <a class="danger" href="/logout">⎋&nbsp; Выйти</a>
        </div>
    </div>
</div>
<script>
if(localStorage.getItem("theme") === "dark"){
    document.body.classList.add("dark");
}

function updateOnlineStatus(){
    var banner = document.getElementById("offline-banner");
    if(!banner) return;
    banner.classList.toggle("show", !navigator.onLine);
}
window.addEventListener("online", updateOnlineStatus);
window.addEventListener("offline", updateOnlineStatus);
updateOnlineStatus();

function resizeImageFile(file, size){
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onerror = () => reject(new Error("read failed"));
        reader.onload = (ev) => {
            const img = new Image();
            img.onerror = () => reject(new Error("decode failed"));
            img.onload = () => {
                const canvas = document.createElement("canvas");
                canvas.width = size;
                canvas.height = size;
                const ctx = canvas.getContext("2d");
                const scale = Math.max(size / img.width, size / img.height);
                const w = img.width * scale, h = img.height * scale;
                ctx.drawImage(img, (size - w) / 2, (size - h) / 2, w, h);
                resolve(canvas.toDataURL("image/jpeg", 0.85));
            };
            img.src = ev.target.result;
        };
        reader.readAsDataURL(file);
    });
}

document.getElementById("photoInput").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if(!file) return;

    const wrap = document.getElementById("avatarEdit");
    wrap.classList.add("busy");

    try{
        const dataUrl = await resizeImageFile(file, 240);

        const res = await fetch("/set-photo", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ photo: dataUrl })
        });

        const result = await res.json();
        if(result.ok){
            location.reload();
        } else {
            alert("Не получилось загрузить фото");
            wrap.classList.remove("busy");
        }
    } catch(err){
        alert("Не получилось обработать изображение");
        wrap.classList.remove("busy");
    }
});
</script>
</body>
</html>
"""

# ---------------- MAIN HTML ----------------
HTML = BASE_STYLE + """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Relay</title><style>
body{
    display:flex;
    height:100vh;
    height:100dvh;
    overflow:hidden;
}

/* ---------- APP SHELL ---------- */
.chat{
    flex:1;
    display:flex;
    flex-direction:column;
    height:100vh;
    height:100dvh;
    min-width:0;
}

.chat-header{
    padding:14px 18px;
    background:var(--surface);
    border-bottom:1px solid var(--border);
    display:flex;
    align-items:center;
    gap:14px;
    flex-shrink:0;
}

.icon-btn{
    width:38px;
    height:38px;
    border-radius:10px;
    border:none;
    background:var(--surface-2);
    color:var(--text);
    font-size:18px;
    display:flex;
    align-items:center;
    justify-content:center;
    flex-shrink:0;
}
.icon-btn:hover{ background:var(--primary-dim); color:var(--primary); }

.back-btn{
    display:flex;
    align-items:center;
    gap:6px;
    height:38px;
    padding:0 14px 0 10px;
    border-radius:10px;
    border:none;
    background:var(--surface-2);
    color:var(--text);
    font-size:14px;
    font-weight:600;
    text-decoration:none;
    flex-shrink:0;
}
.back-btn:hover{ background:var(--primary-dim); color:var(--primary); }

.peer-title{
    display:flex;
    align-items:center;
    gap:10px;
}
.peer-name{
    font-weight:600;
    font-size:15px;
    line-height:1.25;
}

/* ---------- HOME / FRIEND LIST ---------- */
.home-wrap{
    padding:8px 20px 20px;
    overflow-y:auto;
}

.search-box{
    position:relative;
    margin:16px 0 6px;
}

.search-box input{
    width:100%;
    padding:13px 16px;
    border-radius:999px;
    border:1px solid var(--border);
    background:var(--surface);
    color:var(--text);
    font-size:14.5px;
    outline:none;
}

.search-box input:focus{ border-color:var(--primary); }

.section-label{
    font-size:12px;
    font-weight:600;
    letter-spacing:.06em;
    text-transform:uppercase;
    color:var(--text-dim);
    margin:22px 2px 10px;
}

#results .result-row, .friend-row{
    display:flex;
    align-items:center;
    gap:12px;
    padding:11px 12px;
    border-radius:var(--radius-md);
    background:var(--surface);
    border:1px solid var(--border);
    margin-bottom:8px;
    text-decoration:none;
    color:var(--text);
    animation:fadeInUp .3s ease both;
}

#results .result-row:hover, .friend-row:hover{
    transform:translateY(-2px);
    box-shadow:var(--shadow);
}

.avatar-badge{
    width:38px;
    height:38px;
    border-radius:50%;
    display:flex;
    align-items:center;
    justify-content:center;
    color:white;
    font-weight:600;
    font-size:14px;
    font-family:'Space Grotesk', sans-serif;
    flex-shrink:0;
    animation:popIn .3s ease both;
}
img.avatar-badge{ object-fit:cover; }

.avatar-wrap{
    position:relative;
    flex-shrink:0;
    display:flex;
}

.status-dot{
    position:absolute;
    bottom:-1px;
    right:-1px;
    width:11px;
    height:11px;
    border-radius:50%;
    background:#9CA3AF;
    border:2px solid var(--surface);
    transition:background .2s ease;
}

.status-dot.online{ background:#22C55E; }

.peer-status-text{
    font-size:11.5px;
    font-weight:400;
    color:var(--text-dim);
}
.peer-status-text.online{ color:#22C55E; font-weight:500; }

.result-meta{ flex:1; min-width:0; }
.result-meta .u{ font-weight:600; font-size:14.5px; }
.result-meta .id{ font-size:12px; color:var(--text-dim); }

.unread-badge{
    background:var(--primary);
    color:white;
    font-size:11.5px;
    font-weight:700;
    min-width:20px;
    height:20px;
    padding:0 6px;
    border-radius:999px;
    display:none;
    align-items:center;
    justify-content:center;
    flex-shrink:0;
}
.unread-badge.show{ display:flex; animation:pulseOnce .35s ease; }

.add-btn{
    border:none;
    background:var(--primary-dim);
    color:var(--primary);
    padding:7px 13px;
    border-radius:999px;
    font-size:13px;
    font-weight:600;
    flex-shrink:0;
}
.add-btn:hover{ opacity:0.85; }

.empty-state{
    color:var(--text-dim);
    font-size:13.5px;
    padding:14px 2px;
}

/* ---------- TOAST NOTIFICATIONS ---------- */
#toast-stack{
    position:fixed;
    top:16px;
    left:50%;
    z-index:999999;
    display:flex;
    flex-direction:column;
    gap:8px;
    align-items:center;
    pointer-events:none;
}

.toast{
    pointer-events:auto;
    left:50%;
    transform:translateX(-50%);
    background:var(--surface);
    border:1px solid var(--border);
    box-shadow:var(--shadow);
    border-radius:999px;
    padding:9px 16px 9px 9px;
    display:flex;
    align-items:center;
    gap:10px;
    max-width:88vw;
    cursor:pointer;
    animation:slideDownFade .3s ease both;
}

.toast .avatar-badge{ width:30px; height:30px; font-size:12px; animation:none; }
.toast .toast-text{ font-size:13.5px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:220px; }
.toast .toast-text b{ font-weight:600; }

.toast.leaving{ animation:fadeOutUp .25s ease both; }

@keyframes fadeOutUp{
    to{ opacity:0; transform:translate(-50%,-10px); }
}

/* ---------- CALLS ---------- */
.call-overlay{
    display:none;
    position:fixed;
    inset:0;
    z-index:99999999;
    background:rgba(10,10,20,0.55);
    align-items:center;
    justify-content:center;
}
.call-overlay.show{ display:flex; }

.call-card{
    width:min(90vw, 360px);
    background:var(--surface);
    border-radius:var(--radius-lg);
    box-shadow:var(--shadow);
    padding:32px 24px;
    text-align:center;
    animation:fadeInUp .3s ease both;
}

.call-name{ font-family:'Space Grotesk', sans-serif; font-weight:600; font-size:18px; }
.call-sub{ color:var(--text-dim); font-size:13.5px; margin-top:4px; margin-bottom:18px; }

.call-local-video{
    display:none;
    width:100px;
    border-radius:12px;
    position:absolute;
    top:16px;
    right:16px;
    box-shadow:var(--shadow);
}
.call-remote-video{
    display:none;
    width:100%;
    border-radius:14px;
    background:#000;
    margin-bottom:16px;
}

.group-call-tiles{
    display:none;
    flex-wrap:wrap;
    gap:8px;
    justify-content:center;
    margin-bottom:16px;
}
.group-call-tiles.show{ display:flex; }
.group-call-tiles .tile{
    position:relative;
    width:120px;
}
.group-call-tiles video{
    width:120px;
    height:90px;
    object-fit:cover;
    border-radius:10px;
    background:#000;
}
.group-call-tiles .tile-label{
    position:absolute;
    bottom:4px;
    left:6px;
    color:white;
    font-size:11px;
    text-shadow:0 1px 2px rgba(0,0,0,0.8);
}

.call-actions{
    display:flex;
    justify-content:center;
    gap:20px;
}

.call-btn{
    width:56px;
    height:56px;
    border-radius:50%;
    border:none;
    font-size:22px;
    color:white;
}
.call-btn.accept{ background:#22C55E; }
.call-btn.decline{ background:#EF4444; }

.group-call-banner{
    display:none;
    align-items:center;
    justify-content:space-between;
    gap:10px;
    padding:10px 16px;
    background:var(--primary-dim);
    color:var(--primary);
    font-size:13.5px;
    font-weight:500;
    border-bottom:1px solid var(--border);
}

.group-settings-panel{
    padding:16px;
    background:var(--surface);
    border-bottom:1px solid var(--border);
    text-align:center;
}

/* ---------- CHAT BUBBLES ---------- */
.chat-box{
    flex:1;
    overflow-y:auto;
    padding:20px;
    display:flex;
    flex-direction:column;
    gap:4px;
}

.msg-row{
    display:flex;
    margin:3px 0;
}
.msg-row.me{ justify-content:flex-end; }
.msg-row.them{ justify-content:flex-start; }

.msg{
    max-width:72%;
    padding:10px 14px;
    border-radius:16px;
    font-size:14.5px;
    line-height:1.4;
    box-shadow:var(--shadow);
    word-wrap:break-word;
}

.msg-row.me .msg{
    background:var(--bubble-me);
    color:var(--bubble-me-text);
    border-bottom-right-radius:4px;
}

.msg-row.them .msg{
    background:var(--bubble-them);
    border:1px solid var(--border);
    border-bottom-left-radius:4px;
}

.msg .sender{
    display:block;
    font-size:11px;
    font-weight:600;
    opacity:.65;
    margin-bottom:2px;
}

.msg-row.msg-in{
    animation:fadeInUp .25s ease both;
}

.msg-media{
    display:block;
    max-width:240px;
    max-height:320px;
    border-radius:10px;
    margin-bottom:4px;
}

/* ---------- INPUT ---------- */
.input-bar{
    display:flex;
    gap:10px;
    padding:14px 16px;
    padding-bottom:calc(14px + env(safe-area-inset-bottom));
    border-top:1px solid var(--border);
    background:var(--surface);
    flex-shrink:0;
}

.input-bar input{
    flex:1;
    padding:12px 16px;
    border-radius:999px;
    border:1px solid var(--border);
    outline:none;
    background:var(--surface-2);
    color:var(--text);
    font-size:14.5px;
}

.input-bar input:focus{ border-color:var(--primary); }

.input-bar button{
    width:44px;
    height:44px;
    border-radius:50%;
    border:none;
    background:var(--primary);
    color:white;
    font-size:16px;
    flex-shrink:0;
}
.input-bar button:hover{ opacity:0.9; }

/* ---------- SIDE MENU ---------- */
#menu{
    position:fixed;
    left:0;
    top:0;
    width:250px;
    height:100%;
    background:var(--surface);
    border-right:1px solid var(--border);
    padding:22px 18px;
    z-index:99999;
    transform:translateX(-100%);
    transition:transform .28s ease;
    display:flex;
    flex-direction:column;
}

#menu.open{ transform:translateX(0); }

#menu .wordmark{ margin-bottom:22px; font-size:17px; }

.menu-profile{
    text-align:center;
    padding:10px 0 18px;
    border-bottom:1px solid var(--border);
    margin-bottom:16px;
}

.menu-profile img{
    width:64px;
    height:64px;
    border-radius:50%;
    object-fit:cover;
}

.menu-profile h3{
    margin:10px 0 2px;
    font-size:15px;
}

.menu-profile p{
    margin:0;
    font-size:12px;
    color:var(--text-dim);
}

#menu button.menu-item{
    width:100%;
    padding:12px 13px;
    margin-bottom:8px;
    border:none;
    border-radius:var(--radius-md);
    background:var(--surface-2);
    color:var(--text);
    text-align:left;
    font-size:14px;
    font-weight:500;
    display:flex;
    align-items:center;
    gap:10px;
}
#menu button.menu-item:hover{ background:var(--primary-dim); color:var(--primary); }

#overlay{
    display:none;
    position:fixed;
    inset:0;
    background:rgba(10,10,20,0.35);
    z-index:99998;
}

/* ---------- LOGIN (embedded) ---------- */
.login-wrap{
    flex:1;
    display:flex;
    align-items:center;
    justify-content:center;
    flex-direction:column;
    gap:16px;
    text-align:center;
    padding:20px;
    animation:fadeInUp .4s ease both;
}

.login-wrap .wordmark{ font-size:26px; }
.login-wrap p{ color:var(--text-dim); max-width:280px; font-size:14px; margin:0; }

.google-btn{
    display:flex;
    align-items:center;
    gap:10px;
    padding:12px 22px;
    border-radius:999px;
    border:1px solid var(--border);
    background:var(--surface);
    color:var(--text);
    font-weight:600;
    font-size:14.5px;
    box-shadow:var(--shadow);
}
.google-btn:hover{ transform:translateY(-1px); }
</style>
</head>

<body>

<div id="offline-banner">Нет доступа к интернету</div>
<div id="toast-stack"></div>
<div id="overlay" onclick="toggleMenu()"></div>

<div id="call-overlay" class="call-overlay" onclick="retryMediaPlayback()">
    <div class="call-card">
        <div class="avatar-badge" id="callAvatar" style="width:72px;height:72px;font-size:26px;margin:0 auto 14px;"></div>
        <div class="call-name" id="callName">—</div>
        <div class="call-sub" id="callSub">—</div>

        <video id="localVideo" class="call-local-video" autoplay playsinline muted></video>
        <video id="remoteVideo" class="call-remote-video" autoplay playsinline></video>
        <audio id="remoteAudio" autoplay></audio>
        <div id="groupCallTiles" class="group-call-tiles"></div>

        <div class="call-actions" id="callActionsIncoming" style="display:none;">
            <button class="call-btn decline" onclick="declineIncomingCall()">✕</button>
            <button class="call-btn accept" onclick="acceptIncomingCall()">✓</button>
        </div>
        <div class="call-actions" id="callActionsActive" style="display:none;">
            <button class="call-btn decline" onclick="hangUp()">✕</button>
        </div>
    </div>
</div>

<div id="menu">
    <div class="wordmark"><span class="dot"></span>Relay</div>

    {% if session.get("username") %}
    <div class="menu-profile">
        {% if photo %}
            <img src="{{photo}}">
        {% else %}
            <div class="avatar-badge" style="width:64px;height:64px;font-size:24px;margin:0 auto;background:hsl({{ (session.get('username')|length * 47) % 360 }},60%,45%)">
                {{session.get("username")[0]|upper}}
            </div>
        {% endif %}
        <h3>{{session.get("username")}}</h3>
        <p class="mono">ID {{my_id}}</p>
    </div>

    <button class="menu-item" onclick="window.location.href='/'">💬&nbsp; Чаты</button>
    <button class="menu-item" onclick="toggleTheme()">🌗&nbsp; Тема</button>
    <button class="menu-item" onclick="window.location.href='/settings'">⚙️&nbsp; Настройки</button>
    {% endif %}
</div>

<div class="chat">

{% if not session.get("email") %}

<div class="login-wrap">
    <div class="wordmark"><span class="dot"></span>Relay</div>
    <p>Простой чат для тех, кто ценит прямой разговор.</p>
    <button class="google-btn" onclick="loginGoogle()">Войти через Google</button>
</div>

<script src="https://www.gstatic.com/firebasejs/10.7.1/firebase-app-compat.js"></script>
<script src="https://www.gstatic.com/firebasejs/10.7.1/firebase-auth-compat.js"></script>

<script>
const firebaseConfig = {
  apiKey: "AIzaSyByRxM7bQhYSK5XCuaZMRo0s42DGeaav6Y",
  authDomain: "my-chat2-ae3ca.firebaseapp.com",
  projectId: "my-chat2-ae3ca",
};

firebase.initializeApp(firebaseConfig);

function loginGoogle(){
  const provider = new firebase.auth.GoogleAuthProvider();

  firebase.auth().signInWithPopup(provider)
  .then(result => {
      const user = result.user;

      return fetch("/google-login", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
            name: user.displayName,
            email: user.email,
            photo: user.photoURL
        })
      });
  })
  .then(() => location.reload())
  .catch(err => alert("Ошибка входа"));
}
</script>

{% elif not session.get("username") %}

<div class="login-wrap">
    <div class="wordmark"><span class="dot"></span>Relay</div>
    <p>Осталось придумать username.</p>
    <button class="google-btn" onclick="window.location.href='/set-username'">Создать username</button>
</div>

{% elif not peer and not group %}

<div class="chat-header">
    <button class="icon-btn" onclick="toggleMenu()">☰</button>
    <div class="peer-title">Чаты</div>
</div>

<div class="home-wrap">

    <div class="search-box">
        <input id="search" type="text" placeholder="Найти пользователя по имени или ID" oninput="searchUser()">
    </div>

    <div id="results"></div>

    <div class="section-label">Чаты</div>

    {% if friends %}
        {% for f in friends %}
            <a class="friend-row" href="/chat/{{f.username}}" data-friend="{{f.username}}">
                <div class="avatar-wrap">
                    {% if f.photo %}
                        <img class="avatar-badge" src="{{f.photo}}">
                    {% else %}
                        <div class="avatar-badge" style="background:hsl({{ (f.username|length * 47) % 360 }},60%,45%)">
                            {{f.username[0]|upper}}
                        </div>
                    {% endif %}
                    <span class="status-dot" data-status="{{f.username}}"></span>
                </div>
                <div class="result-meta">
                    <div class="u">{{f.username}}</div>
                </div>
                <span class="unread-badge" data-badge="{{f.username}}"></span>
            </a>
        {% endfor %}
    {% else %}
        <div class="empty-state">Пока пусто — найди кого-нибудь через поиск выше, и переписка появится здесь.</div>
    {% endif %}

    <div class="section-label" style="display:flex;align-items:center;justify-content:space-between;">
        <span>Группы</span>
        <button class="add-btn" onclick="toggleGroupForm()" style="font-size:12px;">+ Создать</button>
    </div>

    <div id="groupForm" style="display:none;margin-bottom:14px;">
        <input id="groupName" type="text" placeholder="Название группы"
               style="width:100%;padding:11px 14px;border-radius:10px;border:1px solid var(--border);background:var(--surface);color:var(--text);font-size:14px;margin-bottom:10px;">
        <div style="font-size:12px;color:var(--text-dim);margin-bottom:6px;">Добавить из чатов:</div>
        <div id="groupMemberPicker" style="display:flex;flex-direction:column;gap:6px;margin-bottom:10px;">
            {% for f in friends %}
                <label style="display:flex;align-items:center;gap:8px;font-size:14px;">
                    <input type="checkbox" value="{{f.username}}" class="group-member-checkbox">
                    {{f.username}}
                </label>
            {% endfor %}
            {% if not friends %}
                <div class="empty-state" style="padding:0;">Пока не с кем — сначала начни переписку с кем-нибудь.</div>
            {% endif %}
        </div>
        <button class="add-btn" onclick="submitGroup()" style="width:100%;padding:10px;">Создать группу</button>
    </div>

    {% if groups %}
        {% for g in groups %}
            <a class="friend-row" href="/group/{{g.id}}">
                {% if g.photo %}
                    <img class="avatar-badge" src="{{g.photo}}">
                {% else %}
                    <div class="avatar-badge" style="background:hsl({{ (g.name|length * 47) % 360 }},55%,40%)">
                        #
                    </div>
                {% endif %}
                <div class="result-meta">
                    <div class="u">{{g.name}}</div>
                    <div class="id" style="font-size:12px;color:var(--text-dim);">{{g.member_count}} участник(ов)</div>
                </div>
            </a>
        {% endfor %}
    {% else %}
        <div class="empty-state">Групп пока нет.</div>
    {% endif %}

</div>

{% elif group %}

<div class="chat-header">
    <a class="back-btn" href="/">← Чаты</a>
    <div class="peer-title">
        {% if group.photo %}
            <img class="avatar-badge" style="width:32px;height:32px;" src="{{group.photo}}">
        {% else %}
            <div class="avatar-badge" style="width:32px;height:32px;font-size:12px;background:hsl({{ (group.name|length * 47) % 360 }},55%,40%)">#</div>
        {% endif %}
        <div>
            <div class="peer-name">{{group.name}}</div>
            <div class="peer-status-text">{{group.members|length}} участник(ов)</div>
        </div>
    </div>
    <div style="flex:1;"></div>
    <button class="icon-btn" onclick="startGroupCall({{group.id}}, 'audio', {{ (group.photo or None)|tojson }})" title="Аудиозвонок">📞</button>
    <button class="icon-btn" onclick="startGroupCall({{group.id}}, 'video', {{ (group.photo or None)|tojson }})" title="Видеозвонок">🎥</button>
    <button class="icon-btn" onclick="toggleGroupSettings()" title="Настройки группы">⚙️</button>
    <button class="icon-btn" onclick="toggleMenu()">☰</button>
</div>

<div id="groupCallBanner" class="group-call-banner" style="display:none;">
    <span id="groupCallBannerText">Идёт звонок</span>
    <button class="add-btn" onclick="joinActiveGroupCall()">Присоединиться</button>
</div>

<div id="groupSettingsPanel" class="group-settings-panel" style="display:none;">
    <label class="avatar-edit" id="groupAvatarEdit" style="margin:0 auto 12px;">
        {% if group.photo %}
            <img class="avatar" src="{{group.photo}}">
        {% else %}
            <div class="avatar"></div>
        {% endif %}
        <span class="cam">✎</span>
        <span class="uploading">...</span>
        <input type="file" accept="image/*" id="groupPhotoInput">
    </label>
    <input id="groupNameInput" type="text" value="{{group.name}}" style="width:100%;padding:11px 14px;border-radius:10px;border:1px solid var(--border);background:var(--surface);color:var(--text);font-size:14px;margin-bottom:10px;">
    <button class="add-btn" onclick="saveGroupName({{group.id}})" style="width:100%;padding:10px;margin-bottom:10px;">Сохранить название</button>

    <div style="text-align:left;margin:16px 0 10px;font-size:12px;color:var(--text-dim);">Пригласить в группу:</div>
    <input id="inviteInput" type="text" placeholder="Найти пользователя по имени или ID" oninput="searchInviteUser({{group.id}})"
           style="width:100%;padding:11px 14px;border-radius:10px;border:1px solid var(--border);background:var(--surface);color:var(--text);font-size:14px;">
    <div id="inviteResults" style="text-align:left;margin-top:8px;margin-bottom:10px;"></div>

    <button class="add-btn" onclick="leaveGroup({{group.id}})" style="width:100%;padding:10px;margin-bottom:10px;">Выйти из группы</button>
    {% if group.created_by == me %}
        <button class="add-btn" onclick="deleteGroup({{group.id}})" style="width:100%;padding:10px;background:#EF4444;color:white;">Удалить группу</button>
    {% endif %}
</div>

<div class="chat-box" id="chatBox">
    {% for m in messages %}
        <div class="msg-row {{ 'me' if m[0] == me else 'them' }}">
            <div class="msg">
                {% if m[0] != me %}<span class="sender">{{m[0]}}</span>{% endif %}
                {% if m[2] == 'image' %}<img class="msg-media" src="{{m[3]}}">{% elif m[2] == 'video' %}<video class="msg-media" src="{{m[3]}}" controls playsinline></video>{% endif %}
                {{m[1]}}
            </div>
        </div>
    {% endfor %}
</div>

<form class="input-bar" id="sendForm" method="POST" action="/send-group/{{group.id}}">
    <label class="icon-btn" style="cursor:pointer;">
        📎
        <input type="file" id="mediaInput" accept="image/*,video/*" style="display:none;">
    </label>
    <input name="msg" placeholder="Написать в группу..." autocomplete="off">
    <button type="submit">➤</button>
</form>

<script>
const ME = {{ me|tojson }};
const GROUP_ID = {{ group.id|tojson }};
const GROUP_PHOTO = {{ (group.photo or None)|tojson }};
const GROUP_MEMBERS = {{ group.members|tojson }};

function escapeHtml(str){
    return str.replace(/[&<>"']/g, s => ({
        "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
    }[s]));
}

let knownCount = {{ messages|length }};

async function updateChat(){
    let res = await fetch("/group-messages/" + GROUP_ID);
    let data = await res.json();

    let box = document.getElementById("chatBox");
    if(data.length === knownCount) return;

    let atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;

    if(data.length < knownCount){
        box.innerHTML = "";
        knownCount = 0;
    }

    data.slice(knownCount).forEach(m => {
        const mine = m[0] === ME;
        const row = document.createElement("div");
        row.className = `msg-row msg-in ${mine ? 'me' : 'them'}`;
        let mediaHtml = "";
        if(m[2] === "image"){ mediaHtml = `<img class="msg-media" src="${m[3]}">`; }
        else if(m[2] === "video"){ mediaHtml = `<video class="msg-media" src="${m[3]}" controls playsinline></video>`; }
        row.innerHTML = `
            <div class="msg">
                ${mine ? '' : `<span class="sender">${escapeHtml(m[0])}</span>`}
                ${mediaHtml}
                ${escapeHtml(m[1])}
            </div>
        `;
        box.appendChild(row);
    });

    knownCount = data.length;
    if(atBottom){ box.scrollTop = box.scrollHeight; }
}

document.getElementById("chatBox").scrollTop = document.getElementById("chatBox").scrollHeight;
setInterval(updateChat, 2000);

document.getElementById("sendForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = e.target.msg;
    const text = input.value.trim();
    if(!text) return;
    input.value = "";
    await fetch("/send-group/" + GROUP_ID, {
        method: "POST",
        headers: {"Content-Type":"application/x-www-form-urlencoded"},
        body: "msg=" + encodeURIComponent(text)
    });
    updateChat();
});

document.getElementById("mediaInput").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if(!file) return;
    e.target.value = "";

    try{
        let mediaType, mediaData;

        if(file.type.startsWith("image/")){
            mediaType = "image";
            mediaData = await resizeImageForChat(file, 1280);
        } else if(file.type.startsWith("video/")){
            if(file.size > 12 * 1024 * 1024){
                alert("Видео слишком большое (максимум примерно 12 МБ). Попробуй покороче или более сжатое.");
                return;
            }
            mediaType = "video";
            mediaData = await new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onload = () => resolve(reader.result);
                reader.onerror = () => reject(new Error("read failed"));
                reader.readAsDataURL(file);
            });
        } else {
            alert("Можно отправить только фото или видео");
            return;
        }

        const res = await fetch("/send-group-media/" + GROUP_ID, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ media_type: mediaType, media_data: mediaData })
        });
        const result = await res.json();
        if(result.ok){ updateChat(); } else { alert(result.error || "Не получилось отправить файл"); }
    } catch(err){
        alert("Не получилось обработать файл");
    }
});

async function searchInviteUser(groupId){
    const q = document.getElementById("inviteInput").value.trim();
    const results = document.getElementById("inviteResults");

    if(!q){ results.innerHTML = ""; return; }

    const res = await fetch("/search?q=" + encodeURIComponent(q));
    const data = await res.json();

    const filtered = data.results.filter(u => u[0] !== ME && !GROUP_MEMBERS.includes(u[0]));

    if(filtered.length === 0){
        results.innerHTML = `<div class="empty-state" style="padding:4px 0;">Никого не нашлось</div>`;
        return;
    }

    let html = "";
    filtered.forEach(u => {
        const hue = (u[0].length * 47) % 360;
        const avatarHtml = u[2]
            ? `<img class="avatar-badge" style="width:32px;height:32px;" src="${u[2]}">`
            : `<div class="avatar-badge" style="width:32px;height:32px;font-size:12px;background:hsl(${hue},60%,45%)">${u[0][0].toUpperCase()}</div>`;
        html += `
        <div class="result-row">
            ${avatarHtml}
            <div class="result-meta"><div class="u">${u[0]}</div></div>
            <button class="add-btn" onclick="inviteToGroup(${groupId}, '${u[0]}')">Пригласить</button>
        </div>
        `;
    });
    results.innerHTML = html;
}

async function inviteToGroup(groupId, username){
    const res = await fetch(`/group/${groupId}/invite`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username })
    });
    const result = await res.json();
    if(result.ok){
        location.reload();
    } else {
        alert(result.error || "Не получилось пригласить");
    }
}

function toggleGroupSettings(){
    const panel = document.getElementById("groupSettingsPanel");
    panel.style.display = panel.style.display === "none" ? "block" : "none";
}

async function saveGroupName(groupId){
    const name = document.getElementById("groupNameInput").value.trim();
    if(!name) return;
    const res = await fetch(`/group/${groupId}/rename`, {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({ name })
    });
    const result = await res.json();
    if(result.ok){ location.reload(); } else { alert("Не получилось переименовать группу"); }
}

async function leaveGroup(groupId){
    if(!confirm("Выйти из группы?")) return;
    await fetch(`/group/${groupId}/leave`, { method: "POST" });
    window.location.href = "/";
}

async function deleteGroup(groupId){
    if(!confirm("Удалить группу насовсем? Это действие нельзя отменить.")) return;
    const res = await fetch(`/group/${groupId}/delete`, { method: "POST" });
    const result = await res.json();
    if(result.ok){
        window.location.href = "/";
    } else {
        alert(result.error || "Не получилось удалить группу");
    }
}

document.getElementById("groupPhotoInput").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if(!file) return;

    const wrap = document.getElementById("groupAvatarEdit");
    wrap.classList.add("busy");

    try{
        const dataUrl = await resizeImageFile(file, 240);
        const res = await fetch(`/group/${GROUP_ID}/photo`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ photo: dataUrl })
        });
        const result = await res.json();
        if(result.ok){ location.reload(); } else { alert("Не получилось загрузить фото"); wrap.classList.remove("busy"); }
    } catch(err){
        alert("Не получилось обработать изображение");
        wrap.classList.remove("busy");
    }
});

async function pollGroupCallBanner(){
    try{
        const res = await fetch("/group-call/status/" + GROUP_ID);
        const data = await res.json();
        const banner = document.getElementById("groupCallBanner");
        if(!banner) return;

        if(data.active && callState === "idle"){
            document.getElementById("groupCallBannerText").textContent =
                (data.kind === "video" ? "Идёт видеозвонок" : "Идёт звонок") + " · " + data.participant_count + " участник(ов)";
            banner.dataset.room = data.room;
            banner.dataset.kind = data.kind;
            banner.style.display = "flex";
        } else {
            banner.style.display = "none";
        }
    } catch(e){}
}

function joinActiveGroupCall(){
    const banner = document.getElementById("groupCallBanner");
    if(banner && banner.dataset.room){
        joinGroupCall(GROUP_ID, banner.dataset.room, banner.dataset.kind, "Группа", GROUP_PHOTO);
    }
}

setInterval(pollGroupCallBanner, 3000);
pollGroupCallBanner();
</script>

{% else %}

<div class="chat-header">
    <a class="back-btn" href="/">← Чаты</a>
    <div class="peer-title">
        <div class="avatar-wrap">
            {% if peer_photo %}
                <img class="avatar-badge" style="width:32px;height:32px;" src="{{peer_photo}}">
            {% else %}
                <div class="avatar-badge" style="width:32px;height:32px;font-size:12px;background:hsl({{ (peer|length * 47) % 360 }},60%,45%)">
                    {{peer[0]|upper}}
                </div>
            {% endif %}
            <span class="status-dot" data-status="{{peer}}"></span>
        </div>
        <div>
            <div class="peer-name">{{peer}}</div>
            <div class="peer-status-text" id="peerStatusText">—</div>
        </div>
    </div>
    <div style="flex:1;"></div>
    <button class="icon-btn" onclick="startCall('{{peer}}', 'audio', {{ (peer_photo or None)|tojson }})" title="Аудиозвонок">📞</button>
    <button class="icon-btn" onclick="startCall('{{peer}}', 'video', {{ (peer_photo or None)|tojson }})" title="Видеозвонок">🎥</button>
    <button class="icon-btn" onclick="toggleMenu()">☰</button>
</div>

<div class="chat-box" id="chatBox">
    {% for m in messages %}
        <div class="msg-row {{ 'me' if m[0] == me else 'them' }}">
            <div class="msg">
                {% if m[0] != me %}<span class="sender">{{m[0]}}</span>{% endif %}
                {% if m[2] == 'image' %}<img class="msg-media" src="{{m[3]}}">{% elif m[2] == 'video' %}<video class="msg-media" src="{{m[3]}}" controls playsinline></video>{% endif %}
                {{m[1]}}
            </div>
        </div>
    {% endfor %}
</div>

<form class="input-bar" id="sendForm" method="POST" action="/send/{{peer}}">
    <label class="icon-btn" style="cursor:pointer;">
        📎
        <input type="file" id="mediaInput" accept="image/*,video/*" style="display:none;">
    </label>
    <input name="msg" placeholder="Написать сообщение..." autocomplete="off">
    <button type="submit">➤</button>
</form>

<script>
const ME = {{ me|tojson }};
const PEER = {{ peer|tojson }};

function escapeHtml(str){
    return str.replace(/[&<>"']/g, s => ({
        "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
    }[s]));
}

let knownCount = {{ messages|length }};

async function updateChat(){
    let res = await fetch("/messages/" + encodeURIComponent(PEER));
    let data = await res.json();

    let box = document.getElementById("chatBox");

    if(data.length === knownCount) return;

    let atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;

    if(data.length < knownCount){
        // conversation shrank unexpectedly - do a full, unanimated redraw
        box.innerHTML = "";
        knownCount = 0;
    }

    data.slice(knownCount).forEach(m => {
        const mine = m[0] === ME;
        const row = document.createElement("div");
        row.className = `msg-row msg-in ${mine ? 'me' : 'them'}`;
        let mediaHtml = "";
        if(m[2] === "image"){ mediaHtml = `<img class="msg-media" src="${m[3]}">`; }
        else if(m[2] === "video"){ mediaHtml = `<video class="msg-media" src="${m[3]}" controls playsinline></video>`; }
        row.innerHTML = `
            <div class="msg">
                ${mine ? '' : `<span class="sender">${escapeHtml(m[0])}</span>`}
                ${mediaHtml}
                ${escapeHtml(m[1])}
            </div>
        `;
        box.appendChild(row);
    });

    knownCount = data.length;

    if(atBottom){ box.scrollTop = box.scrollHeight; }
}

document.getElementById("chatBox").scrollTop = document.getElementById("chatBox").scrollHeight;
setInterval(updateChat, 2000);

document.getElementById("sendForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = e.target.msg;
    const text = input.value.trim();
    if(!text) return;
    input.value = "";
    await fetch("/send/" + encodeURIComponent(PEER), {
        method: "POST",
        headers: {"Content-Type":"application/x-www-form-urlencoded"},
        body: "msg=" + encodeURIComponent(text)
    });
    updateChat();
});

document.getElementById("mediaInput").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if(!file) return;
    e.target.value = "";

    try{
        let mediaType, mediaData;

        if(file.type.startsWith("image/")){
            mediaType = "image";
            mediaData = await resizeImageForChat(file, 1280);
        } else if(file.type.startsWith("video/")){
            if(file.size > 12 * 1024 * 1024){
                alert("Видео слишком большое (максимум примерно 12 МБ). Попробуй покороче или более сжатое.");
                return;
            }
            mediaType = "video";
            mediaData = await new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onload = () => resolve(reader.result);
                reader.onerror = () => reject(new Error("read failed"));
                reader.readAsDataURL(file);
            });
        } else {
            alert("Можно отправить только фото или видео");
            return;
        }

        const res = await fetch("/send-media/" + encodeURIComponent(PEER), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ media_type: mediaType, media_data: mediaData })
        });
        const result = await res.json();
        if(result.ok){ updateChat(); } else { alert(result.error || "Не получилось отправить файл"); }
    } catch(err){
        alert("Не получилось обработать файл");
    }
});
</script>

{% endif %}

</div>

<script>
const MY_USERNAME = {{ (me or None)|tojson }};

function escapeHtml(str){
    return str.replace(/[&<>"']/g, s => ({
        "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
    }[s]));
}

function toggleMenu(){
    let menu = document.getElementById("menu");
    let overlay = document.getElementById("overlay");
    let isOpen = menu.classList.contains("open");

    if(!isOpen){
        menu.classList.add("open");
        overlay.style.display = "block";
    } else {
        menu.classList.remove("open");
        overlay.style.display = "none";
    }
}

function toggleTheme(){
    document.body.classList.toggle("dark");
    localStorage.setItem("theme", document.body.classList.contains("dark") ? "dark" : "light");
}

async function searchUser(){
    let q = document.getElementById("search").value;
    let results = document.getElementById("results");

    if(q.length === 0){
        results.innerHTML = "";
        return;
    }

    let r = await fetch("/search?q=" + encodeURIComponent(q));
    let data = await r.json();

    if(data.results.length === 0){
        results.innerHTML = `<div class="empty-state">Никого не нашлось по запросу «${q}»</div>`;
        return;
    }

    let html = "";
    data.results.forEach(u => {
        const hue = (u[0].length * 47) % 360;
        const avatarHtml = u[2]
            ? `<img class="avatar-badge" src="${u[2]}">`
            : `<div class="avatar-badge" style="background:hsl(${hue},60%,45%)">${u[0][0].toUpperCase()}</div>`;
        html += `
        <div class="result-row" onclick="window.location.href='/chat/${encodeURIComponent(u[0])}'" style="cursor:pointer;">
            ${avatarHtml}
            <div class="result-meta">
                <div class="u">${u[0]}</div>
                <div class="id mono">ID ${u[1]}</div>
            </div>
            <button class="add-btn" onclick="event.stopPropagation(); addFriend('${u[0]}')">Добавить</button>
        </div>
        `;
    });
    results.innerHTML = html;
}

function resizeImageFile(file, size){
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onerror = () => reject(new Error("read failed"));
        reader.onload = (ev) => {
            const img = new Image();
            img.onerror = () => reject(new Error("decode failed"));
            img.onload = () => {
                const canvas = document.createElement("canvas");
                canvas.width = size;
                canvas.height = size;
                const ctx = canvas.getContext("2d");
                const scale = Math.max(size / img.width, size / img.height);
                const w = img.width * scale, h = img.height * scale;
                ctx.drawImage(img, (size - w) / 2, (size - h) / 2, w, h);
                resolve(canvas.toDataURL("image/jpeg", 0.85));
            };
            img.src = ev.target.result;
        };
        reader.readAsDataURL(file);
    });
}

function resizeImageForChat(file, maxDim){
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onerror = () => reject(new Error("read failed"));
        reader.onload = (ev) => {
            const img = new Image();
            img.onerror = () => reject(new Error("decode failed"));
            img.onload = () => {
                const scale = Math.min(1, maxDim / Math.max(img.width, img.height));
                const w = Math.round(img.width * scale);
                const h = Math.round(img.height * scale);
                const canvas = document.createElement("canvas");
                canvas.width = w;
                canvas.height = h;
                canvas.getContext("2d").drawImage(img, 0, 0, w, h);
                resolve(canvas.toDataURL("image/jpeg", 0.78));
            };
            img.src = ev.target.result;
        };
        reader.readAsDataURL(file);
    });
}

async function addFriend(username){
    await fetch("/add-friend", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username })
    });
    location.reload();
}

function toggleGroupForm(){
    const form = document.getElementById("groupForm");
    if(!form) return;
    form.style.display = form.style.display === "none" ? "block" : "none";
}

async function submitGroup(){
    const nameInput = document.getElementById("groupName");
    const name = nameInput.value.trim();
    if(!name){
        alert("Введи название группы");
        return;
    }

    const members = Array.from(document.querySelectorAll(".group-member-checkbox:checked")).map(el => el.value);

    const res = await fetch("/create-group", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, members })
    });
    const result = await res.json();

    if(result.ok){
        window.location.href = "/group/" + result.group_id;
    } else {
        alert("Не получилось создать группу");
    }
}

if(localStorage.getItem("theme") === "dark"){
    document.body.classList.add("dark");
}

function updateOnlineStatus(){
    var banner = document.getElementById("offline-banner");
    if(!banner) return;
    banner.classList.toggle("show", !navigator.onLine);
}
window.addEventListener("online", updateOnlineStatus);
window.addEventListener("offline", updateOnlineStatus);
updateOnlineStatus();

function showToast(friend, text, photo){
    const stack = document.getElementById("toast-stack");
    if(!stack) return;

    const hue = (friend.length * 47) % 360;
    const avatarHtml = photo
        ? `<img class="avatar-badge" src="${photo}">`
        : `<div class="avatar-badge" style="background:hsl(${hue},60%,45%)">${escapeHtml(friend[0].toUpperCase())}</div>`;

    const t = document.createElement("div");
    t.className = "toast";
    t.innerHTML = `
        ${avatarHtml}
        <div class="toast-text"><b>${escapeHtml(friend)}</b>: ${escapeHtml(text)}</div>
    `;
    t.onclick = () => { window.location.href = "/chat/" + encodeURIComponent(friend); };
    stack.appendChild(t);

    setTimeout(() => {
        t.classList.add("leaving");
        setTimeout(() => t.remove(), 250);
    }, 3800);
}

function requestNotifyPermission(){
    if(!("Notification" in window)) return;
    if(Notification.permission === "default"){
        Notification.requestPermission().catch(() => {});
    }
}

function notifyNewMessage(friend, text, photo){
    const canUseSystem = ("Notification" in window) && Notification.permission === "granted" && document.hidden;

    if(canUseSystem){
        try{
            const n = new Notification(friend, { body: text, tag: "relay-" + friend, icon: photo || undefined });
            n.onclick = () => {
                window.focus();
                window.location.href = "/chat/" + encodeURIComponent(friend);
            };
            return;
        } catch(e){ /* fall through to in-page toast */ }
    }

    showToast(friend, text, photo);
}

function applyUnreadBadges(data){
    document.querySelectorAll("[data-badge]").forEach(el => {
        const info = data[el.getAttribute("data-badge")];
        if(info && info.count > 0){
            el.textContent = info.count > 9 ? "9+" : info.count;
            el.classList.add("show");
        } else {
            el.textContent = "";
            el.classList.remove("show");
        }
    });
}

let knownUnread = null;

async function pollUnread(){
    try{
        let res = await fetch("/unread-counts");
        let data = await res.json();

        const banner = document.getElementById("offline-banner");
        if(banner) banner.classList.remove("show");

        if(knownUnread !== null){
            for(const friend in data){
                const prevCount = (knownUnread[friend] && knownUnread[friend].count) || 0;
                if(data[friend].count > prevCount){
                    notifyNewMessage(friend, data[friend].last, data[friend].photo);
                }
            }
        }

        knownUnread = data;
        applyUnreadBadges(data);
    } catch(e){
        const banner = document.getElementById("offline-banner");
        if(banner) banner.classList.add("show");
    }
}

async function pollPresence(){
    const dots = document.querySelectorAll("[data-status]");
    if(dots.length === 0) return;

    const usernames = Array.from(new Set(Array.from(dots).map(el => el.getAttribute("data-status"))));

    try{
        const res = await fetch("/presence", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ usernames })
        });
        const data = await res.json();

        dots.forEach(el => {
            el.classList.toggle("online", !!data[el.getAttribute("data-status")]);
        });

        const statusText = document.getElementById("peerStatusText");
        const activePeer = (typeof PEER !== "undefined") ? PEER : null;
        if(statusText && activePeer){
            const online = !!data[activePeer];
            statusText.textContent = online ? "в сети" : "не в сети";
            statusText.classList.toggle("online", online);
        }
    } catch(e){ /* ignore transient network errors */ }
}

/* ---------- CALLS (WebRTC) ---------- */
let callState = "idle"; // idle | ringing-out | ringing-in | active
let currentRoom = null;
let currentPeerName = null;
let currentPeerPhoto = null;
let currentKind = null;
let pc = null;
let localStream = null;
let lastSignalId = 0;
let incomingPollTimer = null;
let statusPollTimer = null;
let signalsPollTimer = null;

function showCallOverlay(){ document.getElementById("call-overlay").classList.add("show"); }
function hideCallOverlay(){ document.getElementById("call-overlay").classList.remove("show"); }

function setCallHeader(name, sub, photo){
    const av = document.getElementById("callAvatar");
    if(photo){
        av.style.background = "none";
        av.style.backgroundImage = `url(${photo})`;
        av.style.backgroundSize = "cover";
        av.style.backgroundPosition = "center";
        av.textContent = "";
    } else {
        const hue = (name.length * 47) % 360;
        av.style.backgroundImage = "none";
        av.style.background = `hsl(${hue},60%,45%)`;
        av.textContent = name[0] ? name[0].toUpperCase() : "?";
    }
    document.getElementById("callName").textContent = name;
    document.getElementById("callSub").textContent = sub;
}

/* ---------- RING SOUNDS (generated tones, no audio files needed) ---------- */
let ringCtx = null;
let ringTimer = null;

function getRingCtx(){
    if(!ringCtx){
        try{ ringCtx = new (window.AudioContext || window.webkitAudioContext)(); }
        catch(e){ return null; }
    }
    if(ringCtx.state === "suspended"){ ringCtx.resume().catch(() => {}); }
    return ringCtx;
}

function beep(freq, durationMs){
    const ctx = getRingCtx();
    if(!ctx) return;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = freq;
    gain.gain.value = 0.2;
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + durationMs / 1000);
}

function startRingback(){
    stopRinging();
    beep(425, 1000);
    ringTimer = setInterval(() => beep(425, 1000), 3000);
}

function startRingtone(){
    stopRinging();
    const pattern = () => { beep(480, 400); setTimeout(() => beep(440, 400), 500); };
    pattern();
    ringTimer = setInterval(pattern, 2500);
}

function stopRinging(){
    if(ringTimer){ clearInterval(ringTimer); ringTimer = null; }
}

async function startCall(callee, kind, photo){
    if(callState !== "idle"){ alert("У тебя уже есть активный звонок"); return; }

    const res = await fetch("/call/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ callee, kind })
    });
    const data = await res.json();
    if(!data.ok){ alert("Не получилось начать звонок"); return; }

    currentRoom = data.room;
    currentPeerName = callee;
    currentPeerPhoto = photo;
    currentKind = kind;
    callState = "ringing-out";
    lastSignalId = 0;

    setCallHeader(callee, kind === "video" ? "Звоним... (видео)" : "Звоним...", photo);
    document.getElementById("callActionsIncoming").style.display = "none";
    document.getElementById("callActionsActive").style.display = "flex";
    showCallOverlay();
    startRingback();

    stopIncomingPoll();
    statusPollTimer = setInterval(pollOutgoingStatus, 1200);
}

async function pollOutgoingStatus(){
    if(!currentRoom || callState !== "ringing-out") return;
    try{
        const res = await fetch("/call/status/" + currentRoom);
        const data = await res.json();
        if(!data) return;

        if(data.status === "accepted"){
            clearInterval(statusPollTimer);
            stopRinging();
            await beginWebRTC(true);
        } else if(data.status === "declined" || data.status === "ended"){
            clearInterval(statusPollTimer);
            stopRinging();
            setCallHeader(currentPeerName, "Звонок отклонён", currentPeerPhoto);
            setTimeout(cleanupCall, 1500);
        }
    } catch(e){}
}

async function pollIncomingCall(){
    if(callState !== "idle") return;
    try{
        const res = await fetch("/call/incoming");
        const data = await res.json();
        if(!data) return;

        currentRoom = data.room;
        currentPeerName = data.caller;
        currentPeerPhoto = data.caller_photo;
        currentKind = data.kind;
        callState = "ringing-in";
        lastSignalId = 0;

        setCallHeader(data.caller, data.kind === "video" ? "Входящий видеозвонок" : "Входящий звонок", data.caller_photo);
        document.getElementById("callActionsIncoming").style.display = "flex";
        document.getElementById("callActionsActive").style.display = "none";
        showCallOverlay();
        startRingtone();
    } catch(e){}
}

async function acceptIncomingCall(){
    stopRinging();
    await fetch("/call/respond", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ room: currentRoom, action: "accept" })
    });
    document.getElementById("callActionsIncoming").style.display = "none";
    document.getElementById("callActionsActive").style.display = "flex";
    setCallHeader(currentPeerName, currentKind === "video" ? "Видеозвонок" : "Аудиозвонок", currentPeerPhoto);
    await beginWebRTC(false);
}

async function declineIncomingCall(){
    stopRinging();
    await fetch("/call/respond", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ room: currentRoom, action: "decline" })
    });
    cleanupCall();
}

async function beginWebRTC(asCaller){
    callState = "active";

    try{
        localStream = await navigator.mediaDevices.getUserMedia({
            audio: true,
            video: currentKind === "video"
        });
    } catch(e){
        alert("Не удалось получить доступ к камере/микрофону");
        hangUp();
        return;
    }

    if(currentKind === "video"){
        const lv = document.getElementById("localVideo");
        lv.srcObject = localStream;
        lv.style.display = "block";
    }

    pc = new RTCPeerConnection({ iceServers: [{ urls: "stun:stun.l.google.com:19302" }] });

    localStream.getTracks().forEach(track => pc.addTrack(track, localStream));

    pc.ontrack = (event) => {
        if(currentKind === "video"){
            const rv = document.getElementById("remoteVideo");
            rv.srcObject = event.streams[0];
            rv.style.display = "block";
            rv.play().catch(() => {});
        } else {
            const ra = document.getElementById("remoteAudio");
            ra.srcObject = event.streams[0];
            ra.play().catch(() => {});
        }
    };

    pc.onicecandidate = (event) => {
        if(event.candidate){
            sendSignal({ type: "candidate", payload: JSON.stringify(event.candidate) });
        }
    };

    signalsPollTimer = setInterval(pollSignals, 900);
    statusPollTimer = setInterval(pollActiveStatus, 2000);

    if(asCaller){
        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);
        sendSignal({ type: "offer", payload: JSON.stringify(offer) });
    }
}

async function sendSignal(msg){
    if(!currentRoom) return;
    try{
        await fetch("/signals/" + currentRoom, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ data: JSON.stringify(msg) })
        });
    } catch(e){}
}

async function pollSignals(){
    if(!currentRoom || !pc) return;
    try{
        const res = await fetch("/signals/" + currentRoom + "?after=" + lastSignalId);
        const items = await res.json();

        for(const item of items){
            lastSignalId = Math.max(lastSignalId, item.id);
            const msg = JSON.parse(item.data);
            const inner = JSON.parse(msg.payload);

            if(msg.type === "offer"){
                await pc.setRemoteDescription(new RTCSessionDescription(inner));
                const answer = await pc.createAnswer();
                await pc.setLocalDescription(answer);
                sendSignal({ type: "answer", payload: JSON.stringify(answer) });
            } else if(msg.type === "answer"){
                await pc.setRemoteDescription(new RTCSessionDescription(inner));
            } else if(msg.type === "candidate"){
                try{ await pc.addIceCandidate(new RTCIceCandidate(inner)); } catch(e){}
            }
        }
    } catch(e){}
}

async function pollActiveStatus(){
    if(!currentRoom || callState !== "active") return;
    try{
        const res = await fetch("/call/status/" + currentRoom);
        const data = await res.json();
        if(!data || data.status === "ended"){
            cleanupCall();
        }
    } catch(e){}
}

/* ---------- GROUP CALLS (multi-party mesh) ---------- */
let groupCallRoom = null;
let groupCallGroupId = null;
let groupPeers = {};
let groupPollTimer = null;
let groupRosterTimer = null;
let groupLastSignalId = 0;

function retryMediaPlayback(){
    const rv = document.getElementById("remoteVideo");
    const ra = document.getElementById("remoteAudio");
    if(rv && rv.srcObject) rv.play().catch(() => {});
    if(ra && ra.srcObject) ra.play().catch(() => {});
    document.querySelectorAll("#groupCallTiles video, #groupCallTiles audio").forEach(el => {
        if(el.srcObject) el.play().catch(() => {});
    });
}

async function startGroupCall(groupId, kind, photo){
    if(callState !== "idle"){ alert("У тебя уже есть активный звонок"); return; }

    const res = await fetch("/group-call/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ group_id: groupId, kind })
    });
    const data = await res.json();
    if(!data.ok){ alert("Не получилось начать звонок"); return; }

    await joinGroupCall(groupId, data.room, kind, "Группа", photo);
}

async function joinGroupCall(groupId, room, kind, label, photo){
    if(callState !== "idle"){ alert("У тебя уже есть активный звонок"); return; }

    stopIncomingPoll();

    groupCallGroupId = groupId;
    groupCallRoom = room;
    currentRoom = room;
    currentKind = kind;
    callState = "active";
    groupLastSignalId = 0;
    groupPeers = {};

    setCallHeader(label, kind === "video" ? "Групповой видеозвонок" : "Групповой звонок", photo);
    document.getElementById("callActionsIncoming").style.display = "none";
    document.getElementById("callActionsActive").style.display = "flex";
    document.getElementById("groupCallTiles").classList.add("show");
    showCallOverlay();

    try{
        localStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: kind === "video" });
    } catch(e){
        alert("Не удалось получить доступ к камере/микрофону");
        cleanupGroupCall();
        return;
    }

    if(kind === "video"){
        const lv = document.getElementById("localVideo");
        lv.srcObject = localStream;
        lv.style.display = "block";
    }

    const res = await fetch("/group-call/join", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ room })
    });
    const data = await res.json();
    if(!data.ok){
        alert("Не получилось присоединиться к звонку");
        cleanupGroupCall();
        return;
    }

    for(const peerName of data.participants){
        await connectToGroupPeer(peerName, true);
    }

    groupPollTimer = setInterval(pollGroupSignals, 900);
    groupRosterTimer = setInterval(pollGroupRoster, 4000);
}

function createGroupPeerConnection(peerName){
    const pc2 = new RTCPeerConnection({ iceServers: [{ urls: "stun:stun.l.google.com:19302" }] });

    localStream.getTracks().forEach(track => pc2.addTrack(track, localStream));

    pc2.onicecandidate = (event) => {
        if(event.candidate){
            sendGroupSignal(peerName, { type: "candidate", payload: JSON.stringify(event.candidate) });
        }
    };

    pc2.ontrack = (event) => {
        addOrUpdateGroupTile(peerName, event.streams[0]);
    };

    groupPeers[peerName] = pc2;
    return pc2;
}

async function connectToGroupPeer(peerName, asOfferer){
    if(groupPeers[peerName]) return;
    const pc2 = createGroupPeerConnection(peerName);

    if(asOfferer){
        const offer = await pc2.createOffer();
        await pc2.setLocalDescription(offer);
        sendGroupSignal(peerName, { type: "offer", payload: JSON.stringify(offer) });
    }
}

function sendGroupSignal(target, msg){
    fetch("/signals/" + groupCallRoom, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ data: JSON.stringify(msg), target })
    }).catch(() => {});
}

async function pollGroupSignals(){
    if(!groupCallRoom) return;
    try{
        const res = await fetch("/signals/" + groupCallRoom + "?after=" + groupLastSignalId);
        const items = await res.json();

        for(const item of items){
            groupLastSignalId = Math.max(groupLastSignalId, item.id);
            const from = item.from;
            const msg = JSON.parse(item.data);
            const inner = JSON.parse(msg.payload);

            if(msg.type === "offer"){
                let pc2 = groupPeers[from];
                if(!pc2) pc2 = createGroupPeerConnection(from);
                await pc2.setRemoteDescription(new RTCSessionDescription(inner));
                const answer = await pc2.createAnswer();
                await pc2.setLocalDescription(answer);
                sendGroupSignal(from, { type: "answer", payload: JSON.stringify(answer) });
            } else if(msg.type === "answer"){
                const pc2 = groupPeers[from];
                if(pc2) await pc2.setRemoteDescription(new RTCSessionDescription(inner));
            } else if(msg.type === "candidate"){
                const pc2 = groupPeers[from];
                if(pc2){ try{ await pc2.addIceCandidate(new RTCIceCandidate(inner)); } catch(e){} }
            }
        }
    } catch(e){}
}

async function pollGroupRoster(){
    if(!groupCallRoom) return;
    try{
        const res = await fetch("/group-call/participants/" + groupCallRoom);
        const current = await res.json();

        for(const name in groupPeers){
            if(!current.includes(name)){
                groupPeers[name].close();
                delete groupPeers[name];
                removeGroupTile(name);
            }
        }
    } catch(e){}
}

function addOrUpdateGroupTile(peerName, stream){
    let tile = document.getElementById("tile-" + peerName);
    if(!tile){
        tile = document.createElement("div");
        tile.className = "tile";
        tile.id = "tile-" + peerName;

        const media = document.createElement(currentKind === "video" ? "video" : "audio");
        media.autoplay = true;
        media.playsInline = true;
        media.id = "media-" + peerName;
        tile.appendChild(media);

        const label = document.createElement("div");
        label.className = "tile-label";
        label.textContent = peerName;
        tile.appendChild(label);

        document.getElementById("groupCallTiles").appendChild(tile);
    }
    const media = document.getElementById("media-" + peerName);
    media.srcObject = stream;
    media.play().catch(() => {});
}

function removeGroupTile(peerName){
    const tile = document.getElementById("tile-" + peerName);
    if(tile) tile.remove();
}

async function leaveGroupCallCleanup(){
    if(groupCallRoom){
        try{
            await fetch("/group-call/leave", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ room: groupCallRoom })
            });
        } catch(e){}
    }
    cleanupGroupCall();
}

function cleanupGroupCall(){
    if(groupPollTimer) clearInterval(groupPollTimer);
    if(groupRosterTimer) clearInterval(groupRosterTimer);
    groupPollTimer = null;
    groupRosterTimer = null;

    for(const name in groupPeers){
        groupPeers[name].close();
    }
    groupPeers = {};
    document.getElementById("groupCallTiles").innerHTML = "";
    document.getElementById("groupCallTiles").classList.remove("show");

    if(localStream){ localStream.getTracks().forEach(t => t.stop()); localStream = null; }
    document.getElementById("localVideo").style.display = "none";
    document.getElementById("localVideo").srcObject = null;

    hideCallOverlay();

    groupCallRoom = null;
    groupCallGroupId = null;
    currentRoom = null;
    currentKind = null;
    callState = "idle";

    startIncomingPoll();
}

async function hangUp(){
    if(groupCallRoom){
        await leaveGroupCallCleanup();
        return;
    }

    if(currentRoom){
        try{
            await fetch("/call/end", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ room: currentRoom })
            });
        } catch(e){}
    }
    cleanupCall();
}

function cleanupCall(){
    stopRinging();
    if(statusPollTimer) clearInterval(statusPollTimer);
    if(signalsPollTimer) clearInterval(signalsPollTimer);
    statusPollTimer = null;
    signalsPollTimer = null;

    if(pc){ pc.close(); pc = null; }
    if(localStream){ localStream.getTracks().forEach(t => t.stop()); localStream = null; }

    document.getElementById("localVideo").style.display = "none";
    document.getElementById("localVideo").srcObject = null;
    document.getElementById("remoteVideo").style.display = "none";
    document.getElementById("remoteVideo").srcObject = null;
    document.getElementById("remoteAudio").srcObject = null;

    hideCallOverlay();

    currentRoom = null;
    currentPeerName = null;
    currentKind = null;
    callState = "idle";

    startIncomingPoll();
}

function stopIncomingPoll(){
    if(incomingPollTimer){ clearInterval(incomingPollTimer); incomingPollTimer = null; }
}
function startIncomingPoll(){
    if(!incomingPollTimer && MY_USERNAME){
        incomingPollTimer = setInterval(pollIncomingCall, 2500);
    }
}

if(MY_USERNAME){
    requestNotifyPermission();
    pollUnread();
    setInterval(pollUnread, 3000);
    pollPresence();
    setInterval(pollPresence, 5000);
    startIncomingPoll();
}
</script>

</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
