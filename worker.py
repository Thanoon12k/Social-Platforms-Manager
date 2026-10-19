"""Always-on worker: Telegram bot by long polling + the publishing clock, in one process.

    python worker.py

Use it wherever a process can stay running (a VPS, a home PC, Railway/Render, a
PythonAnywhere always-on task). It switches the bot from webhook to polling, so posts sent
to the bot and scheduled posts go out within seconds, with no cron needed.
Without it, the web app's webhook + a cron on /tick (see .github/workflows/tick.yml) do the same.
"""
import threading
import time

from spm import bot, engine, store
from spm.platforms.telegram import tg


def clock():
    """Publish whatever is due every 15 s (engine.run_due is lock-protected)."""
    while True:
        try:
            engine.run_due()
        except Exception as e:  # noqa: BLE001 - keep the worker alive
            store.log(f"worker: {e}", level="error")
        time.sleep(15)


def main():
    threading.Thread(target=clock, daemon=True).start()
    hooked, offset = None, None
    print("worker started", flush=True)
    while True:
        token = store.load(store.CFG, {}).get("telegram", {}).get("token")
        if not token:
            time.sleep(20)
            continue
        if hooked != token:
            tg(token, "deleteWebhook")
            store.patch("telegram", mode="polling")
            hooked = token
        r = tg(token, "getUpdates", http_timeout=70, timeout=50, offset=offset,
               allowed_updates=["message", "callback_query"])
        if not r.get("ok"):
            time.sleep(5)
            continue
        for u in r["result"]:
            offset = u["update_id"] + 1
            bot.handle(u)
            threading.Thread(target=engine.run_due, daemon=True).start()  # "publish now" goes out at once


if __name__ == "__main__":
    main()
