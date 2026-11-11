"""Checks a v1 edition.json before it is published (spec 2.13): the 1.2 app must decode it and show it.

Two layers:
  1. The 1.2 app's own decoder (tools/v1decode/, frozen Swift copies of Article.swift + Edition.swift), compiled
     once into <state_dir>/bin/v1decode: natively with swiftc where there is one (a Mac), else in the
     swift:6.2-jammy Docker image with a static stdlib, so the binary then runs on the box without Docker.
  2. Rules the decoder can't see: every category in the closed enum, every Top article ranked, AI text or the
     aiText: false flag on every card, http(s) URLs that older iOS URL parsing accepts, dates without fractions.

The worker calls check() on every publish and refuses to publish on any problem.
Usage: python3 tools/check_edition.py <edition.json> [--no-decoder]      exit 0 = publishable
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from worker import config  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SWIFT_DIR = os.path.join(HERE, "v1decode")
SWIFT_FILES = ("Article.swift", "Edition.swift", "Disk.swift", "main.swift")
SWIFT_IMAGE = os.environ.get("SWIFT_IMAGE", "swift:6.2-jammy")
CATEGORIES = {"Top", "AI", "Startups", "Dev", "Blogs"}
DATE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
# Characters URL(string:) accepted before iOS 17 (RFC 3986 plus %); anything else made the whole decode fail.
URL_OK = re.compile(r"^https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+$")
REQUIRED = ("url", "title", "excerpt", "content", "source", "category", "publishedAt")


class Result:
    def __init__(self):
        self.problems = []
        self.decoder = None         # the Swift decoder's summary, or None when it didn't run

    @property
    def ok(self):
        return not self.problems

    def add(self, msg):
        if len(self.problems) < 50:
            self.problems.append(msg)


def rules(edition, res):
    if not isinstance(edition, dict) or not isinstance(edition.get("articles"), list):
        res.add("no articles list")
        return
    if not DATE.match(str(edition.get("publishedAt", ""))):
        res.add(f"publishedAt {edition.get('publishedAt')!r} is not a whole-second UTC ISO date")
    arts = edition["articles"]
    if not arts:
        res.add("no articles (1.2 treats an empty edition as stale)")
    urls = set()
    for i, a in enumerate(arts):
        where = f"articles[{i}]"
        missing = [k for k in REQUIRED if not isinstance(a.get(k), str)]
        if missing:
            res.add(f"{where} missing {missing}")
            continue
        if a["category"] not in CATEGORIES:
            res.add(f"{where} category {a['category']!r}")
        if not URL_OK.match(a["url"]):
            res.add(f"{where} url {a['url']!r}")
        if a["url"] in urls:
            res.add(f"{where} duplicate url (the app's article id)")
        urls.add(a["url"])
        if a.get("image") is not None and not URL_OK.match(str(a["image"])):
            res.add(f"{where} image {a['image']!r}")
        if not DATE.match(a["publishedAt"]):
            res.add(f"{where} publishedAt {a['publishedAt']!r}")
        if a["category"] == "Top" and not isinstance(a.get("topRank"), int):
            res.add(f"{where} is Top without topRank")
        for k, t in (("topRank", int), ("points", int), ("alsoCoveredBy", list), ("headline", str),
                     ("summary", str)):
            if a.get(k) is not None and not isinstance(a[k], t):
                res.add(f"{where} {k} has the wrong type")
        if not ((a.get("headline") and a.get("summary")) or a.get("aiText") is False):
            res.add(f"{where} has neither AI text nor aiText: false")
        if not a["title"].strip():
            res.add(f"{where} empty title")


def _source_hash():
    h = hashlib.sha256()
    for f in SWIFT_FILES:
        h.update(open(os.path.join(SWIFT_DIR, f), "rb").read())
    return h.hexdigest()[:16]


def decoder_path(cfg):
    """Builds the decoder if the sources changed. Returns the binary's path, or None with no way to build it."""
    bindir = os.path.join(cfg["state_dir"], "bin")
    binary = os.path.join(bindir, "v1decode")
    stamp = binary + ".hash"
    want = _source_hash()
    if os.path.exists(binary) and os.path.exists(stamp) and open(stamp).read().strip() == want:
        return binary
    os.makedirs(bindir, exist_ok=True)
    files = [os.path.join(SWIFT_DIR, f) for f in SWIFT_FILES]
    if shutil.which("swiftc"):
        cmd = ["swiftc", "-O", "-swift-version", "5", "-o", binary, *files]
    elif shutil.which("docker"):
        cmd = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
               "-v", f"{SWIFT_DIR}:/src:ro", "-v", f"{bindir}:/out", "-w", "/src", SWIFT_IMAGE,
               "swiftc", "-O", "-swift-version", "5", "-static-stdlib", "-module-cache-path", "/tmp/mc",
               "-o", "/out/v1decode", *SWIFT_FILES]
    else:
        return None
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not os.path.exists(binary):
        raise RuntimeError(f"v1decode build failed: {(r.stderr or r.stdout)[-500:]}")
    with open(stamp, "w") as f:
        f.write(want)
    return binary


def decode(path, cfg, res):
    try:
        binary = decoder_path(cfg)
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
        res.add(f"decoder unavailable: {e}")
        return
    if binary is None:
        res.add("decoder unavailable: no swiftc or docker to build tools/v1decode")
        return
    try:
        r = subprocess.run([binary, path], capture_output=True, text=True, timeout=60)
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError) as e:
        res.add(f"decoder did not run: {type(e).__name__}")
        return
    res.decoder = out
    if not out.get("ok"):
        res.add(f"1.2 decoder: {out.get('error')}")


def check(path, cfg=None, decoder=True):
    cfg = cfg or config.load()
    res = Result()
    try:
        edition = json.load(open(path))
    except (OSError, ValueError) as e:
        res.add(f"not JSON: {e}")
        return res
    rules(edition, res)
    if decoder:
        decode(path, cfg, res)
        if res.decoder and res.decoder.get("ok") and res.decoder.get("articles") != len(edition["articles"]):
            res.add("decoder count differs from the file")
    return res


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        sys.exit(__doc__)
    res = check(args[0], decoder="--no-decoder" not in sys.argv)
    print(json.dumps({"ok": res.ok, "problems": res.problems, "decoder": res.decoder}, indent=1))
    sys.exit(0 if res.ok else 1)


if __name__ == "__main__":
    main()
