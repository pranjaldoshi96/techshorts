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
    # Other lengths to pick from (validate.select): the model can't count, so it offers short and long versions and
    # code picks the combination that fits. On the fixture it ran short and long about equally often, and a retry
    # asking for "N words" mostly came back unchanged. Not in the spec's schema; absent ones just mean fewer choices.
    headline_alt: str = ""
    headline_short: str = ""
    s1_alt: str = ""
    s2_alt: str = ""
    s3_alt: str = ""
    s1_long: str = ""
    s2_long: str = ""
    s3_long: str = ""

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

    @field_validator("headline", "s1", "s2", "s3", "notif_line", "headline_alt", "headline_short", "s1_alt",
                     "s2_alt", "s3_alt", "s1_long", "s2_long", "s3_long", mode="before")
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

    def chat(self, model, messages, max_tokens=1100, kind="write", tries=3, temperature=0.2):
        body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
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
 "headline": "...", "headline_alt": "...", "headline_short": "...",
 "s1": "...", "s1_alt": "...", "s1_long": "...", "s2": "...", "s2_alt": "...", "s2_long": "...",
 "s3": "...", "s3_alt": "...", "s3_long": "...", "notif_line": "..."}

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
- topics: any of ai, startups_funding, bigtech, dev_tools, security, policy, hardware, india, science, consumer. \
"security" means software and cyber security only (vulnerabilities, breaches, malware); not national, military, \
physical or child safety. "dev_tools" means things developers build with (languages, frameworks, APIs, coding tools).
- region: "us" only for US-only policy, US local news or US-market consumer news. Anything about a global company \
or product is "global" (an Anthropic IPO, CoreWeave earnings and an Nvidia probe are all global). "in" for \
India-market companies, Indian policy or Indian funding.
- entities: names exactly as in the text; amounts as written ("$1.4T").
- event: which candidate event below this article covers (see "Candidate events"), else "new". \
material_update: true when it adds a major new fact to that event.

Writing:
- headline: 50 to 70 characters. Fact-first, sentence case, active voice, present tense. The subject is the \
company or person. Numbers as digits ($80M, $1.4T, 3x). No question, teaser, emoji or final period.
- s1 (18 to 22 words): who did what, with the key number.
- s2 (16 to 20 words): the most important detail or context. S1 and S2 together stand alone as a summary.
- s3 (15 to 19 words): the concrete consequence for builders, investors or operators.
- Each fact once across the three sentences.
- Also give other lengths of each, same facts, still complete sentences: headline_alt (at most 55 characters) and \
headline_short (at most 40 characters); s1_alt, s2_alt, s3_alt (11 to 14 words each); s1_long, s2_long, s3_long \
(24 to 28 words each, adding one more specific fact from the text). Techgist picks the versions that fit the card.
- notif_line: one complete sentence of at most 12 words (under 90 characters), with new information that is \
not in the headline.
Length example only (its facts are invented; never reuse them):
{"headline": "Acme raises $40M to build battery packs for delivery vans",
 "headline_alt": "Acme raises $40M for delivery van batteries",
 "headline_short": "Acme raises $40M for van batteries",
 "s1": "Acme, a Berlin battery startup, raised $40M in a Series B round led by Index Ventures on Tuesday.",
 "s1_alt": "Berlin battery startup Acme raised $40M in a Series B round.",
 "s1_long": "Acme, a Berlin startup that makes battery packs for delivery vans, raised $40M in a Series B round led \
by Index Ventures on Tuesday.",
 "s2": "The company makes swappable battery packs for electric delivery vans and already supplies two of Germany's \
largest parcel carriers.",
 "s2_alt": "Its swappable packs power electric delivery vans for two large parcel carriers.",
 "s2_long": "The company's swappable packs can be changed in five minutes, and it already supplies electric delivery \
vans for two of Germany's largest parcel carriers.",
 "s3": "Fleet operators get a cheaper way to electrify vans without waiting hours for each vehicle to recharge.",
 "s3_alt": "Fleet operators can electrify their vans without waiting for long charging stops.",
 "s3_long": "Fleet operators get a cheaper way to electrify their vans, since a swap takes minutes instead of the hours \
each vehicle would spend at a charger.",
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
Candidate events (stories already in the feed):
<<candidates>>
If this article covers the same real-world event as a candidate (the same announcement, filing, deal, incident or \
scandal), set "event" to that id, even when it takes a different angle, adds analysis or reports a smaller detail: \
one card per event. If it reports a clearly new development of a candidate's story (a resignation, a ruling, a \
second deal), set "event" to "followup:<id>". Otherwise "new".

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
            # A retry at the first call's low temperature mostly came back word for word, ignoring the feedback.
            reply = client.chat(model, messages, kind="write", temperature=0.5 if rung == 2 else 0.2)
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
                e = entail.check(client, llm_cfg["model"], item["title"], entail.context(out, text, attribution),
                                 out.headline, out.s1)
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


def write_pending(db, srcs, cfg, client, fetch_text=None, now=None, candidates_for=None):
    """Text → (embed + candidate events) → LLM write → validate → entail for due items. Texts and writes run in
    parallel; results are recorded serially. `candidates_for([(row, text)])` returns {url: [candidate events]}
    for the prompt (cluster.py); it gets one batched embedding call for the whole tick."""
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
    # A thin item can only ever join an event, and its event often didn't exist yet at its first triage. So a retry
    # of a thin item that is still in no event asks again when there are candidates now (one call, no writing).
    clustered = {r["url_canonical"] for r in db.q("SELECT url_canonical FROM event_members")}
    if fetch_text is None:
        session = requests.Session()
        session.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=llm_cfg.get("concurrency", 6)))
        robots = text_mod.Robots(cfg["fetch"]["user_agent"], session)

        def fetch_text(item, source):
            return text_mod.article_text(item, source, cfg["fetch"]["user_agent"], robots, session)

    def prepare(row):
        source = by_id[row["source_id"]]
        author = json.loads(row["extra"] or "{}").get("author")
        attr = attribution_mod.detect(row["url"], row["title"], author, source.id)
        return row, fetch_text(dict(row), source), attr, author

    with cf.ThreadPoolExecutor(llm_cfg.get("concurrency", 6)) as pool:
        prepared = list(pool.map(prepare, rows))
    cands = {}
    if candidates_for is not None:
        try:
            cands = candidates_for([(row, text) for row, text, _, _ in prepared]) or {}
        except LLMError as e:
            # Candidates only help the model name the event; clustering still runs on cosine and entities.
            log.warning("embedding for candidates failed: %s", e)
            stats["embed_failed"] += 1

    def job(p):
        row, text, attr, author = p
        item = dict(row)
        source = by_id[row["source_id"]]
        c = cands.get(row["url_canonical"], ())
        try:
            if text.thin:
                url = row["url_canonical"]
                if url in triaged and (url in clustered or not c):
                    return row, text, attr, WriteResult("thin", reason="no_text")   # only the text is retried
                return row, text, attr, triage_thin(client, cfg, item, source.name, text, attr, c)
            return row, text, attr, write_item(client, cfg, item, source.name, text, attr, author, c)
        except AuthError:
            return row, None, attr, None

    with cf.ThreadPoolExecutor(llm_cfg.get("concurrency", 6)) as pool:
        results = list(pool.map(job, prepared))

    auth_failed = False
    for row, text, attr, result in results:
        url = row["url_canonical"]
        if result is None:
            auth_failed = True          # left as it was; retried when the key works again
            continue
        if text is not None and text.og_image is not None:
            db.conn.execute("UPDATE items SET og_image = ? WHERE url_canonical = ?", (text.og_image, url))
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


# Event rewrite (spec 2.9 "Event text")

REWRITE_PROMPT = """Several outlets covered one event. Write its card from their reports.

Return this JSON object:
{"importance": 1-10, "headline": "...", "headline_alt": "...", "headline_short": "...",
 "s1": "...", "s1_alt": "...", "s1_long": "...", "s2": "...", "s2_alt": "...", "s2_long": "...",
 "s3": "...", "s3_alt": "...", "s3_long": "...", "notif_line": "..."}

- importance, for the event as a whole: 10 industry-defining. 8-9 frontier model from a top lab, >= $500M \
funding, a breach or outage hitting millions, major regulation. 6-7 $25-500M funding (or >= $15M in India), \
notable launch or earnings. 4-5 smaller launches, open source, policy proposals, strong analysis. 1-3 how-to, \
reviews, explainers, promos, minor updates.
- Same writing rules as a single card: headline 50 to 70 characters, fact-first, sentence case, present tense, \
numbers as digits, no question or final period; s1 18-22 words (who did what, with the key number), s2 16-20 \
words (the most important detail or context), s3 15-19 words (the concrete consequence); _alt versions 11-14 \
words (headline_alt at most 55 characters, headline_short at most 40), _long versions 24-28 words; notif_line one \
sentence under 90 characters with information not in the headline.
- Lead with the newest material fact.
- Numbers: use a number only if REPORT 1 states it or at least two reports state it. When reports disagree, use \
REPORT 1's value with "up to" or "about", or leave the number out.
- Keep hedges (suspected, alleged, reportedly, according to). Use only facts in the reports. Plain text, no \
first person, never "the article". Never use: revolutionize, game-changer, groundbreaking, delve, landscape, \
seamless, cutting-edge.

<<reports>>"""

REWRITE_MEMBERS = 4
REWRITE_WORDS = 300


def representative(members, n=REWRITE_MEMBERS):
    """The lead first, then the hard-news members closest on average to the other hard-news members."""
    import numpy as np
    hard = [m for m in members if m.hard and not m.thin and m.text]
    if len(hard) <= n:
        return hard

    def rep(m):
        return float(np.mean([np.dot(m.vec, o.vec) for o in hard if o is not m]))
    return sorted(hard, key=lambda m: (-rep(m), m.url))[:n]


def rewrite_numbers_ok(out, lead_text, member_texts):
    """Each number in the new text must be in the lead's text or in >= 2 member texts (spec 2.9)."""
    lead_nums = validate.numbers(lead_text)
    member_nums = [validate.numbers(t) for t in member_texts]
    for n in validate.numbers(" ".join([out.headline, out.s1, out.s2, out.s3, out.notif_line])):
        if validate.number_grounded(n, lead_nums):
            continue
        if sum(validate.number_grounded(n, ns) for ns in member_nums) >= 2:
            continue
        return n.text
    return None


def rewrite_event(client, cfg, store, ev):
    """One rewrite call (plus one feedback retry) for an event; on failure the lead's own text stays.
    Returns True when the event got new text."""
    from . import cluster
    row = ev.row
    lead = next((m for m in ev.members if m.url == row.get("lead_url")), None)
    if lead is None or not lead.text:
        return False
    picked = [lead] + [m for m in representative(ev.members) if m is not lead][:REWRITE_MEMBERS - 1]
    reports = "\n\n".join(
        f"REPORT {i}: {store.by_id[m.source_id].name if m.source_id in store.by_id else m.source_id}\n"
        f"Title: {m.title}\nText: {text_mod.cap_words(m.text, REWRITE_WORDS)}" for i, m in enumerate(picked, 1))
    base = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": entail.fill(REWRITE_PROMPT, reports=reports)}]
    model = cfg["llm"]["model"]
    source_text = "\n".join(m.text for m in picked)
    feedback, reply = None, None
    now = store.now
    row["rewritten_at"] = cluster.iso(now)
    row["rewrite_reason"] = None
    for attempt in range(2):
        messages = list(base)
        if feedback:
            messages += [{"role": "assistant", "content": reply},
                         {"role": "user", "content": "Your answer failed these checks:\n- " + "\n- ".join(feedback)
                          + "\nFix them and reply with the whole JSON object again."}]
        try:
            reply = client.chat(model, messages, kind="rewrite", temperature=0.5 if attempt else 0.2)
            data = json.loads(re.search(r"\{.*\}", reply or "", re.S).group(0))
            out = WriteOutput.model_validate({
                "is_news": lead.is_news, "type": lead.type, "relevance": lead.relevance,
                "region": lead.region, "topics": lead.topics, **{k: v for k, v in data.items() if k in (
                    "importance", "headline", "headline_alt", "headline_short", "s1", "s1_alt", "s1_long", "s2",
                    "s2_alt", "s2_long", "s3", "s3_alt", "s3_long", "notif_line")}})
        except AuthError:
            raise
        except (LLMError, AttributeError, ValueError, ValidationError) as e:
            log.info("rewrite %s: %s", ev.id, type(e).__name__)
            return False
        validate.select(out)
        v = validate.check(out, source_text, lead.title, "", None, None, " ".join(lead.text.split()[:200]))
        bad = rewrite_numbers_ok(out, lead.text, [m.text for m in picked])
        if bad:
            v.fail("ungrounded", f"'{bad}' is not in REPORT 1 or in two reports; use REPORT 1's figure or drop it.")
        if not v.ok:
            feedback = v.feedback
            continue
        try:
            e = entail.check(client, model, lead.title, "\n".join(
                text_mod.cap_words(m.text, 200) for m in picked), out.headline, out.s1)
        except AuthError:
            raise
        except (LLMError, ValueError):
            return False
        if not e.ok:
            feedback = [e.feedback()]
            continue
        row["rewrite_importance"] = out.importance
        row["rewrite_publishers"] = row.get("publishers")
        cluster._set_text(row, out.headline, v.full, v.short, out.notif_line, out.s1, 1, "rewrite", now)
        return True
    return False
