from flask import Flask, request, render_template_string, session, redirect, url_for
import sqlite3

app = Flask(__name__)
app.secret_key = "chat_secret"

DB = "chat.db"

# --- INIT DB ---
def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        name TEXT UNIQUE
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
  apiKey: "ТВОЙ_API_KEY",
  authDomain: "ТВОЙ_PROJECT.firebaseapp.com",
  projectId: "ТВОЙ_PROJECT",
  appId: "ТВОЙ_APP_ID"
};

firebase.initializeApp(firebaseConfig);

function loginGoogle() {
  const provider = new firebase.auth.GoogleAuthProvider();

  firebase.auth().signInWithPopup(provider)
    .then((result) => {
      const user = result.user;

      fetch("/google-login", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          name: user.displayName
        })
      }).then(() => {
        location.reload();
      });
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

{% if not name %}
<button onclick="loginGoogle()">Войти через Google</button>
{% elif not peer %}
<h3>Привет, {{name}}</h3>
<p>Выбери пользователя слева 👈</p>

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

    c.execute("SELECT name FROM users")
    users = [u[0] for u in c.fetchall()]

    conn.close()

    return render_template_string(HTML, users=users, name=session.get("name"), peer=None)

# --- LOGIN (REGISTRATION) ---
@app.route("/login", methods=["POST"])
def login():
    name = request.form["name"]
    session["name"] = name

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    try:
        c.execute("INSERT INTO users (name) VALUES (?)", (name,))
    except:
        pass

    conn.commit()
    conn.close()

    return redirect("/")

# --- CHAT ---
@app.route("/chat/<user>")
def chat(user):
    me = session.get("name")
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

    # users list
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT name FROM users")
    users = [u[0] for u in c.fetchall()]
    conn.close()

    return render_template_string(HTML,
        users=users,
        name=me,
        peer=user,
        messages=messages
    )

# --- SEND ---
@app.route("/send/<user>", methods=["POST"])
def send(user):
    me = session.get("name")
    msg = request.form["msg"]

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("INSERT INTO messages VALUES (NULL, ?, ?, ?)",
              (me, user, msg))

    conn.commit()
    conn.close()

    return redirect("/chat/" + user)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
from flask import jsonify

@app.route("/google-login", methods=["POST"])
def google_login():
    data = request.get_json()

    session["name"] = data["name"]

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("INSERT OR IGNORE INTO users (name) VALUES (?)",
              (data["name"],))

    conn.commit()
    conn.close()

    return jsonify({"ok": True})
