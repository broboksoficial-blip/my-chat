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
        username TEXT
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
    CREATE TABLE IF NOT EXISTS contacts (
        owner TEXT,
        contact TEXT
    )
    """)

    conn.commit()
    conn.close()

init_db()

# ---------------- GOOGLE LOGIN ----------------
@app.route("/google-login", methods=["POST"])
def google_login():
    data = request.get_json()

    name = data["name"]
    email = data["email"]

    session["email"] = email
    session["name"] = name

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("""
    INSERT OR IGNORE INTO users (email, name)
    VALUES (?, ?)
    """, (email, name))

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
        <h3>Введите имя пользователя</h3>
        <input name="username" placeholder="nickname">
        <button>Сохранить</button>
    </form>
    """


# ---------------- SEARCH USERS ----------------
@app.route("/search")
def search():
    q = request.args.get("q", "")

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("""
    SELECT username FROM users
    WHERE username LIKE ?
    """, (f"%{q}%",))

    users = [u[0] for u in c.fetchall()]
    conn.close()

    return {"results": users}


# ---------------- ADD CONTACT ----------------
@app.route("/add-contact", methods=["POST"])
def add_contact():
    data = request.get_json()

    owner = session.get("username")
    contact = data["contact"]

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("""
    INSERT INTO contacts (owner, contact)
    VALUES (?, ?)
    """, (owner, contact))

    conn.commit()
    conn.close()

    return jsonify({"ok": True})


# ---------------- GET CONTACTS ----------------
@app.route("/contacts")
def contacts():
    owner = session.get("username")

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("""
    SELECT contact FROM contacts
    WHERE owner=?
    """, (owner,))

    contacts = [c[0] for c in c.fetchall()]
    conn.close()

    return jsonify({"contacts": contacts})


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

    c.execute("SELECT username FROM users")
    users = [u[0] for u in c.fetchall()]

    conn.close()

    return render_template_string(HTML,
        users=users,
        peer=user,
        messages=messages
    )


# ---------------- SEND MESSAGE ----------------
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


# ---------------- HOME HTML ----------------
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

<script src="https://www.gstatic.com/firebasejs/10.7.1/firebase-app-compat.js"></script>
<script src="https://www.gstatic.com/firebasejs/10.7.1/firebase-auth-compat.js"></script>

<script>
const firebaseConfig = {
  apiKey: "AIzaSyByRxM7bQhYSK5XCuaZMRo0s42DGeaav6Y",
  authDomain: "my-chat2-ae3ca.firebaseapp.com",
  projectId: "my-chat2-ae3ca",
};

firebase.initializeApp(firebaseConfig);

function loginGoogle() {
  const provider = new firebase.auth.GoogleAuthProvider();

  firebase.auth().signInWithPopup(provider)
    .then((result) => {
      const user = result.user;

      return fetch("/google-login", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          name: user.displayName,
          email: user.email
        })
      });
    })
    .then(() => {
      window.location.href = "/set-username";
    });
}

function searchUser() {
  let q = document.getElementById("q").value;

  fetch("/search?q=" + q)
    .then(r => r.json())
    .then(data => {
      let html = "";

      data.results.forEach(u => {
        html += `
          <div>
            ${u}
            <button onclick="addContact('${u}')">+</button>
          </div>
        `;
      });

      document.getElementById("results").innerHTML = html;
    });
}

function addContact(u) {
  fetch("/add-contact", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({contact: u})
  }).then(() => alert("Добавлено"));
}
</script>

<div class="left">

<h3>Поиск</h3>
<input id="q" placeholder="username">
<button onclick="searchUser()">Найти</button>
<div id="results"></div>

<h3>Контакты</h3>
{% for u in users %}
  <p><a href="/chat/{{u}}">{{u}}</a></p>
{% endfor %}

</div>

<div class="chat">

{% if not session.get("username") %}
  <button onclick="loginGoogle()">Войти через Google</button>

{% elif not peer %}
  <h3>Привет {{session.get("username")}}</h3>
  <p>Выбери контакт</p>

{% else %}
  <h3>Чат с {{peer}}</h3>

  {% for m in messages %}
    <div class="msg"><b>{{m[0]}}:</b> {{m[1]}}</div>
  {% endfor %}

  <form method="POST" action="/send/{{peer}}">
    <input name="msg" placeholder="Сообщение">
    <button>Отправить</button>
  </form>

{% endif %}

</div>

</body>
</html>
"""


# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
