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
        INSERT INTO users (email, name, username, user_id)
        VALUES (?, ?, ?, ?)
        """, (email, name, None, new_id))

        user_id = new_id
        username = None
        conn.commit()
    else:
        user_id, username = user

    conn.close()

    session["email"] = email
    session["name"] = name
    session["photo"] = photo
    session["user_id"] = user_id
    session["username"] = username  # 👈 ВСЕГДА сохраняем (даже None)

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

    return """
    <h2>Создать username</h2>
    <form method="POST">
        <input name="username" placeholder="username">
        <button>OK</button>
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

    if not me:
        return jsonify({"ok": False})

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("INSERT INTO friends (user, friend) VALUES (?, ?)", (me, other))

    conn.commit()
    conn.close()

    return jsonify({"ok": True})


# ---------------- CHAT ----------------
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

    # сообщения
    c.execute("""
    SELECT sender, message FROM messages
    WHERE (sender=? AND receiver=?)
    OR (sender=? AND receiver=?)
    ORDER BY id
    """, (me, user, user, me))

    messages = c.fetchall()

    # 🔥 ДОБАВЛЯЕМ ДРУЗЕЙ
    c.execute("SELECT friend FROM friends WHERE user=?", (me,))
    friends = [r[0] for r in c.fetchall()]

    conn.close()

    return render_template_string(
        HTML,
        friends=friends,
        peer=user,
        messages=messages,
        my_id=session.get("user_id")
    )


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

    return redirect("/chat/" + user)


# ---------------- LOGOUT ----------------
@app.route("/logout")
def logout():
    session.clear()

    return """
    <script>
    firebase.auth().signOut().then(() => {
        window.location.href = "/";
    });
    </script>

    <script src="https://www.gstatic.com/firebasejs/10.7.1/firebase-app-compat.js"></script>
    <script src="https://www.gstatic.com/firebasejs/10.7.1/firebase-auth-compat.js"></script>

    <script>
    const firebaseConfig = {
      apiKey: "AIzaSyByRxM7bQhYSK5XCuaZMRo0s42DGeaav6Y",
      authDomain: "my-chat2-ae3ca.firebaseapp.com",
      projectId: "my-chat2-ae3ca",
    };

    firebase.initializeApp(firebaseConfig);
    </script>
    """


# ---------------- HOME ----------------
@app.route("/")
def home():
    email = session.get("email")

    if not email:
        return render_template_string(HTML, friends=[], peer=None, my_id="LOGIN FIRST")

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("SELECT username, user_id FROM users WHERE email=?", (email,))
    row = c.fetchone()

    conn.close()

    if not row:
        return render_template_string(HTML, friends=[], peer=None, my_id="NO USER")

    username, user_id = row

    session["user_id"] = user_id
    session["username"] = username

    friends = []

    if username:
        conn = sqlite3.connect(DB)
        c = conn.cursor()
        c.execute("SELECT friend FROM friends WHERE user=?", (username,))
        friends = [r[0] for r in c.fetchall()]
        conn.close()

    return render_template_string(
        HTML,
        friends=friends,
        peer=None,
        my_id=user_id
    )
# ---------------- Settings ----------------
@app.route("/settings")
def settings():
    if not session.get("email"):
        return redirect("/")

    return render_template_string("""
    <h2>⚙️ Настройки</h2>

    <p>📧 Email: {{email}}</p>
    <p>👤 Username: {{username}}</p>
    <p>🆔 ID: {{user_id}}</p>

    <hr>

    <a href="/set-username">✏️ Изменить username</a><br><br>
    <a href="/logout">🚪 Выйти</a><br><br>
    <a href="/">⬅️ Назад</a>
    """,
    email=session.get("email"),
    username=session.get("username"),
    user_id=session.get("user_id")
    )

# ---------------- HTML ----------------
HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Chat</title>

<style>
body {
    font-family: Arial;
    display: flex;
    margin: 0;
    height: 100vh;
    background: var(--bg);
    color: var(--text);
}
.chat-header {
    padding: 15px;
    background: var(--header);
    color: white;
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-weight: bold;
}
:root {
    --bg: white;
    --text: black;
    --left: #eeeeee;
    --msg: #f5f5f5;
    --header: #4a76a8;
}

body.dark {
    --bg: #0f0f0f;
    --text: white;
    --left: #1b1b1b;
    --msg: #2a2a2a;
    --header: #202020;
}
.chat-actions {
    display: flex;
    gap: 10px;
}

.chat-actions .btn {
    color: white;
    text-decoration: none;
    background: rgba(255,255,255,0.2);
    padding: 5px 10px;
    border-radius: 10px;
    font-size: 14px;
}

.chat-actions .btn:hover {
    background: rgba(255,255,255,0.35);
}
/* левая панель */
.left {
    width: 250px;
    background: var(--left);
    padding: 10px;
    overflow: auto;
}

/* чат */
.chat {
    flex: 1;
    display: flex;
    flex-direction: column;
    height: 100vh;
}

/* блок сообщений */
.chat-box {
    flex: 1;
    overflow-y: auto;
    padding: 20px;
}

/* сообщение */
.msg {
    margin: 5px 0;
    padding: 8px;
    background: var(--msg);
    border-radius: 10px;
}

/* нижняя панель */
.input-bar {
    display: flex;
    padding: 10px;
    border-top: 1px solid #ccc;
    background: var(--left);
}

.input-bar input {
    flex: 1;
    padding: 10px;
    border-radius: 20px;
    border: 1px solid #555;
    outline: none;
    background: var(--msg);
    color: var(--text);
}

.input-bar button {
    margin-left: 10px;
    padding: 10px 15px;
    border-radius: 50%;
    border: none;
    background: #4a76a8;
    color: white;
}
</style>
</head>

<body>

<div id="menu" style="
display:none;
position:fixed;
left:0;
top:0;
width:220px;
height:100%;
background:var(--left);
padding:15px;
z-index:99999;
">

{% if session.get("username") %}

<div style="text-align:center;">
    <img src="{{session.get('photo')}}"
         width="70"
         style="border-radius:50%;">

    <h3>{{session.get("username")}}</h3>

    <p>ID: {{my_id}}</p>
</div>

<hr>

<button onclick="toggleTheme()">
🌙/☀️ Theme
</button>

<br><br>

<a href="/settings">⚙️ Настройки</a>

<br><br>

<a href="/logout">🚪 Выйти</a>

{% endif %}

</div>

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

<div class="chat">
<div style="padding:10px; background:#4a76a8; color:white;">
    <button onclick="toggleMenu()" style="font-size:20px;">
        ☰
    </button>
</div>

{% if not session.get("email") %}

<h2>Вход</h2>

<button onclick="loginGoogle()">Войти через Google</button>

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
  .then(() => {
      window.location.href = "/";
  })
  .catch(err => {
      console.error("LOGIN ERROR:", err);
      alert("Ошибка входа — смотри F12 Console");
  });
}
</script>

{% elif not session.get("username") %}

<h2>Create username</h2>
<a href="/set-username">Set username</a>

{% elif not peer %}

<h2>Welcome {{session.get("username")}}</h2>
<p>Select chat</p>

{% else %}

<div class="chat-header">
    <button onclick="toggleMenu()" style="
    font-size:22px;
    background:none;
    border:none;
    color:white;
    cursor:pointer;
    ">
        ☰
    </button>

    <span>💬 {{peer}}</span>
</div>

<div class="chat-box">
    {% for m in messages %}
        <div class="msg">
            <b>{{m[0]}}</b>: {{m[1]}}
        </div>
    {% endfor %}
</div>

<form class="input-bar" method="POST" action="/send/{{peer}}">
    <input name="msg" placeholder="Написать сообщение..." autocomplete="off">
    <button>➤</button>
</form>

{% endif %}

</div>

</body>
<script>
function toggleTheme(){
    document.body.classList.toggle("dark");

    if(document.body.classList.contains("dark")){
        localStorage.setItem("theme", "dark");
    } else {
        localStorage.setItem("theme", "light");
    }
}

if(localStorage.getItem("theme") === "dark"){
    document.body.classList.add("dark");
}
</script>
</html>
<script>
function toggleMenu(){
    let menu = document.getElementById("menu");

    if(menu.style.display === "none" || menu.style.display === ""){
        menu.style.display = "block";
    } else {
        menu.style.display = "none";
    }
}
</script>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
