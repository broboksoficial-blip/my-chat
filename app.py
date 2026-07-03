from flask import Flask, request, render_template_string, session, redirect, jsonify
import sqlite3
import random

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

        user_id, username = new_id, None
        conn.commit()
    else:
        user_id, username = user

    conn.close()

    session["email"] = email
    session["name"] = name
    session["photo"] = photo
    session["user_id"] = user_id
    session["username"] = username

    return jsonify({"ok": True})


# ---------------- THEME ----------------
@app.route("/set-theme/<mode>")
def set_theme(mode):
    if mode in ["dark", "light"]:
        session["theme"] = mode
    return redirect("/")


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
        <input name="username">
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

    c.execute("""
    SELECT sender, message FROM messages
    WHERE (sender=? AND receiver=?)
    OR (sender=? AND receiver=?)
    ORDER BY id
    """, (me, user, user, me))

    messages = c.fetchall()

    c.execute("SELECT friend FROM friends WHERE user=?", (me,))
    friends = [r[0] for r in c.fetchall()]

    conn.close()

    return render_template_string(HTML,
        friends=friends,
        peer=user,
        messages=messages,
        my_id=session.get("user_id")
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
        return render_template_string(HTML, friends=[], peer=None, my_id="LOGIN FIRST")

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("SELECT username, user_id FROM users WHERE email=?", (email,))
    row = c.fetchone()

    conn.close()

    if not row:
        return render_template_string(HTML, friends=[], peer=None, my_id="NO USER")

    username, user_id = row

    session["username"] = username or None
    session["user_id"] = user_id
    session.setdefault("theme", "light")

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


# ---------------- SETTINGS ----------------
@app.route("/settings")
def settings():
    if not session.get("email"):
        return redirect("/")

    return render_template_string("""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">

<style>
:root{
    --bg:white;
    --text:black;
    --card:#f5f5f5;
}

body.dark{
    --bg:#0f0f0f;
    --text:white;
    --card:#1f1f1f;
}

body{
    margin:0;
    font-family:Arial;
    background:var(--bg);
    color:var(--text);
    transition:.3s;
}

.box{
    max-width:500px;
    margin:40px auto;
    background:var(--card);
    padding:20px;
    border-radius:15px;
}

a{
    color:#4a76a8;
    text-decoration:none;
    display:block;
    margin:12px 0;
}

hr{
    border:none;
    border-top:1px solid #555;
}
</style>

</head>

<body>

<div class="box">

<h2>⚙️ Настройки</h2>

<p>📧 Email: {{email}}</p>
<p>👤 Username: {{username}}</p>
<p>🆔 ID: {{user_id}}</p>

<hr>

<a href="/set-username">✏️ Изменить username</a>

<a href="/logout">🚪 Выйти</a>

<a href="/">⬅️ Назад</a>

</div>

<script>
if(localStorage.getItem("theme") === "dark"){
    document.body.classList.add("dark");
}
</script>

</body>
</html>
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

body {
    margin: 0;
    font-family: Arial;
    display: flex;
    height: 100vh;
    background: var(--bg);
    color: var(--text);
}

/* HEADER */
.chat-header {
    padding: 15px;
    background: var(--header);
    color: white;
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-weight: bold;
}

/* CHAT */
.chat {
    flex: 1;
    display: flex;
    flex-direction: column;
    height: 100vh;
}

.chat-box {
    flex: 1;
    overflow-y: auto;
    padding: 20px;
}

.msg {
    margin: 5px 0;
    padding: 8px;
    background: var(--msg);
    border-radius: 10px;
}

/* INPUT */
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

/* MENU */
#menu {
    position: fixed;
    left: 0;
    top: 0;
    width: 220px;
    height: 100%;
    background: var(--left);
    padding: 15px;
    z-index: 99999;

    transform: translateX(-100%);
    transition: 0.3s;
}

#menu.open {
    transform: translateX(0);
}

#menu button{
    width:100%;
    padding:10px;
    margin-bottom:10px;
    border:none;
    border-radius:8px;
    background:var(--msg);
    color:var(--text);
    cursor:pointer;
    font-size:16px;
    text-align:left;
}

#menu button:hover{
    opacity:0.9;
}

/* OVERLAY */
#overlay {
    display: none;
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0,0,0,0.3);
    z-index: 99998;
}
</style>
</head>

<body>

<!-- OVERLAY -->
<div id="overlay" onclick="toggleMenu()"></div>

<!-- MENU -->
<div id="menu">

{% if session.get("username") %}

<div style="text-align:center;">
    <img src="{{session.get('photo')}}" width="70" style="border-radius:50%;">
    <h3>{{session.get("username")}}</h3>
    <p>ID: {{my_id}}</p>
</div>

<hr>

<button onclick="toggleTheme()">🌙/☀️ Theme</button>
<br><br>

<button onclick="window.location.href='/settings'">
    ⚙️ Настройки
</button>
<br><br>

{% endif %}

</div>

<div class="chat">

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
  .then(() => location.reload())
  .catch(err => alert("Ошибка входа"));
}
</script>

{% elif not session.get("username") %}

<h2>Создай username</h2>
<a href="/set-username">Создать</a>

{% elif not peer %}

<div class="chat-header">
    <button onclick="toggleMenu()" style="font-size:22px;background:none;border:none;color:white;">
        ☰
    </button>
</div>

<h2>Welcome {{session.get("username")}}</h2>
<p>Выбери чат</p>

<!-- Поиск -->
<div style="padding:20px; max-width:500px;">

    <input
        id="search"
        type="text"
        placeholder="🔍 Найти пользователя"
        style="
            width:100%;
            padding:12px;
            border-radius:10px;
            border:1px solid #666;
            font-size:16px;
        "
        oninput="searchUser()"
    >

    <div id="results" style="margin-top:15px;"></div>

</div>

<!-- Друзья -->
<h3 style="padding-left:20px;">👥 Друзья</h3>

<div style="padding:0 20px;">

{% for f in friends %}
    <div style="margin:10px 0;">
        <a href="/chat/{{f}}">
            {{f}}
        </a>
    </div>
{% endfor %}

</div>

{% else %}

<div class="chat-header">
    <button onclick="toggleMenu()" style="font-size:22px;background:none;border:none;color:white;">
        ☰
    </button>

    <span>💬 {{peer}}</span>
</div>

<div class="chat-box" id="chatBox">
<script>
async function updateChat(){

    let res = await fetch("/messages/{{peer}}");
    let data = await res.json();

    let box = document.querySelector(".chat-box");

    box.innerHTML = "";

    data.forEach(m => {
        box.innerHTML += `
            <div class="msg">
                <b>${m[0]}</b>: ${m[1]}
            </div>
        `;
    });
}

// обновление каждые 2 секунды
setInterval(updateChat, 2000);

// первый запуск
updateChat();
</script>

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

<script>
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
    localStorage.setItem("theme",
        document.body.classList.contains("dark") ? "dark" : "light"
    );
}

async function searchUser(){

    let q = document.getElementById("search").value;

    if(q.length == 0){
        document.getElementById("results").innerHTML="";
        return;
    }

    let r = await fetch("/search?q="+encodeURIComponent(q));
    let data = await r.json();

    let html="";

    data.results.forEach(u=>{

        html += `
        <div style="padding:10px;border-bottom:1px solid #444;">
            <b>${u[0]}</b><br>
            ID: ${u[1]}
            <br><br>

            <button onclick="addFriend('${u[0]}')">
                Добавить
            </button>
        </div>
        `;

    });

    document.getElementById("results").innerHTML = html;
}

async function addFriend(username){

    await fetch("/add-friend",{
        method:"POST",
        headers:{
            "Content-Type":"application/json"
        },
        body:JSON.stringify({
            username:username
        })
    });

    location.reload();
}

if(localStorage.getItem("theme") === "dark"){
    document.body.classList.add("dark");
}
</script>

</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
