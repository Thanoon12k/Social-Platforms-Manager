"""Instagram professional account linked to the Facebook Page (same Page token).

Instagram downloads the media itself from a public URL, then publishes in two steps:
create a container, wait until it is FINISHED, publish it. Text posts go out as a card
image; video and audio posts go out as Reels.
"""
import time

from .facebook import call, err

KEY, NAME, ICON = "instagram", "إنستغرام", "📸"


def connected(cfg):
    f = cfg.get("facebook", {})
    return bool(f.get("ig_id") and f.get("page_token"))


def check(cfg):
    f = cfg["facebook"]
    r = call("GET", f["ig_id"], fields="username,followers_count", access_token=f["page_token"])
    if r.get("error"):
        return False, err(r)
    return True, f"@{r.get('username')} · {r.get('followers_count', '?')} متابع"


def publish(cfg, post, v, media):
    f = cfg["facebook"]
    ig, token = f["ig_id"], f["page_token"]
    if media.kind in ("video", "audio"):
        params = {"media_type": "REELS", "video_url": media.url(media.video()), "share_to_feed": "true"}
        wait = 600
    else:
        params = {"image_url": media.url(media.image_jpeg())}
        wait = 120
    c = call("POST", ig + "/media", caption=v["text"], access_token=token, **params)
    if not c.get("id"):
        return {"ok": False, "error": err(c)}
    deadline = time.time() + wait
    while True:
        s = call("GET", c["id"], fields="status_code,status", access_token=token)
        code = s.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED") or s.get("error"):
            return {"ok": False, "error": s.get("status") or err(s)}
        if time.time() > deadline:
            return {"ok": False, "error": "إنستغرام ما خلّص معالجة الملف بالوقت"}
        time.sleep(5)
    r = call("POST", ig + "/media_publish", creation_id=c["id"], access_token=token)
    if not r.get("id"):
        return {"ok": False, "error": err(r)}
    link = call("GET", r["id"], fields="permalink", access_token=token).get("permalink", "")
    return {"ok": True, "id": r["id"], "url": link}
