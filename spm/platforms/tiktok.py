"""TikTok through the Content Posting API (video only; photos/text/audio are turned into video).

Connect with OAuth from the accounts page: an app on developers.tiktok.com with Login Kit and
Content Posting API, scopes user.info.basic + video.publish + video.upload, and the redirect
URL <site>/oauth/tiktok/callback. Until TikTok audits the app, direct posts can only be
private (SELF_ONLY); the app then retries privately and says so. Mode "inbox" instead sends
the video to the TikTok app as a draft to finish there.
Access tokens last 24 h and are refreshed automatically with the refresh token (1 year).
"""
import os
import time
from urllib.parse import urlencode

import requests

from .. import store

KEY, NAME, ICON = "tiktok", "تيك توك", "🎵"
API = "https://open.tiktokapis.com/v2/"
SCOPES = "user.info.basic,video.publish,video.upload"
MB = 1024 * 1024


def _post(path, token, body, timeout=60):
    try:
        r = requests.post(API + path, json=body, timeout=timeout,
                          headers={"Authorization": "Bearer " + token, "Content-Type": "application/json; charset=UTF-8"})
        return r.json()
    except (requests.RequestException, ValueError) as e:
        return {"error": {"code": "network", "message": str(e)}}


def _err(r):
    e = r.get("error") or {}
    return f"{e.get('code')}: {e.get('message')}" if e.get("code") not in (None, "ok") else ""


# ---------- OAuth ----------

def auth_url(cfg, redirect, state):
    return "https://www.tiktok.com/v2/auth/authorize/?" + urlencode({
        "client_key": cfg["tiktok"]["client_key"], "response_type": "code", "scope": SCOPES,
        "redirect_uri": redirect, "state": state})


def _token_fields(r):
    if not r.get("access_token"):
        raise RuntimeError(r.get("error_description") or r.get("error") or r)
    t = int(time.time())
    return {"token": r["access_token"], "refresh": r.get("refresh_token", ""), "open_id": r.get("open_id", ""),
            "expires": t + int(r.get("expires_in", 0)), "refresh_expires": t + int(r.get("refresh_expires_in", 0))}


def finish_auth(cfg, code, redirect):
    tk = cfg["tiktok"]
    r = requests.post(API + "oauth/token/", timeout=30, headers={"Content-Type": "application/x-www-form-urlencoded"}, data={
        "client_key": tk["client_key"], "client_secret": tk["client_secret"], "code": code,
        "grant_type": "authorization_code", "redirect_uri": redirect}).json()
    out = _token_fields(r)
    me = requests.get(API + "user/info/?fields=open_id,display_name,username", timeout=30,
                      headers={"Authorization": "Bearer " + out["token"]}).json()
    user = me.get("data", {}).get("user", {})
    out.update(name=user.get("display_name", ""), username=user.get("username", ""))
    return out


def token(cfg):
    """A live access token, refreshed (and saved) when it is about to expire."""
    tk = cfg["tiktok"]
    if tk.get("expires", 0) - time.time() > 600:
        return tk["token"]
    r = requests.post(API + "oauth/token/", timeout=30, headers={"Content-Type": "application/x-www-form-urlencoded"}, data={
        "client_key": tk["client_key"], "client_secret": tk["client_secret"],
        "grant_type": "refresh_token", "refresh_token": tk.get("refresh", "")}).json()
    fields = _token_fields(r)
    tk.update(fields)
    store.patch("tiktok", **fields)
    return tk["token"]


def connected(cfg):
    tk = cfg.get("tiktok", {})
    return bool(tk.get("token") and tk.get("client_key"))


def check(cfg):
    try:
        tok = token(cfg)
    except Exception as e:  # noqa: BLE001
        return False, f"تجديد التوكن فشل: {e}"
    r = _post("post/publish/creator_info/query/", tok, {})
    if _err(r):
        return False, _err(r)
    d = r.get("data", {})
    mode = "مسودة بالتطبيق" if cfg["tiktok"].get("mode") == "inbox" else "نشر مباشر"
    return True, f"@{d.get('creator_username', '')} · {mode} · خصوصية: {', '.join(d.get('privacy_level_options', []))}"


# ---------- publish ----------

def _chunks(size):
    if size <= 5 * MB:
        return size, 1
    chunk = 10 * MB
    return chunk, size // chunk  # the last chunk takes the remainder (allowed up to 128 MB)


def _upload(url, path, size, chunk, count):
    with open(path, "rb") as f:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1
            f.seek(start)
            data = f.read(end - start + 1)
            r = requests.put(url, data=data, timeout=600, headers={
                "Content-Type": "video/mp4", "Content-Length": str(len(data)),
                "Content-Range": f"bytes {start}-{end}/{size}"})
            if r.status_code >= 300:
                raise RuntimeError(f"رفع الجزء {i + 1}/{count} فشل: HTTP {r.status_code} {r.text[:200]}")


def publish(cfg, post, v, media):
    tk, tok = cfg["tiktok"], token(cfg)
    path = media.video()
    size = os.path.getsize(path)
    chunk, count = _chunks(size)
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}
    note = ""
    if tk.get("mode") == "inbox":
        r = _post("post/publish/inbox/video/init/", tok, {"source_info": source})
        note = "انرسل لتطبيق تيك توك كمسودة، افتحه واضغط نشر"
    else:
        info = _post("post/publish/creator_info/query/", tok, {}).get("data", {})
        options = info.get("privacy_level_options") or ["SELF_ONLY"]
        privacy = tk.get("privacy") or "PUBLIC_TO_EVERYONE"
        privacy = privacy if privacy in options else options[0]
        body = {"post_info": {"title": v["text"][:2200], "privacy_level": privacy, "disable_duet": False,
                              "disable_comment": False, "disable_stitch": False, "video_cover_timestamp_ms": 1000},
                "source_info": source}
        r = _post("post/publish/video/init/", tok, body)
        if "unaudited" in _err(r) and privacy != "SELF_ONLY":
            body["post_info"]["privacy_level"] = "SELF_ONLY"
            r = _post("post/publish/video/init/", tok, body)
            note = "انتشر خاص (SELF_ONLY) لأن تطبيقك بعده ما مدقق من تيك توك"
    if _err(r):
        return {"ok": False, "error": _err(r)}
    pub, url = r["data"]["publish_id"], r["data"]["upload_url"]
    _upload(url, path, size, chunk, count)
    deadline = time.time() + 300
    while time.time() < deadline:
        s = _post("post/publish/status/fetch/", tok, {"publish_id": pub})
        d = s.get("data", {})
        st = d.get("status")
        if st == "FAILED":
            return {"ok": False, "error": d.get("fail_reason", "FAILED")}
        if st in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            ids = d.get("publicaly_available_post_id") or []
            user = tk.get("username")
            link = f"https://www.tiktok.com/@{user}/video/{ids[0]}" if ids and user else (
                f"https://www.tiktok.com/@{user}" if user else "")
            return {"ok": True, "id": pub, "url": link, "note": note}
        time.sleep(5)
    return {"ok": True, "id": pub, "url": "", "note": (note + " · " if note else "") + "تيك توك بعده يعالج الفيديو"}
