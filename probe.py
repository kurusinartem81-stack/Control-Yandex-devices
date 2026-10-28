"""Чтение состояния: python probe.py [--device pickle|mini2]"""

import asyncio
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from glagol import (
    account_login,
    connect,
    conversation_token,
    describe,
    load_config,
    make_envelope,
    pick_device,
)


def parse_args(argv):
    device = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--device", "-d"):
            i += 1
            if i >= len(argv):
                raise SystemExit("После --device нужно имя устройства")
            device = argv[i]
        elif a in ("--help", "-h"):
            raise SystemExit(__doc__)
        i += 1
    return device


async def main():
    cfg = load_config()
    dev = pick_device(cfg, parse_args(sys.argv[1:]))
    token = conversation_token(cfg, dev)

    print(f"Устройство: {describe(dev)}")
    login = account_login(cfg)
    if login:
        print(f"Аккаунт: {login}")

    try:
        ws = await connect(dev)
    except OSError as e:
        raise SystemExit(f"Колонка недоступна ({dev['host']}:1961): {e}")

    try:
        async with ws:
            _, text = make_envelope(token, {"command": "softwareVersion"})
            await ws.send(text)
            raw = await asyncio.wait_for(ws.recv(), timeout=8)
    except Exception as e:
        if "4000" in str(e) or "Invalid token" in str(e):
            raise SystemExit(
                "Колонка отвергла токен (4000 Invalid token).\n"
                "Проверьте, что music_token взят под аккаунтом-владельцем колонки,\n"
                "и что часы на колонке верные. Если токен верный — перезагрузите колонку."
            )
        raise

    d = json.loads(raw)
    print("Прошивка:", d.get("softwareVersion"))
    print("Статус:", d.get("status"))

    sf = d.get("supported_features") or []
    uf = d.get("unsupported_features") or []
    print(f"Поддерживается ({len(sf)}):")
    for x in sf:
        print("  +", x)
    if uf:
        print(f"Не поддерживается ({len(uf)}):")
        for x in uf:
            print("  -", x)

    st = d.get("state") or {}
    print("Состояние:", ", ".join(sorted(st)))
    print("Громкость:", st.get("volume"))
    ps = st.get("playerState") or {}
    if ps:
        print("Плеер:", ps.get("playerType"), "|", ps.get("title"))


asyncio.run(main())
