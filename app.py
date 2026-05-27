from flask import Flask, request

app = Flask(__name__)

messages = []

@app.route("/")
def home():
    chat = "<h2>💬 Простой чат</h2>"

    for msg in messages:
        chat += f"<p>{msg}</p>"

    chat += """
    <form action="/send" method="POST">
        <input name="msg" placeholder="Напиши сообщение" required>
        <button type="submit">Отправить</button>
    </form>
    """

    return chat

@app.route("/send", methods=["POST"])
def send():
    msg = request.form["msg"]
    messages.append(msg)
    return "Отправлено! <a href='/'>Назад</a>"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "Сайт работает 🚀"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
