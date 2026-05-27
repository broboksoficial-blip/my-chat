from flask import Flask, request, render_template_string, session, redirect, jsonify
import sqlite3

app = Flask(__name__)
app.secret_key = "chat_secret"

DB = "chat.db"

# --- DB ---
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

    conn.commit()
    conn.close()

init_db()

# --- GOOGLE LOGIN ---
@app.route("/google-login", methods=["POST"])
def google_login():
    data = request.get_json()

    name = data["name"]
    email = data["email"]

    session["name"] = name
    session["email"] = email
    session.permanent = True

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("""
    INSERT OR IGNORE INTO users (email, name)
    VALUES (?, ?)
    """, (email, name))

    conn.commit()
    conn.close()

    return jsonify({"ok": True})


# --- SET USERNAME ---
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
        <h3>Придумай имя</h3>
        <input name="username" placeholder="например: kirill123">
        <button>Сохранить</button>
    </form>
    """


# --- HOME ---
HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Chat</title>
<style>
body { font-family: Arial; display:flex; }
.left { width:200px; background:#eee; padding:10px; }
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
  appId: "1:407628010061:web:72f3cb30760c52101cc204"
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
</script>

<div class="left">
<h3>Контакты</h3>
{% for u in users %}
  <p><a href="/chat/{{u}}">{{u}}</a></p>
{% endfor %}
</div>

<div class="chat">

{% if not session.get("email") %}
  <button onclick="loginGoogle()">Войти через Google</button>

{% elif not session.get("username") %}
  <h3>Задай имя</h3>
  <a href="/set-username">Перейти</a>

{% elif not peer %}
  <h3>Привет, {{session.get("username")}}</h3>
  <p>Выбери пользователя</p>

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


# --- HOME ---
@app.route("/")
def home():
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("SELECT email FROM users")
    users = [u[0] for u in c.fetchall()]

    conn.close()

    return render_template_string(HTML, users=users, peer=None)


# --- CHAT ---
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

    return render_template_string(HTML,
        users=[],
        peer=user,
        messages=messages
    )


# --- SEND ---
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
