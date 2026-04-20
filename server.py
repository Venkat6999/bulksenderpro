import os
import asyncio
import base64
import threading
import random
import time
import logging
from io import BytesIO

from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit

from whatsapp_client import WhatsAppClient

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Environment ───────────────────────────────────────────────────────────────
IS_RENDER = os.environ.get("RENDER", "false").lower() == "true"
IS_DOCKER = os.environ.get("DOCKER", "false").lower() == "true"
PORT      = int(os.environ.get("PORT", 3000))

AUTH_PATH = "/data/.wwebjs_auth" if (IS_RENDER or IS_DOCKER) else "./sessions"

# ── Flask + SocketIO ──────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="app/static", template_folder="app/templates")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "bulksender-secret-key")

# Use eventlet mode for production stability on Render
# Note: eventlet monkey-patching is handled in the gunicorn command
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode=None, # Auto-detect (it will pick eventlet if available)
    logger=True, 
    engineio_logger=True,
    ping_timeout=120, # Increased for high latency / mobile networks
    ping_interval=25
)

# ── State ─────────────────────────────────────────────────────────────────────
is_currently_sending = False
stop_requested       = False
wa_client: WhatsAppClient | None = None


# ── SocketIO events ───────────────────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    log.info(f"Client connected: {request.sid}")
    if wa_client:
        if wa_client.is_ready:
            log.info("Emitting ready status to new client")
            emit("status", "ready")
        elif wa_client.last_qr:
            log.info(f"Emitting cached QR to new client (length: {len(wa_client.last_qr)})")
            emit("qr", wa_client.last_qr)
            emit("status", "initializing")
        else:
            log.info("Emitting initializing status to new client")
            emit("status", "initializing")
    else:
        log.info("WhatsApp client not initialized yet")
        emit("status", "initializing")


@socketio.on("disconnect")
def on_disconnect():
    log.info(f"Client disconnected: {request.sid}")


# ── WhatsApp event callbacks (called from WhatsAppClient) ─────────────────────
def on_qr(qr_data_url: str):
    log.info("QR Code ready 📲")
    try:
        log.info(f"Emitting QR event (length: {len(qr_data_url)})")
        socketio.emit("qr", qr_data_url)
        log.info("QR event emitted successfully")
    except Exception as e:
        log.error(f"Failed to emit QR event: {e}")


def on_authenticated():
    log.info("WhatsApp authenticated ✅")
    try:
        socketio.emit("status", "authenticated")
        log.info("Authenticated event emitted successfully")
    except Exception as e:
        log.error(f"Failed to emit authenticated event: {e}")


def on_ready():
    log.info("WhatsApp ready ✅✅✅")
    try:
        socketio.emit("status", "ready")
        log.info("Ready event emitted successfully - clients should redirect to dashboard")
    except Exception as e:
        log.error(f"Failed to emit ready event: {e}")


def on_auth_failure(msg: str):
    log.error(f"Auth failure ❌: {msg}")
    try:
        socketio.emit("status", "auth_failure")
    except Exception as e:
        log.error(f"Failed to emit auth_failure event: {e}")


def on_disconnected(reason: str):
    log.warning(f"WhatsApp disconnected: {reason}")
    try:
        socketio.emit("status", "disconnected")
        if reason == "LOGOUT":
            log.info("Logout detected — re-initializing for new QR...")
            threading.Thread(target=_delayed_reinit, daemon=True).start()
        else:
            socketio.emit("status", "disconnected_unexpectedly")
    except Exception as e:
        log.error(f"Failed to emit disconnect event: {e}")


def _delayed_reinit():
    time.sleep(3)
    if wa_client:
        wa_client.initialize()


# ── Routes ────────────────────────────────────────────────────────────────────

# Serve the app folder (login.html, dashboard.html, etc.)
@app.route("/")
def index():
    return send_from_directory("app", "login.html")

@app.route("/<path:filename>")
def serve_app(filename):
    return send_from_directory("app", filename)

@app.get("/health")
def health():
    return "OK", 200


@app.get("/debug-state")
def debug_state():
    """Diagnostic endpoint to check WhatsApp client state"""
    if not wa_client:
        return jsonify({"error": "WhatsApp client not initialized"}), 500
    
    return jsonify({
        "is_ready": wa_client.is_ready,
        "last_qr": bool(wa_client.last_qr),
        "last_qr_length": len(wa_client.last_qr) if wa_client.last_qr else 0,
        "session_path": str(wa_client.session_path),
        "is_docker": wa_client.is_docker,
    }), 200


@app.post("/logout")
def logout():
    global is_currently_sending, stop_requested
    try:
        log.info("Logout requested...")
        if wa_client and wa_client.is_ready:
            wa_client.logout()
        is_currently_sending = False
        stop_requested = True
        return "Logged out successfully", 200
    except Exception as e:
        log.error(f"Logout error: {e}")
        return f"Logout failed: {str(e)}", 500


@app.post("/stop-send")
def stop_send():
    global stop_requested
    stop_requested = True
    log.info("🛑 Stop requested")
    return "Stopping... 🛑", 200


@app.post("/bulk-send")
def bulk_send():
    global is_currently_sending, stop_requested

    if not wa_client or not wa_client.is_ready:
        return "WhatsApp is not ready (initializing or disconnected).", 530

    if is_currently_sending:
        return "A campaign is already in progress. Please wait or stop the current one.", 429

    # ── Parse form data ──
    numbers         = request.form.get("numbers", "")
    message         = request.form.get("message", "")
    delay_min       = int(request.form.get("delayMin", 3000))
    delay_max       = int(request.form.get("delayMax", 6000))
    msgs_per_number = int(request.form.get("messagesPerNumber", 1))
    file            = request.files.get("file")

    if not numbers or not message:
        return "Missing numbers or message.", 400

    # Parse number list
    import re
    num_list = [n.strip() for n in re.split(r"[\n,]+", numbers) if n.strip()]

    # Read file into memory before thread (file object may close)
    file_data = None
    if file:
        file_data = {
            "mimetype":     file.mimetype,
            "data":         file.read(),
            "filename":     file.filename,
        }

    is_currently_sending = True
    stop_requested = False

    # Run in background thread so we can return immediately
    threading.Thread(
        target=_bulk_send_worker,
        args=(num_list, message, delay_min, delay_max, msgs_per_number, file_data),
        daemon=True,
    ).start()

    return "Starting bulk send...", 200


def _bulk_send_worker(num_list, message, delay_min, delay_max, msgs_per_number, file_data):
    global is_currently_sending, stop_requested

    sent   = 0
    failed = 0
    total  = len(num_list)

    log.info(f"Starting bulk send to {total} numbers")

    try:
        for raw_num in num_list:
            if stop_requested:
                log.info("🛑 Bulk send halted by user")
                break

            num     = "".join(filter(str.isdigit, raw_num))
            chat_id = num + "@c.us"

            try:
                log.info(f"\n{'='*50}")
                log.info(f"Processing: {num}")
                
                # Skip is_registered check for now (it's unreliable)
                # registered = wa_client.is_registered(chat_id)
                registered = True  # Assume valid, let send_message handle invalid numbers
                
                if registered:
                    for i in range(msgs_per_number):
                        if stop_requested:
                            break

                        log.info(f"Sending message {i+1}/{msgs_per_number} to {num}...")

                        if file_data:
                            b64 = base64.b64encode(file_data["data"]).decode()
                            wa_client.send_media(
                                chat_id,
                                mimetype=file_data["mimetype"],
                                data=b64,
                                filename=file_data["filename"],
                                caption=message,
                            )
                        else:
                            wa_client.send_message(chat_id, message)

                        sent += 1
                        log.info(f"✅ Successfully sent to {num}")
                        
                        socketio.emit("progress", {
                            "type":    "success",
                            "number":  num,
                            "current": sent + failed,
                            "total":   total * msgs_per_number,
                        })

                        if msgs_per_number > 1 and i < msgs_per_number - 1:
                            time.sleep(1)
                else:
                    failed += 1
                    log.warning(f"❌ {num} is not registered on WhatsApp")
                    socketio.emit("progress", {
                        "type":   "fail",
                        "number": num,
                        "reason": "Not registered on WhatsApp",
                        "current": sent + failed,
                        "total":  total * msgs_per_number,
                    })

            except Exception as e:
                failed += 1
                error_msg = str(e)
                log.error(f"❌ Failed to send to {num}: {error_msg}")
                
                socketio.emit("progress", {
                    "type":   "fail",
                    "number": num,
                    "reason": error_msg[:100],  # Truncate long errors
                    "current": sent + failed,
                    "total":  total * msgs_per_number,
                })

            # Delay between numbers
            if raw_num != num_list[-1] and not stop_requested:
                delay_ms = random.randint(delay_min, delay_max)
                log.info(f"⏱️ Waiting {delay_ms/1000:.1f}s before next message...")
                time.sleep(delay_ms / 1000)

    except Exception as e:
        log.error(f"FATAL ERROR in bulk send: {e}", exc_info=True)
    finally:
        is_currently_sending = False
        summary_msg = f"Campaign complete: {sent} sent, {failed} failed out of {total * msgs_per_number}"
        log.info(f"\n{'='*50}")
        log.info(summary_msg)
        
        socketio.emit("completed", {
            "sent":    sent,
            "failed":  failed,
            "total":   total * msgs_per_number,
            "stopped": stop_requested,
        })


# ── Startup Logic ─────────────────────────────────────────────────────────────
def start_whatsapp():
    global wa_client
    log.info("⏳ Initializing WhatsApp client...")
    wa_client = WhatsAppClient(
        session_path=AUTH_PATH,
        on_qr=on_qr,
        on_authenticated=on_authenticated,
        on_ready=on_ready,
        on_auth_failure=on_auth_failure,
        on_disconnected=on_disconnected,
        is_docker=(IS_RENDER or IS_DOCKER),
    )
    wa_client.initialize()

# Start WhatsApp client immediately (crucial for Gunicorn)
# Since we use -w 1 in Gunicorn, this will only run once.
if IS_RENDER or IS_DOCKER or not __name__ == "__main__":
    # Start in a separate thread to not block Gunicorn worker
    threading.Thread(target=start_whatsapp, daemon=True).start()

if __name__ == "__main__":
    log.info(f"🚀 Server starting on port {PORT} (Manual Start)")
    socketio.run(app, host="0.0.0.0", port=PORT, allow_unsafe_werkzeug=True)
