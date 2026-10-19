"""Facebook Page through the Graph API (also hosts the shared helpers Instagram uses).

Connect once from the accounts page with a user token from the Graph API Explorer
(permissions: pages_show_list, pages_manage_posts, pages_read_engagement, instagram_basic,
instagram_content_publish, business_management). With the app id and secret the token is
swapped for a long-lived one first, so the stored Page token does not expire.
"""
import os

import requests

KEY, NAME, ICON = "facebook", "فيسبوك", "📘"
VERSION = os.environ.get("GRAPH_VERSION", "v23.0")
GRAPH = f"https://graph.facebook.com/{VERSION}/"
GRAPH_VIDEO = f"https://graph-video.facebook.com/{VERSION}/"


def call(method, path, host=GRAPH, files=None, timeout=60, **params):
    try:
        r = requests.request(method, host + path, params=params if method == "GET" else None,
                             data=None if method == "GET" else params, files=files, timeout=timeout).json()
    except (requests.RequestException, ValueError) as e:
        r = {"error": {"message": str(e)}}
    return r


def err(r):
    e = r.get("error", {})
    return e.get("error_user_msg") or e.get("message") or str(r)[:300]


def connect(user_token, app_id="", app_secret="", page_id=""):
    """Find the page (and its Instagram account) for a user or page token. Returns (fields, error)."""
    tok = user_token.strip()
    if app_id and app_secret:
        r = call("GET", "oauth/access_token", grant_type="fb_exchange_token", client_id=app_id,
                 client_secret=app_secret, fb_exchange_token=tok)
        if r.get("access_token"):
            tok = r["access_token"]
    fields = "id,name,access_token,instagram_business_account{id,username}"
    pages = call("GET", "me/accounts", fields=fields, access_token=tok, limit=100).get("data") or []
    if not pages:  # maybe it is already a page token
        me = call("GET", "me", fields="id,name,instagram_business_account{id,username}", access_token=tok)
        if me.get("error"):
            return None, err(me)
        me["access_token"] = tok
        pages = [me]
    page = next((p for p in pages if p["id"] == page_id.strip()), pages[0]) if page_id else pages[0]
    ig = page.get("instagram_business_account") or {}
    return {"page_id": page["id"], "page_name": page.get("name", ""), "page_token": page["access_token"],
            "pages": [{"id": p["id"], "name": p.get("name", "")} for p in pages],
            "ig_id": ig.get("id", ""), "ig_username": ig.get("username", "")}, None


def connected(cfg):
    f = cfg.get("facebook", {})
    return bool(f.get("page_id") and f.get("page_token"))


def check(cfg):
    f = cfg["facebook"]
    r = call("GET", f["page_id"], fields="name,followers_count", access_token=f["page_token"])
    if r.get("error"):
        return False, err(r)
    return True, f"{r.get('name')} · {r.get('followers_count', '?')} متابع"


def publish(cfg, post, v, media):
    f = cfg["facebook"]
    page, token, text = f["page_id"], f["page_token"], v["text"]
    if media.kind == "image":
        with open(media.original, "rb") as fh:
            r = call("POST", page + "/photos", files={"source": fh}, timeout=180, caption=text, access_token=token)
        pid = r.get("post_id") or r.get("id")
        url = f"https://www.facebook.com/{pid}" if pid else ""
    elif media.kind in ("video", "audio"):
        path = media.original if media.kind == "video" else media.video()
        with open(path, "rb") as fh:
            r = call("POST", page + "/videos", host=GRAPH_VIDEO, files={"source": fh}, timeout=900,
                     description=text, title=(post.get("title") or "")[:250] or None, access_token=token)
        pid = r.get("id")
        url = f"https://www.facebook.com/{page}/videos/{pid}" if pid else ""
    else:
        r = call("POST", page + "/feed", message=text, link=post.get("link") or None, access_token=token)
        pid = r.get("id")
        url = f"https://www.facebook.com/{pid}" if pid else ""
    return {"ok": True, "id": pid, "url": url} if pid else {"ok": False, "error": err(r)}
