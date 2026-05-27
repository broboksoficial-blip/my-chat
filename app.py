from flask import Flask, render_template_string

app = Flask(__name__)

HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Chat</title>
</head>
<body>

<h2>💬 Чат</h2>

<!-- LOGIN -->
<div id="login">
  <h3>Вход по номеру 📱</h3>
  <input id="phone" placeholder="+79991234567">
  <button onclick="sendCode()">Отправить код</button>

  <br><br>

  <input id="code" placeholder="SMS код">
  <button onclick="verifyCode()">Подтвердить</button>

  <div id="recaptcha-container"></div>
</div>

<hr>

<!-- CHAT -->
<div id="chat" style="display:none;">
  <h3>Чат 💬</h3>

  <div id="messages"></div>

  <input id="msg" placeholder="Сообщение">
  <button onclick="sendMessage()">Отправить</button>
</div>

<script src="https://www.gstatic.com/firebasejs/10.7.1/firebase-app-compat.js"></script>
<script src="https://www.gstatic.com/firebasejs/10.7.1/firebase-auth-compat.js"></script>

<script>
// 🔥 ТВОЙ FIREBASE CONFIG
const firebaseConfig = {
  apiKey: "AIzaSyByRxM7bQhYSK5XCuaZMRo0s42DGeaav6Y",
  authDomain: "my-chat2-ae3ca.firebaseapp.com",
  projectId: "my-chat2-ae3ca",
  appId: "1:407628010061:web:72f3cb30760c52101cc204"
};

firebase.initializeApp(firebaseConfig);

let confirmationResult;

// reCAPTCHA
window.recaptchaVerifier = new firebase.auth.RecaptchaVerifier('recaptcha-container', {
  size: 'invisible'
});

// 📱 отправка SMS
function sendCode() {
  const phone = document.getElementById("phone").value;

  firebase.auth().signInWithPhoneNumber(phone, window.recaptchaVerifier)
    .then((result) => {
      confirmationResult = result;
      alert("Код отправлен!");
    })
    .catch((error) => {
      alert(error.message);
    });
}

// 🔐 проверка кода
function verifyCode() {
  const code = document.getElementById("code").value;

  confirmationResult.confirm(code)
    .then((result) => {
      document.getElementById("login").style.display = "none";
      document.getElementById("chat").style.display = "block";
      alert("Вход выполнен!");
    })
    .catch((error) => {
      alert("Неверный код");
    });
}

// 💬 чат (пока локальный)
function sendMessage() {
  const msg = document.getElementById("msg").value;
  const div = document.getElementById("messages");

  div.innerHTML += "<p>" + msg + "</p>";
  document.getElementById("msg").value = "";
}
</script>

</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HTML)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
