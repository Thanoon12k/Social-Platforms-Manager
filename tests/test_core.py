"""Offline tests: adaptation, media, the publishing engine, the bot and the web app.

No network: platform publishers and the Telegram API are replaced with fakes.
    pip install pytest && python -m pytest -q
"""
import importlib
import io
import os
import shutil
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture()
def env(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("SPM_DATA", d)
    monkeypatch.setenv("SPM_INSECURE_COOKIE", "1")
    import spm.store
    importlib.reload(spm.store)
    from spm import adapt, bot, engine, media, platforms  # noqa: F401
    from spm.platforms import blog
    for m in (media, blog, engine, bot):
        importlib.reload(m)
    calls = []

    def fake_tg(token, method, files=None, http_timeout=60, **params):
        calls.append((method, params))
        return {"ok": True, "result": {"message_id": len(calls), "id": 1, "username": "spm_bot",
                                       "file_path": "x"}}

    monkeypatch.setattr(engine, "tg", fake_tg)
    monkeypatch.setattr(bot, "tg", fake_tg)
    published = []

    class Fake:
        def __init__(self, key, fail=False):
            self.KEY, self.NAME, self.ICON, self.fail = key, key, "•", fail

        def connected(self, cfg):
            return True

        def check(self, cfg):
            return True, "ok"

        def publish(self, cfg, post, v, m):
            published.append((self.KEY, post["id"], v["text"], m.kind))
            if self.fail:
                raise RuntimeError("boom")
            return {"ok": True, "id": "1", "url": f"https://x/{self.KEY}"}

    fakes = [Fake("linkedin"), Fake("facebook"), Fake("instagram"), Fake("telegram"), blog]
    monkeypatch.setattr(platforms, "ORDER", fakes)
    monkeypatch.setattr(platforms, "BY_KEY", {m.KEY: m for m in fakes})
    with spm.store.state() as (cfg, _):
        cfg["telegram"] = {"token": "T", "owner": "42"}
        cfg["settings"] = {"base_url": "https://site.test"}
    yield {"dir": d, "calls": calls, "published": published, "fakes": fakes}
    shutil.rmtree(d, ignore_errors=True)


def test_adapt_rules():
    from spm import adapt
    post = {"text": "أهلاً بالجميع\nهذا منشور تجريبي https://a.b/c\n\n#تقنية #برمجة #ai #x1 #x2 #x3",
            "link": "https://a.b/c"}
    v = adapt.rules(post)
    assert v["telegram"]["text"].endswith("#تقنية #برمجة #ai")  # 3 tags max
    assert "https://a.b/c" in v["linkedin"]["text"] and v["linkedin"]["text"].count("https://a.b/c") == 1
    assert "الرابط بالبايو" in v["instagram"]["text"]
    assert v["blog"]["title"] == "أهلاً بالجميع" and v["blog"]["tags"][0] == "تقنية"
    long = adapt.rules({"text": "كلمة " * 1000})
    assert len(long["instagram"]["text"]) <= 2200 and long["instagram"]["text"].endswith("…")


def test_linkedin_little_text():
    from spm.platforms import linkedin
    s = linkedin.little("Hi (team) #AI #الكود_الذهبي")
    assert s == "Hi \\(team\\) {hashtag|\\#|AI} \\#الكود\\_الذهبي"


def test_tiktok_chunks():
    from spm.platforms import tiktok
    assert tiktok._chunks(3 * tiktok.MB) == (3 * tiktok.MB, 1)
    chunk, n = tiktok._chunks(25 * tiktok.MB + 7)
    assert chunk == 10 * tiktok.MB and n == 2  # last chunk carries the 5 MB remainder


def test_media_card_and_video(env):
    from PIL import Image
    from spm import engine, media
    p = engine.create(text="عنوان المنشور\nسطر أول من النص وسطر ثاني أطول شوية حتى ينلف", source="test")
    m = media.Prepared(p, "https://site.test", "@brand")
    card = m.image_jpeg()
    assert Image.open(card).size == (1080, 1350)
    assert m.url(card).startswith(f"https://site.test/m/{p['id']}/{p['token']}/card.jpg")
    if media.ffmpeg():
        assert os.path.getsize(m.video()) > 1000


def make_png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (300, 200), (255, 0, 0, 128)).save(buf, "PNG")
    return buf.getvalue()


def test_engine_publish_partial_and_retry(env):
    from spm import engine, store
    src = os.path.join(env["dir"], "up.png")
    with open(src, "wb") as f:
        f.write(make_png())
    env["fakes"][2].fail = True  # instagram fails the first time
    p = engine.create(text="صورة جديدة #test", file_path=src, filename="pic.png", source="test")
    assert p["media"]["kind"] == "image" and p["targets"] == ["linkedin", "facebook", "instagram", "telegram", "blog"]
    assert engine.schedule(p["id"])
    assert engine.run_due() == {"published": [p["id"]]}
    p = store.get_post(p["id"])
    assert p["status"] == "partial" and p["results"]["instagram"]["error"] == "boom"
    assert [k for k, *_ in env["published"]] == ["linkedin", "facebook", "instagram", "telegram"]
    assert p["results"]["blog"]["url"] == f"https://site.test/blog/{p['results']['blog']['id']}"
    env["fakes"][2].fail = False
    assert engine.retry(p["id"])
    engine.run_due()
    p = store.get_post(p["id"])
    assert p["status"] == "done"
    assert [k for k, *_ in env["published"]].count("linkedin") == 1  # what worked is never re-posted
    final = [c for c in env["calls"] if c[0] in ("sendMessage", "editMessageText")][-1][1]["text"]
    assert "انتشر في كل المنصات (5/5)" in final


def test_interrupted_upload_is_not_reposted(env):
    from spm import engine, store
    p = engine.create(text="نص", source="test")
    with store.state() as (_, posts):
        q = store.find(posts, p["id"])
        q.update(status="publishing", results={"linkedin": {"ok": True}, "facebook": {"ok": None}})
    engine.run_due()
    q = store.get_post(p["id"])
    assert q["results"]["facebook"]["error"] == engine.INTERRUPTED
    assert [k for k, *_ in env["published"]] == ["instagram", "telegram"]
    assert q["status"] == "partial"


def test_schedule_later(env):
    from datetime import timedelta
    from spm import engine, store
    p = engine.create(text="بعدين", source="test")
    engine.schedule(p["id"], store.now() + timedelta(hours=2))
    assert engine.run_due() == {"published": []}
    assert store.get_post(p["id"])["status"] == "scheduled"


def test_bot_flow(env):
    from spm import bot, engine, store
    with store.state() as (cfg, _):
        cfg["telegram"].update(owner=None, claim="CODE")
    bot.handle({"update_id": 1, "message": {"chat": {"id": 99}, "text": "/start WRONG"}})
    assert store.load(store.CFG, {})["telegram"]["owner"] is None
    bot.handle({"update_id": 2, "message": {"chat": {"id": 42}, "text": "/start CODE"}})
    assert store.load(store.CFG, {})["telegram"]["owner"] == "42"
    bot.handle({"update_id": 3, "message": {"chat": {"id": 42}, "text": "منشور من البوت #تجربة"}})
    p = store.load(store.POSTS, [])[-1]
    assert p["source"] == "telegram" and p["tg_preview"]
    bot.handle({"update_id": 3, "message": {"chat": {"id": 42}, "text": "مكرر"}})  # same update id: ignored
    assert len(store.load(store.POSTS, [])) == 1
    bot.handle({"update_id": 4, "callback_query": {"id": "c", "from": {"id": 42}, "data": f"tgl:{p['id']}:instagram"}})
    assert "instagram" not in store.get_post(p["id"])["targets"]
    bot.handle({"update_id": 5, "callback_query": {"id": "c", "from": {"id": 42}, "data": f"pub:{p['id']}"}})
    assert store.get_post(p["id"])["status"] == "queued"
    engine.run_due()
    assert store.get_post(p["id"])["status"] == "done"
    assert "instagram" not in [k for k, *_ in env["published"]]


def test_web_app(env):
    import app as webapp
    importlib.reload(webapp)
    c = webapp.app.test_client()
    assert c.get("/").status_code == 302
    c.get("/login")
    with c.session_transaction() as s:
        tok = s["csrf"]
    assert c.post("/login", data={"csrf": tok, "password": "password123"}).status_code == 302
    assert "منشور جديد" in c.get("/").get_data(as_text=True)
    r = c.post("/compose", data={"csrf": tok, "text": "من الداشبورد", "action": "publish",
                                 "targets": ["facebook", "blog"], "file": (io.BytesIO(make_png()), "a.png")},
               content_type="multipart/form-data")
    assert r.status_code == 302
    from spm import engine, store
    engine.run_due()
    p = store.load(store.POSTS, [])[-1]
    assert p["status"] == "done" and p["targets"] == ["facebook", "blog"]
    assert "من الداشبورد" in c.get(f"/post/{p['id']}").get_data(as_text=True)
    assert c.get("/accounts").status_code == 200 and c.get("/log").status_code == 200
    page = c.get("/blog").get_data(as_text=True)
    assert "من الداشبورد" in page
    assert c.get("/blog/feed.xml").status_code == 200
    assert c.get(f"/m/{p['id']}/{p['token']}/original.png").status_code == 200
    assert c.get(f"/m/{p['id']}/wrong/original.png").status_code == 404
    key = store.load(store.CFG, {})["settings"]["tick_key"]
    assert c.get("/tick?key=bad").status_code == 403
    assert c.get(f"/tick?key={key}").get_json() == {"published": []}
