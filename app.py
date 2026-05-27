from flask import Flask, request, render_template_string, session, redirect, url_for
import sqlite3

app = Flask(__name__)
app.secret_key = "chat_secret"

DB = "chat.db"

# --- DB INIT ---
def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()

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

# --- HTML ---
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

<div class="left">
<h3>Контакты</h3>
{% for u in users %}
  <p><a href="/chat/{{u}}">{{u}}</a></p>
{% endfor %}
</div>

<div class="chat">

{% if not name %}
<form method="POST" action="/login">
  <input name="name" placeholder="Имя">
  <button>Войти</button>
</form>

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

# --- USERS (временно) ---
users = ["Аня", "Вася", "Петя"]

@app.route("/")
def home():
    return render_template_string(HTML, users=users, name=session.get("name"), peer=None)

@app.route("/login", methods=["POST"])
def login():
    session["name"] = request.form["name"]
    return redirect(url_for("home"))

@app.route("/chat/<user>")
def chat(user):
    if "name" not in session:
        return redirect("/")

    me = session["name"]

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
        users=users,
        name=me,
        peer=user,
        messages=messages
    )

@app.route("/send/<user>", methods=["POST"])
def send(user):
    me = session.get("name")
    msg = request.form["msg"]

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    c.execute("INSERT INTO messages (sender, receiver, message) VALUES (?, ?, ?)",
              (me, user, msg))

    conn.commit()
    conn.close()

    return redirect("/chat/" + user)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
