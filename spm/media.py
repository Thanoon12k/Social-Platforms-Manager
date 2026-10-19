"""Turn one post's media into what each platform accepts.

Every post keeps its original file in data/media/<id>/. Derived files sit next to it and are
made once, on first need:
    image.jpg   the photo as an RGB JPEG (Instagram accepts JPEG only)
    card.jpg    a 1080x1350 text card, for text-only posts on photo/video platforms
    video.mp4   H.264/AAC vertical video: the original video re-used as is, or a still
                (photo or card) shown for a few seconds, or a still over the audio track
The public URL of any of these is <base>/m/<id>/<token>/<name>; Instagram and TikTok fetch
media from there, so the site must be reachable over HTTPS.
"""
import os
import shutil
import subprocess

from PIL import Image, ImageDraw, ImageFont, ImageOps, features

from . import store

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = os.path.join(HERE, "fonts", "Cairo.ttf")
RAQM = features.check("raqm")
KW = {"direction": "rtl"} if RAQM else {}  # for measuring Arabic lines
BG, PANEL, INK, MUTED, ACCENT = (18, 16, 26), (31, 28, 43), (243, 239, 230), (167, 161, 182), (255, 107, 61)
KINDS = {"image": ("jpg", "jpeg", "png", "webp", "gif"), "video": ("mp4", "mov", "m4v", "webm", "mkv"),
         "audio": ("mp3", "m4a", "aac", "ogg", "oga", "opus", "wav", "flac")}


class MediaError(Exception):
    pass


def kind_of(filename, mime=""):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    for k, exts in KINDS.items():
        if ext in exts or (mime or "").startswith(k + "/"):
            return k
    return None


def ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - optional dependency
        return None


# ---------- text card ----------

def _font(size, weight=700):
    f = ImageFont.truetype(FONT, size, layout_engine=ImageFont.Layout.RAQM if RAQM else ImageFont.Layout.BASIC)
    try:
        f.set_variation_by_axes([weight, 0])
    except OSError:
        pass
    return f


def _shape(text):
    if RAQM:
        return text
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        return get_display(arabic_reshaper.reshape(text))
    except ImportError:
        return text


def _rtl(text):
    return any("؀" <= c <= "ۿ" for c in text)


def _wrap(text, f, max_w):
    out = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            cand = (cur + " " + word).strip()
            if cur and f.getlength(_shape(cand), **KW) > max_w:
                out.append(cur)
                cur = word
            else:
                cur = cand
        out.append(cur)
    return out


def _draw(d, xy, text, f, fill, anchor):
    kw = ({"direction": "rtl" if _rtl(text) else "ltr"} if RAQM else {})
    d.text(xy, _shape(text), font=f, fill=fill, anchor=anchor, **kw)


def card(title, body, brand="", out=None):
    """A clean 1080x1350 card: accent bar, headline, body (vertically centred), brand at the foot."""
    W, H, pad = 1080, 1350, 90
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((50, 50, W - 50, H - 50), radius=48, fill=PANEL)
    rtl = _rtl(title + body)
    x, anchor = (W - pad, "ra") if rtl else (pad, "la")
    tf, bf = _font(68, 800), _font(44, 500)
    heads = _wrap(title, tf, W - 2 * pad)[:3]
    lines = _wrap(body, bf, W - 2 * pad)
    top, bottom = 190, H - 200
    room = (bottom - top - len(heads) * 98 - 40) // 66
    if len(lines) > room:
        lines = lines[:room - 1] + [lines[room - 1].rstrip(".،,") + " …"]
    y = max(top, (top + bottom) // 2 - (len(heads) * 98 + 40 + len(lines) * 66) // 2)
    d.rounded_rectangle((x - 140, y - 70, x, y - 54) if rtl else (x, y - 70, x + 140, y - 54), radius=8, fill=ACCENT)
    for line in heads:
        _draw(d, (x, y), line, tf, INK, anchor)
        y += 98
    y += 40
    for line in lines:
        _draw(d, (x, y), line, bf, INK, anchor)
        y += 66
    if brand:
        _draw(d, (x, H - 130), brand, _font(36, 700), ACCENT, anchor)
    im.save(out, "JPEG", quality=92)
    return out


# ---------- per-post media ----------

class Prepared:
    """Lazy access to the media variants of one post."""

    def __init__(self, post, base_url="", brand=""):
        self.p, self.base, self.brand = post, base_url.rstrip("/"), brand
        self.dir = os.path.join(store.MEDIA, str(post["id"]))
        os.makedirs(self.dir, exist_ok=True)
        m = post.get("media") or {}
        self.kind = m.get("kind") or "text"
        self.original = os.path.join(self.dir, m["file"]) if m.get("file") else None

    def path(self, name):
        return os.path.join(self.dir, name)

    def url(self, path):
        if not self.base:
            raise MediaError("عنوان الموقع العام (base URL) غير مضبوط، افتح الداشبورد من رابطه الحقيقي مرة")
        return f"{self.base}/m/{self.p['id']}/{self.p['token']}/{os.path.basename(path)}"

    def card(self):
        out = self.path("card.jpg")
        if not os.path.exists(out):
            text = (self.p.get("text") or "").strip()
            title = (self.p.get("title") or "").strip()
            if not title:
                title, _, text = text.partition("\n")
            card(title.strip()[:120], text.strip(), self.brand, out)
        return out

    def image_jpeg(self):
        """The post photo as JPEG (max 1440px); the text card for text-only posts; else None."""
        if self.kind == "text":
            return self.card()
        if self.kind != "image":
            return None
        out = self.path("image.jpg")
        if not os.path.exists(out):
            im = ImageOps.exif_transpose(Image.open(self.original))
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            im = im.convert("RGB")
            im.thumbnail((1440, 1440))
            im.save(out, "JPEG", quality=92)
        return out

    def still(self):
        """The picture shown in a generated video: the post photo, else the text card."""
        return self.image_jpeg() if self.kind == "image" else self.card()

    def video(self):
        """An MP4 for video-only platforms (TikTok, Reels) and for audio posts everywhere."""
        if self.kind == "video" and self.original.lower().endswith((".mp4", ".m4v", ".mov")):
            return self.original
        out = self.path("video.mp4")
        if os.path.exists(out):
            return out
        exe = ffmpeg()
        if not exe:
            raise MediaError("تحويل الصورة/الصوت/النص إلى فيديو يحتاج ffmpeg (ثبّت ffmpeg أو pip install imageio-ffmpeg)")
        vf = ("scale=1080:1920:force_original_aspect_ratio=decrease,"
              "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=0x12101a,format=yuv420p")
        common = ["-c:v", "libx264", "-preset", "veryfast", "-r", "30", "-c:a", "aac", "-b:a", "160k",
                  "-movflags", "+faststart", "-pix_fmt", "yuv420p"]
        if self.kind == "video":  # webm/mkv -> mp4
            cmd = [exe, "-y", "-i", self.original, "-vf", vf] + common + [out]
        elif self.kind == "audio":
            cmd = [exe, "-y", "-loop", "1", "-i", self.still(), "-i", self.original, "-vf", vf, "-tune", "stillimage",
                   "-shortest", "-t", "600"] + common + [out]
        else:  # photo or text card, 7 silent seconds
            cmd = [exe, "-y", "-loop", "1", "-i", self.still(), "-f", "lavfi", "-i",
                   "anullsrc=channel_layout=stereo:sample_rate=44100", "-t", "7", "-vf", vf] + common + [out]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode != 0 or not os.path.exists(out):
            raise MediaError("ffmpeg فشل: " + r.stderr[-300:])
        return out
