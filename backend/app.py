"""
app.py -- Quantum-secured chat server (WhatsApp-style)
--------------------------------------------------------
Flask + Flask-SocketIO backend. Manages users, rooms (one per pair of
chatting users), and a BB84-derived key pool per room. Every message is
masked with fresh key bits before "transmission" and unmasked before
delivery -- the quantum key pool refills automatically when it runs low.

Run with:  python app.py
Then open: http://localhost:5000
No API key needed by default (local Aer/Statevector simulation only).
"""

# MUST be the very first thing that runs, before any other import.
# eventlet needs to monkey-patch Python's standard library (socket,
# threading, etc.) so it can juggle many concurrent WebSocket
# connections cooperatively. Without this, only one tab/connection at
# a time gets served properly -- a second browser tab appears to "not
# show up" because the server never gets around to processing it.
import eventlet
eventlet.monkey_patch()

import os
from flask import Flask, send_from_directory, request
from flask_socketio import SocketIO, emit, join_room

import quantum_crypto as qc

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")
app.config["SECRET_KEY"] = "dev-secret-change-me"
socketio = SocketIO(app, cors_allowed_origins="*")

# ------------------------------------------------------------------
# In-memory state (fine for a demo; use a real DB/session store for anything real)
# ------------------------------------------------------------------
online_users = {}        # username -> socket id (sid)
sid_to_username = {}     # sid -> username, for clean disconnect handling
room_key_pools = {}      # room_id -> {"key": [...], "cursor": int}
KEY_TOPUP_THRESHOLD = 200   # regenerate more key material when fewer bits remain
KEY_TOPUP_AMOUNT_RAW = 4000 # raw BB84 qubits requested per top-up


def room_id_for(user_a: str, user_b: str) -> str:
    return "__".join(sorted([user_a, user_b]))


def ensure_key_pool(room_id: str, min_bits_needed: int):
    """Makes sure the room's key pool has at least min_bits_needed bits,
    generating more via BB84 if not."""
    pool = room_key_pools.get(room_id)
    remaining = 0 if pool is None else (len(pool["key"]) - pool["cursor"])

    if pool is None or remaining < max(min_bits_needed, KEY_TOPUP_THRESHOLD):
        raw_bits = max(KEY_TOPUP_AMOUNT_RAW, min_bits_needed * 6)
        result = qc.bb84_generate_key(raw_bits)
        new_key = result["key"]
        if pool is None:
            room_key_pools[room_id] = {"key": new_key, "cursor": 0}
        else:
            # keep unused old bits, append fresh ones
            unused = pool["key"][pool["cursor"]:]
            room_key_pools[room_id] = {"key": unused + new_key, "cursor": 0}
        return result  # so caller can report qber/detection to the UI
    return None


def take_key_bits(room_id: str, n: int) -> list:
    pool = room_key_pools[room_id]
    bits = pool["key"][pool["cursor"]: pool["cursor"] + n]
    pool["cursor"] += n
    return bits


# ------------------------------------------------------------------
# Static frontend
# ------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


# ------------------------------------------------------------------
# Socket events
# ------------------------------------------------------------------
@socketio.on("register")
def on_register(data):
    username = data["username"]

    # CHANGED: reject a username that's already taken, and reply only to
    # the requester (not everyone) so they get clear feedback instead of
    # the page silently doing nothing.
    if username in online_users:
        emit("registration_error", {"message": f'"{username}" is already in use. Pick another name.'})
        return

    online_users[username] = request.sid
    sid_to_username[request.sid] = username
    join_room(username)  # each user has a personal room for direct delivery

    # CHANGED: confirm success directly to this client before it switches screens
    emit("registered", {"username": username})
    emit("online_users", list(online_users.keys()), broadcast=True)
    print(f"[+] {username} connected (sid={request.sid})")


@socketio.on("start_chat")
def on_start_chat(data):
    """Called when a user opens a chat with someone -- ensures a quantum
    key pool exists for that pair before any messages are sent."""
    me, other = data["me"], data["other"]
    room = room_id_for(me, other)
    result = ensure_key_pool(room, min_bits_needed=800)
    payload = {"room": room}
    if result:
        payload["key_ceremony"] = {
            "qber": round(result["qber"], 4),
            "eavesdropper_detected": result["eavesdropper_detected"],
            "key_bits_generated": result["final_key_length"],
        }
    emit("chat_ready", payload)


@socketio.on("send_message")
def on_send_message(data):
    sender, recipient, text = data["sender"], data["recipient"], data["text"]
    room = room_id_for(sender, recipient)

    n_bits_needed = len(qc.text_to_bits(text))
    ensure_key_pool(room, n_bits_needed)
    key_bits = take_key_bits(room, n_bits_needed)

    # FIX: use the chunked encrypt/decrypt -- the old single-circuit
    # versions crashed on any message longer than a few characters
    # (Qiskit's statevector qubit limit), which is why messages weren't
    # being delivered.
    ciphertext_bits = qc.otp_encrypt_message(text, key_bits)
    ciphertext_hex = qc.bits_to_hex(ciphertext_bits)   # what "traveled the wire"
    recovered_text = qc.otp_decrypt_message(ciphertext_bits, key_bits)  # recipient-side unmask

    pool = room_key_pools[room]
    remaining = len(pool["key"]) - pool["cursor"]

    message_payload = {
        "sender": sender,
        "text": recovered_text,
        "ciphertext_hex": ciphertext_hex,
        "key_bits_used": n_bits_needed,
        "key_bits_remaining": remaining,
    }
    emit("receive_message", message_payload, room=recipient)
    emit("receive_message", message_payload, room=sender)  # echo back to sender


@socketio.on("disconnect")
def on_disconnect():
    username = sid_to_username.pop(request.sid, None)
    if username:
        online_users.pop(username, None)
        print(f"[-] {username} disconnected")
        emit("online_users", list(online_users.keys()), broadcast=True)


if __name__ == "__main__":
    # CHANGED: print the LAN address so a second device knows what to type.
    # "localhost" always means "this machine" -- a phone or another
    # computer must use the host machine's actual network IP instead.
    import socket as _socket
    try:
        _s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        _s.connect(("8.8.8.8", 80))
        lan_ip = _s.getsockname()[0]
        _s.close()
    except Exception:
        lan_ip = "YOUR_MACHINE_IP"

    print("=" * 60)
    print(f"  Same computer, another tab/browser:  http://localhost:5000")
    print(f"  Another device on the same Wi-Fi:     http://{lan_ip}:5000")
    print("=" * 60)

    socketio.run(app, host="0.0.0.0", port=5000, debug=False)