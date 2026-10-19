"""One master post -> one variant per platform.

Rules always run: length limits, how many hashtags fit, where the link goes (Instagram and
TikTok captions have no clickable links), a title for the blog. With an AI key (Groq or
Gemini, both free tiers) the post can also be rewritten in each platform's tone first; the
rules then clip whatever the model returned. Every variant stays editable before publishing.
"""
import json
import re

import requests

LIMITS = {"linkedin": 3000, "facebook": 60000, "instagram": 2200, "tiktok": 2200, "telegram": 4096,
          "blog": 100000, "wordpress": 100000}
MAX_TAGS = {"linkedin": 5, "facebook": 3, "instagram": 15, "tiktok": 5, "telegram": 3, "blog": 0, "wordpress": 0}
TAG = re.compile(r"#[\w؀-ۿ]+")

PROVIDERS = [  # (config key, OpenAI-compatible endpoint, model)
    ("groq_key", "https://api.groq.com/openai/v1/chat/completions", "openai/gpt-oss-120b"),
    ("gemini_key", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "gemini-2.5-flash"),
]

STYLE = """You adapt ONE social media post for several platforms. Keep the author's language and dialect
(Arabic stays Arabic), keep every fact, name, number and link exactly; never invent anything.
- linkedin: professional and clear, a strong first line, short paragraphs, ends with a question or takeaway.
- facebook: friendly and conversational, 1-3 emojis at most.
- instagram: a hook first line, short lines, a few emojis, no URLs (say the link is in the bio).
- tiktok: very short caption (<= 150 characters before hashtags), punchy hook.
- telegram: concise, readable, keeps the link.
- blog_title: a clear headline (<= 80 characters); blog: a fuller article version in plain paragraphs
  separated by blank lines (no markdown headings), expanding only on what the post already says.
- hashtags: 3-8 relevant hashtags without spaces, including any the author used.
Do not put hashtags inside the texts; they are added per platform.
Return JSON only: {"linkedin": "", "facebook": "", "instagram": "", "tiktok": "", "telegram": "",
"blog_title": "", "blog": "", "hashtags": ["#..."]}"""


def split_tags(text, extra=()):
    """(body without hashtag-only lines, unique hashtags in order of appearance)."""
    tags = []
    for t in TAG.findall(text or "") + list(extra or []):
        t = t if t.startswith("#") else "#" + t
        if t.lower() not in (x.lower() for x in tags):
            tags.append(t)
    lines = [ln for ln in (text or "").strip().split("\n") if not (ln.strip() and not TAG.sub("", ln).strip())]
    return "\n".join(lines).strip(), tags


def clip(text, limit):
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    sp = max(cut.rfind(" "), cut.rfind("\n"))
    return (cut[:sp] if sp > limit * 0.7 else cut).rstrip(" ,.،") + "…"


def compose(platform, body, tags, link=""):
    tags = tags[:MAX_TAGS[platform]]
    tail = " ".join(tags)
    if platform == "instagram":
        extra = ("🔗 الرابط بالبايو" if _rtl(body) else "🔗 Link in bio") if link else ""
        foot = "\n\n".join(x for x in (extra, tail) if x)
    elif platform == "tiktok":
        foot = tail
    else:
        foot = "\n\n".join(x for x in ((link if link and link not in body else ""), tail) if x)
    room = LIMITS[platform] - (len(foot) + 2 if foot else 0)
    return (clip(body, room) + ("\n\n" + foot if foot else "")).strip()


def _rtl(text):
    return any("؀" <= c <= "ۿ" for c in text)


def blog_title(post, body):
    if post.get("title"):
        return post["title"].strip()
    first = body.strip().split("\n", 1)[0]
    return clip(re.sub(r"https?://\S+", "", first).strip(" -–—:"), 80) or "منشور جديد"


def rules(post):
    body, tags = split_tags(post.get("text", ""), post.get("hashtags"))
    link = (post.get("link") or "").strip()
    out = {k: {"text": compose(k, body, tags, link)} for k in ("linkedin", "facebook", "instagram", "tiktok", "telegram")}
    tiktok_body = clip(body, 150) if len(body) > 300 else body
    out["tiktok"] = {"text": compose("tiktok", tiktok_body, tags)}
    first, _, rest = body.partition("\n")
    title = blog_title(post, body)
    blog_body = rest.strip() if not post.get("title") and rest.strip() and title == clip(first.strip(), 80) else body
    blog_body += f"\n\n{link}" if link and link not in blog_body else ""
    for k in ("blog", "wordpress"):
        out[k] = {"title": title, "text": blog_body, "tags": [t[1:] for t in tags]}
    return out


def provider(cfg):
    ai = cfg.get("ai", {})
    return next(((ai[k], url, ai.get("model") or model) for k, url, model in PROVIDERS if ai.get(k)), None)


def ai_rewrite(cfg, post):
    """Variants written by the model, passed through the same limits. Raises on any failure."""
    key, url, model = provider(cfg)
    body, tags = split_tags(post.get("text", ""), post.get("hashtags"))
    user = json.dumps({"post": body, "title": post.get("title", ""), "link": post.get("link", ""),
                       "hashtags": tags}, ensure_ascii=False)
    r = requests.post(url, timeout=90, headers={"Authorization": "Bearer " + key}, json={
        "model": model, "temperature": 0.6, "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": STYLE}, {"role": "user", "content": user}]})
    content = r.json()["choices"][0]["message"]["content"]
    d = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip()))
    tags = split_tags("", (d.get("hashtags") or []) + tags)[1]
    link = (post.get("link") or "").strip()
    out = rules(post)
    for k in ("linkedin", "facebook", "instagram", "tiktok", "telegram"):
        if (d.get(k) or "").strip():
            out[k]["text"] = compose(k, split_tags(d[k])[0], tags, "" if k == "tiktok" else link)
    if (d.get("blog") or "").strip():
        for k in ("blog", "wordpress"):
            out[k].update(title=clip(d.get("blog_title") or out[k]["title"], 100), text=d["blog"].strip(),
                          tags=[t[1:] for t in tags])
    return out


def adapt(cfg, post, use_ai=False):
    """(variants, note). Falls back to the rules when the AI call fails."""
    if use_ai and provider(cfg):
        try:
            return ai_rewrite(cfg, post), "✨ صياغة بالذكاء الاصطناعي"
        except Exception as e:  # noqa: BLE001 - any API/JSON problem -> rules
            return rules(post), f"⚠️ الذكاء الاصطناعي ما اشتغل ({str(e)[:120]})، استخدمت القواعد"
    return rules(post), ""
