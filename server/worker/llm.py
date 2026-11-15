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

from . import (attribution as attribution_mod, entail, health, newsdate, sources as sources_mod, text as text_mod,
               validate)
from .db import from_iso, iso, utcnow

log = logging.getLogger("techgist")

TYPES = ("news", "launch", "funding", "acquisition", "earnings", "policy", "security_incident", "analysis", "opinion",
         "interview", "review", "tutorial", "explainer", "release_notes", "customer_story", "podcast", "promo", "deal",
         "roundup", "other")
TOPICS = ("ai", "startups_funding", "bigtech", "dev_tools", "security", "policy", "hardware", "india", "science",
          "consumer")


def api_key(env="NVIDIA_API_KEY"):
    """The key named `env`: that variable, else the file named by `<env>_FILE` (a root-owned file the service's
    group can read, so the key never sits in a unit file or the journal), else the last export in ~/.bashrc.
    The key is only ever held in memory: it is never logged, written or sent anywhere but its own endpoint."""
    key = os.environ.get(env)
    path = os.environ.get(env + "_FILE")
    if not key and path:
        try:
            key = open(path).read().strip() or None
        except OSError as e:
            log.error("cannot read %s_FILE: %s", env, e.strerror)
    if not key:  # systemd and cron don't load ~/.bashrc; take the last export there
        try:
            lines = [l for l in open(os.path.expanduser("~/.bashrc")) if l.startswith(f"export {env}=")]
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
    # How much a tech-curious reader would want to tap and forward the story (owner's direction, 2026-11-15). It is
    # a ranking and selection factor next to importance and traction (rank.py). 5 = "not scored" for old rows.
    spice: int = Field(default=5, ge=1, le=10)
    breaking: bool = False
    sensitive: bool = False
    topics: List[str] = []
    region: Literal["in", "us", "global"]
    entities: Entities = Entities()
    event: str = "new"
    material_update: bool = False
    # The day the reported news happened when the text shows it is older than the article (YYYY-MM-DD), else "".
    # Used only when the text itself carries that date (newsdate.news_at).
    news_date: str = ""
    headline: str
    s1: str
    s2: str
    s3: str = ""
    notif_line: str = ""
    # The push alert's short title ("Nvidia CEO ousted"); notif_line is its body, a hook of at most 110 characters.
    notif_title: str = ""
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

    @field_validator("news_date", mode="before")
    @classmethod
    def day_or_empty(cls, v):
        # A malformed or missing date is no date; it never costs a retry.
        return v.strip() if isinstance(v, str) and newsdate.parse_day(v) else ""

    @field_validator("topics", mode="before")
    @classmethod
    def known_topics(cls, v):
        # An invented topic shouldn't cost a retry; it just doesn't count.
        return [t for t in (v or []) if t in TOPICS]

    @field_validator("relevance", "importance", "spice", mode="before")
    @classmethod
    def clamp(cls, v, info):
        # Off-scale scores ("relevance": 4) are the commonest schema miss; clamping beats spending a retry.
        lo, hi = (0, 3) if info.field_name == "relevance" else (1, 10)
        if v is None and info.field_name == "spice":
            return 5
        try:
            return min(hi, max(lo, int(round(float(v)))))
        except (TypeError, ValueError):
            return v

    @field_validator("headline", "s1", "s2", "s3", "notif_line", "notif_title", "headline_alt", "headline_short", "s1_alt",
                     "s2_alt", "s3_alt", "s1_long", "s2_long", "s3_long", mode="before")
    @classmethod
    def strip(cls, v, info):
        v = re.sub(r"\s+", " ", v or "").strip()
        # A sentence the model left unpunctuated would run into the next one in the joined summary.
        if not info.field_name.startswith(("headline", "notif_title")) and v and v[-1] not in ".!?\"'”’":
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


class CircuitOpen(LLMError):
    """The endpoint timed out BREAKER_TIMEOUTS times in a row this tick: the remaining items are left untouched for
    the next tick instead of each blocking a worker thread for another timeout."""


# Consecutive timeouts (across threads) that open the circuit. Any answer closes it. Once open it stays open for
# BREAKER_COOLDOWN_S, across ticks: reset_breaker() at the start of a tick used to close it again, so during an
# outage every tick paid the endpoint's full timeout before the backup model took over (60 s per item in the
# 2026-11-15 hub-outage test). After the cooldown one call probes the endpoint; one more timeout re-opens it.
BREAKER_TIMEOUTS = 2
BREAKER_COOLDOWN_S = 300
TIMEOUT_OUTCOMES = ("ReadTimeout", "ConnectTimeout", "Timeout")


class Client:
    # Request fields added to every chat call. The default is Nemotron's "straight answer, no reasoning trace";
    # the hub's GPT and Gemini models take `reasoning_effort` instead and Claude models reject unknown fields, so
    # each provider names its own in config.yaml (llm.providers.<name>.params).
    DEFAULT_PARAMS = {"chat_template_kwargs": {"enable_thinking": False}}

    def __init__(self, base_url, key, bucket=None, session=None, timeout=30, clock=time.monotonic,
                 sleep=time.sleep, params=None):
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.params = dict(self.DEFAULT_PARAMS if params is None else params)
        self.bucket = bucket or TokenBucket(clock=clock, sleep=sleep)
        if session is None:
            # One connection per worker thread (the default pool of 10 was discarding connections at 12).
            session = requests.Session()
            session.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=32))
        self.session = session
        self.timeout = timeout
        self.clock, self.sleep = clock, sleep
        # (kind, model, outcome, ms, prompt tokens, completion tokens) for health.json; drained by the stage
        self.events = []
        self.lock = threading.Lock()
        self.timeouts_in_row = 0
        self.open_until = None

    def reset_breaker(self):
        """Start of a tick: closes a circuit whose cooldown is over, leaving it one timeout from opening again."""
        with self.lock:
            if self.open_until is None:
                self.timeouts_in_row = 0
            elif self.clock() >= self.open_until:
                self.timeouts_in_row = BREAKER_TIMEOUTS - 1
                self.open_until = None

    def _answered(self):
        with self.lock:
            self.timeouts_in_row = 0
            self.open_until = None

    @property
    def circuit_open(self):
        return self.timeouts_in_row >= BREAKER_TIMEOUTS

    def _timed_out(self):
        with self.lock:
            self.timeouts_in_row += 1
            if self.timeouts_in_row >= BREAKER_TIMEOUTS:
                self.open_until = self.clock() + BREAKER_COOLDOWN_S

    def _event(self, kind, model, outcome, ms, tok_in=None, tok_out=None):
        with self.lock:
            self.events.append((kind, model, outcome, ms, tok_in, tok_out))

    def drain(self):
        with self.lock:
            out, self.events = self.events, []
        return out

    def chat(self, model, messages, max_tokens=1100, kind="write", tries=3, temperature=0.2, params=None):
        """`params` replaces the provider's extra request fields for this call (llm.model_params: a model that
        rejects the provider's defaults)."""
        body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
                "response_format": {"type": "json_object"}, **(self.params if params is None else params)}
        for attempt in range(tries):
            if self.circuit_open:
                raise CircuitOpen("circuit open after repeated timeouts")
            self.bucket.acquire()
            started = self.clock()
            try:
                r = self.session.post(f"{self.base_url}/chat/completions", json=body, timeout=self.timeout,
                                      headers={"Authorization": f"Bearer {self.key}"})
            except requests.Timeout as e:
                # No retry: a timed-out call has already cost timeout_s, and in the 2026-11 shadow run timeouts came
                # in bursts where the next try timed out too (3 tries x 45 s stalled ticks for up to 460 s).
                self._event(kind, model, type(e).__name__, int((self.clock() - started) * 1000))
                self._timed_out()
                raise LLMError("timeout")
            except requests.RequestException as e:
                self._event(kind, model, type(e).__name__, int((self.clock() - started) * 1000))
                continue
            self._answered()
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
            try:
                data = r.json()
                usage = data.get("usage") if isinstance(data, dict) else None
            except ValueError:
                data, usage = None, None
            usage = usage if isinstance(usage, dict) else {}
            self._event(kind, model, "ok", ms, usage.get("prompt_tokens"), usage.get("completion_tokens"))
            try:
                return data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError):
                raise LLMError("malformed response")
        raise LLMError("gave up after retries")


def _retry_after(value, default):
    try:
        return max(1.0, float(value))
    except (TypeError, ValueError):
        return default


class Router:
    """Several OpenAI-compatible endpoints behind one Client-shaped object (chat, drain, reset_breaker,
    circuit_open, key).

    A model is named "provider:model id" ("hub:openai/openai/gpt-6.1-sol"); a bare id goes to the default
    provider. When a call fails at the endpoint (timeout, 5xx, 429s, a bad key, a retired model, an open circuit)
    the same messages go once to each backup model in turn: the reader-facing decisions run on the hub's
    GPT/Gemini models, and an outage there costs quality, not the feed. A backup on the provider that just failed
    as a whole (timed out, refused the key, circuit open) is skipped; one on the same provider is tried when only
    the model failed (a 5xx or 429 from one vendor behind the hub).
    Calls are logged under the full "provider:model" name, so health.json shows who answered, and every call a
    backup answered adds a "failover" event under the model that should have: that count is the outage signal
    (degraded.backup_state), since the backup's own calls log "ok"."""

    def __init__(self, clients, default, backup=None, known=(), model_params=None):
        self.clients, self.default = dict(clients), default
        self.backups = [b for b in (backup if isinstance(backup, (list, tuple)) else [backup]) if b]
        self.backup = self.backups[0] if self.backups else None
        # Every configured provider, with or without a key: a model on a keyless one fails over to the backup
        # instead of being sent to the default endpoint under a name it doesn't know.
        self.known = set(known) | set(self.clients)
        self.model_params = dict(model_params or {})
        self.failovers = []
        self.lock = threading.Lock()
        self._local = threading.local()

    def split(self, model):
        prov, sep, name = (model or "").partition(":")
        if sep and prov in self.known:
            return prov, name
        return self.default, model

    @property
    def key(self):
        return self.clients[self.default].key

    def reset_breaker(self):
        for c in self.clients.values():
            c.reset_breaker()

    @property
    def circuit_open(self):
        return all(c.circuit_open for c in self.clients.values())

    def provider_down(self, model):
        """True when `model`'s provider cannot be asked now (no key, or its circuit is open)."""
        prov, _ = self.split(model)
        return prov not in self.clients or self.clients[prov].circuit_open

    def answered_by_backup(self):
        """Whether this thread's last chat call was answered by a backup model."""
        return bool(getattr(self._local, "backup", False))

    def drain(self):
        out = []
        for prov, c in self.clients.items():
            out += [(kind, f"{prov}:{model}", *rest) for kind, model, *rest in c.drain()]
        with self.lock:
            out += self.failovers
            self.failovers = []
        return out

    def _full(self, model):
        prov, name = self.split(model)
        return f"{prov}:{name}"

    def chat(self, model, messages, **kw):
        self._local.backup = False
        first, dead = None, set()
        chain = [model] + [b for b in self.backups if self._full(b) != self._full(model)]
        for i, m in enumerate(chain):
            prov, name = self.split(m)
            client = self.clients.get(prov)
            if prov in dead or (i and (client is None or client.circuit_open)):
                continue
            try:
                if client is None:
                    raise LLMError(f"no key for provider {prov}")
                call = dict(kw)
                if i:
                    # One try: a backup is there to keep the tick moving, not to wait out a second outage.
                    call["tries"] = 1
                if self._full(m) in self.model_params:
                    call["params"] = self.model_params[self._full(m)]
                out = client.chat(name, messages, **call)
            except LLMError as e:
                first = first or e
                if client is None or client.circuit_open or isinstance(e, AuthError) or str(e) == "timeout":
                    dead.add(prov)
                if i + 1 < len(chain):
                    # Debug only: in an outage this is every call. The failover count is in health.json.
                    log.debug("%s failed (%s); trying the next backup", m, type(e).__name__)
                continue
            if i:
                self._local.backup = True
                with self.lock:
                    self.failovers.append((kw.get("kind", "write"), self._full(model), "failover", 0, None, None))
            return out
        raise first


def make_router(cfg, keys=api_key):
    """The chat client from config.yaml: a Router over llm.providers, or None when no provider has a key."""
    llm_cfg = cfg["llm"]
    providers = llm_cfg.get("providers") or {
        "nvidia": {"base_url": llm_cfg["base_url"], "key_env": "NVIDIA_API_KEY",
                   "rate_per_min": llm_cfg.get("rate_per_min", 30)}}
    clients = {}
    for name, p in providers.items():
        key = keys(p.get("key_env") or "NVIDIA_API_KEY")
        if not key:
            log.warning("no key for LLM provider %s (%s)", name, p.get("key_env"))
            continue
        bucket = TokenBucket(rate=p.get("rate_per_min", llm_cfg.get("rate_per_min", 30)), per=60.0)
        clients[name] = Client(p["base_url"], key, bucket, timeout=p.get("timeout_s", llm_cfg["timeout_s"]),
                               params=p.get("params"))
    if not clients:
        return None
    default = llm_cfg.get("default_provider")
    if default not in clients:
        default = "nvidia" if "nvidia" in clients else next(iter(clients))
    backups = llm_cfg.get("backup_models") or llm_cfg.get("backup_model")
    return Router(clients, default, backups, known=providers, model_params=llm_cfg.get("model_params"))


def role_model(cfg, role):
    """The model for a role: `llm.<role>_model`, else `llm.model`. Roles: write (cards), judge (the fact-check),
    cluster (same-development rulings; the judge's model unless set), triage (screening) and thin (classifying
    an item too short to write; the triage model unless set)."""
    if role == "cluster" and not cfg["llm"].get("cluster_model"):
        role = "judge"
    if role == "thin" and not cfg["llm"].get("thin_model"):
        role = "triage" if cfg["llm"].get("triage_model") else "write"
    return cfg["llm"].get(f"{role}_model") or cfg["llm"]["model"]


# Prompt

SYSTEM = ("You are the editor of Techgist, a tech news app for tech-curious readers in Bengaluru and the Bay Area. "
          "Each story is one card and one push alert. You classify one article and write its card: punchy, and "
          "true to the article in every word. Reply with only a JSON object.")

PROMPT = """Return this JSON object:
{"is_news": true, "type": "...", "relevance": 0-3, "importance": 1-10, "spice": 1-10, "breaking": false,
 "sensitive": false, "topics": [...], "region": "in|us|global",
 "entities": {"orgs": [], "people": [], "products": [], "amounts": []},
 "event": "new", "material_update": false, "news_date": "",
 "headline": "...", "headline_alt": "...", "headline_short": "...",
 "s1": "...", "s1_alt": "...", "s2": "...", "s2_alt": "...", "s3": "...", "s3_alt": "...",
 "notif_title": "...", "notif_line": "..."}

Classification:
- is_news: true when it reports something that happened or was announced; false for evergreen how-tos, \
explainers, reviews, essays without news, promos.
- type, one of: news (reporting a new fact; the default), launch (a product, model or feature release), \
funding (a startup raise or a new VC fund), acquisition, earnings, policy (law, regulation, court or government \
action), security_incident (breach, hack, outage, exploited vulnerability), analysis (reported analysis or \
commentary on news), opinion (a columnist's or pundit's view), interview, review (a hands-on review of a product \
for buyers; a benchmark, test or study that reports what it found is news or analysis, not review), tutorial, \
explainer (evergreen background), release_notes (version notes for a library, tool or app), customer_story (a \
vendor's story about a customer using its product), podcast (an episode page or its show notes; an article that \
reports what someone said or what a project released is news or interview even if a recording is attached), \
promo, deal (a shopping deal), roundup (a digest or newsletter issue that lists several unrelated stories), \
other. A profile ("Who is ..."), timeline, "what we \
know" or "what happens next" piece, live blog or round-up of reactions to a known story is explainer or analysis, \
not news, even when it is current.
- relevance, does the story have a tech angle: 3 = tech is the story (a tech company, product, founder, \
investor, technology or its regulation); 2 = a real tech angle in a wider story (a scandal, lawsuit, firing, \
feud, layoff, leak, ban, outage, hack, big money or viral moment that involves a tech company or a tech person; \
creators, gaming, crypto, space, EVs or gadgets when the story is big; science that surprises; any story where \
AI is the subject or the means); 1 = tech is only mentioned in passing; 0 = no tech angle (general politics, \
sport, lifestyle, celebrity or business news with no tech company, person or technology in it).
- importance: <<importance>>
- spice: <<spice>>
- breaking: true only for sudden, just-happened news of importance 8 or more.
- sensitive: true when the story involves crime, espionage, sexual conduct, lawsuits, or allegations against a \
named person.
- topics: any of ai, startups_funding, bigtech, dev_tools, security, policy, hardware, india, science, consumer; \
the main topic first. "ai" whenever AI models, AI companies or AI's effects (on jobs, markets, law) are the \
subject. "startups_funding" for startups, founders, venture investors and their rounds, exits, shutdowns and \
dramas. "security" means software and cyber security only (vulnerabilities, breaches, malware); not national, \
military, physical or child safety. "dev_tools" only when developers are the audience: languages, frameworks, \
APIs, SDKs, IDEs, coding agents, cloud or devops platforms; never for a company's funding, earnings, deals, \
hiring or leadership, nor for hardware, rockets or consumer app outages.
- region: "us" only for US-only policy, US local news or US-market consumer news. Anything about a global company \
or product is "global" (an Anthropic IPO, CoreWeave earnings and an Nvidia probe are all global). "in" for \
India-market companies, Indian policy or Indian funding.
- entities: names exactly as in the text; amounts as written ("$1.4T").
- event: which candidate event below this article covers (see "Candidate events"), else "new". \
material_update: true when it adds a major new fact to that event, above all when something the candidate's \
headline describes as coming has now happened (an IPO that was "upcoming" has priced or listed, a planned launch \
has shipped, a deal "in talks" is signed, a resignation has taken effect, a vote or ruling has been made). \
False for reactions, analysis, background and smaller details.
- news_date: <<news_date>>

Writing. The card must make a tech reader want to tap, using only what the article states:
- headline: 50 to 70 characters. Lead with the story's most striking true fact: the conflict, the name, the \
number or the surprise. Sentence case, active voice. The subject is the company or person, and the headline keeps \
the object of the action ("urge Nvidia board to keep door open for Huang's return", not "... to keep door \
open"). Numbers as digits ($80M, $1.4T, 3x). No question, no teaser that withholds the fact, no emoji, no final \
period. Strong verbs are welcome when the article supports them; never a stronger claim than the article makes.
- s1 (18 to 22 words): the next fact after the headline. The reader has just read the headline, so do not \
restate it or open with its words: start with what it lacks (the key number, who else, when, how, why).
- s2 (16 to 20 words): the most important detail or context. S1 and S2 together stand alone as a summary.
- s3 (15 to 19 words): one more fact stated in the text: what happens next (a date, a vote, a hearing), a \
reaction or a number. A fact from the text, never your own view of what it means or may lead to.
- Each fact once across the headline and the three sentences. State the fact; never tease it ("one key \
quality", "here is what happened").
- Also give other lengths, same facts, still complete sentences: headline_alt (at most 55 characters) and \
headline_short (at most 40 characters); s1_alt, s2_alt, s3_alt (11 to 14 words each). Techgist picks the versions \
that fit the card.
- notif_title: the push alert's title, at most 40 characters: who or what, and what happened ("Nvidia CEO \
ousted", "Meta fined 805M euros"). Never only a name ("Gary Marcus", "Nvidia"): it must say what happened.
- notif_line: the push alert's body, one sentence of at most 110 characters, written to be tapped: the single \
most striking concrete fact of the story (a name, a number, a quoted word), worded differently from the \
headline. Every accuracy rule below applies to it. Not a question and not a teaser. A flat line restates the \
topic; a good one hands over the detail a friend would repeat. Examples of the difference (invented stories):
  flat "Varnet is seeking guarantees from its tenants."  ->  good "Varnet wants its tenants, Orbitly among \
them, to guarantee $4B of its power bills."
  flat "A rocket failed after launch on Sunday."  ->  good "Kestrel's rocket broke up 4 minutes after liftoff, \
taking 40 Novacloud satellites with it."
  flat "The CEO commented on remote work in a memo."  ->  good "'Tourists': that is what Novacloud's CEO calls \
remote staff in a leaked memo."
- A short text (under 150 words: a teaser, a paywall's opening, a blurb): write the headline, s1 and s2 from \
its facts and leave s3 and s3_alt empty. Every sentence states a fact about the story itself; never fill a \
sentence with scene-setting or colour (what hung on a wall, where the writer was sitting, how someone felt, an \
app-store ranking that is not the news).
Length example only (its facts are invented; never reuse them):
{"headline": "Acme fires CEO Dana Ruiz a week after its $40M raise",
 "headline_alt": "Acme fires CEO a week after $40M raise",
 "headline_short": "Acme fires CEO after $40M raise",
 "s1": "The Berlin battery startup's board voted on Tuesday to remove Ruiz, who founded the company in 2019.",
 "s1_alt": "The board voted on Tuesday to remove Ruiz, the founder.",
 "s2": "Index Ventures led the Series B round, and chief operating officer Lena Vogt takes over as interim \
chief executive.",
 "s2_alt": "Operating chief Lena Vogt takes over as interim chief executive.",
 "s3": "Acme said it will name a permanent chief executive before its next board meeting in March.",
 "s3_alt": "A permanent chief executive is due before March's board meeting.",
 "notif_title": "Acme fires its founder-CEO",
 "notif_line": "Dana Ruiz is out at Acme seven days after closing a $40M round; the COO steps in."}

Accuracy (these outrank punchiness; a card that breaks one is thrown away):
- Use only facts in the text below. Never add names, places, numbers, dates, weekdays or claims that are not in \
it, and keep the text's own wording for names and figures ("third quarter", not "Q3"). If the text is short, be \
less specific rather than inventing.
- Tense and certainty match the text. What is planned, expected, proposed, reported to be coming or under \
discussion stays that way ("to issue", "plans to", "is expected to", "in talks"): "Sebi to issue AI guidelines" \
must never become "Sebi issues AI guidelines"; an expected price is not a price; an invitation to buy is not a \
sale; a filing is not a ruling; talks are not a deal. Something the text describes as having happened earlier \
(last year, in June) is background, not today's news, and keeps its date.
- A number keeps the meaning the text gives it: what was measured, by whom, over which period. Do not attach a \
figure to a different thing, and do not turn "among the largest" into "the largest" or "record", or "close to" \
into "matches".
- Keep hedges: if the text says suspected, alleged, reportedly, accused, claims or according to, the headline \
keeps that word or attributes the claim ("X says ...", "Report: ...").<<hedges>>
- Sourcing: when the facts rest on unnamed sources or on another outlet's reporting ("people familiar with the \
matter", "sources said", "Bloomberg reported"), the headline or s1 says so ("sources say", "CNBC reports", \
"Report: ..."). "Report:" is only for that kind of third-party reporting, never for what a company itself \
announced, posted or filed.
- Allegations against a named person or company are written as allegations, with who makes them ("prosecutors \
allege", "the lawsuit claims", "X accuses Y of"); never as established fact. Never add a cause, motive, blame or \
consequence that the text does not state. Nothing sexual or graphic beyond what the text reports in plain news \
language.
- A digest or newsletter issue covering several unrelated stories: set type to "roundup"; never join facts from \
two of its items into one claim.
- Opinion, analysis and interview pieces: the headline starts with the author's surname and a colon \
("Thompson: ...") or with "Analysis:", and presents claims as that person's view.
- Plain text. No markdown, no first person (I, we, our), never "the article". Don't mention subscribing, \
newsletters or podcasts. Never use: revolutionize, game-changer, groundbreaking, delve, landscape, seamless, \
cutting-edge.
<<attribution>>
Candidate events (stories already in the feed):
<<candidates>>
If this article reports the same specific development as a candidate (the same announcement, filing, letter, \
statement, deal, incident or ruling), set "event" to that id, even when it takes a different angle or adds \
detail: one card per development. If it reports a new development in a candidate's ongoing story (a new \
statement, a new revelation, a resignation, a ruling, a reaction by a new actor), set "event" to \
"followup:<id>". Otherwise "new".

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

# Room for the card JSON plus a reasoning model's short thinking (the hub's GPT models count it against the limit).
WRITE_TOKENS = 2200
# Under this many words of text a card is two sentences, not three. A third sentence written from a paywall's
# 80-word teaser or a podcast blurb was colour, not information ("Framed art by rock legends John Lennon and David
# Bowie hung on the wall"; "he describes himself as grouchy").
SHORT_TEXT_WORDS = 150


@dataclass
class Step:
    rung: int               # 1 attempt, 2 feedback retry, 3 fallback model
    model: str
    outcome: str            # ok | invalid | bad_json | entail_fail | llm_error
    reason: str = None
    feedback: list = field(default_factory=list)


@dataclass
class WriteResult:
    status: str                         # ok | failed | llm_error | thin | thin_failed
    output: WriteOutput = None
    full: str = None
    short: str = None
    reason: str = None                  # first failure code of the last step, when not ok
    entailed: bool = None               # None when the gate didn't apply
    passed_at: int = None               # rung that passed: 1, 2 or 3
    model: str = None
    steps: list = field(default_factory=list)
    # A backup model answered a write or fact-check call of this run (the primary's endpoint was failing).
    by_backup: bool = False
    # When the news happened, if days before the article (newsdate.news_at); set by write_pending.
    news_at: object = None


# Extra feedback retries for a card being written by a backup model (write_item).
BACKUP_EXTRA_RUNGS = 1


def _primary_down(client, model):
    fn = getattr(client, "provider_down", None)
    return bool(fn and fn(model))


def _by_backup(client):
    fn = getattr(client, "answered_by_backup", None)
    return bool(fn and fn())


def write_item(client, cfg, item, source_name, text, attribution=None, author=None, candidates=(), prior=None):
    """Runs the ladder for one item whose text is long enough. Never raises except AuthError and CircuitOpen.

    `prior` is (last reply, feedback) from this item's previous ladder run, for a hold retry after a content
    failure: the run then continues that conversation (a feedback turn at a higher temperature) instead of
    sending the same prompt again, which in the shadow run mostly reproduced the same failure."""
    llm_cfg = cfg["llm"]
    writer = role_model(cfg, "write")
    rungs = [(1, writer), (2, writer)]
    if llm_cfg.get("fallback_model"):
        rungs.append((3, llm_cfg["fallback_model"]))
    base = build_messages(item, source_name, text.text, attribution, candidates)
    feedback, last_reply, last_out, steps = [], None, None, []
    by_backup = False
    continued = bool(prior and prior[0] and prior[1])
    if continued:
        last_reply, feedback = prior[0], list(prior[1])
    hedge_scope = validate.hedge_scope(item["title"], text.lead() or text.text)
    gone = set()
    extra_rungs = BACKUP_EXTRA_RUNGS
    i = -1
    while i + 1 < len(rungs):
        i += 1
        rung, model = rungs[i]
        # A backup model needs more tries to pass the same checks (the 2026-11-15 replay with the hub taken away:
        # half its failures were a second "ungrounded" after fixing the first). While a backup is answering, a
        # failed feedback retry gets one more, with the latest feedback; the checks themselves do not change.
        if i == len(rungs) - 1 and rung == 2 and extra_rungs and (by_backup or _primary_down(client, model)):
            extra_rungs -= 1
            rungs.append((2, model))
        if model in gone:
            continue
        messages = list(base)
        retrying = bool(feedback and last_reply)
        if retrying:
            messages += [{"role": "assistant", "content": last_reply},
                         {"role": "user", "content": "Your answer failed these checks:\n- " + "\n- ".join(feedback)
                          + "\nFix them and reply with the whole JSON object again."}]
        try:
            # A retry at the first call's low temperature mostly came back word for word, ignoring the feedback.
            # A hold retry continues the last run: its first call is already a feedback turn, its second warmer still.
            temperature = (0.2 if rung == 3 else {1: 0.5, 2: 0.7}[rung] if continued
                           else 0.5 if rung == 2 else 0.2)
            if i >= 2 and rung == 2:
                temperature = 0.7       # the extra retry on a backup model
            reply = client.chat(model, messages, kind="write", temperature=temperature, max_tokens=WRITE_TOKENS)
            by_backup = by_backup or _by_backup(client)
        except ModelGone:
            gone.add(model)
            steps.append(Step(rung, model, "llm_error", "model_gone"))
            continue
        except (AuthError, CircuitOpen):
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
        attribution_mod.apply(out, attribution, item["url"], item["title"])
        own_headline = out.headline
        validate.select(out, attribution, hedge_source=hedge_scope)
        last_out = out
        v = validate.check(out, text.text, item["title"], source_name, attribution, author, text.lead(),
                           short_card=text.words < SHORT_TEXT_WORDS)
        if not v.ok:
            feedback = v.feedback
            steps.append(Step(rung, model, "invalid", v.reason, feedback))
            continue
        entailed = None
        if entail.needed(out):
            try:
                e = entail.check(client, role_model(cfg, "judge"), item["title"],
                                 entail.context(out, text, attribution), out.headline, out.s1, out.s2,
                                 out.s3 if v.full != v.short else "", out.notif_title, out.notif_line,
                                 source_name)
                by_backup = by_backup or _by_backup(client)
            except (AuthError, CircuitOpen):
                raise
            except (LLMError, ValueError) as err:
                steps.append(Step(rung, model, "llm_error", f"entail: {err}"))
                continue
            if not e.ok:
                feedback = [e.feedback()]
                if "headline" in e.line.lower():
                    # The label the fact-checker objects to may be one code added (validate.select).
                    hint = validate.label_feedback(out.headline, own_headline, hedge_scope)
                    feedback += [hint] if hint else []
                steps.append(Step(rung, model, "entail_fail", "entail_fail", feedback))
                continue
            entailed = True
        steps.append(Step(rung, model, "ok"))
        return WriteResult("ok", out, v.full, v.short, None, entailed, rung, model, steps, by_backup=by_backup)
    # The classification of a failed write is still kept (type, relevance...), but never its text.
    content = [s for s in steps if s.outcome in ("invalid", "entail_fail")]
    if content and last_out is not None and text.words < SHORT_TEXT_WORDS and any(
            s.reason == validate.TEASER for s in content):
        # A title with no report under it (validate.TEASER): the item is classified, not written, like any item
        # with too little text. It can back an event up, and start one that gets a brief if the story has
        # traction (cluster.thin_can_seed), instead of shipping a card of scene-setting.
        return WriteResult("thin", last_out, reason="no_text", model=writer, steps=steps)
    if content:
        # A timeout on the feedback retry doesn't hide that the text failed validation first.
        return WriteResult("failed", output=last_out, reason=content[-1].reason, steps=steps, by_backup=by_backup)
    if steps and all(s.outcome == "llm_error" for s in steps):
        # The endpoint failed, not the text: transient, held, and kept out of the validation pass rate.
        return WriteResult("llm_error", output=last_out, reason="llm_error", steps=steps)
    return WriteResult("failed", output=last_out, reason=steps[-1].reason if steps else "llm_error", steps=steps)


def triage_thin(client, cfg, item, source_name, text, attribution=None, candidates=()):
    """Thin items (< 60 words) get one call for their classification only: they may join an event as a member
    (WP3) but never lead one, so their text fields are not used or validated."""
    # The smaller model: only the classification of a thin item is kept, and the paywalled business feeds
    # (a title and a sentence each) are a large share of every tick.
    model = role_model(cfg, "thin")
    try:
        reply = client.chat(model, build_messages(item, source_name, text.text or item["title"], attribution,
                                                  candidates), kind="triage", max_tokens=WRITE_TOKENS)
        out = parse(reply)
    except (AuthError, CircuitOpen):
        raise
    except (LLMError, BadOutput) as e:
        return WriteResult("thin_failed", reason="no_text", steps=[Step(1, model, "llm_error", str(e))])
    attribution_mod.apply(out, attribution, item["url"], item["title"])
    return WriteResult("thin", out, reason="no_text", model=model, steps=[Step(1, model, "ok")])


def prior_attempt(db, url):
    """(last reply, its feedback) from the item's stored failed write, for a hold retry; None when there is none."""
    row = db.one("SELECT status, output, steps FROM writes WHERE url_canonical = ?", url)
    if row is None or row["status"] != "failed" or not row["output"]:
        return None
    steps = json.loads(row["steps"] or "[]")
    fb = next((s.get("feedback") for s in reversed(steps) if s.get("feedback")), None)
    return (row["output"], fb) if fb else None


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
    rows = priority(rows, srcs, db)
    # One cheap screening call for the tick's unseen items, before any of them costs a full write: the wide source
    # list (business press, gossip, gaming, crypto, regulators) carries much that has no tech angle at all.
    if llm_cfg.get("triage") and rescreen_on_prompt_change(db, now):
        rows = priority([r for r in db.pending_writes(now)
                         if r["source_id"] in by_id and not by_id[r["source_id"]].is_signal], srcs, db)
    if llm_cfg.get("triage") and rows:
        vetted = _techmeme_linked(db)
        fresh = [r for r in rows if r["status"] == "new" and not r["triaged"] and r["url_canonical"] not in vetted
                 and not by_id[r["source_id"]].curated
                 and not json.loads(r["extra"] or "{}").get("via")][:TRIAGE_BATCH]
        drops = triage_batch(client, cfg, fresh, by_id)
        for r in fresh:
            url = r["url_canonical"]
            if url in drops:
                rule = f"triage_{drops[url]}"
                db.conn.execute("UPDATE items SET status = 'dropped', drop_rule = ? WHERE url_canonical = ?",
                                (rule, url))
                db.log_drop(now, r["source_id"], r["url"], r["title"], rule)
                stats["triaged_out"] += 1
            else:
                db.conn.execute("UPDATE items SET triaged = 1 WHERE url_canonical = ?", (url,))
        rows = [r for r in rows if r["url_canonical"] not in drops]
    rows = rows[:llm_cfg.get("max_items_per_tick", 18)]
    if not rows:
        for kind, model, outcome, ms, tok_in, tok_out in client.drain():
            db.log_write(now, None, kind, model, outcome, None, ms, tok_in=tok_in, tok_out=tok_out)
        db.commit()
        return stats
    # Items the tech world is already passing around (a Techmeme pick, a Hacker News or Lobsters link past the
    # import bar): a failed or text-less one is retried within minutes, not at +30 min and +2 h.
    techmeme_urls = _techmeme_linked(db)
    hot = {r["url_canonical"] for r in rows
           if r["url_canonical"] in techmeme_urls or json.loads(r["extra"] or "{}").get("via")}
    triaged = {r["url_canonical"] for r in db.q("SELECT url_canonical FROM writes WHERE status = 'thin'")}
    teasers = {r["url_canonical"]: r["text_words"] for r in db.q(
        "SELECT url_canonical, text_words FROM writes WHERE status = 'thin' AND steps LIKE ?",
        f"%{validate.TEASER}%")}
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

    priors = {row["url_canonical"]: prior_attempt(db, row["url_canonical"])
              for row, text, _, _ in prepared if row["status"] == "held" and not text.thin}
    client.reset_breaker()

    def job(p):
        row, text, attr, author = p
        item = dict(row)
        source = by_id[row["source_id"]]
        # An article imported from a signal's link under the catch-all source is named by its host.
        source_name = json.loads(row["extra"] or "{}").get("publisher") or source.name
        c = cands.get(row["url_canonical"], ())
        try:
            if client.circuit_open:
                return row, None, attr, "circuit"
            url = row["url_canonical"]
            if not text.thin and teasers.get(url) == text.words:
                # Classified on an earlier run as a teaser (validate.TEASER) and its text has not changed: only
                # another text would change that, so the ladder is not run again.
                return row, text, attr, WriteResult("thin", reason="no_text")
            if text.thin:
                if url in triaged and (url in clustered or not c):
                    return row, text, attr, WriteResult("thin", reason="no_text")   # only the text is retried
                return row, text, attr, triage_thin(client, cfg, item, source_name, text, attr, c)
            return row, text, attr, write_item(client, cfg, item, source_name, text, attr, author, c,
                                               priors.get(row["url_canonical"]))
        except AuthError:
            return row, None, attr, None
        except CircuitOpen:
            return row, None, attr, "circuit"

    with cf.ThreadPoolExecutor(llm_cfg.get("concurrency", 6)) as pool:
        results = list(pool.map(job, prepared))

    auth_failed = False
    for row, text, attr, result in results:
        url = row["url_canonical"]
        if isinstance(result, str):
            stats["circuit_skipped"] += 1   # left as it was (new or due), for the next tick
            continue
        if result is None:
            auth_failed = True          # left as it was; retried when the key works again
            continue
        if text is not None and text.og_image is not None:
            db.conn.execute("UPDATE items SET og_image = ? WHERE url_canonical = ?", (text.og_image, url))
        attempt = (row["attempts"] or 0) + 1
        published = backdate(db, row, text)
        if result.status == "ok":
            result.news_at = newsdate.news_at(result.output.news_date, published, row["title"], text.text,
                                               result.output.type)
        if result.output is not None or result.status == "failed":
            db.save_write(url, now, result, text, attr)
        if result.status == "ok":
            db.mark_written(url)
            stats["written"] += 1
            stats[f"passed_at_{result.passed_at}"] += 1
            db.log_write(now, url, "item", result.model, "ok", None, attempt=attempt)
        elif result.status == "failed" and result.by_backup:
            # The card failed on a backup model while the writer's or fact-checker's endpoint was out. That says
            # little about the card: it waits for the primary without using up its retries (it used to be held,
            # retried on the backup again, and dropped). Counted apart from the validation pass rate; a run of
            # these is what turns an outage into a degraded edition (degraded.backup_state).
            db.hold(url, BACKUP_FAILED, now)
            stats["backup_failed"] += 1
            db.log_write(now, url, "item", result.model, BACKUP_FAILED, result.reason, attempt=attempt)
        else:
            outcome = db.hold(url, result.reason or "no_text", now, hot=url in hot)
            stats[outcome] += 1
            logged = "thin" if result.status.startswith("thin") else result.status
            stats[logged] += 1
            db.log_write(now, url, "item", result.model, logged, result.reason, attempt=attempt)
    for kind, model, outcome, ms, tok_in, tok_out in client.drain():
        db.log_write(now, None, kind, model, outcome, None, ms, tok_in=tok_in, tok_out=tok_out)
    if auth_failed:
        health.notify(db, "llm_auth", "LLM API returned 401/403; card writing stopped")
        stats["auth_failed"] += 1
    db.commit()
    return stats


BACKUP_FAILED = "backup_failed"
PAGE_DATE_SLACK = timedelta(hours=1)
PAGE_DATE_MAX = timedelta(days=30)


def backdate(db, row, text):
    """The item's publisher time, moved back to the page's own publication time when the page states an earlier
    one: a feed that dates an entry by its last update, or carries no date at all (the item was then dated by
    when the worker first fetched it, and a days-old post read as minutes old). Never forward. Returns the
    item's published_at after any change."""
    published = from_iso(row["published_at"])
    page_at = getattr(text, "page_published_at", None) if text is not None else None
    if page_at is None or page_at >= published or published - page_at > PAGE_DATE_MAX:
        return published
    if row["feed_published_at"] is not None and published - page_at < PAGE_DATE_SLACK:
        return published           # the feed's own date, within an hour of the page's: the feed's stands
    db.conn.execute("UPDATE items SET published_at = ? WHERE url_canonical = ?", (iso(page_at), row["url_canonical"]))
    return page_at


# Briefs: a short attributed card for a story with traction and no full text (cluster.brief_due)

BRIEF_PROMPT = """A story is breaking and only short reports of it are available: outlets' headlines and \
opening lines (a paywall, or a page that could not be read), and Techmeme's summary of the report. Write a short \
card from these LINES and nothing else. A HACKER NEWS line, when there is one, says how widely the link is being \
discussed: the card may state that as a fact about the discussion, but Hacker News is never whose reporting it is.

Return this JSON object:
{"spice": 1-10, "headline": "...", "headline_alt": "...", "headline_short": "...", "s1": "...", "s2": "...",
 "notif_title": "...", "notif_line": "..."}

- spice: <<spice>>
- headline: 50 to 70 characters, leading with the most striking fact in the LINES. Sentence case, numbers as \
digits, no question, no teaser, no final period. It says whose reporting this is: start with "Report:" or name \
the outlet ("FT: ...", "..., Bloomberg reports"). headline_alt: the same in at most 55 characters. \
headline_short: at most 40 characters.
- s1 (12 to 24 words): the next fact in the LINES after the headline, naming the outlet that reports it.
- s2 (8 to 22 words): one more fact stated in the LINES; an empty string when they hold no further fact. Never \
pad, never explain what the story means, never add background from memory.
- notif_title: at most 40 characters: who or what, and what happened; never only a name.
- notif_line: one sentence of at most 110 characters written to be tapped: the most striking concrete fact in \
the LINES, worded differently from the headline. Not a question, not a teaser.
- Accuracy: use only facts in the LINES; names, numbers and wording as given. Tense and certainty as given: \
what is discussed, planned, expected or sought stays that way. Keep hedges and sourcing ("sources say", \
"reportedly", "according to"). Allegations are written as allegations with who makes them. Never add a cause, \
motive, superlative or consequence. Plain text, no first person, never "the article" or "the lines".

LINES
<<lines>>"""
BRIEF_PROMPT = BRIEF_PROMPT.replace("<<spice>>", "see the scale below")


def brief_event(client, cfg, store, ev):
    """One call (plus one feedback retry) that writes a brief for an event with traction and no full text, from
    the lines available (cluster.brief_lines), checked by the same deterministic rules as any card and
    fact-checked against those lines only. Returns True when the event got a card.

    The card names whose reporting it is, links to the best-placed outlet (the event's lead) and is replaced by
    a member's own validated card as soon as an open outlet reports the story (cluster.refresh)."""
    from . import cluster
    row, now = ev.row, store.now
    lead = next((m for m in ev.members if m.url == row.get("lead_url")), None)
    lines = cluster.brief_lines(store, ev)
    if lead is None or not lines:
        return False
    row["rewritten_at"] = cluster.iso(now)
    row["rewrite_publishers"] = row.get("publishers")
    # Whose reporting it is: the outlets, never the place it is being discussed.
    outlets = [name for name, _, _ in lines if name != cluster.HN_LINE]
    if not outlets:
        return False
    block = "\n".join(f"{name.upper()}: {title}" + (f" | {text}" if text else "") for name, title, text in lines)
    source_text = "\n".join(f"{name}: {title}. {text}" for name, title, text in lines)
    base = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": entail.fill(BRIEF_PROMPT, lines=block) + "\n\nSpice scale: " + SPICE}]
    model = role_model(cfg, "write")
    feedback, reply = None, None
    for attempt in range(2):
        messages = list(base)
        if feedback:
            messages += [{"role": "assistant", "content": reply},
                         {"role": "user", "content": "Your answer failed these checks:\n- " + "\n- ".join(feedback)
                          + "\nFix them and reply with the whole JSON object again."}]
        try:
            reply = client.chat(model, messages, kind="brief", temperature=0.5 if attempt else 0.2,
                                max_tokens=WRITE_TOKENS)
            data = json.loads(re.search(r"\{.*\}", reply or "", re.S).group(0))
            out = WriteOutput.model_validate({
                "is_news": lead.is_news, "type": lead.type, "relevance": lead.relevance, "region": lead.region,
                "topics": lead.topics, "importance": lead.importance, "s2": "",
                **{k: v for k, v in data.items() if k in ("spice", "headline", "headline_alt", "headline_short",
                                                          "s1", "s2", "notif_line", "notif_title") and v}})
        except AuthError:
            raise
        except (LLMError, AttributeError, ValueError, ValidationError) as e:
            log.info("brief %s: %s", ev.id, type(e).__name__)
            return False
        if out.s2 == ".":
            out.s2 = ""
        heads = [h for h in (out.headline, out.headline_alt) if h and not validate.headline_problems(h)]
        out.headline = heads[0] if heads else out.headline
        if not 0 < len(out.notif_title) <= validate.NOTIF_TITLE_MAX or validate.name_only(out.notif_title):
            short = out.headline_short
            out.notif_title = short if 0 < len(short) <= validate.NOTIF_TITLE_MAX else ""
        v = validate.check(out, source_text, lead.title, lead.publisher or "", None, None, source_text,
                           brief=True, outlets=outlets)
        if not v.ok:
            feedback = v.feedback
            continue
        try:
            e = entail.check(client, role_model(cfg, "judge"), lead.title, block, out.headline, out.s1, out.s2,
                             "", out.notif_title, out.notif_line, outlets[0])
        except AuthError:
            raise
        except (LLMError, ValueError):
            return False
        if not e.ok:
            feedback = [e.feedback()]
            continue
        row["rewrite_spice"] = out.spice
        cluster._set_text(row, out.headline, v.full, v.short, out.notif_line, out.s1, 1, cluster.BRIEF, now,
                          out.notif_title)
        log.info("brief written for %s from %d lines: %s", ev.id, len(lines), out.headline)
        return True
    return False


# Event rewrite (spec 2.9 "Event text")

REWRITE_PROMPT = """Several outlets covered one development. Write its card from their reports.

Return this JSON object:
{"importance": 1-10, "spice": 1-10, "headline": "...", "headline_alt": "...", "headline_short": "...",
 "s1": "...", "s1_alt": "...", "s2": "...", "s2_alt": "...", "s3": "...", "s3_alt": "...",
 "notif_title": "...", "notif_line": "..."}

- importance, for the event as a whole: <<importance>>
- spice: <<spice>>
- Same writing rules as a single card: headline 50 to 70 characters, leading with the most striking true fact, \
sentence case, numbers as digits, keeping the object of the action, no question, teaser or final period; s1 18-22 \
words (the next fact after the headline, never restating it), s2 16-20 words (the most important detail or \
context), s3 15-19 words (one more fact from the reports: what happens next, a reaction or a number; never your \
own inference); _alt versions 11-14 words (headline_alt at most 55 characters, headline_short at most 40); \
notif_title at most 40 characters (who or what, and the gist); notif_line one sentence of at most 110 characters \
written to be tapped: the most striking concrete fact, worded differently from the headline.
- Lead with the newest material fact. Each report carries its publication time. Where reports give different \
accounts of the same thing (a cause, a status, a count, whether something planned has happened), the newest \
report's account is the one the card states, attributed to whoever gives it; an older account is left out or \
given as what was said earlier ("after first blaming ..."). Never state as still to come what a newer report \
says has happened.
- Numbers: use a number only if REPORT 1 states it or at least two reports state it. When reports disagree, use \
REPORT 1's value with "up to" or "about", or leave the number out. A number keeps the meaning the reports give it.
- Tense and certainty match the reports: planned, expected, proposed or reported-to-be-coming stays that way; \
earlier events keep their dates. Keep hedges (suspected, alleged, reportedly, according to). When the facts rest \
on unnamed sources or one outlet's reporting, say so ("sources say", "CNBC reports"). Allegations against a named \
person or company are written as allegations with who makes them. Never add a cause, motive, superlative or \
consequence the reports do not state. Use only facts in the reports. Plain text, no first person, never "the \
article". Never use: revolutionize, game-changer, groundbreaking, delve, landscape, seamless, cutting-edge.

<<reports>>"""

# The importance scale and its anchors, shared by the write and rewrite prompts (spec 2.10: "the same anchors").
# The anchors are invented stories of each tier, not real ones: the first set named the week's two biggest real
# stories (the Anthropic IPO = 10, the Nvidia CEO spy probe = 9), and in the 2026-11 shadow run every follow-up,
# explainer and colour piece on them copied that 9-10 (89 of 211 items scored 8+ were Huang follow-ups).
# The scale is described one step at a time with an example at every step from 3 to 10: the earlier version gave
# bands ("6-7", "8-9") with one example each, and the VPS test run's scores piled up at 4-5 and 8 with nothing at 7
# (raw items: 2 at 7, 15 at 8).
IMPORTANCE = """how much it matters to the tech industry, whatever its entertainment value. Use the whole \
scale; most stories are 4 to 6.
  10 industry-defining, once or twice a year. 9 a frontier model from a top lab, a top tech CEO ousted, a \
>= $5B deal. 8 a breach or outage hitting millions, >= $500M funding, a major law or ruling taking effect. \
7 a major company's earnings, a $100-500M round, a significant product from a big company, an enforcement \
action or lawsuit against a big company, a well-known founder or executive leaving. 6 a notable launch, a \
$25-100M round (>= $15M in India), a regulator's formal move, a mid-size acquisition, layoffs at a known company. \
5 a smaller launch or round, a policy proposal, a notable open source release, a new claim in a running story. \
4 a routine update, a small round, a partnership, strong analysis. 3 release notes, minor features, local \
events. 1-2 how-to, reviews, explainers, promos.
  A running story: a new development is scored as the news it is, on the scale above, whether or not the \
story is already a candidate event below. A government or minister acting on or publicly pressing a top \
company, a board or major investors moving, a probe opening, a ruling, a resignation or a comeback bid in a \
top company's crisis are 7 or 8, not 5. A reaction, explainer, live blog, profile, market wrap or colour piece \
on a known story is 4 to 5.
  Calibration examples (invented stories, for the scale only):
  "Orbitly files for IPO targeting a $900B valuation, the largest tech listing ever" = 10, news
  "Kestrel AI releases Kestrel 5, topping every major coding and reasoning benchmark" = 9, launch
  "Orbitly CEO resigns after the board learns of an undisclosed relationship with a supplier" = 9, news
  "Hackers steal 80 million customer records from telecom giant Varnet" = 8, security_incident
  "Finance minister publicly urges Orbitly's board to pick an outsider as its next CEO" = 8, policy
  "Ousted Orbitly CEO has discussed a comeback with allies, sources say" = 7, news
  "Prosecutors open a probe into Varnet's chip sales to a sanctioned buyer" = 7, policy
  "Orbitly supplier opens an internal probe after reports on its chip orders" = 6, news
  "Novacloud beats on revenue and lifts full-year guidance" = 7, earnings
  "Regulator sues Varnet over hidden fees charged to 12 million customers" = 7, policy
  "Brightloop co-founder and CTO quits to start a rival" = 7, news
  "Emberfall releases Starfall 2, the most awaited game in a decade" = 7, launch (topics: consumer)
  "Orbitly shares fall 6% a day after its CEO's exit" (a market reaction) = 5, news
  "Brightloop raises $80M Series B for warehouse robots" = 6, funding
  "Zentra releases a 1T-parameter open model for coding agents" = 6, launch
  "Markets regulator to issue rules for AI use by stock brokers" = 5, policy
  "Top scorer in a national exam admits using a chatbot to cheat" = 5, news (topics: ai)
  "Novacloud CEO's first interview since the outage: what we learned" = 5, interview
  "Who is Mira Patel, Orbitly's interim CEO?" (a profile after the CEO's exit) = 4, explainer
  "Pundit says Orbitly's growth could justify a record-breaking IPO" = 4, opinion
  "Lumen CLI 0.36" (a tool's release notes) = 3, release_notes
  "How a law firm rethinks contracts with AI" (a vendor's story about a customer) = 2, customer_story
  "How long do Blu-ray discs last?" = 1, explainer"""

# Spice (the owner's direction of 2026-11-15): "all the spicy news, anything even remotely related to tech that
# can make a really good headline and make the user click the notification". It is judged on the story's own
# facts and scored apart from importance, so a dull but large deal and a small but juicy feud both rank honestly.
SPICE = """how much a normal tech-curious person would want to tap this and forward it to a friend, judged on \
the story's own facts (never on how it could be worded) and separately from importance.
  9-10 jaw-dropping: a famous founder or CEO ousted, arrested or caught in a scandal; a feud between tech \
titans; a hack or outage everyone felt; a shocking leak. 7-8 drama, conflict, big money or a real surprise: \
lawsuits, firings and resignations, layoffs, leaks, bans and government fights, fraud claims, giant valuations \
or pay packages, embarrassing failures, viral moments, a big-name product reveal, a surprising scientific \
result, a crime, arrest or violence at a tech or games company, and whatever surrounds the year's most awaited \
game or gadget. 5-6 solid news with a hook: a notable launch, a round with a twist, a policy move many people will feel. \
3-4 routine: an ordinary funding round, a feature update, earnings in line, a partnership. 1-2 dry: a minor \
version bump, a press release, a how-to, a conference agenda.
  Examples (invented): "Orbitly board fires founder-CEO over undisclosed relationship with a supplier" = 10; \
"Leaked memo: Novacloud CEO calls remote staff 'tourists'" = 8 (importance 5); "Varnet sues its former CTO for \
taking source code to a rival" = 8; "Kestrel AI's chatbot told users to eat rocks, screenshots show" = 7; \
"Police arrest a suspect after shots are fired at game studio Emberfall's campus" = 8 (importance 5); \
"Starfall 2 boxes ship without the download code, buyers say" = 7 (importance 4); \
"Brightloop raises $80M Series B" = 4; "Novacloud prices $2B bond offering" = 3 (importance 7); "Lumen CLI \
0.36" = 1."""
# When the news happened (newsdate.py). One wording for the write prompt and for `date_news`, which asks the same
# question about a card written before the writer was asked it (tools/reexamine.py).
NEWS_DATE = """the day the news the headline reports happened, when the text says that was two or more days \
before the article's Published date ("began selling on Friday", "announced on Tuesday", "on Nov. 13", "last \
week"): that day as YYYY-MM-DD, worked out from the Published date. A late write-up of older news is common; \
date the news, not the article. Empty when the text presents the news as new (today or yesterday) or gives no \
day for it. Empty when the article is itself the news: an exclusive, an investigation or a leak that first \
reports something ("according to people familiar", "the Journal reported"), or a new development about an \
earlier incident (a new finding, statement, reaction, arrest or lawsuit after "Friday's outage"); the day of the \
incident is then background. Never the date of background ("founded in 2019", "resigned on Thursday" in a \
story about what followed), and never for analysis, opinion or explainer pieces."""
PROMPT = PROMPT.replace("<<importance>>", IMPORTANCE).replace("<<spice>>", SPICE).replace("<<news_date>>", NEWS_DATE)
DATE_PROMPT = """An article and the card a news app shows for it. Reply with only JSON: {"news_date": "..."}

- news_date: <<news_date>> The news is what the card's headline reports.

Published: <<published>>
Original title: <<title>>
Card headline: <<headline>>
Text:
<<text>>""".replace("<<news_date>>", NEWS_DATE)
DATE_WORDS = 250


def date_news(client, cfg, published_at, title, headline, text):
    """The writer's `news_date` for a card that was written before the write prompt asked for one: one small
    call on the article's opening. Returns "" (the article's own time stands) or a YYYY-MM-DD string, which
    newsdate.news_at then accepts only when the text itself carries that day. Raises LLMError."""
    reply = client.chat(role_model(cfg, "write"), [{"role": "user", "content": entail.fill(
        DATE_PROMPT, published=published_at, title=title, headline=headline or title,
        text=text_mod.cap_words(text or "", DATE_WORDS))}], kind="date", max_tokens=400, temperature=0.0, tries=2)
    try:
        day = json.loads(re.search(r"\{.*\}", reply or "", re.S).group(0)).get("news_date")
    except (AttributeError, ValueError):
        raise LLMError("date: unreadable reply")
    return day.strip() if isinstance(day, str) and newsdate.parse_day(day) else ""
REWRITE_PROMPT = REWRITE_PROMPT.replace("<<importance>>", IMPORTANCE).replace("<<spice>>", SPICE)

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
    for n in validate.numbers(" ".join([out.headline, out.s1, out.s2, out.s3, out.notif_line, out.notif_title])):
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
    # The newest material update goes in as REPORT 2: the representative members are the most central ones, so the
    # report that the "upcoming" IPO had listed was left out and the rewrite kept saying "upcoming".
    # Failing that, the newest hard-news report when it is hours newer than the lead: the FAA outage card kept the
    # first day's cause ("routine maintenance on a copper line") while a member from the next day reported the
    # secretary blaming a router, and two follow-up cards gave two more causes.
    hard = [m for m in ev.members if m is not lead and m.hard and not m.thin and m.text]
    newest = sorted((m for m in hard if m.material_update), key=lambda m: m.published_at, reverse=True)[:1]
    material = bool(newest)
    if not newest:
        newest = sorted((m for m in hard if m.published_at - lead.published_at >= cluster.NEWER_REPORT),
                        key=lambda m: m.published_at, reverse=True)[:1]
    picked = [lead] + newest + [m for m in representative(ev.members)
                                if m is not lead and all(m is not n for n in newest)]
    picked = picked[:REWRITE_MEMBERS]
    note = ""
    if newest:
        note = (f"REPORT 2 is the newest {'development' if material else 'report'} "
                f"({cluster.iso(newest[0].published_at)}); lead with {'it' if material else 'what it adds'}.\n\n")
    reports = note + "\n\n".join(
        f"REPORT {i}: {store.by_id[m.source_id].name if m.source_id in store.by_id else m.source_id}\n"
        f"Published: {cluster.iso(m.published_at)}\n"
        f"Title: {m.title}\nText: {text_mod.cap_words(m.text, REWRITE_WORDS)}" for i, m in enumerate(picked, 1))
    base = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": entail.fill(REWRITE_PROMPT, reports=reports)}]
    model = role_model(cfg, "write")
    # The titles are part of what the card may state (a member's title often carries the newest fact), and the
    # tense rule reads the newest report's title: the lead's "says he will sign" must not bind a card whose
    # newest report says he signed.
    source_text = "\n".join(f"{m.title}. {m.text}" for m in picked)
    tense_title = newest[0].title if newest else lead.title
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
            reply = client.chat(model, messages, kind="rewrite", temperature=0.5 if attempt else 0.2,
                                max_tokens=WRITE_TOKENS)
            data = json.loads(re.search(r"\{.*\}", reply or "", re.S).group(0))
            out = WriteOutput.model_validate({
                "is_news": lead.is_news, "type": lead.type, "relevance": lead.relevance,
                "region": lead.region, "topics": lead.topics, **{k: v for k, v in data.items() if k in (
                    "importance", "spice", "headline", "headline_alt", "headline_short", "s1", "s1_alt", "s1_long",
                    "s2", "s2_alt", "s2_long", "s3", "s3_alt", "s3_long", "notif_line", "notif_title")}})
        except AuthError:
            raise
        except (LLMError, AttributeError, ValueError, ValidationError) as e:
            log.info("rewrite %s: %s", ev.id, type(e).__name__)
            return False
        validate.select(out)
        v = validate.check(out, source_text, tense_title, "", None, None, " ".join(lead.text.split()[:200]))
        bad = rewrite_numbers_ok(out, lead.text, [m.text for m in picked])
        if bad:
            v.fail("ungrounded", f"'{bad}' is not in REPORT 1 or in two reports; use REPORT 1's figure or drop it.")
        if not v.ok:
            feedback = v.feedback
            continue
        try:
            e = entail.check(client, role_model(cfg, "judge"), lead.title, "\n\n".join(
                text_mod.cap_words(m.text, REWRITE_WORDS) for m in picked), out.headline, out.s1, out.s2,
                out.s3 if v.full != v.short else "", out.notif_title, out.notif_line)
        except AuthError:
            raise
        except (LLMError, ValueError):
            return False
        if not e.ok:
            feedback = [e.feedback()]
            continue
        row["rewrite_importance"] = out.importance
        row["rewrite_spice"] = out.spice
        row["rewrite_publishers"] = row.get("publishers")
        cluster._set_text(row, out.headline, v.full, v.short, out.notif_line, out.s1, 1, "rewrite", now,
                          out.notif_title)
        return True
    return False


# Batch triage of new items (one cheap call per tick) and the clustering judge

TRIAGE_PROMPT = """You screen incoming items for Techgist, a tech news app whose readers want every story with \
a tech angle that makes a good headline: product and funding news, but also founder and executive drama, feuds, \
firings and resignations, lawsuits, scandals, leaks, layoffs, big money (IPOs, valuations, pay packages, fraud), \
outages and hacks, bans and government fights, culture-war and viral moments involving tech companies or people, \
and creators, gaming, crypto, space, EVs, gadgets and science when the story is big or surprising.

A tech person or company in a culture moment counts as a tech angle: a comedy sketch, talk show, film, meme or \
celebrity story about tech founders or products is kept ("SNL gathers all the tech tycoons for a lesson in \
dating and spycraft" is a keep).

For each numbered item decide whether a writer should spend time on it. DROP only when you are sure:
- "offtopic": no tech company, tech person or technology in it at all (general politics, sport, lifestyle, \
personal finance, celebrity, crime or business news that names no tech company, founder, executive or product).
- "shopping": deals, buying guides, product comparisons, reviews, how-tos, listicles, horoscopes, quizzes.
- "dull": routine items nobody would tap: minor version bumps, award or event announcements, sponsored or \
press-release fluff, generic market wraps, small local items.
Keep everything else, and keep when unsure.

Reply with only JSON: {"drop": [{"n": 3, "why": "offtopic"}]}  (an empty list when nothing is dropped)

Items:
<<items>>"""
TRIAGE_BATCH = 40
TRIAGE_RULES = ("offtopic", "shopping", "dull")
RESCREEN_WINDOW = timedelta(hours=36)


def rescreen_on_prompt_change(db, now):
    """Puts the last day and a half of screening drops back in the queue when the screening prompt has changed
    since they were judged (or was never recorded): a drop is the prompt's judgement, and it should not outlive
    the prompt. The SNL sketch about the tech tycoons was dropped as off-topic an hour before the prompt learned
    that a culture moment about tech people is a keep, and never came back. Returns the number requeued."""
    import hashlib
    sha = hashlib.sha1(TRIAGE_PROMPT.encode()).hexdigest()[:12]
    if db.get_meta("triage_prompt_sha") == sha:
        return 0
    db.set_meta("triage_prompt_sha", sha)
    cur = db.conn.execute("UPDATE items SET status = 'new', drop_rule = NULL, triaged = NULL WHERE status = 'dropped'"
                          " AND drop_rule LIKE 'triage_%' AND published_at >= ?", (iso(now - RESCREEN_WINDOW),))
    if cur.rowcount:
        log.info("screening prompt changed: %d dropped items go back to screening", cur.rowcount)
    return cur.rowcount


def triage_batch(client, cfg, rows, by_id):
    """{url: rule} for the rows the triage model is sure nobody would tap. Fails open: on any error nothing is
    dropped, and the full write call (which scores relevance itself) decides."""
    if not rows:
        return {}
    lines = []
    for n, r in enumerate(rows, 1):
        src = by_id.get(r["source_id"])
        excerpt = " ".join((r["feed_text"] or "").split()[:30])
        lines.append(f"{n}. [{src.name if src else r['source_id']}] {r['title']}" + (f" | {excerpt}" if excerpt else ""))
    try:
        reply = client.chat(role_model(cfg, "triage"), [{"role": "user", "content": entail.fill(
            TRIAGE_PROMPT, items="\n".join(lines))}], kind="screen", max_tokens=1500, temperature=0.0, tries=1)
        data = json.loads(re.search(r"\{.*\}", reply or "", re.S).group(0))
    except (AuthError, CircuitOpen):
        return {}
    except (LLMError, AttributeError, ValueError):
        return {}
    out = {}
    for d in data.get("drop") or []:
        try:
            n, why = int(d.get("n")), str(d.get("why") or "dull").strip().lower()
        except (TypeError, ValueError, AttributeError):
            continue
        if 1 <= n <= len(rows):
            out[rows[n - 1]["url_canonical"]] = why if why in TRIAGE_RULES else "dull"
    # A reply that drops nearly everything is a misread of the task, not a judgement.
    return out if len(out) <= 0.85 * len(rows) or len(rows) < 6 else {}


JUDGE_PROMPT = """A news app shows one card per development. Decide how a NEW ARTICLE relates to each EVENT \
already in the feed.

- "same": the article reports the same specific development as the event: the same announcement, filing, letter, \
statement, deal, incident, ruling or leak, perhaps from another outlet, with another angle or more detail.
- "followup": the same ongoing story, but a different development: a new statement, a new revelation, a new \
actor reacting, a consequence, a next step. Also an overview, analysis, profile or opinion piece about the story.
- "supersedes": a followup whose news makes the event's card wrong or out of date, so that showing both would \
mislead: the outcome of what the event reports as under way or planned (a rocket reported launched is now \
reported lost; a deal reported agreed is called off), a correction or retraction, or a newer official account \
that replaces the one the event gives (a different stated cause, toll or status of the same incident). Only a \
news report can supersede, never an analysis or opinion piece. When the event's card is still true as far as it \
goes, the answer is "followup".
- "different": another story, even if it shares a company or person.
Two reports about the same company or the same scandal are "same" only when they report the same development. \
"Investors urge the board to bring the CEO back" and "A minister urges the company to hire an outside CEO" are \
followups of one story, not the same development.

Reply with only JSON: {"relations": {"<event id>": "same" | "followup" | "supersedes" | "different"}}

NEW ARTICLE
Title: <<title>>
Text: <<text>>

EVENTS
<<events>>"""
RELATIONS = ("same", "followup", "supersedes", "different")


def judge_events(client, cfg, title, text, events):
    """{event id: "same" | "followup" | "different"} for `events` = [(id, headline, first report's title)].
    Raises LLMError when the judge can't be asked; an event missing from the reply counts as "different"."""
    listing = "\n".join(f"- {eid}: {headline or first}" + (f" (first report: {first})" if headline and first
                                                           and first != headline else "")
                        for eid, headline, first in events)
    reply = client.chat(role_model(cfg, "cluster"), [{"role": "user", "content": entail.fill(
        JUDGE_PROMPT, title=title, text=text_mod.cap_words(text or "", 150) or "(no text)", events=listing)}],
        kind="cluster", max_tokens=600, temperature=0.0, tries=2)
    try:
        data = json.loads(re.search(r"\{.*\}", reply or "", re.S).group(0)).get("relations") or {}
    except (AttributeError, ValueError):
        raise LLMError("cluster judge: unreadable reply")
    out = {}
    for eid, _, _ in events:
        rel = str(data.get(eid) or "different").strip().lower()
        out[eid] = rel if rel in RELATIONS else "different"
    return out
