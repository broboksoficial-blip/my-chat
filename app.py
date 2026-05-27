from flask import Flask, request, render_template_string, session, redirect, url_for

app = Flask(__name__)
app.secret_key = "chat_secret"

messages = []

HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Чат</title>
  <style>
    body { font-family: Arial; background:#f2f2f2; }
    .box { width:400px; margin:50px auto; background:white; padding:20px; border-radius:10px; }
    .msg { padding:5px; border-bottom:1px solid #eee; }
    input { width:80%; padding:8px; }
    button { padding:8px; }
  </style>
</head>
<body>

<div class="box">

{% if not name %}
  <h3>Вход в чат</h3>
  <form method="POST" action="/login">
    <input name="name" placeholder="Введи имя" required>
    <button>Войти</button>
  </form>
{% else %}
  <h3>💬 Чат (ты: {{name}})</h3>

  <div>
    {% for m in messages %}
      <div class="msg"><b>{{m["name"]}}:</b> {{m["text"]}}</div>
    {% endfor %}
  </div>

  <form method="POST" action="/send">
    <input name="msg" placeholder="Сообщение" required>
    <button>Отправить</button>
  </form>

  <br>
  <a href="/logout">Выйти</a>

{% endif %}

</div>

</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HTML, messages=messages, name=session.get("name"))

@app.route("/login", methods=["POST"])
def login():
    session["name"] = request.form["name"]
    return redirect(url_for("home"))

@app.route("/logout")
def logout():
    session.pop("name", None)
    return redirect(url_for("home"))

@app.route("/send", methods=["POST"])
def send():
    if "name" not in session:
        return redirect(url_for("home"))

    messages.append({
        "name": session["name"],
        "text": request.form["msg"]
    })

    return redirect(url_for("home"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
