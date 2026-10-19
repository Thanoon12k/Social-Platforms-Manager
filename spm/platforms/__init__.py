"""Every platform module exposes KEY, NAME, ICON, connected(cfg), check(cfg) -> (ok, info) and
publish(cfg, post, variant, media) -> {"ok", "id", "url", "error", "note"}.

ORDER is the order posts go out in, one platform after the other.
"""
from . import blog, facebook, instagram, linkedin, telegram, tiktok, wordpress, youtube

ORDER = [linkedin, facebook, instagram, tiktok, youtube, telegram, blog, wordpress]
BY_KEY = {m.KEY: m for m in ORDER}


def label(key):
    m = BY_KEY.get(key)
    return f"{m.ICON} {m.NAME}" if m else key


def connected(cfg):
    return [m.KEY for m in ORDER if m.connected(cfg)]


def check_all(cfg):
    out = {}
    for m in ORDER:
        if not m.connected(cfg):
            out[m.KEY] = (None, "غير مربوط")
            continue
        try:
            out[m.KEY] = m.check(cfg)
        except Exception as e:  # noqa: BLE001 - a broken platform must not hide the others
            out[m.KEY] = (False, str(e)[:300])
    return out
