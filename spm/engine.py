"""Posts: create once, adapt per platform, publish one platform after another, report.

Life of a post: draft -> scheduled | queued -> publishing -> done | partial | failed (or cancelled).
run_due() is the only publisher. The worker, the /tick URL and the web app (right after a
"publish now") all call it; a second lock makes sure only one of them publishes at a time.
Each platform's result is saved as soon as it is known, so a crash never re-posts what
already went out: a platform that was mid-upload is marked "interrupted" for a manual retry.
"""
import fcntl
import os
import secrets
import shutil

from . import adapt, media, platforms, store
from .platforms.telegram import tg

STATUS = {"draft": "📝 مسودة", "scheduled": "🕒 مجدول", "queued": "⏳ بالطابور", "publishing": "🚀 جاري النشر",
          "done": "✅ انتشر بكل المنصات", "partial": "⚠️ انتشر جزئياً", "failed": "❌ فشل", "cancelled": "🚫 ملغي"}
KIND = {"text": "📝 نص", "image": "🖼 صورة", "video": "🎬 فيديو", "audio": "🎧 صوت"}
INTERRUPTED = "انقطع النشر بالنص، تأكد من المنصة وبعدين اضغط إعادة المحاولة"
DEFAULT_EXT = {"image": "jpg", "video": "mp4", "audio": "mp3"}


def create(text="", title="", link="", file_path=None, filename="", mime="", targets=None,
           source="dashboard", use_ai=False, chat=None):
    """Store a new draft (moving file_path into its media folder) with all its variants."""
    cfg = store.load(store.CFG, {})
    kind = None
    if file_path:
        kind = media.kind_of(filename or file_path, mime)
        if not kind:
            raise ValueError("نوع الملف غير مدعوم (صورة، فيديو، أو صوت فقط)")
    if not (text or "").strip() and not file_path:
        raise ValueError("المنشور فارغ")
    draft = {"text": (text or "").strip(), "title": (title or "").strip(), "link": (link or "").strip()}
    variants, note = adapt.adapt(cfg, draft, use_ai)
    with store.state() as (c, posts):
        pid = max([p["id"] for p in posts] + [0]) + 1
        ext = (filename.rsplit(".", 1)[-1].lower() if "." in (filename or "") else "") or DEFAULT_EXT.get(kind, "bin")
        p = dict(draft, id=pid, token=secrets.token_urlsafe(12), source=source, created=store.now().isoformat(),
                 media={"kind": kind, "file": f"original.{ext}"} if kind else {"kind": "text"},
                 targets=[k for k in (targets or platforms.connected(c)) if k in platforms.BY_KEY],
                 variants=variants, note=note, status="draft", results={}, chat=chat)
        posts.append(p)
    if kind:
        d = os.path.join(store.MEDIA, str(pid))
        os.makedirs(d, exist_ok=True)
        shutil.move(file_path, os.path.join(d, p["media"]["file"]))
    store.log(f"منشور جديد #{pid} ({KIND.get(p['media']['kind'])}) من {source}", pid=pid)
    return p


def update(pid, **fields):
    with store.state() as (_, posts):
        p = store.find(posts, pid)
        if p:
            p.update(fields)
        return p


def readapt(pid, use_ai=False):
    cfg, posts = store.read()
    p = store.find(posts, pid)
    variants, note = adapt.adapt(cfg, p, use_ai)
    return update(pid, variants=variants, note=note)


def schedule(pid, when=None):
    """Queue now (when is None or past) or at `when`. Returns the post or None if it can't move."""
    with store.state() as (_, posts):
        p = store.find(posts, pid)
        if not p or p["status"] in ("publishing", "done"):
            return None
        if not p["targets"]:
            return None
        p["results"] = {k: r for k, r in p["results"].items() if r.get("ok")}  # failed ones go again
        if when and when > store.now():
            p.update(status="scheduled", scheduled_at=when.isoformat())
        else:
            p.update(status="queued", scheduled_at=store.now().isoformat())
        return p


def retry(pid):
    """Queue again only the platforms that failed; the ones that worked are kept."""
    p = store.get_post(pid)
    return schedule(pid) if p and p["status"] in ("partial", "failed") else None


def cancel(pid):
    with store.state() as (_, posts):
        p = store.find(posts, pid)
        if p and p["status"] in ("draft", "scheduled", "queued"):
            p["status"] = "cancelled"
            return p
    return None


def delete(pid):
    with store.state() as (_, posts):
        p = store.find(posts, pid)
        if p and p["status"] != "publishing":
            posts.remove(p)
            shutil.rmtree(os.path.join(store.MEDIA, str(pid)), ignore_errors=True)
            return True
    return False


def due(posts):
    t = store.now().isoformat()
    for p in posts:
        if p["status"] in ("queued", "publishing") or (p["status"] == "scheduled" and p.get("scheduled_at", "") <= t):
            return p["id"]
    return None


def run_due(limit=10):
    """Publish every post that is due. Returns {"published": [ids]} or {"busy": True}."""
    with open(store.RUN_LOCK, "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"busy": True}
        done = []
        for _ in range(limit):
            pid = due(store.load(store.POSTS, []))
            if not pid:
                break
            publish(pid)
            done.append(pid)
        return {"published": done}


def ordered(targets):
    return [m.KEY for m in platforms.ORDER if m.KEY in targets]


def publish(pid):
    """Send one post to its platforms in order, saving and reporting each result as it lands."""
    with store.state() as (_, posts):
        p = store.find(posts, pid)
        for r in p["results"].values():
            if r.get("ok") is None:  # a previous run died during this upload
                r.update(ok=False, error=INTERRUPTED)
        p.update(status="publishing", started=store.now().isoformat())
    notify(pid)
    for k in ordered(p["targets"]):
        if k in store.get_post(pid)["results"]:
            continue
        with store.state() as (_, posts):
            store.find(posts, pid)["results"][k] = {"ok": None, "at": store.now().isoformat()}
        notify(pid)
        cfg = store.load(store.CFG, {})
        p = store.get_post(pid)
        mod = platforms.BY_KEY[k]
        s = cfg.get("settings", {})
        prepared = media.Prepared(p, s.get("base_url", ""), s.get("brand", ""))
        v = p["variants"].get(k) or adapt.rules(p)[k]
        if not mod.connected(cfg):
            res = {"ok": False, "error": "المنصة غير مربوطة"}
        else:
            try:
                res = mod.publish(cfg, p, v, prepared)
            except Exception as e:  # noqa: BLE001 - one platform failing must not stop the rest
                res = {"ok": False, "error": str(e)[:400]}
        res["at"] = store.now().isoformat()
        with store.state() as (_, posts):
            store.find(posts, pid)["results"][k] = res
        store.log(f"#{pid} {platforms.label(k)}: " + ("✅ " + (res.get("url") or "") if res["ok"] else "❌ " + res.get("error", "")),
                  level="info" if res["ok"] else "error", pid=pid)
        notify(pid)
    with store.state() as (_, posts):
        p = store.find(posts, pid)
        ok = [k for k in p["targets"] if p["results"].get(k, {}).get("ok")]
        p["status"] = "done" if len(ok) == len(p["targets"]) else ("partial" if ok else "failed")
        p["finished"] = store.now().isoformat()
    notify(pid)
    return p


# ---------- reporting to the owner on Telegram ----------

def line(k, r):
    if r is None:
        return f"{platforms.label(k)}: ⏸ بالانتظار"
    if r.get("ok") is None:
        return f"{platforms.label(k)}: ⏳ جاري النشر…"
    if r["ok"]:
        return f"{platforms.label(k)}: ✅ {r.get('url') or 'تم'}" + (f"\n   ℹ️ {r['note']}" if r.get("note") else "")
    return f"{platforms.label(k)}: ❌ {r.get('error', '')[:200]}"


def report(p):
    targets = ordered(p["targets"])
    ok = sum(1 for k in targets if p["results"].get(k, {}).get("ok"))
    head = {"done": f"🎉 انتشر في كل المنصات ({ok}/{len(targets)})",
            "partial": f"⚠️ انتشر في {ok} من {len(targets)} منصات",
            "failed": "❌ ما انتشر بأي منصة"}.get(p["status"], f"🚀 جاري النشر… ({ok}/{len(targets)})")
    body = "\n".join(line(k, p["results"].get(k)) for k in targets)
    title = (p.get("title") or p.get("text") or "")[:60].replace("\n", " ")
    return f"منشور #{p['id']} · {title}\n{head}\n\n{body}"


def notify(pid):
    cfg = store.load(store.CFG, {})
    t = cfg.get("telegram", {})
    owner = t.get("owner")
    p = store.get_post(pid)
    if not (t.get("token") and owner and p):
        return
    kb = {"inline_keyboard": [[{"text": "🔁 أعد المحاولة للفاشلة", "callback_data": f"rt:{pid}"}]]} \
        if p["status"] in ("partial", "failed") else None
    text = report(p)[:4096]
    if p.get("tg_progress"):
        r = tg(t["token"], "editMessageText", chat_id=owner, message_id=p["tg_progress"], text=text,
               reply_markup=kb or {"inline_keyboard": []}, disable_web_page_preview=True)
        if r.get("ok") or "not modified" in r.get("description", ""):
            return
    r = tg(t["token"], "sendMessage", chat_id=owner, text=text, reply_markup=kb, disable_web_page_preview=True)
    if r.get("ok"):
        update(pid, tg_progress=r["result"]["message_id"])
