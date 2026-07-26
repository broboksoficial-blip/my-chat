from flask import Flask, request, render_template_string, session, redirect, jsonify
import sqlite3
import random
import time

app = Flask(__name__)
app.secret_key = "chat_secret"

DB = "chat.db"
ONLINE_THRESHOLD_SECONDS = 15

# ---------------- DB ----------------
def init_db():
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

    # migration: add photo column to users if it doesn't exist yet
    c.execute("PRAGMA table_info(users)")
    existing_cols = [r[1] for r in c.fetchall()]
    if "photo" not in existing_cols:
        c.execute("ALTER TABLE users ADD COLUMN photo TEXT")

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
    SELECT sender, message FROM messages
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
        peer=user,
        peer_photo=get_profile_photo(user),
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
        SELECT sender, message FROM messages
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
        return render_template_string(HTML, friends=[], peer=None, my_id="LOGIN FIRST", me=None, photo=None)

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("SELECT username, user_id FROM users WHERE email=?", (email,))
    row = c.fetchone()

    conn.close()

    if not row:
        return render_template_string(HTML, friends=[], peer=None, my_id="NO USER", me=None, photo=None)

    username, user_id = row

    session["username"] = username or None
    session["user_id"] = user_id

    chats = with_photos(get_chat_partners(username)) if username else []

    return render_template_string(
        HTML,
        friends=chats,
        peer=None,
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

<div id="menu">
    <div class="wordmark"><span class="dot"></span>Relay</div>

    {% if session.get("username") %}
    <div class="menu-profile">
        {% if photo %}
            <img src="{{photo}}">
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

{% elif not peer %}

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

</div>

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
    <button class="icon-btn" onclick="toggleMenu()">☰</button>
</div>

<div class="chat-box" id="chatBox">
    {% for m in messages %}
        <div class="msg-row {{ 'me' if m[0] == me else 'them' }}">
            <div class="msg">
                {% if m[0] != me %}<span class="sender">{{m[0]}}</span>{% endif %}{{m[1]}}
            </div>
        </div>
    {% endfor %}
</div>

<form class="input-bar" id="sendForm" method="POST" action="/send/{{peer}}">
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
        row.innerHTML = `
            <div class="msg">
                ${mine ? '' : `<span class="sender">${escapeHtml(m[0])}</span>`}${escapeHtml(m[1])}
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

async function addFriend(username){
    await fetch("/add-friend", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username })
    });
    location.reload();
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

if(MY_USERNAME){
    requestNotifyPermission();
    pollUnread();
    setInterval(pollUnread, 3000);
    pollPresence();
    setInterval(pollPresence, 5000);
}
</script>

</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
