from flask import Flask, request, render_template_string, session, redirect, jsonify
import sqlite3

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
        username TEXT UNIQUE
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

    session["email"] = data["email"]
    session["name"] = data["name"]

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("""
    INSERT OR IGNORE INTO users (email, name)
    VALUES (?, ?)
    """, (data["email"], data["name"]))

    conn.commit()
    conn.close()

    return jsonify({"ok": True})


# ---------------- SET USERNAME ----------------
@app.route("/set-username", methods=["GET", "POST"])
def set_username():
    if request.method == "POST":
        username = request.form["username"]
        email = session.get("email")

        conn = sqlite3.connect(DB)
        c = conn.cursor()

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
        email = session.get("email")

        conn = sqlite3.connect(DB)
        c = conn.cursor()

        c.execute("""
        SELECT username FROM users
        WHERE username=?
        """, (new_username,))

        if c.fetchone():
            conn.close()
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
        <h3>Изменить username</h3>
        <input name="username" placeholder="новый username">
        <button>Сохранить</button>
    </form>
    """

# ---------------- SEARCH ----------------
@app.route("/search")
def search():
    q = request.args.get("q", "")

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("""
    SELECT username FROM users
    WHERE username LIKE ?
    """, (f"%{q}%",))

    res = [r[0] for r in c.fetchall() if r[0]]
    conn.close()

    return jsonify({"results": res})


# ---------------- ADD FRIEND ----------------
@app.route("/add-friend", methods=["POST"])
def add_friend():
    data = request.get_json()

    me = session.get("username")
    other = data["username"]

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("""
    INSERT INTO friends (user, friend)
    VALUES (?, ?)
    """, (me, other))

    conn.commit()
    conn.close()

    return jsonify({"ok": True})


# ---------------- HOME ----------------
HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Chat</title>
<style>
body { font-family: Arial; display:flex; }
.left { width:250px; background:#eee; padding:10px; }
.chat { flex:1; padding:10px; }
.msg { margin:5px 0; }
</style>
</head>
<body>

<script>
function searchUser(){
    let q = document.getElementById("q").value;

    fetch("/search?q=" + q)
    .then(r => r.json())
    .then(d => {
        let html = "";

        d.results.forEach(u => {
            html += `
                <div>
                    ${u}
                    <button onclick="addFriend('${u}')">+</button>
                </div>
            `;
        });

        document.getElementById("results").innerHTML = html;
    });
}

function addFriend(u){
    fetch("/add-friend", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({username: u})
    }).then(() => alert("Добавлено в друзья"));
}
</script>

<div class="left">

<h3>Поиск</h3>
<input id="q" placeholder="username">
<button onclick="searchUser()">Найти</button>
<div id="results"></div>

<h3>Друзья</h3>
{% for f in friends %}
  <p><a href="/chat/{{f}}">{{f}}</a></p>
{% endfor %}

</div>

<div class="chat">

{% if not session.get("email") %}
  <a href="#" onclick="loginGoogle()">Войти через Google</a>

{% elif not session.get("username") %}
  <a href="/set-username">Создать username</a>

{% elif not peer %}
  <h3>Привет {{session.get("username")}}</h3>

{% else %}
  <h3>Чат с {{peer}}</h3>

  {% for m in messages %}
    <div class="msg"><b>{{m[0]}}:</b> {{m[1]}}</div>
  {% endfor %}

  <form method="POST" action="/send/{{peer}}">
    <input name="msg">
    <button>Отправить</button>
  </form>

{% endif %}

</div>

</body>
</html>
"""


# ---------------- HOME ----------------
@app.route("/")
def home():
    me = session.get("username")

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("SELECT friend FROM friends WHERE user=?", (me,))
    friends = [r[0] for r in c.fetchall()]

    conn.close()

    return render_template_string(HTML, friends=friends, peer=None)


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
    ORDER BY id
    """, (me, user, user, me))

    messages = c.fetchall()
    conn.close()

    return render_template_string(HTML, friends=[], peer=user, messages=messages)


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
