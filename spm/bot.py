"""The control bot: send it a post (text, photo, video, music or voice note), get back a preview
with buttons, publish everywhere in one tap, and watch every platform report back.

Only the owner can use it: the first /start must carry the one-time code shown on the
dashboard (the dashboard's "open the bot" link does that).
"""
import os
import secrets
from datetime import datetime, timedelta

import requests

from . import adapt, engine, platforms, store
from .platforms.telegram import tg

MAX_DOWNLOAD = 20 * 1024 * 1024  # Bot API getFile limit
MENU = {"keyboard": [[{"text": "📝 منشور جديد"}, {"text": "📊 حالة المنصات"}],
                     [{"text": "🗂 آخر المنشورات"}, {"text": "❓ مساعدة"}]], "resize_keyboard": True}
HELP = """أهلاً 👋 هذا بوت النشر التلقائي.

ارسل أي شي تريد تنشره:
• نص
• صورة / فيديو / ملف صوت (موسيقى) / فويس، ويا وصف (caption)

أرجعلك معاينة، وبضغطة «🚀 انشر الآن» ينتشر بكل المنصات المربوطة وحدة وحدة،
وأخبرك بكل منصة شنو صار وبالنهاية: «🎉 انتشر في كل المنصات».

الأوامر: /status حالة المنصات · /last آخر المنشورات · /cancel إلغاء الخطوة الحالية
حد التحميل من تيليجرام 20MB؛ الملفات الأكبر ارفعها من الداشبورد."""


def cfg_tg():
    return store.load(store.CFG, {}).get("telegram", {})


def say(text, **kw):
    t = cfg_tg()
    return tg(t.get("token"), "sendMessage", chat_id=t.get("owner"), text=text[:4096],
              disable_web_page_preview=True, **kw)


def set_await(what=None, pid=None):
    with store.state() as (cfg, _):
        store.section(cfg, "telegram")["await"] = {"what": what, "pid": pid} if what else None


# ---------- keyboards ----------

def main_kb(p):
    cfg = store.load(store.CFG, {})
    pid, n = p["id"], len(p["targets"])
    rows = [[{"text": f"🚀 انشر الآن بكل المنصات ({n})", "callback_data": f"pub:{pid}"}],
            [{"text": "🕒 جدولة", "callback_data": f"sch:{pid}"}, {"text": "🎯 المنصات", "callback_data": f"tg:{pid}"}],
            [{"text": "👁 معاينة كل منصة", "callback_data": f"pv:{pid}"}, {"text": "✏️ تعديل النص", "callback_data": f"ed:{pid}"}]]
    if adapt.provider(cfg):
        rows.append([{"text": "✨ صياغة لكل منصة بالذكاء الاصطناعي", "callback_data": f"ai:{pid}"}])
    rows.append([{"text": "❌ إلغاء", "callback_data": f"no:{pid}"}])
    return {"inline_keyboard": rows}


def targets_kb(p):
    cfg = store.load(store.CFG, {})
    rows = [[{"text": ("✅ " if k in p["targets"] else "⬜ ") + platforms.label(k), "callback_data": f"tgl:{p['id']}:{k}"}]
            for k in platforms.connected(cfg)]
    rows.append([{"text": "⬅️ رجوع", "callback_data": f"back:{p['id']}"}])
    return {"inline_keyboard": rows}


def time_kb(pid):
    n = store.now()
    opts = [("بعد ساعة", n + timedelta(hours=1)), ("بعد 3 ساعات", n + timedelta(hours=3))]
    for h in (12, 18, 21):
        t = n.replace(hour=h, minute=0, second=0, microsecond=0)
        opts.append((("اليوم " if t > n else "باجر ") + f"{h}:00", t if t > n else t + timedelta(days=1)))
    opts.append(("باجر 10:00", (n + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)))
    rows = [[{"text": a, "callback_data": f"at:{pid}:{int(b.timestamp())}"} for a, b in opts[i:i + 3]] for i in (0, 3)]
    rows.append([{"text": "✍️ أكتب الوقت", "callback_data": f"typ:{pid}"}, {"text": "⬅️ رجوع", "callback_data": f"back:{pid}"}])
    return {"inline_keyboard": rows}


def preview_text(p):
    names = "، ".join(platforms.label(k) for k in engine.ordered(p["targets"])) or "ولا منصة (اربط منصة من الداشبورد)"
    when = f"\n🕒 موعد النشر: {store.fmt(p.get('scheduled_at'))}" if p["status"] == "scheduled" else ""
    body = (p.get("text") or "")[:1500]
    return (f"📝 منشور #{p['id']} · {engine.KIND.get(p['media']['kind'])} · {engine.STATUS[p['status']]}{when}\n"
            f"المنصات: {names}\n" + (f"{p['note']}\n" if p.get("note") else "") + f"━━━━━━━━\n{body}")[:4096]


def send_preview(p):
    r = say(preview_text(p), reply_markup=main_kb(p))
    if r.get("ok"):
        engine.update(p["id"], tg_preview=r["result"]["message_id"])


def refresh(p, kb=None):
    t = cfg_tg()
    if not p.get("tg_preview"):
        return send_preview(p)
    keep = p["status"] in ("draft", "scheduled")
    tg(t["token"], "editMessageText", chat_id=t["owner"], message_id=p["tg_preview"], text=preview_text(p),
       disable_web_page_preview=True, reply_markup=kb or (main_kb(p) if keep else {"inline_keyboard": []}))


# ---------- incoming content ----------

def pick_file(msg):
    """(file object, filename, mime) of the media in a message, or None."""
    if msg.get("photo"):
        return msg["photo"][-1], "photo.jpg", "image/jpeg"
    for field, name, mime in (("video", "video.mp4", "video/mp4"), ("animation", "animation.mp4", "video/mp4"),
                              ("video_note", "note.mp4", "video/mp4"), ("audio", "audio.mp3", "audio/mpeg"),
                              ("voice", "voice.ogg", "audio/ogg"), ("document", "file", "")):
        if msg.get(field):
            f = msg[field]
            return f, f.get("file_name") or name, f.get("mime_type") or mime
    return None


def download(file_id):
    t = cfg_tg()
    info = tg(t["token"], "getFile", file_id=file_id)
    if not info.get("ok"):
        raise RuntimeError(info.get("description", "getFile failed"))
    tmp = os.path.join(store.MEDIA, "tmp")
    os.makedirs(tmp, exist_ok=True)
    out = os.path.join(tmp, secrets.token_hex(8))
    with requests.get(f"https://api.telegram.org/file/bot{t['token']}/{info['result']['file_path']}",
                      stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(out, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
    return out


def new_post(msg):
    text = msg.get("text") or msg.get("caption") or ""
    got = pick_file(msg)
    path = name = mime = None
    if got:
        f, name, mime = got
        if f.get("file_size", 0) > MAX_DOWNLOAD:
            return say("⚠️ الملف أكبر من 20MB وتيليجرام ما يسمح للبوت ينزّله. ارفعه من الداشبورد.")
        say("⬇️ دا أنزّل الملف…")
        try:
            path = download(f["file_id"])
        except Exception as e:  # noqa: BLE001
            return say(f"❌ ما كدرت أنزّل الملف: {e}")
    try:
        p = engine.create(text=text, file_path=path, filename=name or "", mime=mime or "", source="telegram")
    except ValueError as e:
        if path and os.path.exists(path):
            os.remove(path)
        return say(f"⚠️ {e}")
    if msg.get("media_group_id"):
        say("ℹ️ ارسلت ألبوم؛ أخذت أول ملف بس. كل منشور ملف واحد.")
    send_preview(p)


# ---------- commands ----------

def status():
    cfg = store.load(store.CFG, {})
    lines = []
    for k, (ok, info) in platforms.check_all(cfg).items():
        mark = "⚪" if ok is None else ("🟢" if ok else "🔴")
        lines.append(f"{mark} {platforms.label(k)}: {info}")
    say("📊 حالة المنصات\n\n" + "\n".join(lines))


def last():
    posts = store.load(store.POSTS, [])[-8:]
    if not posts:
        return say("ما كو منشورات بعد.")
    out = []
    for p in reversed(posts):
        ok = sum(1 for r in p["results"].values() if r.get("ok"))
        title = (p.get("title") or p.get("text") or engine.KIND.get(p["media"]["kind"]))[:40].replace("\n", " ")
        out.append(f"#{p['id']} {engine.STATUS[p['status']]} · {ok}/{len(p['targets'])} · {title}")
    say("🗂 آخر المنشورات\n\n" + "\n".join(out))


def on_message(msg):
    t = cfg_tg()
    chat, text = str(msg["chat"]["id"]), (msg.get("text") or "").strip()
    if not t.get("owner"):
        code = text.split(maxsplit=1)[1] if text.startswith("/start ") else ""
        if t.get("claim") and code and secrets.compare_digest(code, t["claim"]):
            with store.state() as (cfg, _):
                store.section(cfg, "telegram").update(owner=chat, claim=None)
            return say("✅ صار هذا البوت مالك. " + HELP, reply_markup=MENU)
        return tg(t.get("token"), "sendMessage", chat_id=chat, text="🔒 هذا البوت خاص. افتحه من رابط الداشبورد.")
    if chat != str(t["owner"]):
        return None
    if text in ("/start", "/help", "/menu", "❓ مساعدة") or text.startswith("/start "):
        return say(HELP, reply_markup=MENU)
    if text in ("/status", "📊 حالة المنصات"):
        return status()
    if text in ("/last", "🗂 آخر المنشورات"):
        return last()
    if text in ("/new", "📝 منشور جديد"):
        set_await()
        return say("ارسل النص، أو صورة/فيديو/صوت ويا الوصف 👇")
    if text == "/cancel":
        set_await()
        return say("تمام، لغيت الخطوة.", reply_markup=MENU)
    wait = t.get("await") or {}
    if wait.get("what") and text:
        set_await()
        p = store.get_post(wait["pid"])
        if not p:
            return say("المنشور ما موجود.")
        if wait["what"] == "edit":
            engine.update(p["id"], text=text)
            p = engine.readapt(p["id"])
            say("✏️ انحفظ النص الجديد وانعادت الصياغة لكل منصة.")
            return send_preview(p)
        if wait["what"] == "time":
            when = store.parse_time(text)
            if not when:
                set_await("time", p["id"])
                return say("ما فهمت الوقت. اكتب مثل 21:30 أو 2026-10-20 18:00")
            return scheduled(p["id"], when)
    return new_post(msg)


def scheduled(pid, when):
    p = engine.schedule(pid, when)
    if not p:
        return say("ما ينفع أجدول هذا المنشور (اختار منصة وحدة على الأقل).")
    refresh(p)
    if p["status"] == "queued":
        return say("🚀 الوقت فات، راح ينتشر هسه.")
    return say(f"🕒 مجدول: {store.fmt(p['scheduled_at'])}")


def on_callback(cq):
    t = cfg_tg()
    if str(cq["from"]["id"]) != str(t.get("owner")):
        return None
    tg(t["token"], "answerCallbackQuery", callback_query_id=cq["id"])
    action, _, rest = cq.get("data", "").partition(":")
    pid_s, _, arg = rest.partition(":")
    p = store.get_post(int(pid_s)) if pid_s.isdigit() else None
    if not p:
        return say("المنشور ما موجود.")
    pid = p["id"]
    if action == "pub":
        if not engine.schedule(pid):
            return say("ما ينفع أنشره (اختار منصة وحدة على الأقل، أو هو منشور سابقاً).")
        return refresh(store.get_post(pid))
    if action == "sch":
        return refresh(p, time_kb(pid))
    if action == "at":
        return scheduled(pid, datetime.fromtimestamp(int(arg), store.tz()))
    if action == "typ":
        set_await("time", pid)
        return say("اكتب وقت النشر: 21:30 أو 2026-10-20 18:00")
    if action == "tg":
        return refresh(p, targets_kb(p))
    if action == "tgl":
        targets = [k for k in p["targets"] if k != arg] if arg in p["targets"] else p["targets"] + [arg]
        p = engine.update(pid, targets=engine.ordered(targets))
        return refresh(p, targets_kb(p))
    if action == "back":
        return refresh(p)
    if action == "pv":
        for k in engine.ordered(p["targets"]):
            v = p["variants"].get(k) or adapt.rules(p)[k]
            head = f"{platforms.label(k)}" + (f" · {v['title']}" if v.get("title") else "")
            say(f"{head}\n━━━━━━━━\n{v['text'][:3800]}")
        return None
    if action == "ed":
        set_await("edit", pid)
        return say("ارسل النص الجديد للمنشور 👇 (أو /cancel)")
    if action == "ai":
        say("✨ دا أعيد الصياغة لكل منصة…")
        p = engine.readapt(pid, use_ai=True)
        return send_preview(p)
    if action == "no":
        if engine.cancel(pid):
            return refresh(store.get_post(pid))
        return say("ما ينفع ألغيه هسه.")
    if action == "rt":
        if engine.retry(pid):
            return say(f"🔁 دا أعيد النشر للمنصات اللي فشلت بمنشور #{pid}")
        return say("ما كو شي أعيده.")
    return None


def handle(update):
    """Process one Telegram update (webhook or polling), skipping ones already seen."""
    uid = update.get("update_id", 0)
    with store.state() as (cfg, _):
        t = store.section(cfg, "telegram")
        if uid and uid <= t.get("last_update", 0):
            return
        t["last_update"] = uid
    try:
        if "callback_query" in update:
            on_callback(update["callback_query"])
        elif "message" in update:
            on_message(update["message"])
    except Exception as e:  # noqa: BLE001 - tell the owner instead of failing silently
        store.log(f"bot error: {e}", level="error")
        say(f"⚠️ صار خطأ: {str(e)[:300]}")
