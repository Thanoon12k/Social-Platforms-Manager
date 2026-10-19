"""YouTube channel through the YouTube Data API v3 (resumable upload).

Every post becomes a video: a video is uploaded as is, while photos, text and audio are turned
into a vertical 1080x1920 clip, which YouTube shows as a Short.
Connect with OAuth from the accounts page: a Google Cloud project with "YouTube Data API v3"
enabled, an OAuth client of type "Web application" with the redirect URL
<site>/oauth/youtube/callback, and the consent screen set to "In production" (in "Testing" the
refresh token dies after 7 days). Until Google audits the project, uploads stay private; the
result then says so.
"""
import os
import re
import time
from urllib.parse import urlencode

import requests

from .. import store

KEY, NAME, ICON = "youtube", "يوتيوب", "▶️"
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3/"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"


def _err(r):
    try:
        e = r.json().get("error", {})
        return f"HTTP {r.status_code}: {e.get('message') if isinstance(e, dict) else e}"[:400]
    except ValueError:
        return f"HTTP {r.status_code}: {r.text[:300]}"


# ---------- OAuth ----------

def auth_url(cfg, redirect, state):
    return AUTH + "?" + urlencode({"client_id": cfg["youtube"]["client_id"], "redirect_uri": redirect,
                                   "response_type": "code", "scope": SCOPES, "access_type": "offline",
                                   "prompt": "consent", "state": state})


def finish_auth(cfg, code, redirect):
    yt = cfg["youtube"]
    r = requests.post(TOKEN, timeout=30, data={"code": code, "client_id": yt["client_id"],
                                               "client_secret": yt["client_secret"], "redirect_uri": redirect,
                                               "grant_type": "authorization_code"}).json()
    if not r.get("access_token"):
        raise RuntimeError(r.get("error_description") or r.get("error") or r)
    out = {"token": r["access_token"], "expires": int(time.time()) + int(r.get("expires_in", 0))}
    if r.get("refresh_token"):
        out["refresh"] = r["refresh_token"]
    ch = requests.get(API + "channels", params={"part": "snippet", "mine": "true"}, timeout=30,
                      headers={"Authorization": "Bearer " + out["token"]}).json()
    item = (ch.get("items") or [{}])[0]
    out.update(channel_id=item.get("id", ""), name=item.get("snippet", {}).get("title", ""))
    return out


def token(cfg):
    """A live access token (they last an hour), refreshed and saved when needed."""
    yt = cfg["youtube"]
    if yt.get("expires", 0) - time.time() > 300:
        return yt["token"]
    if not yt.get("refresh"):
        raise RuntimeError("ما كو refresh token، اربط يوتيوب من جديد")
    r = requests.post(TOKEN, timeout=30, data={"client_id": yt["client_id"], "client_secret": yt["client_secret"],
                                               "refresh_token": yt["refresh"], "grant_type": "refresh_token"}).json()
    if not r.get("access_token"):
        raise RuntimeError("تجديد التوكن فشل: " + str(r.get("error_description") or r.get("error") or r))
    fields = {"token": r["access_token"], "expires": int(time.time()) + int(r.get("expires_in", 0))}
    yt.update(fields)
    store.patch("youtube", **fields)
    return yt["token"]


def connected(cfg):
    yt = cfg.get("youtube", {})
    return bool(yt.get("client_id") and (yt.get("refresh") or yt.get("token")))


def check(cfg):
    try:
        tok = token(cfg)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    r = requests.get(API + "channels", params={"part": "snippet,statistics", "mine": "true"}, timeout=30,
                     headers={"Authorization": "Bearer " + tok})
    if r.status_code != 200 or not r.json().get("items"):
        return False, _err(r) if r.status_code != 200 else "ما لكيت قناة بهذا الحساب"
    it = r.json()["items"][0]
    subs = it.get("statistics", {}).get("subscriberCount", "?")
    return True, f"{it['snippet']['title']} · {subs} مشترك · {cfg['youtube'].get('privacy') or 'public'}"


# ---------- publish ----------

def clean(s):
    return re.sub(r"[<>]", "", s or "").strip()  # YouTube rejects < and > in titles and descriptions


def publish(cfg, post, v, media):
    yt, tok = cfg["youtube"], token(cfg)
    path = media.video()
    privacy = yt.get("privacy") or "public"
    title = clean(v.get("title") or post.get("title") or (post.get("text") or "").split("\n", 1)[0])[:100] or "فيديو"
    tags, total = [], 0
    for t in v.get("tags", []):
        if total + len(t) + 1 > 450:
            break
        tags.append(t)
        total += len(t) + 1
    meta = {"snippet": {"title": title, "description": clean(v["text"])[:4900], "tags": tags, "categoryId": "22"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    size = os.path.getsize(path)
    start = requests.post(UPLOAD, params={"uploadType": "resumable", "part": "snippet,status"}, json=meta, timeout=60,
                          headers={"Authorization": "Bearer " + tok, "X-Upload-Content-Type": "video/mp4",
                                   "X-Upload-Content-Length": str(size)})
    if start.status_code >= 300 or not start.headers.get("Location"):
        return {"ok": False, "error": _err(start)}
    with open(path, "rb") as f:
        up = requests.put(start.headers["Location"], data=f, timeout=1800,
                          headers={"Content-Type": "video/mp4", "Content-Length": str(size)})
    if up.status_code >= 300:
        return {"ok": False, "error": _err(up)}
    d = up.json()
    got = d.get("status", {}).get("privacyStatus", privacy)
    note = "انرفع خاص (private) لأن مشروع Google بعده ما مدقق من يوتيوب" if got != privacy else ""
    return {"ok": True, "id": d["id"], "url": f"https://youtu.be/{d['id']}", "note": note}
