"""An external WordPress site through its REST API and an Application Password
(WordPress > Users > Profile > Application Passwords)."""
import html
import mimetypes
import os

import requests

from .blog import to_html

KEY, NAME, ICON = "wordpress", "ووردبريس", "🌐"


def _auth(cfg):
    w = cfg["wordpress"]
    return w["user"], w["app_password"].replace(" ", "")


def _api(cfg, path):
    return cfg["wordpress"]["url"].rstrip("/") + "/wp-json/wp/v2/" + path


def connected(cfg):
    w = cfg.get("wordpress", {})
    return bool(w.get("url") and w.get("user") and w.get("app_password"))


def check(cfg):
    try:
        r = requests.get(_api(cfg, "users/me"), auth=_auth(cfg), timeout=30)
        return (r.status_code == 200, r.json().get("name", "") if r.status_code == 200 else f"HTTP {r.status_code}: {r.text[:200]}")
    except (requests.RequestException, ValueError) as e:
        return False, str(e)


def upload(cfg, path):
    name = os.path.basename(path)
    with open(path, "rb") as f:
        r = requests.post(_api(cfg, "media"), auth=_auth(cfg), data=f, timeout=600, headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Content-Type": mimetypes.guess_type(name)[0] or "application/octet-stream"})
    if r.status_code >= 300:
        raise RuntimeError(f"رفع الملف لووردبريس فشل: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


def publish(cfg, post, v, media):
    body, featured = to_html(v["text"]), None
    if media.original:
        m = upload(cfg, media.image_jpeg() if media.kind == "image" else media.original)
        src = html.escape(m.get("source_url", ""))
        if media.kind == "image":
            featured = m["id"]
            body = f'<figure><img src="{src}" alt=""></figure>\n' + body
        elif media.kind == "video":
            body = f'<figure><video controls src="{src}"></video></figure>\n' + body
        else:
            body = f'<figure><audio controls src="{src}"></audio></figure>\n' + body
    r = requests.post(_api(cfg, "posts"), auth=_auth(cfg), timeout=60, json={
        "title": v.get("title") or "منشور", "content": body, "status": "publish",
        **({"featured_media": featured} if featured else {})})
    if r.status_code >= 300:
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:300]}"}
    d = r.json()
    return {"ok": True, "id": str(d.get("id")), "url": d.get("link", "")}
