from flask import Flask, request, render_template_string, session, redirect, url_for

app = Flask(__name__)
app.secret_key = "chat_key"

users = ["Вася", "Петя", "Аня"]

chats = {}  # личные чаты

HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Личные чаты</title>
  <style>
    body { font-family: Arial; display:flex; }

    .left {
      width: 200px;
      background: #f0f0f0;
      padding: 10px;
    }

    .chat {
      flex: 1;
      padding: 10px;
    }

    .msg { margin: 5px 0; }
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
    <input name="name" placeholder="Твоё имя">
    <button>Войти</button>
  </form>

{% elif not peer %}
  <h3>Привет, {{name}}</h3>
  <p>Выбери контакт слева 👈</p>

{% else %}
  <h3>Чат с {{peer}}</h3>

  {% for m in chat %}
    <div class="msg">{{m}}</div>
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

@app.route("/")
def home():
    return render_template_string(HTML, users=users, name=session.get("name"), peer=None)

@app.route("/login", methods=["POST"])
def login():
    session["name"] = request.form["name"]
    return redirect(url_for("home"))

@app.route("/chat/<user>")
def chat(user):
    name = session.get("name")
    if not name:
        return redirect(url_for("home"))

    key = tuple(sorted([name, user]))

    if key not in chats:
        chats[key] = []

    return render_template_string(HTML,
        users=users,
        name=name,
        peer=user,
        chat=chats[key]
    )

@app.route("/send/<user>", methods=["POST"])
def send(user):
    name = session.get("name")

    key = tuple(sorted([name, user]))

    if key not in chats:
        chats[key] = []

    msg = f"{name}: {request.form['msg']}"
    chats[key].append(msg)

    return redirect(f"/chat/{user}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
