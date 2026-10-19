"""Telegram channel. The control bot posts there too, so it must be an admin of the channel."""
import json

import requests

KEY, NAME, ICON = "telegram", "قناة تيليجرام", "✈️"
API = "https://api.telegram.org/bot{}/{}"


def tg(token, method, files=None, http_timeout=60, **params):
    if not token:
        return {"ok": False, "description": "no bot token"}
    data = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
            for k, v in params.items() if v is not None}
    try:
        return requests.post(API.format(token, method), data=data, files=files, timeout=http_timeout).json()
    except (requests.RequestException, ValueError) as e:
        return {"ok": False, "description": str(e).replace(token, "<token>")}


def connected(cfg):
    t = cfg.get("telegram", {})
    return bool(t.get("token") and t.get("channel"))


def check(cfg):
    t = cfg.get("telegram", {})
    chat = tg(t.get("token"), "getChat", chat_id=t.get("channel"))
    if not chat.get("ok"):
        return False, chat.get("description", "")
    me = tg(t["token"], "getMe").get("result", {})
    member = tg(t["token"], "getChatMember", chat_id=t["channel"], user_id=me.get("id")).get("result", {})
    title = chat["result"].get("title", t["channel"])
    if member.get("status") not in ("administrator", "creator"):
        return False, f"{title}: البوت مو أدمن بالقناة"
    return True, title


def link(channel, msg_id, chat):
    username = chat.get("username") or (channel[1:] if str(channel).startswith("@") else "")
    if username:
        return f"https://t.me/{username}/{msg_id}"
    cid = str(chat.get("id", channel))
    return f"https://t.me/c/{cid[4:]}/{msg_id}" if cid.startswith("-100") else ""


def publish(cfg, post, v, media):
    t = cfg["telegram"]
    token, chan, text = t["token"], t["channel"], v["text"]
    send = {"image": ("sendPhoto", "photo", media.original), "video": ("sendVideo", "video", media.original),
            "audio": ("sendAudio", "audio", media.original)}.get(media.kind)
    if send:
        method, field, path = send
        with open(path, "rb") as f:
            cap = text if len(text) <= 1024 else None
            r = tg(token, method, files={field: f}, http_timeout=300, chat_id=chan, caption=cap,
                   supports_streaming=True if field == "video" else None)
        if r.get("ok") and cap is None:
            tg(token, "sendMessage", chat_id=chan, text=text[:4096])
    else:
        r = {"ok": True}
        for i in range(0, max(len(text), 1), 4096):
            r = tg(token, "sendMessage", chat_id=chan, text=text[i:i + 4096])
            if not r.get("ok"):
                break
    if not r.get("ok"):
        return {"ok": False, "error": r.get("description", "")}
    res = r["result"]
    return {"ok": True, "id": str(res["message_id"]), "url": link(chan, res["message_id"], res.get("chat", {}))}
