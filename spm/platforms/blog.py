"""The built-in blog: public pages at <site>/blog, with an RSS feed at /blog/feed.xml."""
import html
import re

from .. import store

KEY, NAME, ICON = "blog", "المدونة (الموقع)", "📰"
URL = re.compile(r"(https?://[^\s<]+)")


def to_html(text):
    """Plain text -> safe HTML paragraphs with clickable links."""
    out = []
    for para in re.split(r"\n\s*\n", (text or "").strip()):
        safe = html.escape(para.strip()).replace("\n", "<br>")
        out.append("<p>" + URL.sub(r'<a href="\1" rel="noopener">\1</a>', safe) + "</p>")
    return "\n".join(out)


def slugify(title, pid):
    s = re.sub(r"[^\w؀-ۿ]+", "-", title.lower()).strip("-")[:60]
    return f"{pid}-{s}" if s else str(pid)


def connected(cfg):
    return not cfg.get("blog", {}).get("off")


def check(cfg):
    n = len(store.load(store.BLOG, []))
    return True, f"{n} مقال منشور · /blog"


def publish(cfg, post, v, media):
    entries = store.load(store.BLOG, [])
    slug = slugify(v.get("title", ""), post["id"])
    entry = {"pid": post["id"], "slug": slug, "title": v.get("title") or "منشور", "html": to_html(v["text"]),
             "summary": v["text"].strip()[:220], "tags": v.get("tags", []), "kind": media.kind,
             "media": media.original and media.url(media.original),
             "at": store.now().isoformat(timespec="seconds")}
    entries = [e for e in entries if e["pid"] != post["id"]] + [entry]
    store.save(store.BLOG, entries)
    base = cfg.get("settings", {}).get("base_url", "").rstrip("/")
    return {"ok": True, "id": slug, "url": f"{base}/blog/{slug}"}
