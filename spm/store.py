"""Storage: config, posts and log as JSON files under data/, guarded by a file lock.

config.json holds every account secret (gitignored); posts.json holds every post with its
per-platform variants and results. Writes go through state() so the web app, the worker
and the Telegram bot never overwrite each other.
"""
import fcntl
import json
import os
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("SPM_DATA") or os.path.join(BASE, "data")
MEDIA = os.path.join(DATA, "media")
os.makedirs(MEDIA, exist_ok=True)
CFG, POSTS, LOG, BLOG = (os.path.join(DATA, n) for n in ("config.json", "posts.json", "log.json", "blog.json"))
LOCK, RUN_LOCK = os.path.join(DATA, ".lock"), os.path.join(DATA, ".run.lock")


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


@contextmanager
def state():
    """Exclusive access to (config, posts); both saved on exit."""
    with open(LOCK, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        cfg, posts = load(CFG, {}), load(POSTS, [])
        yield cfg, posts
        save(CFG, cfg)
        save(POSTS, posts)


def read():
    """(config, posts) for display, without taking the lock."""
    return load(CFG, {}), load(POSTS, [])


def section(cfg, name):
    return cfg.setdefault(name, {})


def patch(name, **values):
    """Merge values into one config section (used by platforms that refresh their tokens)."""
    with state() as (cfg, _):
        section(cfg, name).update(values)


def find(posts, pid):
    return next((p for p in posts if p["id"] == pid), None)


def get_post(pid):
    return find(load(POSTS, []), pid)


def settings(cfg):
    s = section(cfg, "settings")
    s.setdefault("secret_key", secrets.token_hex(32))
    s.setdefault("tick_key", secrets.token_urlsafe(18))
    return s


# ---------- time (one fixed offset, Baghdad by default) ----------

def tz():
    hours = load(CFG, {}).get("settings", {}).get("tz_hours", 3)
    return timezone(timedelta(hours=float(hours)))


def now():
    return datetime.now(tz())


def fmt(iso):
    return datetime.fromisoformat(iso).astimezone(tz()).strftime("%Y-%m-%d %H:%M") if iso else "—"


def parse_time(s, base=None):
    """'21:30', '2026-10-20 21:30', '2026-10-20T21:30', 'الآن'/'now' -> aware datetime, else None."""
    s = (s or "").strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    base = base or now()
    if s in ("الآن", "الان", "هسه", "now"):
        return base
    m = re.fullmatch(r"(\d{4}-\d{1,2}-\d{1,2})[ T](\d{1,2}):(\d{2})", s)
    if m:
        return datetime.strptime(f"{m[1]} {m[2]}:{m[3]}", "%Y-%m-%d %H:%M").replace(tzinfo=base.tzinfo)
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m and int(m[1]) < 24 and int(m[2]) < 60:
        t = base.replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0)
        return t if t > base else t + timedelta(days=1)
    return None


# ---------- activity log ----------

def log(msg, level="info", pid=None):
    with open(LOCK + ".log", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        lines = load(LOG, [])[-299:]
        lines.append({"at": now().isoformat(timespec="seconds"), "level": level, "pid": pid, "msg": str(msg)[:800]})
        save(LOG, lines)
