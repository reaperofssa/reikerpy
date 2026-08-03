import os
import select
import subprocess
import threading
import psutil
from flask import Flask, send_from_directory, jsonify, request, send_file
from flask_socketio import SocketIO, emit
from flask_cors import CORS
from datetime import datetime
import time
import signal
import zipfile
import shutil
import tempfile
import uuid
import base64
import mimetypes
import secrets
import json
from functools import wraps
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import os

# Flask app initialization
app = Flask(__name__, static_folder="static", static_url_path="/")

# The frontend uses fetch(..., { credentials: 'include' }), so CORS must allow
# credentials and must NOT use "*" for origins (browsers reject that combo).
# Set FRONTEND_ORIGIN in your .env to your frontend's origin, e.g.
# FRONTEND_ORIGIN=https://myapp.example.com  (comma-separate multiple origins)
FRONTEND_ORIGINS = [o.strip() for o in os.getenv("FRONTEND_ORIGIN", "http://localhost:3000").split(",") if o.strip()]
CORS(app, supports_credentials=True, origins=FRONTEND_ORIGINS)

START_TIME = time.time()

# --- Data dir for things that should survive restarts (history, saved commands) ---
DATA_DIR = os.path.join(os.getcwd(), "data")
os.makedirs(DATA_DIR, exist_ok=True)
COMMAND_HISTORY_FILE = os.path.join(DATA_DIR, "command_history.json")
SAVED_COMMANDS_FILE = os.path.join(DATA_DIR, "saved_commands.json")

# --- Auth: simple HttpOnly cookie session tokens ---
# token -> {"created": ts, "last_seen": ts}
active_tokens = {}
tokens_lock = threading.Lock()
TOKEN_COOKIE_NAME = "session_token"
TOKEN_TTL_SECONDS = 24 * 60 * 60  # 24h idle expiry
# Cookies for cross-origin fetch(credentials:'include') must be SameSite=None; Secure=True
# (requires HTTPS). If you're serving frontend+backend from the same site over
# plain HTTP locally, set COOKIE_SECURE=false and COOKIE_SAMESITE=Lax in your .env.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() == "true"
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "None")

# Routes that don't require a valid session token
PUBLIC_ROUTES = {"/", "/check-password"}
PUBLIC_PREFIXES = ("/static/",)


def _is_public_route(path):
    if path in PUBLIC_ROUTES:
        return True
    return any(path.startswith(p) for p in PUBLIC_PREFIXES)


def _token_is_valid(token):
    if not token:
        return False
    with tokens_lock:
        entry = active_tokens.get(token)
        if not entry:
            return False
        if time.time() - entry["last_seen"] > TOKEN_TTL_SECONDS:
            del active_tokens[token]
            return False
        entry["last_seen"] = time.time()
        return True


@app.before_request
def enforce_auth():
    # Let CORS preflight through untouched
    if request.method == "OPTIONS":
        return None
    # Frontend assets (JS/CSS/images) are served via Flask's built-in 'static'
    # endpoint at the root path (static_url_path="/"), so gate by endpoint name
    # rather than path prefix -- otherwise the login page's own JS/CSS 401s.
    if request.endpoint == "static" or _is_public_route(request.path):
        return None
    token = request.cookies.get(TOKEN_COOKIE_NAME)
    if not _token_is_valid(token):
        return jsonify({"error": "Unauthorized"}), 401
    return None

# No hard cap on request size (default Flask limit is None already, but being explicit).
# If you want a ceiling, set e.g. 10 * 1024 * 1024 * 1024 for 10GB.
app.config['MAX_CONTENT_LENGTH'] = None
# Chunked uploads land here before being assembled into the final file
UPLOAD_TMP_DIR = os.path.join(tempfile.gettempdir(), "chunked_uploads")
os.makedirs(UPLOAD_TMP_DIR, exist_ok=True)
CHUNK_SIZE_READ = 1024 * 1024  # stream to disk 1MB at a time

load_dotenv()

# Set working directory to /home
HOME_DIR = os.path.join(os.getcwd(), 'home')

# Ensure the home directory exists
os.makedirs(HOME_DIR, exist_ok=True)
def safe_path(path):
    abs_path = os.path.abspath(os.path.join(HOME_DIR, path))
    return abs_path if abs_path.startswith(HOME_DIR) else None

# Flask & Socket.IO setup
socketio = SocketIO(app, cors_allowed_origins=FRONTEND_ORIGINS, cors_credentials=True)


@socketio.on('connect')
def handle_connect():
    """Reject the socket handshake unless a valid session cookie is present."""
    token = request.cookies.get(TOKEN_COOKIE_NAME)
    if not _token_is_valid(token):
        log_message(f"Rejected unauthenticated socket connection from {request.sid}")
        return False  # refuses the connection
    return True

# Hardcoded server stats (Modify based on actual system stats)

# Define allowed commands for security
ALLOWED_COMMANDS = {
    "ls", "cat", "echo", "python", "pip", "python3", "node", "npm", "git", "pyarmor", "pm2", "yarn", "gitclone",
    "pwd", "zip", "unzip", "ping", "curl", "who", "wget", "nano", "vi", "touch", "mkdir",
    "df", "du", "top", "htop", "free", "uptime", "uname", "head", "tail", "less", "more", "help"
}  # Add only safe commands

@app.route('/')
def serve_index():
    """Serve index.html from the static folder."""
    return send_from_directory(app.static_folder, "index.html")

def get_cpu_and_uptime():
    """Fetch real-time CPU usage and properly format uptime."""

    # Get accurate CPU usage inside Docker
    cpu_usage = psutil.cpu_percent(interval=0.5)

    # Calculate uptime
    total_minutes = int((time.time() - START_TIME) // 60)
    hours = total_minutes // 60
    minutes = total_minutes % 60

    uptime_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"

    return {
        "cpu": f"{cpu_usage}%",
        "ram": "16GB",  # Hardcoded
        "disk": "24GB",  # Hardcoded
        "uptime": uptime_str
    }

@app.route('/stats', methods=['GET'])
def stats():
    """Return CPU and uptime stats."""
    return jsonify(get_cpu_and_uptime())


# Add this route for session info
@app.route('/session-info', methods=['GET'])
def session_info():
    """Return current session information"""
    return jsonify({
        "user": "guest",
        "session_id": request.sid if hasattr(request, 'sid') else "unknown",
        "connected_at": datetime.now().isoformat(),
        "server_version": "2.5.0"
    })

# --- Command history: persisted to disk so it survives restarts ---
history_lock = threading.Lock()


def _load_json_list(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save_json_list(path, items):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f)


def add_history_entry(entry):
    with history_lock:
        history = _load_json_list(COMMAND_HISTORY_FILE)
        history.append(entry)
        # Keep the file from growing unbounded
        history = history[-1000:]
        _save_json_list(COMMAND_HISTORY_FILE, history)


def update_history_entry(command_id, **updates):
    with history_lock:
        history = _load_json_list(COMMAND_HISTORY_FILE)
        for entry in history:
            if entry.get("id") == command_id:
                entry.update(updates)
                break
        _save_json_list(COMMAND_HISTORY_FILE, history)


@app.route('/command-history', methods=['GET'])
def get_command_history():
    """Return recent command history, most recent first."""
    limit = request.args.get("limit", default=100, type=int)
    with history_lock:
        history = _load_json_list(COMMAND_HISTORY_FILE)
    return jsonify({"history": list(reversed(history))[:limit]})


@app.route('/command-history', methods=['DELETE'])
def clear_command_history():
    """Clear all stored command history."""
    with history_lock:
        _save_json_list(COMMAND_HISTORY_FILE, [])
    return jsonify({"message": "Command history cleared"}), 200


# --- Saved / favorite commands ---
saved_commands_lock = threading.Lock()


@app.route('/saved-commands', methods=['GET'])
def get_saved_commands():
    with saved_commands_lock:
        return jsonify({"commands": _load_json_list(SAVED_COMMANDS_FILE)})


@app.route('/saved-commands', methods=['POST'])
def add_saved_command():
    data = request.json or {}
    command = (data.get("command") or "").strip()
    label = (data.get("label") or command).strip()

    if not command:
        return jsonify({"error": "Missing command"}), 400

    entry = {
        "id": uuid.uuid4().hex,
        "label": label,
        "command": command,
        "created": time.time(),
    }
    with saved_commands_lock:
        saved = _load_json_list(SAVED_COMMANDS_FILE)
        saved.append(entry)
        _save_json_list(SAVED_COMMANDS_FILE, saved)

    return jsonify(entry), 201


@app.route('/saved-commands/<command_id>', methods=['DELETE'])
def delete_saved_command(command_id):
    with saved_commands_lock:
        saved = _load_json_list(SAVED_COMMANDS_FILE)
        remaining = [c for c in saved if c.get("id") != command_id]
        if len(remaining) == len(saved):
            return jsonify({"error": "Saved command not found"}), 404
        _save_json_list(SAVED_COMMANDS_FILE, remaining)
    return jsonify({"message": "Deleted"}), 200

# Add these routes to your Flask app (app.py)

@app.route('/create-zip', methods=['POST'])
def create_zip():
    """Create a ZIP file from selected files/folders."""
    data = request.json
    zip_name = data.get("zip_name", "archive.zip")
    files = data.get("files", [])
    current_path = data.get("path", "")
    
    if not files:
        return jsonify({"error": "No files selected"}), 400
    
    # Create ZIP file path
    zip_path = os.path.join(HOME_DIR, current_path, zip_name) if current_path else os.path.join(HOME_DIR, zip_name)
    zip_path = safe_path(os.path.join(current_path, zip_name)) if current_path else safe_path(zip_name)
    
    if not zip_path:
        return jsonify({"error": "Invalid path"}), 400
    
    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file_name in files:
                file_path = os.path.join(HOME_DIR, current_path, file_name) if current_path else os.path.join(HOME_DIR, file_name)
                file_path = safe_path(os.path.join(current_path, file_name)) if current_path else safe_path(file_name)
                
                if not file_path or not os.path.exists(file_path):
                    continue
                
                if os.path.isdir(file_path):
                    # Add directory recursively
                    for root, dirs, files_in_dir in os.walk(file_path):
                        for file in files_in_dir:
                            file_full_path = os.path.join(root, file)
                            arcname = os.path.relpath(file_full_path, os.path.join(HOME_DIR, current_path) if current_path else HOME_DIR)
                            zipf.write(file_full_path, arcname)
                else:
                    # Add single file
                    arcname = file_name
                    zipf.write(file_path, arcname)
        
        return jsonify({"message": f"Created {zip_name} with {len(files)} items"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/download-zip', methods=['POST'])
def download_zip():
    """Zip a folder (or a list of files/folders) and stream the result back as a download,
    without permanently writing the zip into the user's home directory.

    Body: {"path": "sub/folder", "files": ["optional", "subset", "of", "names"]}
    If "files" is omitted, the whole folder at "path" is zipped.
    """
    data = request.json or {}
    rel_path = (data.get("path") or "").strip()
    files = data.get("files")  # optional subset; None means "zip everything in path"

    source_dir = safe_path(rel_path) if rel_path else HOME_DIR
    if not source_dir or not os.path.isdir(source_dir):
        return jsonify({"error": "Invalid path"}), 400

    zip_name = secure_filename(data.get("zip_name") or (os.path.basename(rel_path.rstrip("/")) or "archive")) + ".zip"

    # Write to a temp file on disk (not memory) so this scales to large folders.
    tmp_fd, tmp_zip_path = tempfile.mkstemp(suffix=".zip")
    os.close(tmp_fd)

    try:
        with zipfile.ZipFile(tmp_zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            targets = files if files else os.listdir(source_dir)
            for name in targets:
                full_path = safe_path(os.path.join(rel_path, name)) if rel_path else safe_path(name)
                if not full_path or not os.path.exists(full_path):
                    continue
                if os.path.isdir(full_path):
                    for root, dirs, files_in_dir in os.walk(full_path):
                        for f in files_in_dir:
                            file_full_path = os.path.join(root, f)
                            arcname = os.path.relpath(file_full_path, source_dir)
                            zipf.write(file_full_path, arcname)
                else:
                    zipf.write(full_path, os.path.relpath(full_path, source_dir))
    except Exception as e:
        os.remove(tmp_zip_path)
        return jsonify({"error": str(e)}), 500

    def cleanup(response):
        try:
            os.remove(tmp_zip_path)
        except OSError:
            pass
        return response

    response = send_file(
        tmp_zip_path,
        as_attachment=True,
        download_name=zip_name,
        mimetype="application/zip",
        conditional=False,
    )
    response.call_on_close(lambda: os.path.exists(tmp_zip_path) and os.remove(tmp_zip_path))
    return response


@app.route('/download/<path:filepath>', methods=['GET'])
def download_file(filepath):
    """Download a file."""
    file_path = safe_path(filepath)
    
    if not file_path or not os.path.exists(file_path) or os.path.isdir(file_path):
        return jsonify({"error": "File not found"}), 404
    
    try:
        return send_file(file_path, as_attachment=True, download_name=os.path.basename(filepath))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Add system info endpoint
@app.route('/system-info', methods=['GET'])
def system_info():
    """Return detailed system information"""
    import platform
    return jsonify({
        "platform": platform.system(),
        "platform_release": platform.release(),
        "platform_version": platform.version(),
        "architecture": platform.machine(),
        "hostname": platform.node(),
        "processor": platform.processor(),
        "python_version": platform.python_version()
    })
    
@app.route('/myfiles')
def serve_file():
    """Serve the file.html as the base page."""
    return send_from_directory("static", "file.html")

processes = {}  # sid -> {command_id: subprocess.Popen}
process_lock = threading.Lock()  # Prevents race conditions
LOG_FILE = "/tmp/process_log.txt"  # Persistent log for debugging
def log_message(message):
    """Write logs for debugging process issues."""
    with open(LOG_FILE, "a") as f:
        f.write(f"{message}\n")

def cleanup_zombie_processes():
    """Ensure no orphaned or zombie processes linger."""
    with process_lock:
        for sid, cmds in list(processes.items()):
            for command_id, process in list(cmds.items()):
                if process.poll() is not None:  # Process ended unexpectedly
                    log_message(f"Cleaning up zombie process {command_id} ({sid})")
                    del cmds[command_id]
            if not cmds:
                del processes[sid]

@socketio.on('command')
def handle_command(data):
    """Executes any command sent by the client. Multiple commands can run
    concurrently per session -- each gets its own command_id so the frontend
    can track/stop them independently."""
    full_command = data.get("command", "").strip()

    if not full_command:
        socketio.emit("output", {"response": "ERROR: No command provided."}, room=request.sid)
        return

    command_id = uuid.uuid4().hex
    sid = request.sid

    socketio.emit("command_started", {"command_id": command_id, "command": full_command}, room=sid)

    add_history_entry({
        "id": command_id,
        "sid": sid,
        "command": full_command,
        "status": "running",
        "started_at": time.time(),
        "ended_at": None,
        "exit_code": None,
    })

    def run_command(sid, command_id, full_command):
        try:
            process = subprocess.Popen(
                full_command,
                shell=True,
                cwd=HOME_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                universal_newlines=True,
                bufsize=1,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                start_new_session=True
            )

            with process_lock:
                processes.setdefault(sid, {})[command_id] = process

            log_message(f"Started process {process.pid} ({command_id}) for session {sid}")

            # Read stdout and stderr without blocking
            while process.poll() is None:
                read_fds, _, _ = select.select([process.stdout, process.stderr], [], [], 0.1)
                for stream in read_fds:
                    line = stream.readline().strip()
                    if line:
                        socketio.emit("output", {"command_id": command_id, "response": line}, room=sid)

                socketio.sleep(0.1)

            # Read remaining output
            for stream in (process.stdout, process.stderr):
                for line in stream:
                    line = line.strip()
                    if line:
                        socketio.emit("output", {"command_id": command_id, "response": line}, room=sid)

            exit_code = process.poll()
            log_message(f"Process {process.pid} ({command_id}) for session {sid} ended with code {exit_code}")
            update_history_entry(command_id, status="completed", ended_at=time.time(), exit_code=exit_code)

        except Exception as e:
            socketio.emit("output", {"command_id": command_id, "response": f"ERROR: {str(e)}"}, room=sid)
            log_message(f"Error executing command {command_id} for {sid}: {str(e)}")
            update_history_entry(command_id, status="error", ended_at=time.time(), error=str(e))

        finally:
            with process_lock:
                if sid in processes:
                    processes[sid].pop(command_id, None)
                    if not processes[sid]:
                        del processes[sid]
            socketio.emit("command_ended", {"command_id": command_id}, room=sid)  # Notify frontend

    socketio.start_background_task(run_command, sid, command_id, full_command)

@socketio.on('input')
def handle_input(data):
    """Send user input to a running process. Pass command_id to target a
    specific command; if omitted and only one command is running for this
    session, it's used as a fallback."""
    input_text = data.get("text", "").strip()
    command_id = data.get("command_id")

    if not input_text:
        return  # Ignore empty input

    with process_lock:
        sid_processes = processes.get(request.sid, {})
        if not sid_processes:
            socketio.emit("output", {"response": "ERROR: No running process to send input to."}, room=request.sid)
            return

        if command_id:
            targets = [sid_processes[command_id]] if command_id in sid_processes else []
        elif len(sid_processes) == 1:
            targets = list(sid_processes.values())
        else:
            socketio.emit("output", {"response": "ERROR: Multiple commands running -- specify command_id."}, room=request.sid)
            return

        for process in targets:
            if process.poll() is None:  # If still running
                try:
                    process.stdin.write(input_text + "\n")
                    process.stdin.flush()
                except Exception as e:
                    socketio.emit("output", {"response": f"ERROR: Failed to send input: {str(e)}"}, room=request.sid)

@socketio.on('stop')
def stop_commands(data):
    """Stop one command (if command_id given) or all commands for this
    session, along with their child processes."""
    data = data or {}
    command_id = data.get("command_id")
    sid = request.sid

    with process_lock:
        sid_processes = processes.get(sid, {})
        if not sid_processes:
            socketio.emit("output", {"response": "No running commands to stop."}, room=sid)
            return

        if command_id and command_id not in sid_processes:
            socketio.emit("output", {"response": f"No such running command: {command_id}"}, room=sid)
            return

        targets = {command_id: sid_processes[command_id]} if command_id else dict(sid_processes)

        stopped_count = 0
        for cid, process in targets.items():
            try:
                parent = psutil.Process(process.pid)
                children = parent.children(recursive=True)

                for child in children:
                    child.terminate()

                _, still_alive = psutil.wait_procs(children, timeout=3)
                for child in still_alive:
                    child.kill()

                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=3)

                    if process.poll() is None:
                        process.kill()

                sid_processes.pop(cid, None)
                update_history_entry(cid, status="stopped", ended_at=time.time())
                stopped_count += 1
            except Exception as e:
                socketio.emit("output", {"response": f"Error stopping process {cid}: {str(e)}"}, room=sid)
                log_message(f"Error stopping process {cid}: {str(e)}")

        if not sid_processes:
            processes.pop(sid, None)

        socketio.emit("output", {"response": f"Stopped {stopped_count} running command(s)."}, room=sid)

@socketio.on('recover_processes')
def recover_processes():
    """Attempt to recover running processes on startup."""
    log_message("Recovering running processes...")
    with process_lock:
        active_pids = {p.pid for p in psutil.process_iter(attrs=['pid'])}
        for sid, cmds in list(processes.items()):
            for command_id, process in list(cmds.items()):
                if process.pid not in active_pids:  # Process died
                    log_message(f"Removing dead process {command_id} ({sid})")
                    del cmds[command_id]
                else:
                    log_message(f"Process {command_id} ({process.pid}) is still running for {sid}")
            if not cmds:
                del processes[sid]

@socketio.on('list_processes')
def list_processes():
    """Return the list of running commands for this session (id, pid, command)."""
    with process_lock:
        sid_processes = processes.get(request.sid, {})
        running = [
            {"command_id": cid, "pid": process.pid}
            for cid, process in sid_processes.items() if process.poll() is None
        ]
        socketio.emit("output", {"response": f"Running processes: {running}"}, room=request.sid)

@app.route('/files', methods=['GET'])
def list_files():
    """Recursively list all files and directories inside /home."""
    def scan_directory(path):
        result = []
        for item in os.listdir(path):
            item_path = os.path.join(path, item)
            result.append({
                "name": item,
                "is_directory": os.path.isdir(item_path),
                "size": os.path.getsize(item_path),
                "last_modified": os.path.getmtime(item_path),
            })
        return result

    return jsonify(scan_directory(HOME_DIR))

@app.route('/files/<path:filepath>', methods=['GET'])
def get_file(filepath):
    """Retrieve the content of a file inside /home.

    Text files (source code, configs, logs, etc.) are returned as UTF-8 text.
    Anything that isn't valid UTF-8 (images, PDFs, archives, binaries, ...) is
    returned base64-encoded with is_binary: true, plus a guessed mimetype, so
    the frontend can render it (e.g. an <img> data URI) or offer a download
    instead of trying to display it as text.
    """
    file_path = safe_path(filepath)
    if not file_path or not os.path.exists(file_path) or os.path.isdir(file_path):
        return jsonify({"error": "File not found"}), 404

    mimetype, _ = mimetypes.guess_type(file_path)

    with open(file_path, "rb") as f:
        raw = f.read()

    try:
        content = raw.decode("utf-8")
        return jsonify({"content": content, "is_binary": False, "mimetype": mimetype or "text/plain"})
    except UnicodeDecodeError:
        encoded = base64.b64encode(raw).decode("ascii")
        return jsonify({
            "content": encoded,
            "is_binary": True,
            "mimetype": mimetype or "application/octet-stream",
            "size": len(raw)
        })


@app.route('/raw/<path:filepath>', methods=['GET'])
def get_raw_file(filepath):
    """Serve a file's raw bytes directly (no JSON/base64 wrapping) with the
    correct mimetype, so browsers can render images/PDFs/etc. inline, e.g.
    <img src="/raw/photo.png">. Use /download/<path> instead if you want it
    to always download rather than render in-browser."""
    file_path = safe_path(filepath)
    if not file_path or not os.path.exists(file_path) or os.path.isdir(file_path):
        return jsonify({"error": "File not found"}), 404

    return send_file(file_path, as_attachment=False)


@app.route('/edit', methods=['POST'])
def edit_file():
    """Edit an existing file or create a new one inside /home.

    Accepts any filename/extension (previously restricted to a hardcoded
    allowlist). "content" is written as UTF-8 text; for binary content, send
    base64 and set "is_binary": true.
    """
    filename = request.json.get("filename")
    content = request.json.get("content", "")
    is_binary = request.json.get("is_binary", False)
    file_path = safe_path(filename)

    if not file_path or not filename:
        return jsonify({"error": "Invalid filename"}), 400

    try:
        if is_binary:
            with open(file_path, "wb") as f:
                f.write(base64.b64decode(content))
        else:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"message": f"{filename} saved successfully"}), 200

@app.route('/check-password', methods=['POST'])
def check_password():
    data = request.get_json()
    client_password = data.get('password') if data else None

    if not client_password:
        return jsonify(success=False, message='No password provided'), 400

    is_valid = client_password == os.getenv('PASSWORD')

    if not is_valid:
        return jsonify(success=False, message='Incorrect password'), 401

    token = secrets.token_urlsafe(32)
    with tokens_lock:
        active_tokens[token] = {"created": time.time(), "last_seen": time.time()}

    response = jsonify(success=True, message='Password is correct')
    response.set_cookie(
        TOKEN_COOKIE_NAME,
        token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        max_age=TOKEN_TTL_SECONDS,
        path="/",
    )
    return response


@app.route('/logout', methods=['POST'])
def logout():
    """Revoke the current session token."""
    token = request.cookies.get(TOKEN_COOKIE_NAME)
    with tokens_lock:
        active_tokens.pop(token, None)
    response = jsonify({"message": "Logged out"})
    response.delete_cookie(TOKEN_COOKIE_NAME, path="/")
    return response


@app.route('/delete', methods=['POST'])
def delete_file_or_dir():
    """Deletes a file, empty directory, or non-empty directory."""
    filename = request.json.get("filename")
    if not filename:
        return jsonify({"error": "Missing filename"}), 400

    file_path = safe_path(filename)
    if not file_path or not os.path.exists(file_path):
        return jsonify({"error": "File or directory does not exist"}), 404

    try:
        if os.path.isdir(file_path):
            shutil.rmtree(file_path)  # Delete even if non-empty
            return jsonify({"message": f"Deleted directory: {filename}"}), 200
        else:
            os.remove(file_path)
            return jsonify({"message": f"Deleted file: {filename}"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/mkdir', methods=['POST'])
def create_directory():
    """Create a new subdirectory inside /home."""
    dirname = request.json.get("dirname")
    dir_path = safe_path(dirname)

    if not dir_path:
        return jsonify({"error": "Invalid directory name"}), 400

    os.makedirs(dir_path, exist_ok=True)
    return jsonify({"message": f"Directory '{dirname}' created successfully"}), 201

@app.route('/rename', methods=['POST'])
def rename_file():
    """Rename a file or directory inside /home."""
    old_name = request.json.get("old_name")
    new_name = request.json.get("new_name")

    old_path = safe_path(old_name)
    new_path = safe_path(new_name)

    if not old_path or not new_path:
        return jsonify({"error": "Invalid filename"}), 400

    if not os.path.exists(old_path):
        return jsonify({"error": "File not found"}), 404

    if os.path.exists(new_path):
        return jsonify({"error": "A file with the new name already exists"}), 409

    os.rename(old_path, new_path)
    return jsonify({"message": f"Renamed '{old_name}' to '{new_name}' successfully"}), 200

@app.route('/dir/<path:foldername>', methods=['GET'])
def list_directory(foldername):
    """List contents of a specific directory inside /home."""
    dir_path = safe_path(foldername)
    
    if not dir_path or not os.path.isdir(dir_path):
        return jsonify({"error": "Directory not found"}), 404

    return jsonify(scan_directory(dir_path))

def scan_directory(path):
    """Helper function to get files and directories inside a path."""
    result = []
    try:
        for item in os.listdir(path):
            item_path = os.path.join(path, item)
            result.append({
                "name": item,
                "is_directory": os.path.isdir(item_path),
                "size": os.path.getsize(item_path) if os.path.isfile(item_path) else None,
                "last_modified": os.path.getmtime(item_path)
            })
    except Exception as e:
        return {"error": str(e)}

    return result


@app.route('/breadcrumb', methods=['GET'])
@app.route('/breadcrumb/<path:foldername>', methods=['GET'])
def get_breadcrumb(foldername=""):
    """Return the path segments from home down to foldername, each with its
    own relative path, for building breadcrumb navigation.
    e.g. "projects/app/src" ->
    [{"name": "home", "path": ""}, {"name": "projects", "path": "projects"},
     {"name": "app", "path": "projects/app"}, {"name": "src", "path": "projects/app/src"}]
    """
    foldername = (foldername or "").strip("/")
    dir_path = safe_path(foldername) if foldername else HOME_DIR
    if not dir_path or not os.path.isdir(dir_path):
        return jsonify({"error": "Directory not found"}), 404

    crumbs = [{"name": "home", "path": ""}]
    if foldername:
        parts = foldername.split("/")
        accumulated = []
        for part in parts:
            accumulated.append(part)
            crumbs.append({"name": part, "path": "/".join(accumulated)})

    return jsonify({"breadcrumb": crumbs})


def _resolve_batch_paths(paths, base_path=""):
    """Resolve a list of relative names/paths (optionally under base_path) to
    safe absolute paths. Returns (resolved, invalid) where invalid holds the
    original strings that failed to resolve."""
    resolved, invalid = [], []
    for p in paths:
        full = safe_path(os.path.join(base_path, p)) if base_path else safe_path(p)
        if full and os.path.exists(full):
            resolved.append((p, full))
        else:
            invalid.append(p)
    return resolved, invalid


@app.route('/batch-delete', methods=['POST'])
def batch_delete():
    """Delete multiple files/folders at once.
    Body: {"paths": ["a.txt", "sub/b.txt", "folder"], "base_path": "optional/current/dir"}
    """
    data = request.json or {}
    paths = data.get("paths") or []
    base_path = (data.get("base_path") or "").strip()

    if not paths:
        return jsonify({"error": "No paths provided"}), 400

    resolved, invalid = _resolve_batch_paths(paths, base_path)
    deleted, failed = [], []

    for original, full in resolved:
        try:
            if os.path.isdir(full):
                shutil.rmtree(full)
            else:
                os.remove(full)
            deleted.append(original)
        except Exception as e:
            failed.append({"path": original, "error": str(e)})

    failed.extend({"path": p, "error": "Not found"} for p in invalid)

    return jsonify({"deleted": deleted, "failed": failed}), 200


@app.route('/batch-move', methods=['POST'])
def batch_move():
    """Move multiple files/folders into a destination folder.
    Body: {"paths": ["a.txt", "folder"], "base_path": "optional/current/dir", "destination": "target/folder"}
    """
    data = request.json or {}
    paths = data.get("paths") or []
    base_path = (data.get("base_path") or "").strip()
    destination = (data.get("destination") or "").strip()

    if not paths:
        return jsonify({"error": "No paths provided"}), 400

    dest_dir = safe_path(destination) if destination else HOME_DIR
    if not dest_dir or not os.path.isdir(dest_dir):
        return jsonify({"error": "Invalid destination"}), 400

    resolved, invalid = _resolve_batch_paths(paths, base_path)
    moved, failed = [], []

    for original, full in resolved:
        target = os.path.join(dest_dir, os.path.basename(full))
        if os.path.exists(target):
            failed.append({"path": original, "error": "A file with that name already exists at the destination"})
            continue
        try:
            shutil.move(full, target)
            moved.append(original)
        except Exception as e:
            failed.append({"path": original, "error": str(e)})

    failed.extend({"path": p, "error": "Not found"} for p in invalid)

    return jsonify({"moved": moved, "failed": failed}), 200


@app.route('/batch-copy', methods=['POST'])
def batch_copy():
    """Copy multiple files/folders into a destination folder.
    Body: {"paths": ["a.txt", "folder"], "base_path": "optional/current/dir", "destination": "target/folder"}
    """
    data = request.json or {}
    paths = data.get("paths") or []
    base_path = (data.get("base_path") or "").strip()
    destination = (data.get("destination") or "").strip()

    if not paths:
        return jsonify({"error": "No paths provided"}), 400

    dest_dir = safe_path(destination) if destination else HOME_DIR
    if not dest_dir or not os.path.isdir(dest_dir):
        return jsonify({"error": "Invalid destination"}), 400

    resolved, invalid = _resolve_batch_paths(paths, base_path)
    copied, failed = [], []

    for original, full in resolved:
        target = os.path.join(dest_dir, os.path.basename(full))
        if os.path.exists(target):
            failed.append({"path": original, "error": "A file with that name already exists at the destination"})
            continue
        try:
            if os.path.isdir(full):
                shutil.copytree(full, target)
            else:
                shutil.copy2(full, target)
            copied.append(original)
        except Exception as e:
            failed.append({"path": original, "error": str(e)})

    failed.extend({"path": p, "error": "Not found"} for p in invalid)

    return jsonify({"copied": copied, "failed": failed}), 200

@app.route('/unzip', methods=['POST'])
def unzip_file():
    """Extracts a zip file to the current directory user is in."""
    filename = request.json.get("filename")  # Name of the zip file
    path = request.json.get("path", "").strip()  # User's current path

    target_dir = safe_path(path) if path else HOME_DIR
    file_path = os.path.join(target_dir, os.path.basename(filename))  # Ensure it's inside target_dir

    if not file_path.endswith(".zip") or not os.path.exists(file_path):
        return jsonify({"error": "Invalid or missing zip file"}), 400

    if not os.path.isdir(target_dir):
        return jsonify({"error": "Invalid target directory"}), 400

    try:
        with zipfile.ZipFile(file_path, 'r') as zip_ref:
            zip_ref.extractall(target_dir)

        return jsonify({"message": f"Extracted {filename} to {target_dir}"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/create', methods=['POST'])
def create_file():
    """Create a new empty file inside /home."""
    filename = request.json.get("filename")
    file_path = safe_path(filename)

    if not file_path:
        return jsonify({"error": "Invalid filename"}), 400

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("")  # Create an empty file
        return jsonify({"message": f"{filename} created successfully"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/upload', methods=['POST'])
def upload_file():
    """Upload a single file in one request. Fine for small/medium files.
    For large files or progress tracking, use /upload-chunk + /upload-complete instead."""
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400

    path = request.form.get("path", "").strip()
    target_dir = safe_path(path) if path else HOME_DIR

    if not target_dir or not os.path.isdir(target_dir):
        return jsonify({"error": "Invalid target directory"}), 400

    file = request.files['file']
    filename = secure_filename(file.filename)
    if not filename:
        return jsonify({"error": "Invalid filename"}), 400

    file_path = os.path.join(target_dir, filename)

    # Stream to disk in chunks rather than holding it all in memory.
    try:
        with open(file_path, "wb") as out:
            while True:
                chunk = file.stream.read(CHUNK_SIZE_READ)
                if not chunk:
                    break
                out.write(chunk)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"message": f"{filename} uploaded successfully"}), 200


@app.route('/upload-chunk', methods=['POST'])
def upload_chunk():
    """Receive one chunk of a larger upload.

    Expects multipart/form-data with:
      - chunk: the binary chunk data
      - upload_id: a client-generated id unique to this upload (same for every chunk)
      - chunk_index: integer index of this chunk (0-based)
      - total_chunks: total number of chunks expected

    The client can compute progress as (chunks_sent / total_chunks) and, combined
    with an onUploadProgress/xhr.upload.onprogress on each chunk request, get
    fine-grained progress for very large files without buffering them fully
    in memory on the client or server.
    """
    if 'chunk' not in request.files:
        return jsonify({"error": "No chunk provided"}), 400

    upload_id = request.form.get("upload_id", "").strip()
    chunk_index = request.form.get("chunk_index")
    total_chunks = request.form.get("total_chunks")

    if not upload_id or chunk_index is None or total_chunks is None:
        return jsonify({"error": "Missing upload_id, chunk_index, or total_chunks"}), 400

    # Keep upload_id restricted to safe characters to avoid path issues
    if not all(c.isalnum() or c in "-_" for c in upload_id):
        return jsonify({"error": "Invalid upload_id"}), 400

    try:
        chunk_index = int(chunk_index)
    except ValueError:
        return jsonify({"error": "chunk_index must be an integer"}), 400

    session_dir = os.path.join(UPLOAD_TMP_DIR, upload_id)
    os.makedirs(session_dir, exist_ok=True)

    chunk_path = os.path.join(session_dir, f"{chunk_index:08d}.part")
    chunk_file = request.files['chunk']

    try:
        with open(chunk_path, "wb") as out:
            while True:
                data = chunk_file.stream.read(CHUNK_SIZE_READ)
                if not data:
                    break
                out.write(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "message": f"Chunk {chunk_index} received",
        "upload_id": upload_id
    }), 200


@app.route('/upload-complete', methods=['POST'])
def upload_complete():
    """Assemble previously uploaded chunks into the final file.

    Expects JSON body: {"upload_id": "...", "filename": "...", "path": "...", "total_chunks": N}
    """
    data = request.json or {}
    upload_id = data.get("upload_id", "").strip()
    filename = secure_filename(data.get("filename", ""))
    path = (data.get("path") or "").strip()
    total_chunks = data.get("total_chunks")

    if not upload_id or not filename or total_chunks is None:
        return jsonify({"error": "Missing upload_id, filename, or total_chunks"}), 400

    if not all(c.isalnum() or c in "-_" for c in upload_id):
        return jsonify({"error": "Invalid upload_id"}), 400

    session_dir = os.path.join(UPLOAD_TMP_DIR, upload_id)
    if not os.path.isdir(session_dir):
        return jsonify({"error": "Unknown upload_id (no chunks found)"}), 404

    target_dir = safe_path(path) if path else HOME_DIR
    if not target_dir or not os.path.isdir(target_dir):
        return jsonify({"error": "Invalid target directory"}), 400

    try:
        total_chunks = int(total_chunks)
    except ValueError:
        return jsonify({"error": "total_chunks must be an integer"}), 400

    final_path = os.path.join(target_dir, filename)

    try:
        with open(final_path, "wb") as out:
            for i in range(total_chunks):
                part_path = os.path.join(session_dir, f"{i:08d}.part")
                if not os.path.exists(part_path):
                    return jsonify({"error": f"Missing chunk {i}"}), 400
                with open(part_path, "rb") as part:
                    shutil.copyfileobj(part, out, length=CHUNK_SIZE_READ)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        shutil.rmtree(session_dir, ignore_errors=True)

    return jsonify({"message": f"{filename} uploaded successfully"}), 200

if __name__ == '__main__':
    socketio.run(app, host="0.0.0.0", port=7860)
