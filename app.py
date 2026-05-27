from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

messages = []

HTML = """
<h2>💬 Чат</h2>

<div id="chat"></div>

<form id="form">
  <input id="msg" autocomplete="off" placeholder="Сообщение">
  <button>Отправить</button>
</form>

<script>
async function loadMessages() {
    const res = await fetch('/messages');
    const data = await res.json();

    let html = "";
    data.forEach(m => {
        html += "<p>" + m + "</p>";
    });

    document.getElementById("chat").innerHTML = html;
}

document.getElementById("form").onsubmit = async (e) => {
    e.preventDefault();

    const msg = document.getElementById("msg").value;

    await fetch('/send', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({msg})
    });

    document.getElementById("msg").value = "";
    loadMessages();
};

setInterval(loadMessages, 1000);
loadMessages();
</script>
"""

@app.route("/")
def home():
    return render_template_string(HTML)

@app.route("/send", methods=["POST"])
def send():
    data = request.json
    messages.append(data["msg"])
    return "ok"

@app.route("/messages")
def get_messages():
    return jsonify(messages)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
