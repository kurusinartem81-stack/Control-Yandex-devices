"""Установка громкости: python set_volume.py [0.0..1.0] [--device pickle|mini2]"""

import asyncio
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from glagol import connect, conversation_token, describe, load_config, make_envelope, pick_device


def parse_args(argv):
    level = None
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
        else:
            level = float(a.replace(",", "."))
        i += 1
    return level, device


async def main():
    level, device_name = parse_args(sys.argv[1:])
    cfg = load_config()
    dev = pick_device(cfg, device_name)

    if level is None:
        level = float(cfg.get("volume", 0.35))
    level = max(0.0, min(1.0, level))

    token = conversation_token(cfg, dev)
    print(f"Устройство: {describe(dev)}")
    print(f"Ставлю громкость: {round(level * 100)}%")

    try:
        ws = await connect(dev)
    except OSError as e:
        raise SystemExit(f"Колонка недоступна ({dev['host']}:1961): {e}")

    async with ws:
        rid, text = make_envelope(token, {"command": "setVolume", "volume": level})
        await ws.send(text)

        for _ in range(8):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=4)
            except asyncio.TimeoutError:
                break
            d = json.loads(raw)
            if d.get("requestId") == rid:
                print("Ответ:", d.get("status"), d.get("error", ""))
            state = d.get("state")
            if isinstance(state, dict) and "volume" in state:
                if abs(float(state["volume"]) - level) < 0.02:
                    print(f"Подтверждено: volume = {state['volume']}")
                    return
        print("Команда отправлена, но подтверждение не пришло (проверьте колонку)")


asyncio.run(main())
