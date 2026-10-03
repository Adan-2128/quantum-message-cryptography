// chat.js -- QChat client
// Talks to the Flask-SocketIO server over WebSocket. All quantum crypto
// (BB84 key generation + one-time-pad encrypt/decrypt) happens server-side
// in quantum_crypto.py -- this file just handles UI + message routing.

const socket = io(); // connects to whatever origin served this page

let me = null;
let activeChatWith = null;
let onlineUsers = [];

// ---------------- Elements ----------------
const loginScreen = document.getElementById("login-screen");
const appScreen = document.getElementById("app-screen");
const usernameInput = document.getElementById("username-input");
const loginBtn = document.getElementById("login-btn");
const meUsernameEl = document.getElementById("me-username");
const userListEl = document.getElementById("user-list");

const noChatEl = document.getElementById("no-chat");
const activeChatEl = document.getElementById("active-chat");
const chatWithEl = document.getElementById("chat-with");
const keyBadgeEl = document.getElementById("key-ceremony-badge");
const messagesEl = document.getElementById("messages");
const wireContentEl = document.getElementById("wire-content");
const sendForm = document.getElementById("send-form");
const messageInput = document.getElementById("message-input");

// ---------------- Login ----------------
// CHANGED: a dedicated error element under the username field, created
// here so index.html doesn't need to change.
const loginError = document.createElement("div");
loginError.style.color = "#ff6b6b";
loginError.style.fontSize = "12px";
loginError.style.marginTop = "-6px";
loginError.style.marginBottom = "12px";
usernameInput.insertAdjacentElement("afterend", loginError);

function login() {
    const name = usernameInput.value.trim();
    if (!name) return;
    loginError.textContent = "";
    loginBtn.disabled = true;
    loginBtn.textContent = "Connecting...";
    // CHANGED: just send the request -- do NOT switch screens yet.
    // The screen switch now happens only inside the "registered" handler
    // below, once the server actually confirms it.
    socket.emit("register", { username: name });
}

loginBtn.addEventListener("click", login);

// CHANGED: new handlers for the server's actual response
socket.on("registered", (data) => {
    me = data.username;
    meUsernameEl.textContent = me;
    loginScreen.classList.add("hidden");
    appScreen.classList.remove("hidden");
});

socket.on("registration_error", (data) => {
    loginError.textContent = data.message;
    loginBtn.disabled = false;
    loginBtn.textContent = "Enter";
});
usernameInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") login();
});

// ---------------- Online user list ----------------
socket.on("online_users", (users) => {
    onlineUsers = users.filter((u) => u !== me);
    renderUserList();
});

function renderUserList() {
    userListEl.innerHTML = "";
    if (onlineUsers.length === 0) {
        const li = document.createElement("li");
        li.textContent = "No one else online yet";
        li.style.color = "var(--muted)";
        li.style.cursor = "default";
        userListEl.appendChild(li);
        return;
    }
    onlineUsers.forEach((user) => {
        const li = document.createElement("li");
        li.innerHTML = `<span class="dot"></span>${user}`;
        if (user === activeChatWith) li.classList.add("active");
        li.addEventListener("click", () => openChat(user));
        userListEl.appendChild(li);
    });
}

// ---------------- Opening a chat (triggers BB84 key ceremony) ----------------
function openChat(user) {
    activeChatWith = user;
    chatWithEl.textContent = user;
    noChatEl.classList.add("hidden");
    activeChatEl.classList.remove("hidden");
    messagesEl.innerHTML = "";
    wireContentEl.textContent = "--";
    keyBadgeEl.classList.add("hidden");
    renderUserList();

    socket.emit("start_chat", { me, other: user });
}

socket.on("chat_ready", (data) => {
    if (!data.key_ceremony) return; // pool already existed, no new ceremony to report
    const { qber, eavesdropper_detected, key_bits_generated } = data.key_ceremony;
    keyBadgeEl.classList.remove("hidden");
    if (eavesdropper_detected) {
        keyBadgeEl.textContent = `\u26a0 QBER ${qber} -- possible eavesdropper`;
        keyBadgeEl.className = "badge warn";
    } else {
        keyBadgeEl.textContent = `\ud83d\udd10 BB84 key ready -- QBER ${qber} -- ${key_bits_generated} bits`;
        keyBadgeEl.className = "badge ok";
    }
});

// ---------------- Sending messages ----------------
sendForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = messageInput.value;
    if (!text || !activeChatWith) return;
    socket.emit("send_message", { sender: me, recipient: activeChatWith, text });
    messageInput.value = "";
});

// ---------------- Receiving messages ----------------
socket.on("receive_message", (data) => {
    const { sender, text, ciphertext_hex, key_bits_used, key_bits_remaining } = data;

    // Only render into the currently-open conversation
    const otherParty = sender === me ? activeChatWith : sender;
    if (otherParty !== activeChatWith) return;

    const bubble = document.createElement("div");
    bubble.className = "msg " + (sender === me ? "mine" : "theirs");
    bubble.textContent = text;
    messagesEl.appendChild(bubble);
    messagesEl.scrollTop = messagesEl.scrollHeight;

    wireContentEl.textContent =
        `${ciphertext_hex}   (${key_bits_used} key bits used, ${key_bits_remaining} remaining in pool)`;
});