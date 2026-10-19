"""Social Platforms Manager: one post, every platform.

Web side of the project: the admin dashboard (compose, posts, accounts, log), the OAuth
callbacks for LinkedIn and TikTok, the Telegram webhook, the public blog and the public
media URLs that Instagram and TikTok download from. Publishing itself is in spm/engine.py.
"""
import os
import secrets
import time
from datetime import datetime
from email.utils import format_datetime
from functools import wraps
from urllib.parse import urlparse

from flask import (Flask, Response, abort, flash, redirect, render_template, request, send_file, session,
                   url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from spm import adapt, bot, engine, media, platforms, store
from spm.platforms import facebook, linkedin, tiktok, youtube
from spm.platforms.telegram import tg

app = Flask(__name__)
with store.state() as (_cfg, _):
    app.secret_key = store.settings(_cfg)["secret_key"]
app.config.update(SESSION_COOKIE_NAME="spm_session", SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("SPM_INSECURE_COOKIE") != "1",
                  MAX_CONTENT_LENGTH=int(os.environ.get("SPM_MAX_UPLOAD_MB", "500")) * 1024 * 1024)


# ---------- auth ----------

def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def check_csrf():
    if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "")):
        abort(400)


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("admin"):
            return redirect(url_for("login", next=request.path))
        remember_base_url()
        return f(*a, **k)
    return w


def remember_base_url():
    """The public address of the site, needed for media links and OAuth redirects."""
    url = os.environ.get("PUBLIC_URL") or request.host_url
    host = urlparse(url).hostname or ""
    if host not in ("localhost", "127.0.0.1") and url.startswith("http://"):
        url = "https://" + url[7:]
    url = url.rstrip("/")
    cfg = store.load(store.CFG, {})
    s = cfg.get("settings", {})
    if not s.get("base_url_fixed") and s.get("base_url") != url:
        store.patch("settings", base_url=url)


@app.context_processor
def inject():
    return {"csrf": csrf_token(), "label": platforms.label, "fmt": store.fmt, "STATUS": engine.STATUS,
            "KIND": engine.KIND, "ts": lambda t: datetime.fromtimestamp(t, store.tz()).strftime("%Y-%m-%d"),
            "rfc822": lambda iso: format_datetime(datetime.fromisoformat(iso))}


@app.route("/login", methods=["GET", "POST"])
def login():
    cfg = store.load(store.CFG, {})
    first = not cfg.get("settings", {}).get("password")
    if request.method == "POST":
        check_csrf()
        pw = request.form.get("password", "")
        if first:
            if len(pw) < 8:
                flash("كلمة السر لازم 8 أحرف أو أكثر")
                return render_template("login.html", first=first)
            store.patch("settings", password=generate_password_hash(pw))
        elif not check_password_hash(cfg["settings"]["password"], pw):
            time.sleep(1)
            flash("كلمة السر غلط")
            return render_template("login.html", first=first)
        session["admin"] = True
        nxt = request.args.get("next", "/")
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
    return render_template("login.html", first=first)


@app.route("/logout", methods=["POST"])
def logout():
    check_csrf()
    session.pop("admin", None)
    return redirect(url_for("login"))


# ---------- dashboard ----------

@app.route("/")
@login_required
def dashboard():
    cfg, posts = store.read()
    counts = {k: sum(1 for p in posts if p["status"] == k) for k in engine.STATUS}
    sent = sum(1 for p in posts for r in p["results"].values() if r.get("ok"))
    return render_template("dashboard.html", cfg=cfg, posts=list(reversed(posts))[:40], counts=counts, sent=sent,
                           connected=platforms.connected(cfg), order=platforms.ORDER, ai=bool(adapt.provider(cfg)),
                           log=list(reversed(store.load(store.LOG, [])))[:12], now=store.now())


def tmp_upload(f):
    tmp = os.path.join(store.MEDIA, "tmp")
    os.makedirs(tmp, exist_ok=True)
    path = os.path.join(tmp, secrets.token_hex(8))
    f.save(path)
    return path


def when_from_form():
    return store.parse_time(request.form.get("when", ""))


def after_response_publish(resp):
    """Publish right after the response is sent, so the browser does not wait for uploads."""
    resp.call_on_close(engine.run_due)
    return resp


@app.route("/compose", methods=["POST"])
@login_required
def compose():
    check_csrf()
    f = request.form
    up = request.files.get("file")
    path = tmp_upload(up) if up and up.filename else None
    try:
        p = engine.create(text=f.get("text", ""), title=f.get("title", ""), link=f.get("link", ""), file_path=path,
                          filename=secure_filename(up.filename) if path else "", mime=up.mimetype if path else "",
                          targets=f.getlist("targets"), use_ai=bool(f.get("ai")))
    except ValueError as e:
        if path and os.path.exists(path):
            os.remove(path)
        flash(str(e))
        return redirect(url_for("dashboard"))
    return post_action(p["id"], f.get("action", "draft"))


def post_action(pid, action):
    if action == "publish":
        flash("🚀 بدأ النشر. الصفحة تتحدث وحدها، والنتيجة توصلك على تيليجرام." if engine.schedule(pid)
              else "ما ينفع أنشره: اختار منصة وحدة على الأقل")
        return after_response_publish(redirect(url_for("post_page", pid=pid)))
    if action == "schedule":
        when = when_from_form()
        if not when:
            flash("اكتب وقت النشر")
        elif engine.schedule(pid, when):
            flash(f"🕒 مجدول: {store.fmt(when.isoformat())}")
        else:
            flash("ما ينفع أجدوله")
        return after_response_publish(redirect(url_for("post_page", pid=pid)))
    return redirect(url_for("post_page", pid=pid))


@app.route("/post/<int:pid>", methods=["GET", "POST"])
@login_required
def post_page(pid):
    p = store.get_post(pid)
    if not p:
        abort(404)
    if request.method == "POST":
        check_csrf()
        f, action = request.form, request.form.get("action", "save")
        if action in ("save", "publish", "schedule") and p["status"] not in ("publishing", "done"):
            variants = dict(p["variants"])
            for k in platforms.BY_KEY:
                if f"v_{k}" in f:
                    variants[k] = dict(variants.get(k) or {}, text=f[f"v_{k}"])
                if f"t_{k}" in f:
                    variants.setdefault(k, {})["title"] = f[f"t_{k}"]
            engine.update(pid, variants=variants, targets=engine.ordered(f.getlist("targets")))
            if action == "save":
                flash("انحفظ")
                return redirect(url_for("post_page", pid=pid))
            return post_action(pid, action)
        if action == "readapt":
            engine.update(pid, text=f.get("text", p["text"]), title=f.get("title", p.get("title", "")),
                          link=f.get("link", p.get("link", "")))
            engine.readapt(pid, use_ai=bool(f.get("ai")))
            flash("انعادت الصياغة لكل منصة")
        elif action == "retry":
            flash("🔁 دا أعيد النشر للفاشلة" if engine.retry(pid) else "ما كو شي أعيده")
            return after_response_publish(redirect(url_for("post_page", pid=pid)))
        elif action == "cancel":
            flash("🚫 انلغى" if engine.cancel(pid) else "ما ينفع ألغيه")
        elif action == "delete":
            if engine.delete(pid):
                flash(f"انحذف #{pid}")
                return redirect(url_for("dashboard"))
            flash("ما ينفع أحذفه أثناء النشر")
        return redirect(url_for("post_page", pid=pid))
    cfg = store.load(store.CFG, {})
    variants = {k: p["variants"].get(k) or adapt.rules(p)[k] for k in platforms.BY_KEY}
    return render_template("post.html", p=p, cfg=cfg, variants=variants, order=platforms.ORDER,
                           connected=platforms.connected(cfg), ai=bool(adapt.provider(cfg)),
                           ordered=engine.ordered(p["targets"]), limits=adapt.LIMITS,
                           media_url=media_url(p))


def media_url(p):
    f = (p.get("media") or {}).get("file")
    return url_for("public_media", pid=p["id"], token=p["token"], name=f) if f else None


@app.route("/log")
@login_required
def log_page():
    return render_template("log.html", log=list(reversed(store.load(store.LOG, []))))


# ---------- accounts ----------

def keep(sec, f, *names):
    """Copy the non-empty form fields into a config section (empty secret fields keep the old value)."""
    for n in names:
        v = f.get(n, "").strip()
        if v:
            sec[n] = v


@app.route("/accounts", methods=["GET", "POST"])
@login_required
def accounts():
    if request.method == "POST":
        check_csrf()
        save_account(request.form)
        return redirect(url_for("accounts") + "#" + request.form.get("section", ""))
    cfg = store.load(store.CFG, {})
    checks = platforms.check_all(cfg) if request.args.get("check") else {}
    base = cfg.get("settings", {}).get("base_url", "")
    t = cfg.get("telegram", {})
    claim_link = f"https://t.me/{t['bot']}?start={t['claim']}" if t.get("bot") and t.get("claim") else ""
    return render_template("accounts.html", cfg=cfg, checks=checks, base=base, order=platforms.ORDER,
                           claim_link=claim_link, connected=platforms.connected(cfg))


def telegram_connect(cfg, f):
    t = store.section(cfg, "telegram")
    keep(t, f, "token")
    t["channel"] = f.get("channel", "").strip()
    if not t.get("token"):
        return "حط توكن البوت من @BotFather"
    me = tg(t["token"], "getMe")
    if not me.get("ok"):
        return f"التوكن غلط: {me.get('description')}"
    t["bot"] = me["result"].get("username")
    t.setdefault("hook_key", secrets.token_urlsafe(24))
    if not t.get("owner"):
        t["claim"] = t.get("claim") or secrets.token_urlsafe(9)
    t["mode"] = f.get("mode", "webhook")
    if t["mode"] == "webhook":
        url = cfg["settings"].get("base_url", "") + "/tg/" + t["hook_key"]
        r = tg(t["token"], "setWebhook", url=url, secret_token=t["hook_key"],
               allowed_updates=["message", "callback_query"], drop_pending_updates=True)
        if not r.get("ok"):
            return f"البوت انربط بس الـ webhook فشل: {r.get('description')}"
    else:
        tg(t["token"], "deleteWebhook")
    return "✅ البوت مربوط @" + t["bot"]


def save_account(f):
    s = f.get("section")
    with store.state() as (cfg, _):
        store.settings(cfg)
        if s == "telegram":
            if f.get("unlink_owner"):
                store.section(cfg, "telegram").update(owner=None, claim=secrets.token_urlsafe(9))
                flash("انفك ربط صاحب البوت. افتح رابط الربط الجديد من تيليجرامك.")
            else:
                flash(telegram_connect(cfg, f))
        elif s == "facebook":
            fb = store.section(cfg, "facebook")
            keep(fb, f, "app_id", "app_secret")
            if f.get("disconnect"):
                for k in ("page_id", "page_token", "ig_id", "ig_username", "page_name"):
                    fb.pop(k, None)
                flash("انفك ربط فيسبوك وإنستغرام")
            elif f.get("user_token"):
                fields, err = facebook.connect(f["user_token"], fb.get("app_id", ""), fb.get("app_secret", ""),
                                               f.get("page_id", ""))
                if err:
                    flash(f"فشل ربط فيسبوك: {err}")
                else:
                    fb.update(fields)
                    ig = f" وإنستغرام @{fields['ig_username']}" if fields.get("ig_id") else \
                        " (ما لكيت حساب إنستغرام احترافي مربوط بالصفحة)"
                    flash(f"✅ انربطت صفحة {fields['page_name']}{ig}")
            elif f.get("page_id") and fb.get("pages"):
                flash("حط التوكن ويا رقم الصفحة حتى أبدّل الصفحة")
        elif s == "linkedin":
            li = store.section(cfg, "linkedin")
            keep(li, f, "client_id", "client_secret")
            li["author"] = f.get("author", "").strip() or li.get("member", "")
            li["version"] = f.get("version", "").strip()
            if f.get("token"):
                li["token"] = f["token"].strip()
                li.pop("expires", None)
            if f.get("disconnect"):
                for k in ("token", "expires", "member", "name"):
                    li.pop(k, None)
            flash("انحفظ لينكدإن")
        elif s == "tiktok":
            tk = store.section(cfg, "tiktok")
            keep(tk, f, "client_key", "client_secret")
            tk["mode"] = f.get("mode", "direct")
            tk["privacy"] = f.get("privacy", "PUBLIC_TO_EVERYONE")
            if f.get("disconnect"):
                for k in ("token", "refresh", "expires", "refresh_expires", "open_id", "name", "username"):
                    tk.pop(k, None)
            flash("انحفظ تيك توك")
        elif s == "youtube":
            yt = store.section(cfg, "youtube")
            keep(yt, f, "client_id", "client_secret")
            yt["privacy"] = f.get("privacy", "public")
            if f.get("disconnect"):
                for k in ("token", "refresh", "expires", "channel_id", "name"):
                    yt.pop(k, None)
            flash("انحفظ يوتيوب")
        elif s == "wordpress":
            w = store.section(cfg, "wordpress")
            w["url"], w["user"] = f.get("url", "").strip(), f.get("user", "").strip()
            keep(w, f, "app_password")
            if f.get("disconnect"):
                cfg["wordpress"] = {}
            flash("انحفظ ووردبريس")
        elif s == "blog":
            b = store.section(cfg, "blog")
            b.update(title=f.get("title", "").strip() or "المدونة", about=f.get("about", "").strip(),
                     off=bool(f.get("off")))
            flash("انحفظت المدونة")
        elif s == "ai":
            a = store.section(cfg, "ai")
            keep(a, f, "groq_key", "gemini_key")
            a["model"] = f.get("model", "").strip()
            a["default"] = bool(f.get("default"))
            if f.get("clear"):
                cfg["ai"] = {}
            flash("انحفظ الذكاء الاصطناعي")
        elif s == "settings":
            st = cfg["settings"]
            st["brand"] = f.get("brand", "").strip()
            try:
                st["tz_hours"] = float(f.get("tz_hours", "3"))
            except ValueError:
                pass
            if f.get("base_url", "").strip():
                st["base_url"], st["base_url_fixed"] = f["base_url"].strip().rstrip("/"), True
            else:
                st["base_url_fixed"] = False
            if f.get("new_tick_key"):
                st["tick_key"] = secrets.token_urlsafe(18)
            flash("انحفظت الإعدادات")


# ---------- OAuth (LinkedIn, TikTok) ----------

OAUTH = {"linkedin": (linkedin, "client_id"), "tiktok": (tiktok, "client_key"), "youtube": (youtube, "client_id")}


def redirect_uri(name):
    return store.load(store.CFG, {})["settings"]["base_url"] + f"/oauth/{name}/callback"


@app.route("/oauth/<name>/start")
@login_required
def oauth_start(name):
    if name not in OAUTH:
        abort(404)
    mod, id_field = OAUTH[name]
    cfg = store.load(store.CFG, {})
    if not cfg.get(name, {}).get(id_field) or not cfg[name].get("client_secret"):
        flash("حط بيانات التطبيق (ID و Secret) واحفظها أول")
        return redirect(url_for("accounts") + "#" + name)
    session["oauth_state"] = secrets.token_urlsafe(18)
    return redirect(mod.auth_url(cfg, redirect_uri(name), session["oauth_state"]))


@app.route("/oauth/<name>/callback")
@login_required
def oauth_callback(name):
    if name not in OAUTH:
        abort(404)
    if not request.args.get("state") or request.args.get("state") != session.pop("oauth_state", None):
        abort(400)
    if request.args.get("error"):
        flash(f"الربط انلغى: {request.args.get('error_description') or request.args['error']}")
        return redirect(url_for("accounts") + "#" + name)
    mod = OAUTH[name][0]
    try:
        fields = mod.finish_auth(store.load(store.CFG, {}), request.args.get("code", ""), redirect_uri(name))
    except Exception as e:  # noqa: BLE001
        flash(f"فشل الربط: {e}")
        return redirect(url_for("accounts") + "#" + name)
    store.patch(name, **fields)
    flash(f"✅ انربط {mod.NAME}: {fields.get('name', '')}")
    return redirect(url_for("accounts") + "#" + name)


# ---------- machine endpoints ----------

@app.route("/tg/<key>", methods=["POST"])
def telegram_webhook(key):
    t = store.load(store.CFG, {}).get("telegram", {})
    if not t.get("hook_key") or not secrets.compare_digest(key, t["hook_key"]) \
            or request.headers.get("X-Telegram-Bot-Api-Secret-Token") != t["hook_key"]:
        abort(403)
    bot.handle(request.get_json(silent=True) or {})
    return after_response_publish(Response("ok"))


@app.route("/tick")
def tick():
    """For a cron (GitHub Actions, cron-job.org…): publishes scheduled posts. Needs ?key=<tick key>."""
    key = store.load(store.CFG, {}).get("settings", {}).get("tick_key", "")
    if not key or not secrets.compare_digest(request.args.get("key", ""), key):
        abort(403)
    clean_tmp()
    return engine.run_due()


def clean_tmp(age=86400):
    tmp = os.path.join(store.MEDIA, "tmp")
    for n in os.listdir(tmp) if os.path.isdir(tmp) else []:
        p = os.path.join(tmp, n)
        if time.time() - os.path.getmtime(p) > age:
            os.remove(p)


@app.route("/m/<int:pid>/<token>/<name>")
def public_media(pid, token, name):
    p = store.get_post(pid)
    if not p or not secrets.compare_digest(token, p["token"]) or name != secure_filename(name):
        abort(404)
    path = os.path.join(store.MEDIA, str(pid), name)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, conditional=True)


@app.route("/health")
def health():
    return "ok"


# ---------- public blog ----------

def blog_cfg():
    return store.load(store.CFG, {}).get("blog", {})


@app.route("/blog/")
@app.route("/blog")
def blog_index():
    entries = list(reversed(store.load(store.BLOG, [])))
    return render_template("blog_index.html", entries=entries, blog=blog_cfg())


@app.route("/blog/<slug>")
def blog_post(slug):
    e = next((e for e in store.load(store.BLOG, []) if e["slug"] == slug), None)
    if not e:
        abort(404)
    return render_template("blog_post.html", e=e, blog=blog_cfg())


@app.route("/blog/feed.xml")
def blog_feed():
    base = store.load(store.CFG, {}).get("settings", {}).get("base_url", request.host_url.rstrip("/"))
    xml = render_template("feed.xml", entries=list(reversed(store.load(store.BLOG, [])))[:30], blog=blog_cfg(), base=base)
    return Response(xml, mimetype="application/rss+xml")


@app.errorhandler(413)
def too_big(_):
    flash("الملف أكبر من الحد المسموح للرفع")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":  # local run: python app.py
    os.environ.setdefault("SPM_INSECURE_COOKIE", "1")
    app.config["SESSION_COOKIE_SECURE"] = False
    app.run(debug=True, port=int(os.environ.get("PORT", "5000")))
