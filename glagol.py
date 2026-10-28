"""Общие функции: конфиг, conversationToken, подключение к локальному Glagol."""

import base64
import hashlib
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

if getattr(sys, "frozen", False):
    # В собранном .exe конфиг лежит рядом с exe, а не во временной папке.
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CACHE_PATH = os.path.join(BASE_DIR, ".glagol_token_cache.json")
UA = "Mozilla/5.0"


def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit(f"Нет файла конфигурации: {CONFIG_PATH}")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def pick_device(cfg, name=None):
    devices = cfg.get("devices") or {}
    if not devices:
        raise SystemExit("В config.json нет раздела devices")
    key = name or cfg.get("default_device") or next(iter(devices))
    if key not in devices:
        raise SystemExit(
            f"В config.json нет устройства '{key}'. Доступны: {', '.join(devices)}"
        )
    dev = dict(devices[key])
    dev.setdefault("key", key)
    return dev


def _cache_key(cfg, dev):
    music = (cfg.get("music_token") or os.environ.get("GLAGOL_MUSIC") or "").strip()
    raw = f"{music}|{dev['device_id']}|{dev['platform']}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _token_exp(token):
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return int(json.loads(base64.urlsafe_b64decode(part)).get("exp", 0))
    except Exception:
        return 0


def _load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _restrict(path):
    """Оставить доступ к файлу только текущему пользователю (Windows)."""
    if os.name != "nt":
        return
    user = os.environ.get("USERNAME")
    domain = os.environ.get("USERDOMAIN")
    if not user:
        return
    who = f"{domain}\\{user}" if domain else user
    try:
        subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", f"{who}:F"],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


def _save_cache(cache):
    try:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, CACHE_PATH)
        _restrict(CACHE_PATH)
    except Exception:
        pass


def conversation_token(cfg, dev):
    music = (cfg.get("music_token") or os.environ.get("GLAGOL_MUSIC") or "").strip()
    if not music:
        raise SystemExit(
            "В config.json не заполнен music_token (или задайте переменную GLAGOL_MUSIC)"
        )

    key = _cache_key(cfg, dev)
    cache = _load_cache()
    cached = cache.get(key) or {}
    token = cached.get("token") or ""
    if token and _token_exp(token) - time.time() > 600:
        return token

    query = urllib.parse.urlencode(
        {"device_id": dev["device_id"], "platform": dev["platform"]}
    )
    req = urllib.request.Request(
        f"https://quasar.yandex.net/glagol/token?{query}",
        headers={"Authorization": f"OAuth {music}", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SystemExit(
                "Яндекс не принял music_token (AUTH_TOKEN_INVALID). "
                "Получите новый токен и обновите config.json"
            )
        if e.code == 429:
            raise SystemExit(
                "Яндекс временно ограничил выдачу токенов (HTTP 429).\n"
                "Обычно это проходит за 10–15 минут. Подождите и повторите команду\n"
                "один раз — частые повторы только продлевают блокировку."
            )
        raise SystemExit(f"quasar.yandex.net: HTTP {e.code}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Нет связи с quasar.yandex.net: {e.reason}")
    if data.get("status") != "ok" or not data.get("token"):
        raise SystemExit(f"Не удалось получить conversationToken: {data}")

    token = data["token"]
    cache[key] = {"token": token, "exp": _token_exp(token)}
    _save_cache(cache)
    return token


def account_login(cfg):
    """Логин аккаунта, которому принадлежит music_token (для диагностики)."""
    music = (cfg.get("music_token") or os.environ.get("GLAGOL_MUSIC") or "").strip()
    if not music:
        return None
    req = urllib.request.Request(
        "https://login.yandex.ru/info?format=json",
        headers={"Authorization": f"OAuth {music}", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
    except Exception:
        return None
    login = data.get("login") or data.get("default_email") or ""
    if "@" in login:
        name, domain = login.split("@", 1)
        return f"{name[:3]}***@{domain}"
    return (login[:3] + "***") if login else None


def make_envelope(token, payload):
    rid = str(uuid.uuid4())
    text = json.dumps(
        {
            "conversationToken": token,
            "id": rid,
            "payload": payload,
            "sentTime": int(time.time() * 1000),
        }
    )
    return rid, text


async def connect(dev):
    try:
        import websockets
    except ImportError:
        raise SystemExit("Нет библиотеки websockets: python -m pip install websockets")

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return await websockets.connect(
        f"wss://{dev['host']}:1961", ssl=ctx, open_timeout=8
    )


def describe(dev):
    return f"{dev.get('name', dev['key'])} ({dev['host']}, {dev['platform']})"
