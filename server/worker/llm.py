"""LLM triage and write: one call per item gives its type, relevance, region, importance and card text (spec 2.6).

The retry ladder (spec 2.8), used for every item:
  1. attempt; 2. one retry with literal feedback from validate.py / entail.py; 3. one attempt on the fallback
  model; 4. hold with retries at +30 min and +2 h, then drop (db.hold).
Chat calls share one rate limiter (30/min, Retry-After honoured) across 6 worker threads.
"""
import collections
import concurrent.futures as cf
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import List, Literal
from urllib.parse import urlsplit

import requests
from pydantic import BaseModel, Field, ValidationError, field_validator

from . import attribution as attribution_mod, entail, health, sources as sources_mod, text as text_mod, validate
from .db import from_iso, utcnow

log = logging.getLogger("techgist")

TYPES = ("news", "launch", "funding", "acquisition", "earnings", "policy", "security_incident", "analysis", "opinion",
         "interview", "review", "tutorial", "explainer", "release_notes", "customer_story", "podcast", "promo", "deal",
         "other")
TOPICS = ("ai", "startups_funding", "bigtech", "dev_tools", "security", "policy", "hardware", "india", "science",
          "consumer")


def api_key():
    key = os.environ.get("NVIDIA_API_KEY")
    if not key:  # systemd and cron don't load ~/.bashrc; take the last export there
        try:
            lines = [l for l in open(os.path.expanduser("~/.bashrc")) if l.startswith("export NVIDIA_API_KEY=")]
        except OSError:
            lines = []
        key = lines[-1].split("=", 1)[1].strip().strip("'\"") if lines else None
    return key


# Output schema

class Entities(BaseModel):
    orgs: List[str] = []
    people: List[str] = []
    products: List[str] = []
    amounts: List[str] = []


class WriteOutput(BaseModel):
    is_news: bool
    type: Literal[TYPES]
    relevance: int = Field(ge=0, le=3)
    importance: int = Field(ge=1, le=10)
    breaking: bool = False
    sensitive: bool = False
    topics: List[str] = []
    region: Literal["in", "us", "global"]
    entities: Entities = Entities()
    event: str = "new"
    material_update: bool = False
    headline: str
    s1: str
    s2: str
    s3: str = ""
    notif_line: str = ""
    # Shorter versions to pick from (validate.select): the model can't count, so it offers two lengths and code
    # picks the combination that fits. Not in the spec's schema; absent alts just mean one choice.
    headline_alt: str = ""
    s1_alt: str = ""
    s2_alt: str = ""
    s3_alt: str = ""

    @field_validator("topics", mode="before")
    @classmethod
    def known_topics(cls, v):
        # An invented topic shouldn't cost a retry; it just doesn't count.
        return [t for t in (v or []) if t in TOPICS]

    @field_validator("relevance", "importance", mode="before")
    @classmethod
    def clamp(cls, v, info):
        # Off-scale scores ("relevance": 4) are the commonest schema miss; clamping beats spending a retry.
        lo, hi = (0, 3) if info.field_name == "relevance" else (1, 10)
        try:
            return min(hi, max(lo, int(round(float(v)))))
        except (TypeError, ValueError):
            return v

    @field_validator("headline", "s1", "s2", "s3", "notif_line", "headline_alt", "s1_alt", "s2_alt", "s3_alt",
                     mode="before")
    @classmethod
    def strip(cls, v, info):
        v = re.sub(r"\s+", " ", v or "").strip()
        # A sentence the model left unpunctuated would run into the next one in the joined summary.
        if not info.field_name.startswith("headline") and v and v[-1] not in ".!?\"'”’":
            v += "."
        return v


class BadOutput(ValueError):
    pass


def parse(content):
    m = re.search(r"\{.*\}", content or "", re.S)
    if not m:
        raise BadOutput("Reply with only the JSON object.")
    try:
        return WriteOutput.model_validate(json.loads(m.group(0)))
    except json.JSONDecodeError:
        raise BadOutput("The reply was not valid JSON; reply with only the JSON object.")
    except ValidationError as e:
        fields = ", ".join(sorted({".".join(str(p) for p in err["loc"]) for err in e.errors()}))
        raise BadOutput(f"Fix these fields to match the schema: {fields}.")


# Rate limit and HTTP

class TokenBucket:
    """At most `rate` grants in any `per`-second window (a sliding window, so a burst at the end of one minute
    can't be followed by a full burst at the start of the next). `pause` honours a Retry-After."""

    def __init__(self, rate=30, per=60.0, clock=time.monotonic, sleep=time.sleep):
        self.rate, self.per, self.clock, self.sleep = rate, per, clock, sleep
        self.grants = collections.deque()
        self.blocked_until = 0.0
        self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = self.clock()
                while self.grants and now - self.grants[0] >= self.per:
                    self.grants.popleft()
                wait = self.blocked_until - now
                if wait <= 0 and len(self.grants) < self.rate:
                    self.grants.append(now)
                    return now
                if wait <= 0:
                    wait = self.per - (now - self.grants[0])
            self.sleep(max(wait, 0.01))

    def pause(self, seconds):
        with self.lock:
            self.blocked_until = max(self.blocked_until, self.clock() + seconds)


class LLMError(Exception):
    pass


class AuthError(LLMError):
    """401/403: the key is bad or revoked. The stage stops; WP4's degraded mode takes over."""


class ModelGone(LLMError):
    """404/410: the model was retired. Skips straight to the next rung of the ladder."""


class Client:
    def __init__(self, base_url, key, bucket=None, session=None, timeout=30, clock=time.monotonic,
                 sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.bucket = bucket or TokenBucket(clock=clock, sleep=sleep)
        self.session = session or requests.Session()
        self.timeout = timeout
        self.clock, self.sleep = clock, sleep
        self.events = []            # (kind, model, outcome, ms) for health.json; drained by the stage
        self.lock = threading.Lock()

    def _event(self, kind, model, outcome, ms):
        with self.lock:
            self.events.append((kind, model, outcome, ms))

    def drain(self):
        with self.lock:
            out, self.events = self.events, []
        return out

    def chat(self, model, messages, max_tokens=900, kind="write", tries=3):
        body = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                # Straight answer, no reasoning trace (Nemotron); other models ignore it.
                "chat_template_kwargs": {"enable_thinking": False}}
        for attempt in range(tries):
            self.bucket.acquire()
            started = self.clock()
            try:
                r = self.session.post(f"{self.base_url}/chat/completions", json=body, timeout=self.timeout,
                                      headers={"Authorization": f"Bearer {self.key}"})
            except requests.RequestException as e:
                self._event(kind, model, type(e).__name__, int((self.clock() - started) * 1000))
                continue
            ms = int((self.clock() - started) * 1000)
            if r.status_code == 429:
                self._event(kind, model, "http_429", ms)
                self.bucket.pause(_retry_after(r.headers.get("Retry-After"), 10 * (attempt + 1)))
                continue
            if r.status_code in (401, 403):
                self._event(kind, model, f"http_{r.status_code}", ms)
                raise AuthError(f"HTTP {r.status_code}")
            if r.status_code in (404, 410):
                self._event(kind, model, f"http_{r.status_code}", ms)
                raise ModelGone(f"{model}: HTTP {r.status_code}")
            if r.status_code >= 500:
                self._event(kind, model, f"http_{r.status_code}", ms)
                self.sleep(2 * (attempt + 1))
                continue
            if r.status_code != 200:
                self._event(kind, model, f"http_{r.status_code}", ms)
                raise LLMError(f"HTTP {r.status_code}")
            self._event(kind, model, "ok", ms)
            try:
                return r.json()["choices"][0]["message"]["content"] or ""
            except (ValueError, KeyError, IndexError, TypeError):
                raise LLMError("malformed response")
        raise LLMError("gave up after retries")


def _retry_after(value, default):
    try:
        return max(1.0, float(value))
    except (TypeError, ValueError):
        return default


# Prompt

SYSTEM = ("You are the editor of Techgist, a tech news app for tech professionals in Bengaluru and the Bay Area. "
          "Each story is one card. You classify one article and write its card. Reply with only a JSON object.")

PROMPT = """Return this JSON object:
{"is_news": true, "type": "...", "relevance": 0-3, "importance": 1-10, "breaking": false, "sensitive": false,
 "topics": [...], "region": "in|us|global",
 "entities": {"orgs": [], "people": [], "products": [], "amounts": []},
 "event": "new", "material_update": false,
 "headline": "...", "headline_alt": "...", "s1": "...", "s1_alt": "...", "s2": "...", "s2_alt": "...",
 "s3": "...", "s3_alt": "...", "notif_line": "..."}

Classification:
- is_news: true when it reports something that happened or was announced; false for evergreen how-tos, \
explainers, reviews, essays without news, promos.
- type, one of: news (reporting; the default), launch (a product, model or feature release), funding (a startup \
raise or a new VC fund), acquisition, earnings, policy (law, regulation, government action), security_incident \
(breach, hack, outage, exploited vulnerability), analysis (reported analysis or commentary on news), opinion \
(a columnist's or pundit's view), interview, review (a product review), tutorial, explainer (evergreen background), \
release_notes (version notes for a library, tool or app), customer_story (a vendor's story about a customer using \
its product), podcast, promo, deal (a shopping deal), other.
- relevance, how relevant to tech professionals in Bengaluru and the Bay Area: 3 = core tech or business news; \
2 = adjacent (science, or policy with a tech angle); 1 = tangential; 0 = off-topic (US partisan politics, \
entertainment, celebrity, cars without a tech angle).
- importance: 10 industry-defining. 8-9 frontier model from a top lab, >= $500M funding, a breach or outage \
hitting millions, major regulation. 6-7 $25-500M funding (or >= $15M in India), notable launch or earnings. \
4-5 smaller launches, open source, policy proposals, strong analysis. 1-3 how-to, reviews, explainers, promos, \
minor updates. Calibration examples:
  "Anthropic targets valuation of up to $1.4tn in stock market flotation" = 10, news
  "Nvidia investigates CEO's ties to suspected Chinese spy" = 9, news
  "CoreWeave beats on revenue and lifts guidance" = 7, earnings
  "Daylight raises $80M Series B" = 6, funding
  "Jim Cramer says Anthropic's explosive growth could justify a record-breaking IPO" = 4, opinion
  "llm-gemini 0.36" (a plugin version's release notes) = 3, release_notes
  "Gilbert + Tobin rethinks the business of law with AI" (OpenAI's story about a customer) = 2, customer_story
  "How long do Blu-ray discs last?" = 1, explainer
- breaking: true only for sudden, just-happened news of importance 8 or more.
- sensitive: true when the story involves crime, espionage, sexual conduct, lawsuits, or allegations against a \
named person.
- topics: any of ai, startups_funding, bigtech, dev_tools, security, policy, hardware, india, science, consumer.
- region: "us" only for US-only policy, US local news or US-market consumer news. Anything about a global company \
or product is "global" (an Anthropic IPO, CoreWeave earnings and an Nvidia probe are all global). "in" for \
India-market companies, Indian policy or Indian funding.
- entities: names exactly as in the text; amounts as written ("$1.4T").
- event: "new", unless a candidate event below is the same real-world event (then its id), or this is a later \
development of one (then "followup:<id>"). material_update: true when it adds a major new fact to that event.

Writing:
- headline: 50 to 70 characters. Fact-first, sentence case, active voice, present tense. The subject is the \
company or person. Numbers as digits ($80M, $1.4T, 3x). No question, teaser, emoji or final period.
- s1 (16 to 21 words): who did what, with the key number.
- s2 (14 to 20 words): the most important detail or context. S1 and S2 together stand alone as a summary.
- s3 (13 to 19 words): the concrete consequence for builders, investors or operators.
- Each fact once across the three sentences.
- Also give a shorter version of each: headline_alt (at most 55 characters), and s1_alt, s2_alt, s3_alt (10 to 13 \
words each, same facts, still complete sentences). Techgist picks the versions that fit the card.
- notif_line: one complete sentence of at most 12 words (under 90 characters), with new information that is \
not in the headline.
Length example only (its facts are invented; never reuse them):
{"headline": "Acme raises $40M to build battery packs for delivery vans",
 "headline_alt": "Acme raises $40M for delivery van batteries",
 "s1": "Acme, a Berlin battery startup, raised $40M in a Series B round led by Index Ventures on Tuesday.",
 "s1_alt": "Berlin battery startup Acme raised $40M in a Series B round.",
 "s2": "The company makes swappable battery packs for electric delivery vans and already supplies two of Germany's \
largest parcel carriers.",
 "s2_alt": "Its swappable packs power electric delivery vans for two large parcel carriers.",
 "s3": "Fleet operators get a cheaper way to electrify vans without waiting hours for each vehicle to recharge.",
 "s3_alt": "Fleet operators can electrify vans without long charging stops.",
 "notif_line": "Acme already supplies two of Germany's largest parcel carriers."}

Accuracy:
- Use only facts in the text below. Never add names, places, numbers, dates, weekdays or claims that are not in \
it, and keep the text's own wording for names and figures ("third quarter", not "Q3"). If the text is short, be \
less specific rather than inventing.
- Keep hedges: if the text says suspected, alleged, reportedly, accused, claims or according to, the headline \
keeps that word or attributes the claim ("X says ...", "Report: ...").<<hedges>>
- Opinion, analysis and interview pieces: the headline starts with the author's surname and a colon \
("Thompson: ...") or with "Analysis:", and presents claims as that person's view.
- Plain text. No markdown, no first person (I, we, our), never "the article". Don't mention subscribing, \
newsletters or podcasts. Never use: revolutionize, game-changer, groundbreaking, delve, landscape, seamless, \
cutting-edge.
<<attribution>>
Candidate events:
<<candidates>>

Article:
Source: <<source>>
URL path: <<path>>
Published: <<published>>
Original title: <<title>>
Text:
<<text>>"""


def build_messages(item, source_name, text, attribution=None, candidates=()):
    attr = ""
    if attribution is not None:
        attr = (f"- This piece is opinion by {attribution.full}. type must be \"opinion\" and the headline must start "
                f"with \"{attribution.prefix} \" and present the claims as his or her view.")
    cands = "\n".join(f"- {c['id']}: {c['headline']} | {c.get('s1', '')}" for c in candidates) or "(none)"
    path = urlsplit(item["url"]).path or "/"
    found = validate.hedges_in(" ".join(text.split()[:validate.HEDGE_SCOPE_WORDS]))
    hedges = ("\n  Hedged claims in this text (keep the hedge if the headline covers one): "
              + "; ".join(f'"...{h}..."' for h in found)) if found else ""
    content = entail.fill(PROMPT, attribution=attr, candidates=cands, source=source_name, path=path, hedges=hedges,
                          published=item.get("published_at") or "", title=item["title"], text=text)
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}]


# The ladder

@dataclass
class Step:
    rung: int               # 1 attempt, 2 feedback retry, 3 fallback model
    model: str
    outcome: str            # ok | invalid | bad_json | entail_fail | llm_error
    reason: str = None
    feedback: list = field(default_factory=list)


@dataclass
class WriteResult:
    status: str                         # ok | failed | thin | thin_failed
    output: WriteOutput = None
    full: str = None
    short: str = None
    reason: str = None                  # first failure code of the last step, when not ok
    entailed: bool = None               # None when the gate didn't apply
    passed_at: int = None               # rung that passed: 1, 2 or 3
    model: str = None
    steps: list = field(default_factory=list)


def write_item(client, cfg, item, source_name, text, attribution=None, author=None, candidates=()):
    """Runs the ladder for one item whose text is long enough. Never raises except AuthError."""
    llm_cfg = cfg["llm"]
    rungs = [(1, llm_cfg["model"]), (2, llm_cfg["model"])]
    if llm_cfg.get("fallback_model"):
        rungs.append((3, llm_cfg["fallback_model"]))
    base = build_messages(item, source_name, text.text, attribution, candidates)
    feedback, last_reply, last_out, steps = [], None, None, []
    gone = set()
    for rung, model in rungs:
        if model in gone:
            continue
        messages = list(base)
        if feedback and last_reply:
            messages += [{"role": "assistant", "content": last_reply},
                         {"role": "user", "content": "Your answer failed these checks:\n- " + "\n- ".join(feedback)
                          + "\nFix them and reply with the whole JSON object again."}]
        try:
            reply = client.chat(model, messages, kind="write")
        except ModelGone:
            gone.add(model)
            steps.append(Step(rung, model, "llm_error", "model_gone"))
            continue
        except AuthError:
            raise
        except LLMError as e:
            steps.append(Step(rung, model, "llm_error", str(e)))
            continue
        last_reply = reply
        try:
            out = parse(reply)
        except BadOutput as e:
            feedback = [str(e)]
            steps.append(Step(rung, model, "bad_json", "bad_json", feedback))
            continue
        attribution_mod.apply(out, attribution)
        validate.select(out, attribution)
        last_out = out
        v = validate.check(out, text.text, item["title"], source_name, attribution, author, text.lead())
        if not v.ok:
            feedback = v.feedback
            steps.append(Step(rung, model, "invalid", v.reason, feedback))
            continue
        entailed = None
        if entail.needed(out):
            try:
                e = entail.check(client, llm_cfg["model"], item["title"], text.lead(), out.headline, out.s1)
            except AuthError:
                raise
            except (LLMError, ValueError) as err:
                steps.append(Step(rung, model, "llm_error", f"entail: {err}"))
                continue
            if not e.ok:
                feedback = [e.feedback()]
                steps.append(Step(rung, model, "entail_fail", "entail_fail", feedback))
                continue
            entailed = True
        steps.append(Step(rung, model, "ok"))
        return WriteResult("ok", out, v.full, v.short, None, entailed, rung, model, steps)
    reason = steps[-1].reason if steps else "llm_error"
    # The classification of a failed write is still kept (type, relevance...), but never its text.
    return WriteResult("failed", output=last_out, reason=reason, steps=steps)


def triage_thin(client, cfg, item, source_name, text, attribution=None, candidates=()):
    """Thin items (< 60 words) get one call for their classification only: they may join an event as a member
    (WP3) but never lead one, so their text fields are not used or validated."""
    model = cfg["llm"]["model"]
    try:
        reply = client.chat(model, build_messages(item, source_name, text.text or item["title"], attribution,
                                                  candidates), kind="triage")
        out = parse(reply)
    except AuthError:
        raise
    except (LLMError, BadOutput) as e:
        return WriteResult("thin_failed", reason="no_text", steps=[Step(1, model, "llm_error", str(e))])
    attribution_mod.apply(out, attribution)
    return WriteResult("thin", out, reason="no_text", model=model, steps=[Step(1, model, "ok")])


# The worker stage

def _techmeme_linked(db):
    """Canonical URLs Techmeme links to, which jump the queue with tier-1 items (spec 2.6)."""
    out = {r["url_canonical"] for r in db.q("SELECT url_canonical FROM sightings WHERE source_id = 'techmeme'")}
    for r in db.q("SELECT extra FROM items WHERE source_id = 'techmeme'"):
        linked = json.loads(r["extra"] or "{}").get("linked_url")
        if linked:
            out.add(linked)
    return out


def priority(rows, srcs, db):
    """Tier-1 and Techmeme-linked items first, then the rest; newest first within each group so a backlog after
    downtime writes today's news before yesterday's."""
    by_id = srcs.by_id()
    techmeme = _techmeme_linked(db)

    def key(r):
        src = by_id[r["source_id"]]
        first = sources_mod.effective_tier(src, db.check_ok(src.id)) == 1 or r["url_canonical"] in techmeme
        return (0 if first else 1, _neg_time(r["published_at"]))
    return sorted(rows, key=key)


def _neg_time(s):
    return -from_iso(s).timestamp()


def write_pending(db, srcs, cfg, client, fetch_text=None, now=None):
    """Text → LLM write → validate → entail for due items, in parallel; results are recorded serially."""
    now = now or utcnow()
    stats = collections.Counter()
    llm_cfg = cfg["llm"]
    if llm_cfg.get("daily_budget") and db.llm_calls_since(now - timedelta(hours=24)) >= llm_cfg["daily_budget"]:
        health.notify(db, "llm_budget", f"LLM daily budget of {llm_cfg['daily_budget']} calls used; writing paused")
        stats["budget_exhausted"] += 1
        return stats
    by_id = srcs.by_id()
    rows = [r for r in db.pending_writes(now) if r["source_id"] in by_id and not by_id[r["source_id"]].is_signal]
    rows = priority(rows, srcs, db)[:llm_cfg.get("max_items_per_tick", 18)]
    if not rows:
        return stats
    triaged = {r["url_canonical"] for r in db.q("SELECT url_canonical FROM writes WHERE status = 'thin'")}
    if fetch_text is None:
        session = requests.Session()
        session.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=llm_cfg.get("concurrency", 6)))
        robots = text_mod.Robots(cfg["fetch"]["user_agent"], session)

        def fetch_text(item, source):
            return text_mod.article_text(item, source, cfg["fetch"]["user_agent"], robots, session)

    def job(row):
        item = dict(row)
        source = by_id[row["source_id"]]
        author = json.loads(row["extra"] or "{}").get("author")
        attr = attribution_mod.detect(row["url"], row["title"], author, source.id)
        try:
            text = fetch_text(item, source)
            if text.thin:
                if row["url_canonical"] in triaged:     # classified on an earlier try; only the text is retried
                    return row, text, attr, WriteResult("thin", reason="no_text")
                return row, text, attr, triage_thin(client, cfg, item, source.name, text, attr)
            return row, text, attr, write_item(client, cfg, item, source.name, text, attr, author)
        except AuthError:
            return row, None, attr, None

    with cf.ThreadPoolExecutor(llm_cfg.get("concurrency", 6)) as pool:
        results = list(pool.map(job, rows))

    auth_failed = False
    for row, text, attr, result in results:
        url = row["url_canonical"]
        if result is None:
            auth_failed = True          # left as it was; retried when the key works again
            continue
        if result.output is not None or result.status == "failed":
            db.save_write(url, now, result, text, attr)
        if result.status == "ok":
            db.mark_written(url)
            stats["written"] += 1
            stats[f"passed_at_{result.passed_at}"] += 1
            db.log_write(now, url, "item", result.model, "ok", None)
        else:
            outcome = db.hold(url, result.reason or "no_text", now)
            stats[outcome] += 1
            stats["thin" if result.status.startswith("thin") else "failed"] += 1
            db.log_write(now, url, "item", result.model, "thin" if result.status.startswith("thin") else "failed",
                         result.reason)
    for kind, model, outcome, ms in client.drain():
        db.log_write(now, None, kind, model, outcome, None, ms)
    if auth_failed:
        health.notify(db, "llm_auth", "LLM API returned 401/403; card writing stopped")
        stats["auth_failed"] += 1
    db.commit()
    return stats
