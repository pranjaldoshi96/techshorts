#!/usr/bin/env python3
"""Writes Techgist's own headline and 60-word summary for each story, and a lightweight image.

Runs after the Swift publisher writes edition.json and before it's pushed. For each story not seen before it:
1. fetches the article page and extracts its text (falls back to the feed text),
2. asks an LLM (NVIDIA-hosted, OpenAI-compatible API) for a headline and summary in JSON,
3. keeps them only if every number and capitalized name in them appears in the article text,
4. makes a 720 px JPEG of the story's image for the app to load.
Results are cached by link in ~/.techshorts/enriched.json, so each story costs one LLM call. Any failure leaves the
story with its publisher headline and excerpt; this script never stops a publish.

Usage: enrich.py <edition.json> <image dir> <image base URL>      (publish.sh runs it)
       enrich.py --sample N                                       (prints N results without writing anything)
"""
import concurrent.futures as cf, hashlib, html, io, json, os, re, sys, time
from html.parser import HTMLParser
import requests

STATE = os.environ.get("TECHSHORTS_STATE", os.path.expanduser("~/.techshorts"))
CACHE = os.path.join(STATE, "enriched.json")
API = "https://integrate.api.nvidia.com/v1/chat/completions"
MODEL = os.environ.get("TECHGIST_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
MAX_NEW_PER_RUN = int(os.environ.get("TECHGIST_MAX_NEW", "80"))
UA = "Mozilla/5.0 (compatible; TechgistBot/1.0; +https://pranjaldoshi96.github.io/apps/techgist/)"

PROMPT = """You write for Techgist, a tech news app where each story is one card.

From the article below, write:
- "headline": at most 90 characters. Specific and engaging: lead with the most newsworthy fact (who did what, with \
the key number if there is one). Accurate and neutral. No clickbait, no questions, no "you", no emojis, no \
exaggeration beyond what the article says.
- "summary": 55 to 70 words, one paragraph. Cover what happened, who is involved, the key numbers or specifics, and \
why it matters. Use only facts stated in the article. If the text is only the opening of the article, summarize \
just that and do not guess the rest.

Never add numbers, names, products, dates or claims that are not in the article text.
Reply with only a JSON object: {"headline": "...", "summary": "..."}

Source: SOURCE
Original headline: TITLE

Article text:
TEXT"""


def api_key():
    key = os.environ.get("NVIDIA_API_KEY")
    if not key:  # cron doesn't load ~/.bashrc; take the last export there
        lines = [l for l in open(os.path.expanduser("~/.bashrc")) if l.startswith("export NVIDIA_API_KEY=")]
        key = lines[-1].split("=", 1)[1].strip().strip("'\"") if lines else None
    return key


class TextExtractor(HTMLParser):
    """Collects paragraph text, preferring what's inside <article>."""
    def __init__(self):
        super().__init__()
        self.skip = 0; self.in_p = False; self.in_article = 0; self.buf = []; self.paras = []
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer", "aside", "figure", "noscript"): self.skip += 1
        elif tag == "article": self.in_article += 1
        elif tag == "p": self.in_p = True; self.buf = []
    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "aside", "figure", "noscript"): self.skip = max(0, self.skip - 1)
        elif tag == "article": self.in_article = max(0, self.in_article - 1)
        elif tag == "p" and self.in_p:
            text = re.sub(r"\s+", " ", "".join(self.buf)).strip()
            if len(text.split()) >= 8: self.paras.append((self.in_article > 0, text))
            self.in_p = False
    def handle_data(self, data):
        if self.in_p and not self.skip: self.buf.append(data)


def article_text(url, fallback):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        if r.ok and "html" in r.headers.get("content-type", ""):
            p = TextExtractor(); p.feed(r.text[:1_500_000])
            inside = [t for a, t in p.paras if a]
            paras = inside if len(" ".join(inside).split()) >= 80 else [t for _, t in p.paras]
            text = html.unescape(" ".join(paras))
            if len(text.split()) > len(fallback.split()):
                return " ".join(text.split()[:1500])
    except requests.RequestException:
        pass
    return fallback


def grounded(output, source):
    """Every number and capitalized name in the output must appear in the source."""
    hay = source.lower().replace(",", "")
    for n in re.findall(r"\d[\d.,]*[A-Za-z%]{0,3}", output):
        if n.lower().replace(",", "").strip(".") not in hay: return False, n
    for sentence in re.split(r"[.!?:;]", output):
        for w in re.split(r"[\s\-/()\"“”‘]+", sentence.strip())[1:]:
            w = re.split(r"['’]", w)[0].strip(",")
            if len(w) >= 3 and w[0].isupper() and w.isalpha() and w.lower() not in hay: return False, w
    return True, None


def ask(key, article, text):
    prompt = PROMPT.replace("SOURCE", article["source"]).replace("TITLE", article["title"]).replace("TEXT", text)
    for attempt in range(3):
        try:
            r = requests.post(API, timeout=90, headers={"Authorization": f"Bearer {key}"}, json={
                "model": MODEL, "temperature": 0.2, "max_tokens": 600,
                "chat_template_kwargs": {"enable_thinking": False},   # straight answer, no reasoning trace
                "messages": [{"role": "user", "content": prompt}]})
            if r.status_code == 429: time.sleep(10 * (attempt + 1)); continue
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"] or ""
            m = re.search(r"\{.*\}", content, re.S)
            data = json.loads(m.group(0)) if m else {}
            headline, summary = data.get("headline", "").strip(), data.get("summary", "").strip()
            if headline and summary: return headline, summary
        except (requests.RequestException, ValueError, KeyError):
            time.sleep(3)
    return None


def enrich_one(key, article):
    source_text = article.get("content") or article.get("excerpt") or ""
    text = article_text(article["url"], source_text)
    if len(text.split()) < 40: return {"skip": "too little text"}
    result = ask(key, article, text)
    if not result: return {"skip": "model failed"}
    headline, summary = result
    words = len(summary.split())
    if len(headline) > 110 or not 35 <= words <= 100: return {"skip": f"length (headline {len(headline)}, summary {words} words)"}
    ok, bad = grounded(headline + ". " + summary, article["title"] + " " + article["source"] + " " + text)
    if not ok: return {"skip": f"not in article: {bad}"}
    return {"headline": headline, "summary": summary, "model": MODEL}


def thumbnail(url, out_dir):
    """A 720 px wide JPEG of the story image, named by a hash of its URL. Returns the file name or None."""
    try:
        from PIL import Image
    except ImportError:
        return None
    name = hashlib.sha1(url.encode()).hexdigest()[:16] + ".jpg"
    path = os.path.join(out_dir, name)
    if os.path.exists(path): return name
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGBA").convert("RGB")
        if img.width > 720: img = img.resize((720, round(img.height * 720 / img.width)), Image.LANCZOS)
        img.save(path, "JPEG", quality=72, optimize=True, progressive=True)
        return name
    except Exception:
        return None


def main():
    key = api_key()
    if "--sample" in sys.argv:
        n = int(sys.argv[sys.argv.index("--sample") + 1])
        edition = json.load(open(os.path.join(STATE, "site", "edition.json")))
        picks = [a for a in edition["articles"] if a.get("topRank") is not None][:n]
        for a in picks:
            t = time.time(); r = enrich_one(key, a)
            print(f"\n[{a['source']}] {a['title']}\n  -> {r.get('headline', r.get('skip'))}\n  {r.get('summary', '')}\n  ({time.time() - t:.1f}s)")
        return

    edition_path, img_dir, img_base = sys.argv[1], sys.argv[2], sys.argv[3].rstrip("/")
    edition = json.load(open(edition_path))
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    if not key:
        print("Enrich: no NVIDIA_API_KEY, publishing feed headlines")

    todo = [a for a in edition["articles"] if a["url"] not in cache]
    # Top stories and the newest first, so the most-read cards get written first when the run limit applies.
    todo.sort(key=lambda a: (a.get("topRank") is None, -time.mktime(time.strptime(a["publishedAt"][:19], "%Y-%m-%dT%H:%M:%S"))))
    todo = todo[:MAX_NEW_PER_RUN] if key else []
    written = skipped = 0
    with cf.ThreadPoolExecutor(4) as pool:
        for a, r in zip(todo, pool.map(lambda a: enrich_one(key, a), todo)):
            if "skip" in r and r["skip"] == "model failed": continue   # try again next run
            cache[a["url"]] = {**r, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            written += "headline" in r; skipped += "skip" in r

    os.makedirs(img_dir, exist_ok=True)
    used = set()
    for a in edition["articles"]:
        c = cache.get(a["url"], {})
        if "headline" in c: a["headline"], a["summary"] = c["headline"], c["summary"]
        if a.get("image"):
            name = thumbnail(a["image"], img_dir)
            if name: a["image"] = f"{img_base}/{name}"; used.add(name)
    for f in os.listdir(img_dir):   # drop images of stories no longer in the edition
        if f not in used: os.remove(os.path.join(img_dir, f))

    live = {a["url"] for a in edition["articles"]}
    cache = {u: v for u, v in cache.items() if u in live or time.time() - time.mktime(time.strptime(v["at"], "%Y-%m-%dT%H:%M:%SZ")) < 7 * 86400}
    json.dump(cache, open(CACHE, "w"))
    json.dump(edition, open(edition_path, "w"), separators=(",", ":"), ensure_ascii=False)
    have = sum("headline" in a for a in edition["articles"])
    print(f"Enrich: {written} new summaries, {skipped} kept feed text, {have}/{len(edition['articles'])} stories written by {MODEL}; {len(used)} images")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Enrich: error {e!r}; publishing without it")
