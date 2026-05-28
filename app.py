from flask import Flask, request, render_template_string, session, redirect, jsonify
import sqlite3
import random
import os
app = Flask(__name__)
app.secret_key = "chat_secret"

DB = "chat.db"
print("DB EXISTS:", os.path.exists("chat.db"))
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
# ---------------- settings ----------------
@app.route("/settings")
def settings():
    if not session.get("email"):
        return redirect("/")

    return """
    <h2>⚙️ Настройки</h2>

    <p>Аккаунт: {}</p>

    <a href="/change-username">✏️ Изменить username</a><br><br>

    <a href="/logout">🚪 Выйти</a>
    """.format(session.get("email"))
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

    # всегда проверяем пользователя
    c.execute("SELECT user_id FROM users WHERE email=?", (email,))
    row = c.fetchone()

    # если нет — создаём
    if row is None:
        while True:
            new_id = str(random.randint(100000, 999999))

            c.execute("SELECT 1 FROM users WHERE user_id=?", (new_id,))
            if not c.fetchone():
                break

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

    res = c.fetchall()
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


# ---------------- HOME HTML ----------------
HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Chat</title>
<style>
body { font-family: Arial; display:flex; margin:0; }
.left { width:250px; background:#eee; padding:10px; height:100vh; overflow:auto; }
.chat { flex:1; padding:20px; }
.msg { margin:5px 0; padding:8px; background:#f5f5f5; border-radius:10px; }
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
                    ${u[0]} (#${u[1]})
                    <button onclick="addFriend('${u[0]}')">+</button>
                </div>
            `;
        });

        document.getElementById("results").innerHTML = html;
    });
}

function addFriend(u){
    fetch("/add-friend", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({username:u})
    }).then(() => location.reload());
}
</script>

<div class="left">

{% if session.get("username") %}

<h3>
👤 {{session.get("username")}}
</h3>

<a href="/settings">
⚙️ Настройки
</a>

<h3>👤 {{session.get("username")}}</h3>

<p>ID: {{my_id}}</p>

<hr>

<h3>🔍 Поиск</h3>
<input id="q" placeholder="username">
<button onclick="searchUser()">Найти</button>

<div id="results"></div>

<hr>

<h3>💬 Друзья</h3>

{% for f in friends %}
<p><a href="/chat/{{f}}">{{f}}</a></p>
{% endfor %}

{% endif %}

</div>

<div class="chat">

{% if not session.get("email") %}

<h2>Вход</h2>
<button onclick="loginGoogle()">Google Login</button>

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
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          name:user.displayName,
          email:user.email
        })
      });
  })
  .then(() => window.location.href="/set-username");
}
</script>

{% elif not session.get("username") %}

<h2>Создай username</h2>
<a href="/set-username">Создать</a>

{% elif not peer %}

<h2>Привет {{session.get("username")}}</h2>
<p>Выбери чат</p>

{% else %}

<h2>Чат с {{peer}}</h2>

{% for m in messages %}
<div class="msg"><b>{{m[0]}}</b>: {{m[1]}}</div>
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
    email = session.get("email")

    if not email:
        return render_template_string(HTML, friends=[], peer=None, my_id="LOGIN")

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("SELECT username, user_id FROM users WHERE email=?", (email,))
    row = c.fetchone()

    if not row:
        conn.close()
        return render_template_string(HTML, friends=[], peer=None, my_id="NO USER")

    username, my_id = row

    # 🔥 ВАЖНО: сохраняем в session
    session["username"] = username if username else None

    c.execute("SELECT friend FROM friends WHERE user=?", (username,))
    friends = [r[0] for r in c.fetchall()]

    conn.close()

    return render_template_string(
        HTML,
        friends=friends,
        peer=None,
        my_id=my_id
    )


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
# ---------------- LOGOUT ----------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
