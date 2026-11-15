"""Card images (spec section 3): the source picture re-hosted as derived crops, or a generated one.

Order: the lead's og:image, then its feed media:content/enclosure, then the same for the other members by
authority. A picture under 600 px wide, outside a 1.2-2.2 aspect, or under 8 KB is rejected (logos, tracking
pixels, portrait headshots). Accepted pictures get a 16:10 centre crop in two variants:
  std   720x450 progressive JPEG q70: the v1 edition (and later push attachments, which need JPEG or PNG);
  thumb 360x225 WebP.
The hero variant (1290 px WebP) is for the R2 edge and isn't made yet. Files are named by a hash of the image
bytes, so a URL is immutable, and are kept 14 days by mtime.
"""
import concurrent.futures as cf
import hashlib
import io
import logging
import os
import time
from datetime import timedelta

import requests

from . import text as text_mod
from .db import iso

log = logging.getLogger("techgist")

MIN_WIDTH, MIN_BYTES, MAX_BYTES = 600, 8 * 1024, 15 * 1024 * 1024
ASPECT = (1.2, 2.2)
STD, THUMB = (720, 450), (360, 225)
RETENTION = timedelta(days=14)
FAILED_RETRY = timedelta(hours=1)

# Desaturated two-stop gradients per primary topic for the generated image; muted on purpose so a generated card
# reads as "no photo" rather than as a brand colour.
TOPIC_COLORS = {
    "ai": ((58, 64, 92), (112, 104, 140)), "startups_funding": ((48, 78, 70), (104, 136, 118)),
    "bigtech": ((52, 62, 80), (110, 124, 146)), "dev_tools": ((40, 56, 64), (88, 120, 128)),
    "security": ((74, 50, 52), (134, 100, 98)), "policy": ((70, 64, 52), (138, 126, 100)),
    "hardware": ((60, 60, 64), (124, 124, 130)), "india": ((86, 66, 46), (148, 118, 86)),
    "science": ((44, 66, 84), (96, 130, 150)), "consumer": ((76, 58, 74), (138, 110, 132)),
}
DEFAULT_COLORS = ((56, 60, 70), (116, 120, 132))


def _pil():
    from PIL import Image, ImageDraw, ImageOps
    return Image, ImageDraw, ImageOps


def crop_16_10(img):
    w, h = img.size
    target = STD[0] / STD[1]
    if w / h > target:
        nw = round(h * target)
        x = (w - nw) // 2
        return img.crop((x, 0, x + nw, h))
    nh = round(w / target)
    y = (h - nh) // 2
    return img.crop((0, y, w, y + nh))


def dhash(img, size=8):
    """64-bit difference hash as 16 hex chars: survives re-encoding and resizing, so the same press photo from
    two outlets matches."""
    Image, _, _ = _pil()
    g = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = g.tobytes()     # one byte per pixel in mode L
    bits = 0
    for row in range(size):
        for col in range(size):
            bits = (bits << 1) | (px[row * (size + 1) + col] > px[row * (size + 1) + col + 1])
    return f"{bits:016x}"


def check(data):
    """(image, None) for an acceptable picture, else (None, reason)."""
    Image, _, ImageOps = _pil()
    if len(data) < MIN_BYTES:
        return None, "small_file"
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return None, "not_image"
    img = ImageOps.exif_transpose(img)
    w, h = img.size
    if w < MIN_WIDTH:
        return None, "narrow"
    if not ASPECT[0] <= w / h <= ASPECT[1]:
        return None, "aspect"
    return img, None


def derive(img, out_dir, name):
    """Writes img/<name>_std.jpg and img/<name>_thumb.webp."""
    Image, _, _ = _pil()
    os.makedirs(os.path.join(out_dir, "img"), exist_ok=True)
    rgb = img.convert("RGBA")
    flat = Image.new("RGB", rgb.size, (255, 255, 255))
    flat.paste(rgb, mask=rgb.split()[3])
    c = crop_16_10(flat)
    std = c.resize(STD, Image.LANCZOS)
    _save(std, os.path.join(out_dir, "img", f"{name}_std.jpg"), "JPEG", quality=70, optimize=True, progressive=True)
    _save(c.resize(THUMB, Image.LANCZOS), os.path.join(out_dir, "img", f"{name}_thumb.webp"), "WEBP", quality=70)
    return std


def _save(img, path, fmt, **kw):
    tmp = path + ".tmp"
    img.save(tmp, fmt, **kw)
    os.replace(tmp, path)


def files_exist(out_dir, name):
    return all(os.path.exists(os.path.join(out_dir, "img", f"{name}_{v}")) for v in ("std.jpg", "thumb.webp"))


# The no-photo picture comes in GEN_VARIANTS looks per topic, so cards without a photo do not all show the same
# one: in the third review's edition twelve cards shared two pictures (one per topic). A variant differs in the
# gradient's direction, its tint, and the faint glyph (rings, bars, dots or arcs) and where it sits.
GEN_VARIANTS = 24
_GEN_ANGLES = (32, 148, 68, -68, 0, 90)
_GEN_SPOTS = ((0.72, 0.42), (0.28, 0.60), (0.55, 0.30))
_GEN_GLYPHS = ("rings", "bars", "dots", "arcs")


def _tint(color, k):
    """A small, muted shift of `color` for variant k (the topic stays recognisable)."""
    shift = ((k * 7) % 5 - 2) * 4, ((k * 3) % 5 - 2) * 4, ((k * 2) % 5 - 2) * 4
    return tuple(max(0, min(255, v + d)) for v, d in zip(color, shift))


def _gradient(w, h, degrees):
    """An 8-bit ramp across a w x h image along `degrees` (0 = left to right, 90 = top to bottom)."""
    import numpy as np
    Image, _, _ = _pil()
    t = np.radians(degrees)
    x, y = np.meshgrid(np.linspace(-0.5, 0.5, w), np.linspace(-0.5, 0.5, h))
    ramp = x * np.cos(t) + y * np.sin(t)
    ramp = (ramp - ramp.min()) / (ramp.max() - ramp.min())
    return Image.fromarray((ramp * 255).astype("uint8"), "L")


def generated(out_dir, topic, variant=0):
    """The no-photo image for a topic: a gradient with a faint glyph and no text, in one of GEN_VARIANTS looks.
    One file per topic and variant, made once. (The spec's source logo is left out: there are no licensed logos
    to draw.)"""
    topic = topic if topic in TOPIC_COLORS else "default"
    k = int(variant) % GEN_VARIANTS
    name = f"gen_{topic}_{k:02d}"
    if files_exist(out_dir, name):
        return name
    Image, ImageDraw, _ = _pil()
    a, b = (_tint(c, k) for c in TOPIC_COLORS.get(topic, DEFAULT_COLORS))
    w, h = 1280, 800
    img = Image.composite(Image.new("RGB", (w, h), b), Image.new("RGB", (w, h), a),
                          _gradient(w, h, _GEN_ANGLES[k // 4]))
    d = ImageDraw.Draw(img)
    fx, fy = _GEN_SPOTS[(k // 4) % len(_GEN_SPOTS)]
    cx, cy = int(w * fx), int(h * fy)
    glyph = _GEN_GLYPHS[k % 4]
    for i in range(3):
        shade = tuple(min(255, v + 22 - 6 * i) for v in b)
        r = 120 + 80 * i
        if glyph == "rings":
            d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=shade, width=6)
        elif glyph == "bars":
            x = cx - 220 + 150 * i
            d.polygon([(x, cy + 260), (x + 70, cy + 260), (x + 250, cy - 260), (x + 180, cy - 260)], fill=shade)
        elif glyph == "dots":
            for j in range(5):
                x, y = cx - 240 + 120 * j, cy - 120 + 120 * i
                d.ellipse((x - 22, y - 22, x + 22, y + 22), fill=shade)
        else:
            d.arc((cx - r, cy - r, cx + r, cy + r), start=200 + 25 * i, end=340 + 25 * i, fill=shade, width=10)
    derive(img, out_dir, name)
    return name


def pick_variant(key, taken):
    """The variant for the card `key` (an event id): its own by hash, or the next one that `taken` (the variants
    already on cards of the same topic in this edition, with how often) uses least. Stable for a card from one
    publish to the next unless a neighbour took its look first."""
    start = int(hashlib.sha1(key.encode()).hexdigest(), 16) % GEN_VARIANTS
    order = [(start + i) % GEN_VARIANTS for i in range(GEN_VARIANTS)]
    return min(order, key=lambda k: (taken.get(k, 0), order.index(k)))


class Images:
    """Finds, checks and derives event images, remembering every source URL's result in the images table."""

    def __init__(self, db, cfg, out_dir, session=None, now=None):
        self.db, self.cfg, self.out_dir = db, cfg, out_dir
        self.ua = cfg["fetch"]["user_agent"]
        self.session = session or requests.Session()
        self.now = now
        self.max_downloads = (cfg.get("images") or {}).get("max_downloads_per_tick", 60)

    def known(self, src):
        return self.db.one("SELECT * FROM images WHERE src = ?", src)

    def download(self, src):
        """(status, reason, sha, w, h, bytes, dhash) for one source URL. Runs in a thread; no DB access."""
        try:
            r = self.session.get(src, headers={"User-Agent": self.ua}, timeout=15, stream=True)
            if not r.ok:
                return ("failed", f"http_{r.status_code}", None, None, None, None, None)
            data = r.raw.read(MAX_BYTES + 1, decode_content=True)
        except (requests.RequestException, OSError, ValueError) as e:
            return ("failed", type(e).__name__, None, None, None, None, None)
        if len(data) > MAX_BYTES:
            return ("rejected", "too_big", None, None, None, len(data), None)
        img, why = check(data)
        if img is None:
            return ("rejected", why, None, None, None, len(data), None)
        sha = hashlib.sha256(data).hexdigest()[:10]
        try:
            std = derive(img, self.out_dir, sha)
        except Exception as e:      # a corrupt frame Pillow only notices while resizing
            return ("rejected", f"decode:{type(e).__name__}", None, None, None, len(data), None)
        return ("ok", None, sha, img.size[0], img.size[1], len(data), dhash(std))

    def page_images(self, rows):
        """og:image for items whose page was never fetched with image capture (written before WP4). Only the og
        tags are read, so the dummy source asks for no paragraphs."""
        from .sources import Source
        robots = text_mod.Robots(self.ua, self.session)

        def job(row):
            src = Source(row["source_id"], "", "", "", fetch_page=False)
            _, _, image = text_mod.fetch_page(row["url"], src, self.ua, robots, self.session, with_image=True)
            return row["url_canonical"], image or ""
        with cf.ThreadPoolExecutor(8) as pool:
            for url, image in pool.map(job, rows):
                self.db.conn.execute("UPDATE items SET og_image = ? WHERE url_canonical = ?", (image, url))

    def _usable(self, row):
        return row is not None and row["status"] == "ok" and files_exist(self.out_dir, row["sha8"])

    def _retry(self, row):
        # A network failure is retried after an hour; a rejected picture never is.
        if row is None:
            return True
        if row["status"] == "ok":
            return not files_exist(self.out_dir, row["sha8"])
        return row["status"] == "failed" and (self.now is None or iso(self.now - FAILED_RETRY) > row["at"])

    def resolve(self, wanted):
        """`wanted` is {event_id: [candidate source URLs in order]}. Downloads up to 2 untried candidates per event
        (capped per tick), and returns {event_id: images row or None}."""
        todo, seen = [], set()
        for srcs in wanted.values():
            tried = 0
            for s in srcs:
                row = self.known(s)
                if self._usable(row):
                    break
                if self._retry(row):
                    if s not in seen:
                        todo.append(s)
                        seen.add(s)
                    tried += 1
                    if tried >= 2:
                        break
        todo = todo[:self.max_downloads]
        if todo:
            started = time.monotonic()
            with cf.ThreadPoolExecutor(8) as pool:
                results = list(pool.map(self.download, todo))
            for src, res in zip(todo, results):
                self.db.conn.execute("INSERT OR REPLACE INTO images VALUES (?,?,?,?,?,?,?,?,?)",
                                     (src, *res, iso(self.now)))
            log.info("images: %d fetched in %.1fs (%d ok)", len(todo), time.monotonic() - started,
                     sum(r[0] == "ok" for r in results))
        out = {}
        for eid, srcs in wanted.items():
            out[eid] = next((row for row in map(self.known, srcs) if self._usable(row)), None)
        return out


def prune(out_dir, keep, now_ts=None, retention=RETENTION):
    """Deletes derived files older than the retention that the current edition doesn't use."""
    d = os.path.join(out_dir, "img")
    if not os.path.isdir(d):
        return 0
    now_ts = now_ts or time.time()
    n = 0
    for f in os.listdir(d):
        name = f.rsplit("_", 1)[0]
        p = os.path.join(d, f)
        if name in keep or f.startswith("gen_"):
            continue
        if now_ts - os.path.getmtime(p) > retention.total_seconds():
            os.remove(p)
            n += 1
    return n
