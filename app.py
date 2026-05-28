from flask import Flask, request, render_template_string, session, redirect, jsonify
import sqlite3
import random
import os

app = Flask(__name__)
app.secret_key = "chat_secret"

DB = "chat.db"


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

    conn.commit()
    conn.close()

init_db()


# ---------------- GOOGLE LOGIN ----------------
@app.route("/google-login", methods=["POST"])
def google_login():
    data = request.get_json()
    email = data["email"]
    name = data["name"]

    session["email"] = email
    session["name"] = name

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("SELECT user_id, username FROM users WHERE email=?", (email,))
    row = c.fetchone()

    if row is None:
        # создаём нового пользователя
        new_id = str(random.randint(100000, 999999))

        c.execute("""
        INSERT INTO users (email, name, username, user_id)
        VALUES (?, ?, ?, ?)
        """, (email, name, None, new_id))

        conn.commit()

    conn.close()
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

        # проверка занятости
        c.execute("SELECT 1 FROM users WHERE username=?", (username,))
        if c.fetchone():
            return "❌ Username уже занят"

        c.execute("""
        UPDATE users
        SET username=?
        WHERE email=?
        """, (username, email))

        conn.commit()
        conn.close()

        session["username"] = username
        return redirect("/")

    return """
    <form method="POST">
        <input name="username" placeholder="username">
        <button>Save</button>
    </form>
    """


# ---------------- CHANGE USERNAME ----------------
@app.route("/change-username", methods=["GET", "POST"])
def change_username():
    if not session.get("email"):
        return redirect("/")

    if request.method == "POST":
        new_username = request.form["username"]
        email = session["email"]

        conn = sqlite3.connect(DB)
        c = conn.cursor()

        c.execute("SELECT 1 FROM users WHERE username=?", (new_username,))
        if c.fetchone():
            return "❌ Username уже занят"

        c.execute("""
        UPDATE users
        SET username=?
        WHERE email=?
        """, (new_username, email))

        conn.commit()
        conn.close()

        session["username"] = new_username
        return redirect("/")

    return """
    <form method="POST">
        <input name="username" placeholder="новый username">
        <button>Сохранить</button>
    </form>
    """


# ---------------- LOGOUT ----------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


# ---------------- SEARCH ----------------
@app.route("/search")
def search():
    q = request.args.get("q", "")

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("""
    SELECT username, user_id FROM users
    WHERE username LIKE ?
    OR user_id LIKE ?
    """, (f"%{q}%", f"%{q}%"))

    return jsonify({"results": c.fetchall()})


# ---------------- ADD FRIEND ----------------
@app.route("/add-friend", methods=["POST"])
def add_friend():
    data = request.get_json()

    me = session.get("username")
    other = data["username"]

    if not me:
        return jsonify({"error": "not logged"}), 400

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("INSERT INTO friends (user, friend) VALUES (?, ?)", (me, other))

    conn.commit()
    conn.close()

    return jsonify({"ok": True})


# ---------------- HOME ----------------
@app.route("/")
def home():
    email = session.get("email")

    if not email:
        return "LOGIN FIRST"

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("SELECT username, user_id FROM users WHERE email=?", (email,))
    row = c.fetchone()

    if not row:
        return "NO USER (refresh after login)"

    username, user_id = row

    if username:
        session["username"] = username

    c.execute("SELECT friend FROM friends WHERE user=?", (username,))
    friends = [r[0] for r in c.fetchall()]

    return f"""
    <h2>👤 {username or "NO USERNAME"}</h2>
    <p>ID: {user_id}</p>

    <a href="/set-username">Set username</a><br>
    <a href="/change-username">Change username</a><br>
    <a href="/logout">Logout</a>

    <hr>

    <h3>Friends:</h3>
    {friends}
    """


# ---------------- CHAT ----------------
@app.route("/chat/<user>")
def chat(user):
    me = session.get("username")
    if not me:
        return redirect("/")

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("""
    SELECT sender, message FROM messages
    WHERE (sender=? AND receiver=?)
    OR (sender=? AND receiver=?)
    """, (me, user, user, me))

    msgs = c.fetchall()

    return f"""
    <h2>Chat with {user}</h2>
    <pre>{msgs}</pre>

    <form method="POST" action="/send/{user}">
        <input name="msg">
        <button>Send</button>
    </form>
    """


# ---------------- SEND ----------------
@app.route("/send/<user>", methods=["POST"])
def send(user):
    me = session.get("username")
    msg = request.form["msg"]

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("""
    INSERT INTO messages (sender, receiver, message)
    VALUES (?, ?, ?)
    """, (me, user, msg))

    conn.commit()
    conn.close()

    return redirect("/chat/" + user)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
