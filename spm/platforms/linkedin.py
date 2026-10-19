"""LinkedIn profile (or company page) through the versioned Posts API.

Connect with OAuth from the accounts page: an app on linkedin.com/developers with the
products "Sign In with LinkedIn using OpenID Connect" and "Share on LinkedIn", and the
redirect URL <site>/oauth/linkedin/callback. Tokens last 60 days; the status page warns
before that. To post as a company page instead, set the author to urn:li:organization:<id>
(needs the w_organization_social scope, given to approved apps).
"""
import os
import re
import time
from datetime import date, timedelta
from urllib.parse import urlencode

import requests

KEY, NAME, ICON = "linkedin", "لينكدإن", "💼"
API = "https://api.linkedin.com/rest/"
AUTH = "https://www.linkedin.com/oauth/v2/"
SCOPES = "openid profile w_member_social"
RESERVED = re.compile(r"([\\|{}@\[\]()<>#*_~])")
HASHTAG = re.compile(r"#([\w؀-ۿ]+)")


def version(cfg):
    """LinkedIn-Version header: the configured one, else the month of ~2 months ago (versions live a year)."""
    v = cfg.get("linkedin", {}).get("version") or os.environ.get("LINKEDIN_VERSION")
    return v or (date.today().replace(day=1) - timedelta(days=45)).strftime("%Y%m")


def little(text):
    """Escape for LinkedIn's "little text" format; hashtags become real hashtag links."""
    out, pos = [], 0
    for m in HASHTAG.finditer(text):
        out.append(RESERVED.sub(r"\\\1", text[pos:m.start()]))
        tag = m[1]
        out.append("{hashtag|\\#|" + tag + "}" if "_" not in tag else RESERVED.sub(r"\\\1", m[0]))
        pos = m.end()
    out.append(RESERVED.sub(r"\\\1", text[pos:]))
    return "".join(out)


def headers(cfg, json_body=True):
    h = {"Authorization": "Bearer " + cfg["linkedin"]["token"], "LinkedIn-Version": version(cfg),
         "X-Restli-Protocol-Version": "2.0.0"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def err(r):
    try:
        d = r.json()
        return f"HTTP {r.status_code}: {d.get('message') or d}"[:400]
    except ValueError:
        return f"HTTP {r.status_code}: {r.text[:300]}"


# ---------- OAuth ----------

def auth_url(cfg, redirect, state):
    li = cfg["linkedin"]
    return AUTH + "authorization?" + urlencode({"response_type": "code", "client_id": li["client_id"],
                                                "redirect_uri": redirect, "state": state,
                                                "scope": li.get("scopes") or SCOPES})


def finish_auth(cfg, code, redirect):
    """Swap the code for a token and find the member. Returns the fields to store."""
    li = cfg["linkedin"]
    r = requests.post(AUTH + "accessToken", timeout=30, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
        "client_id": li["client_id"], "client_secret": li["client_secret"]}).json()
    if not r.get("access_token"):
        raise RuntimeError(r.get("error_description") or r)
    me = requests.get("https://api.linkedin.com/v2/userinfo", timeout=30,
                      headers={"Authorization": "Bearer " + r["access_token"]}).json()
    out = {"token": r["access_token"], "expires": int(time.time()) + int(r.get("expires_in", 0)),
           "name": me.get("name", ""), "member": f"urn:li:person:{me['sub']}" if me.get("sub") else ""}
    if not li.get("author") or li["author"].startswith("urn:li:person:"):
        out["author"] = out["member"]
    return out


def connected(cfg):
    li = cfg.get("linkedin", {})
    return bool(li.get("token") and li.get("author"))


def check(cfg):
    li = cfg["linkedin"]
    days = (li.get("expires", 0) - time.time()) / 86400 if li.get("expires") else None
    if days is not None and days < 0:
        return False, "التوكن انتهى، اربط لينكدإن من جديد"
    r = requests.get("https://api.linkedin.com/v2/userinfo", timeout=30,
                     headers={"Authorization": "Bearer " + li["token"]})
    if r.status_code != 200:
        return False, err(r)
    note = f" · التوكن يخلص بعد {int(days)} يوم" if days is not None else ""
    return (days is None or days > 7), f"{r.json().get('name', '')} ({li['author']}){note}"


# ---------- upload + post ----------

def upload_image(cfg, path):
    owner = cfg["linkedin"]["author"]
    r = requests.post(API + "images?action=initializeUpload", headers=headers(cfg), timeout=30,
                      json={"initializeUploadRequest": {"owner": owner}})
    if r.status_code >= 300:
        raise RuntimeError("image init " + err(r))
    v = r.json()["value"]
    with open(path, "rb") as f:
        up = requests.put(v["uploadUrl"], data=f, timeout=300,
                          headers={"Authorization": "Bearer " + cfg["linkedin"]["token"]})
    if up.status_code >= 300:
        raise RuntimeError("image upload " + err(up))
    return v["image"]


def upload_video(cfg, path):
    owner, size = cfg["linkedin"]["author"], os.path.getsize(path)
    r = requests.post(API + "videos?action=initializeUpload", headers=headers(cfg), timeout=30, json={
        "initializeUploadRequest": {"owner": owner, "fileSizeBytes": size,
                                    "uploadCaptions": False, "uploadThumbnail": False}})
    if r.status_code >= 300:
        raise RuntimeError("video init " + err(r))
    v = r.json()["value"]
    etags = []
    with open(path, "rb") as f:
        for part in v["uploadInstructions"]:
            f.seek(part["firstByte"])
            chunk = f.read(part["lastByte"] - part["firstByte"] + 1)
            up = requests.put(part["uploadUrl"], data=chunk, timeout=300,
                              headers={"Content-Type": "application/octet-stream"})
            if up.status_code >= 300:
                raise RuntimeError("video upload " + err(up))
            etags.append(up.headers.get("etag", ""))
    fin = requests.post(API + "videos?action=finalizeUpload", headers=headers(cfg), timeout=60, json={
        "finalizeUploadRequest": {"video": v["video"], "uploadToken": v.get("uploadToken", ""),
                                  "uploadedPartIds": etags}})
    if fin.status_code >= 300:
        raise RuntimeError("video finalize " + err(fin))
    urn = v["video"]
    for _ in range(36):  # up to ~3 min for processing; the post may still go out while it finishes
        s = requests.get(API + "videos/" + requests.utils.quote(urn, safe=""), headers=headers(cfg), timeout=30)
        status = s.json().get("status") if s.status_code == 200 else None
        if status in ("AVAILABLE", "PROCESSING_FAILED"):
            if status == "PROCESSING_FAILED":
                raise RuntimeError("لينكدإن فشل بمعالجة الفيديو")
            break
        time.sleep(5)
    return urn


def publish(cfg, post, v, media):
    body = {"author": cfg["linkedin"]["author"], "commentary": little(v["text"]), "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
    title = (post.get("title") or "")[:200]
    if media.kind == "image":
        body["content"] = {"media": {"id": upload_image(cfg, media.image_jpeg()), "title": title or None}}
    elif media.kind in ("video", "audio"):
        body["content"] = {"media": {"id": upload_video(cfg, media.video()), "title": title or None}}
    elif post.get("link"):
        body["content"] = {"article": {"source": post["link"], "title": title or post["link"]}}
    if body.get("content", {}).get("media", {}).get("title") is None:
        body.get("content", {}).get("media", {}).pop("title", None)
    r = requests.post(API + "posts", headers=headers(cfg), json=body, timeout=60)
    if r.status_code >= 300:
        return {"ok": False, "error": err(r)}
    urn = r.headers.get("x-restli-id", "")
    return {"ok": True, "id": urn, "url": f"https://www.linkedin.com/feed/update/{urn}/" if urn else ""}
