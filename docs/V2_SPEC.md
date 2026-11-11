# Techgist Redesign Spec (v1.3 server, v2.0 app, v2.1 push)

Status: approved for build. Scope: server (`server/`), iOS app (`ios/Techgist`), and a Cloudflare Worker edge (`edge/`). Constraint: one engineer-owner on free infrastructure only: the existing Linux box, GitHub (public repo, free Actions), the NVIDIA API key, the Apple developer account and the Cloudflare free tier.

**Release split (decision).** Most of the quality the owner wants comes from the server: correct stories, good summaries, deduplication and freshness. That part reaches the live 1.2 app without App Review.

| Release | Ships | Needs App Review | Target |
|---|---|---|---|
| **R1 – server v1.3 edition** | New worker (sources, filtering, LLM, validation, clustering, ranking). It writes the **existing v1 `edition.json`** every ≤10 min. | No | Weeks 1–3 |
| **R2 – app 2.0** | v2 schema, Cloudflare edge, new card and feed, motion, controls, share pages, reporting. Keeps a slim on-device fallback. | Yes | Weeks 4–7 |
| **R3 – app 2.1** | Server APNs push, Notification Service Extension, sound, permission flow. Widget follows in 2.2. | Yes | Weeks 8–10 |

Baseline correction: `server/README.md:39-41` still documents cron "every 2 hours", while the box runs `publish.sh` every 30 min. R1 replaces both.

---

## 1. Product principles

1. **One card per event, ranked by importance and corroboration.** The feed is an edited edition, not a firehose. Duplicates, non-news, off-topic pieces and stale filler never ship.
2. **Every card is a finished read that fits.** The headline is fact-first and ≤ 72 characters. The summary has 3 sentences: the full version is 45–62 words, and S1+S2 stand alone at 28–42 words. A card shows the full summary when it fits and otherwise S1+S2. **Fonts never shrink.**
3. **Accurate before clever.** Opinion is attributed. Hedges ("suspected", "reportedly") are preserved. Every headline that can reach Top or a push must pass an entailment check against the source.
4. **Fresh means minutes, and age is honest.** Publisher-to-app latency is about 10 min, measured from the publisher's own timestamp. Old stories never look new.
5. **Few notifications, all of them good** (R3). At most 1 morning gist and 2 breaking alerts a day, sent by the server in the user's local time. Each has an image and a custom chime, and opens the right cards.
6. **Never empty, never frozen.** If the LLM, the box or the CDN fails, the feed degrades to publisher titles and excerpts. It is never blank and never more than about 30 min stale.
7. **Native and restrained.** Depth on paging, haptics only for deliberate actions, no looping animations, sharp images.

Measurable targets: section 8 acceptance criteria and the `health.json` metrics in section 6.

---

## 2. Feed algorithm (server, R1)

### 2.1 Architecture
- **One long-running Python worker** in `server/worker/`. It runs as a systemd user service with `Restart=always` and `OnFailure=` pointing to an ntfy unit. State lives in SQLite at `~/.techshorts/state.db`.
- **The 60 s loop:**
  ```
  fetch (conditional GET) → prefilter → timestamps → article text (+boilerplate strip)
  → embed (batched) → LLM triage+write (1 call, parallel) → deterministic trim → validate
  → [entailment gate if Top/push-eligible] → cluster assign (SERIAL, published order) → merge pass
  → event rewrite if needed → event importance + clamps → score → assemble tabs
  → publish (v1 always; v2 from R2) → push (R3)
  ```
- **`main.swift` is not deleted.** It becomes the **safe-mode publisher** (section 6.4). `publish.sh` and `enrich.py` retire at cutover. The prompt and `grounded()` from `enrich.py` move to `llm.py` and `validate.py`.
- **LLM settings:** model and base URL are configurable in `server/config.yaml` (`llm.base_url`, `llm.model`, `llm.fallback_model`, `embed.model`), so any OpenAI-compatible endpoint can be swapped in if the NVIDIA hosted terms or availability change.

### 2.2 Sources (`server/sources.yaml`, the single source list)
Fields per source: `id, name, v1_name, url, tier (1|2|signal), authority (0.5–1.2), reads, region (in|us|global), poll (3m|10m), fetch_page (bool), images (source|generated)`.

`v1_name` is the exact `FeedSource` display name used by 1.2, for example "TechCrunch", "The Verge", "BBC Technology", "New York Times", "CNBC Tech". The v1 output depends on it (section 2.13).

| Group | Sources | Authority | Tier-1 for |
|---|---|---|---|
| News, 3 min poll | TechCrunch, TechCrunch AI, The Verge, CNBC Tech, BBC Tech, NYT Tech (`fetch_page: false`), Guardian Tech | 1.2 TechCrunch; 1.1 others | global/us |
| News, 10 min poll | Ars Technica, Wired, Engadget (0.8), The Decoder, MIT Tech Review AI, Semafor Tech, 404 Media, Platformer | 1.0 | – |
| India, 3 min poll | ET Tech, Moneycontrol Tech, Inc42, YourStory, Entrackr, MediaNama (0.9) | 1.0 | **region=in** (ET Tech, Moneycontrol, Inc42) |
| Lab and company blogs, 10 min poll | OpenAI, Google AI, Anthropic, Hugging Face, GitHub blog, Cloudflare blog | 1.0 | – |
| Reads (`reads: true`), 10 min poll | Stratechery (`fetch_page: false`), Simon Willison, Pragmatic Engineer, Lenny's, Latent Space, Martin Fowler, Julia Evans | 0.9 | – |
| Signal, 3 min poll | Techmeme, HN front page + `hnrss.org/newest?points=100`, The Information (headline only) | – | – |

- **Removed:** Reuters and Bloomberg (no public RSS), DEV Community and Product Hunt (low signal), Changelog (podcast-heavy).
- `server/tools/check_sources.py` runs before launch and nightly. Each feed must return 200 and contain ≥ 1 item. **Tier-1 status applies only to sources that pass.** Any failure is listed in `health.json`.
- **Publisher hygiene:**
  - Sources with `fetch_page: false` (paywalled or bot-walled) use feed text plus `og:description` only.
  - The worker honours `robots.txt` for page fetches.
  - `sources.yaml` carries a `domain_blocklist`.

### 2.3 Fetch and timestamps
- **Fetching:** conditional GET with stored ETag/Last-Modified, a 15 s timeout, a generic UA, and 8 parallel fetches.
- **Per-item fields:** `url_canonical` (strip `utm_*`, `fbclid` and AMP), `first_seen_at`, `feed_published_at`, and `published_at = min(feed_published_at, first_seen_at)`. `published_at` falls back to `first_seen_at` only when the feed has no date.
- **The publisher's time drives everything.** `event_published_at` is the minimum `published_at` over the event's members. It drives freshness F, tab windows, gist eligibility, badges and the time shown on cards. `first_seen_at` is used only for latency metrics. Example: a 5-day-old Stratechery post re-fetched after downtime keeps its 5-day age.
- **Ignored at first sight:** items older than the longest window of any tab they could qualify for, or older than 14 days.
- **First run:** existing items are marked seen and not enriched if older than 36 h.

### 2.4 Prefilter (cheap; every drop is logged with its rule to `health.json.prefilter`)
- **URL drops:** paths `/deals/`, `/coupons/`, `/shopping/`, `/gift-guide/`, `/sponsored/`, `/insights/`, `/brandstory/`, `/partner-content/`, `/podcast/`, `/events/`.
- **Title drops** (case-insensitive):
  - `^(what|why|how|when) (is|are|your|to|you|do|does)\b`
  - review titles: `(^review:|\breview$|:\s*review\b)`
  - `^fragments:`, `^quoting `
  - shopping phrases: `\b(discounted|% off|save \$|best price|lowest price|promo code|coupon)\b`
  - `\bsponsored\b`, `\bsummit 20\d\d\b`, `\b(podcast|episode)\b`, `\bgift guide\b`, `\bbest .* (to buy|for 20\d\d)\b`
- **Not dropped by title:** bare `deal`, `sale` and `review`. "Microsoft closes $69B Activision deal", "US bans Nvidia chip sale to China" and "FTC opens antitrust review of OpenAI" must pass. Reviews are removed later by the LLM `type`.
- **Ported:** the existing `ContentFilter` rules (`FeedService.swift:123-142`).

### 2.5 Article text
1. Run `trafilatura` (only when `fetch_page: true`) and cap the result at 800 words.
2. **Strip boilerplate before counting.** Drop paragraphs matching `subscribe|subscriber|sign up|newsletter|podcast feed|members-only|support our journalism`.
3. If fewer than 120 words remain, use the longer of the feed `content` and `og:description + feed excerpt`. **Never use another item's text.**
4. **Thin items** (still under 60 words):
   - The item may join an event as a member, which still shows in Full coverage.
   - It may never lead an event or stand alone as a card.
   - Without siblings it is held with `attempts` and `retry_at`, retried at +30 min and +2 h, then dropped.

### 2.6 LLM triage and write (one call per item)
**Call settings:**
- JSON mode, temperature 0.2, 30 s timeout.
- One token bucket of 30 chat requests/min, with `Retry-After` honoured and concurrency 6.
- **Priority queue:** tier-1 items and Techmeme-linked items go first, then everything else in `published_at` order.

**Embeddings:**
- One batched call per tick, up to 50 inputs.
- `input_type: query` for headline-vs-title checks and `passage` for item text.
- They do not count against the chat bucket.

**Input to the model:**
- Source name, URL path, original title, published time and text (≤ 800 words).
- Up to 5 candidate events (id, headline, first sentence) from the embedding step.

**Output schema** (pydantic):
```json
{
  "is_news": true,
  "type": "news|launch|funding|acquisition|earnings|policy|security_incident|analysis|opinion|interview|review|tutorial|explainer|release_notes|customer_story|podcast|promo|deal|other",
  "relevance": 0-3,
  "importance": 1-10,
  "breaking": false,
  "sensitive": false,
  "topics": ["ai","startups_funding","bigtech","dev_tools","security","policy","hardware","india","science","consumer"],
  "region": "in|us|global",
  "entities": {"orgs": ["Anthropic"], "people": [], "products": [], "amounts": ["$1.4T"]},
  "event": "ev_xxxxxxxxxx" | "new" | "followup:ev_xxxxxxxxxx",
  "material_update": false,
  "headline": "...",
  "s1": "...", "s2": "...", "s3": "...",
  "notif_line": "..."
}
```

**Definitions given in the prompt:**
- **`relevance`** asks how relevant the item is to tech professionals in Bengaluru and the Bay Area:
  - 3 = core tech or business news;
  - 2 = adjacent (science, or policy with a tech angle);
  - 1 = tangential;
  - 0 = off-topic (US partisan politics, entertainment, cars without a tech angle).
- **`region`**:
  - `us` is only for US-only policy, US local news, or US-market-specific consumer news.
  - Anything about a global company or product is `global`. The Anthropic IPO, CoreWeave earnings and the Nvidia probe are all `global`.
  - `in` is for India-market companies, Indian policy, or Indian funding.
- **`sensitive`** is true when the story involves crime, espionage, sexual conduct, lawsuits, or allegations against a named person.
- **Sentences:** `s1` says who and what, with the key number. `s2` gives the most important detail or context. `s3` gives the concrete consequence for builders, investors or operators. Each sentence is about 18 words.
- **`notif_line`** is ≤ 110 characters and is one complete sentence of new information that does not repeat the headline.
- **Importance anchors** come with 8 few-shot examples from the 2026-11-11 edition: Anthropic IPO = 10; Huang spy probe = 9; CoreWeave earnings = 7; Daylight $80M Series B = 6; Cramer on Anthropic = 4, opinion; llm-gemini 0.36 = 3, release_notes; OpenAI Gilbert + Tobin = 2, customer_story; Engadget Blu-ray lifespan = 1, explainer.
  - 10: industry-defining.
  - 8–9: frontier model from a top lab, ≥ $500M funding, a breach or outage hitting millions, major regulation.
  - 6–7: $25–500M funding, or ≥ $15M in India; notable launch or earnings.
  - 4–5: smaller launches, open source, policy proposals, strong analysis.
  - 1–3: how-to, reviews, explainers, promos, minor updates.

### 2.7 Deterministic attribution (before the LLM)
These rules force `type = opinion` and an attribution prefix, whatever the LLM says:
- CNBC paths containing `/jim-cramer` or `/cramer`, or titles matching `^(Jim )?Cramer\b`;
- Guardian `/commentisfree/`;
- NYT `/opinion/`;
- titles ending in ` | Firstname Lastname`;
- titles matching `^[A-Z][\w.]+ [A-Z][\w.]+ (says|:)`.

The required prefix is `Name:` (for example `Cramer: Anthropic growth could justify a record IPO`). Live failure this fixes: the CNBC Cramer piece currently ships as "Anthropic files IPO paperwork for up to $64.35 billion raise".

### 2.8 Trim and validation (hard gates)
**Deterministic trim, run first:**
- `full = s1+s2+s3`. If it exceeds 62 words, drop `s3`. If the result is 45 words or more it becomes `full`; otherwise the item retries.
- `short = s1+s2` must be 28–42 words.
- The model is never trusted to count.

**Summary gates:**
- Plain text: no markdown, no first person, no "the article".
- No banned words: `revolutionize, game-changer, groundbreaking, delve, landscape, seamless, cutting-edge`.
- No boilerplate terms: `subscribe, newsletter, podcast, sign up`.

**Headline gates:**
- ≤ 72 characters, no `?`, no trailing `.`, not all caps.
- Not truncated by code; an over-long headline retries.
- `notif_line` ≤ 110 characters.

**Grounding** (`grounded()` reworked):
- **Numbers are normalized before comparison.** `million|mn|m|M`, `billion|bn|b|B` and `trillion|tn|T` map to a canonical value. Values are parsed and compared within 0.5%, and the currency symbol must match. So "$80 million" grounds `$80M`, while "£1.4tn" does **not** ground `$1.4T`.
- Capitalized tokens must appear in the source. **There is no months or countries allowlist.** Only tokens from the source name are allowed.

**Hedge preservation:** if the source has `suspected|alleged|allegedly|reportedly|accused|claims|according to` within 12 words of an entity named in the headline, the headline must keep a hedge or an attribution (`report:`, `says`). Example: "Jensen Huang admits affair with Chinese spy" fails; "…with suspected Chinese spy" passes.

**Entailment gate:** a second LLM call, only for items with importance ≥ 6, relevance ≥ 2 or breaking. The prompt is: "Does ORIGINAL TITLE + first 3 paragraphs entail HEADLINE and S1? Answer yes/no + unsupported claim." It replaces the cosine faithfulness gate, which is removed.

**Retry ladder** (one rule, used everywhere):
1. Attempt.
2. Retry once with literal feedback, for example `"S1+S2 was 51 words; make it 36."` or `"Headline drops 'suspected'; keep the hedge."` or `"Source is an opinion by Jim Cramer; prefix 'Cramer:'."`
3. One attempt on the fallback model.
4. Hold, with retries at +30 min and +2 h, then drop.

**Degraded publish:** if a held item belongs to an event with ≥ 2 publishers, the event still publishes in degraded form (section 2.14). An important story is never hidden only because the AI text failed.

### 2.9 Clustering into events
**Calibration first (WP3a), before choosing any thresholds:**
- Embed the 223 fixture items (the live 2026-11-11 v1 edition).
- Hand-label ≥ 60 same-event pairs and ≥ 250 different-event pairs. The different-event pairs must include same-company pairs: Anthropic IPO vs Glasswing CVE criticism, vs the RAISE Act, vs the Australia penalties; Nvidia earnings vs the spy probe.
- Choose `T_cand`, `T_join` and `T_signal` from the precision/recall curve at precision ≥ 0.97, store them in `config.yaml`, and report them in the WP3 PR.

**Assignment** runs **serially** once per tick, after parallel LLM writing, in `published_at` order. Events created earlier in the same tick count as candidates. A join requires **all** of the following:
- cosine(item, event anchor) ≥ `T_join`, where the anchor is the event's first hard-news member, not a drifting centroid. This holds even when the LLM picked the event.
- At least 1 shared non-org entity (person, product or amount), **or** the LLM picked the event and cosine ≥ `T_join + 0.03`.
- Compatible types: `earnings` does not join `security_incident`, and `launch` does not join `funding` unless the LLM picked it.
- The event is younger than 48 h and was updated within the last 24 h.

**Follow-ups:**
- When the LLM returns `followup:ev_x`, or a `material_update` item with importance ≥ 8 arrives for an event older than 48 h, it starts a **new event** with `parent_id = ev_x`.
- Full coverage links the two events.
- Breaking eligibility and freshness use the follow-up's own timestamps. Example: "Huang resigns" two days into the saga can push.

**Merge pass** (each tick):
- Events younger than 6 h whose anchors have cosine ≥ `T_join` and share an entity merge into the **older** id.
- `event_alias(old_id → id)` is kept 30 days, so push taps, Saved and `/e/` links still resolve.

**IDs:** an event ID is `ev_` + the first 10 hex characters of sha1(first member's canonical URL). It never changes. Merges alias; they do not rename.

**Lead article:**
- Chosen only from hard-news members (news, launch, funding, acquisition, earnings, policy, security_incident) that have ≥ 120 words of text.
- Rank by cosine-to-anchor × authority, then earliest `published_at`.
- Opinion, analysis and interview pieces lead only when the event has no hard-news member.
- For the IPO this picks a Guardian, BBC or CNBC filing story over TechCrunch's "founders cash out" angle.

**Event text:**
- The headline and sentences come from the lead's output.
- A rewrite call (top 4 hard-news members by representativeness) happens when the lead changes, when 2 or more new publishers join, or on **any** hard-news `material_update`. A material update bypasses the 2 h rewrite cap.
- **Numbers in a rewrite** must appear in the lead's text or in ≥ 2 member texts. When members disagree (Guardian $1.4tn, BBC $1.3tn, CNBC $1.405T), the rewrite uses the lead's value with "up to" or "about", or omits the number.
- `headline_history` is stored per event.

**`last_material_update_at`** is set only by `material_update = true` from a hard-news type, or by a lead change. A publisher joining does not set it.

**Signal sources** (Techmeme, HN, The Information):
- They attach by exact URL match or cosine ≥ `T_signal`, and set `on_techmeme` and `hn{points, comments, url}`.
- **HN-only import:** items with ≥ 250 points (or ≥ 3 h on the front page) whose linked page yields ≥ 300 words become items. The lead is credited to the linked site at authority 0.8. They are eligible for Dev, AI and Reads only, never Top unless corroborated. Examples: Val Town winding down, Glasswing overwhelming maintainers.
- **Techmeme-only import:** a Techmeme item with no other member after 20 min is imported from its linked article.

### 2.10 Event importance (clamped by hard signals)
- **Base value:** the median importance of hard-news members, re-scored in the rewrite call with the same anchors.
- **Clamps**, applied in order:
  1. Cap at 6 unless the event has ≥ 2 publishers, or `on_techmeme`, or HN ≥ 100.
  2. Cap at 5 for single-publisher `consumer`/`hardware`-only gadget items.
  3. ≥ 8 requires ≥ 2 tier-1 publishers (tier-1 as defined for the event's region), or 1 tier-1 plus Techmeme.
  4. `release_notes`/`tutorial` cap at 4.
  5. Parsed funding ≥ $500M sets a floor of 8, but only when clamp 3 already holds.
- **Drift check:** `health.json` publishes the 24 h histogram of raw LLM importance against clamped importance. ntfy alerts when more than 10% of items score raw ≥ 8.

### 2.11 Scoring (server, region-neutral)
```
score = I × C × A × F
I = (importance / 10) ^ 1.5
C = min(2.2, 1 + 0.5·log2(n_publishers)) × (on_techmeme ? 1.25 : 1) × min(1.3, 1 + 0.15·log2(max(1, hn_points/100)))
A = max authority over non-signal members
F = exp(−age_eff / τ_tab)
age_eff (h) = min(hours since event_published_at, hours since last_material_update_at + 3)
```
**Worked check** (the WP4 unit test must reproduce these to ±0.01):
- **Anthropic IPO:** importance 10, 10 publishers, Techmeme, HN 715, authority 1.2, 14 h, Top τ 10. C = 2.2 × 1.25 × 1.3 = 3.575. Score = 1 × 3.575 × 1.2 × 0.2466 = **1.058**.
- **Fresh $25M Series A:** importance 6, 1 publisher, Techmeme, authority 1.2, 2 h. Score = 0.4648 × 1.25 × 1.2 × 0.8187 = **0.571**.
- **When the IPO yields:** with no material update it yields at t = 14 + 10·ln(1.058/0.571) ≈ **20.2 h**.

**Property test:** for every fixture scenario, the Top #1 at t + 24 h is a different event unless importance = 10 and it had a material update in the last 6 h.

### 2.12 Tabs
An event may appear in several tabs with the same id.

| Tab | Membership | Types | Min importance | Min relevance | Window (age_eff) | τ (h) | Max |
|---|---|---|---|---|---|---|---|
| **Top** | all | news, launch, funding, acquisition, earnings, policy, security_incident; analysis/opinion/interview only if importance ≥ 8 | 6 | 2 | 36 h (72 h if importance ≥ 8) | 10 | 30 |
| **AI** | topics ∋ ai | Top types + analysis, opinion, interview, release_notes | 5 | 2 | 72 h | 18 | 25 |
| **Startups** | topics ∋ startups_funding | Top types + analysis, interview | 5 | 2 | 72 h | 24 | 25 |
| **India** | region = in OR topics ∋ india | Top types + analysis | 5 | 2 | 72 h | 24 | 20 |
| **Dev** | topics ∋ dev_tools or security | Top types + release_notes, analysis, tutorial (≤ 1 per 10) | 4 | 2 | 7 d | 48 | 20 |
| **Reads** | lead source `reads: true` or HN-import analysis | analysis, opinion, interview, essay-like `other` | 5 | 2 | 14 d | 96 | 15 |

- **Never shipped:** `topics = []`; `relevance < 2`; `is_news = false` outside Reads and Dev; the types `review, explainer, customer_story, podcast, promo, deal`.
- **Assembly:**
  1. Filter.
  2. Drop events scoring below 25% of the tab median.
  3. Sort by score.
  4. Lead-publisher diversity: ≤ 2 in ranks 1–10, ≤ 4 per tab.
  5. Image-hash dedupe in the top 10.
  6. **Never pad.**

### 2.13 v1 compatibility output (R1, written on every publish, permanent until 1.2 < 5% of sessions)
`edition.json` keeps the exact v1 shape that `Edition.swift` and `Article.swift` decode.

**Mapping:**
- **`category`** must be one of the closed enum values `Top|AI|Startups|Dev|Blogs` (`Article.swift:3-5`). India → `Startups` if the type is funding or acquisition, else `Top`. Reads → `Blogs`. Dev → `Dev`. An unknown value would make the whole decode fail (`Edition.swift:16-20` `try?`), and 1.2 would fall back to raw feeds.
- **`topRank`** is the Top rank.
- **`alsoCoveredBy`** comes from the `v1_name` of the sources.
- **`source`** is the lead's `v1_name`. New sources keep their `name`, which per-source toggles simply ignore.
- `title` is the headline, `excerpt` is the full summary, `content` is the full summary, and `image` is the 720 px URL.

**Checks:** `check_edition.py` decodes every published `edition.json` with a Swift copy of the 1.2 decoders (`server/tools/v1decode/`, built once on the box) **on every publish**. On failure the worker does not publish and sends an ntfy alert.

### 2.14 Degraded mode
**LLM outage** (401, 403, 429 storm, or a pass rate below 50% for 30 min):
- Events with ≥ 2 publishers, or a tier-1 lead, publish with the publisher title and a feed excerpt trimmed to ≤ 55 words at a sentence boundary, with `aiText: false`.
- Previously validated text is reused.
- Ranking uses coverage × authority × F with importance fixed at 6.
- Cards show "Summary pending" and no sparkles.
- No pushes.

**Box outage:** a GitHub Actions workflow (`.github/workflows/safe-mode.yml`, every 15 min, free for public repos) reads the published `health.json`. If it is more than 30 min stale, it runs `main.swift` (existing feed code, no LLM) to write a degraded v1 edition and a v2 manifest with `degraded: true`. It stops publishing as soon as the box publishes again.

---

## 3. Story format

**Headline:**
- ≤ 72 characters, sentence case, active voice, present tense.
- The subject is the company or person; numbers are digits (`$80M`, `$1.4T`, `3x`).
- No questions, teasers, emoji or trailing period.
- Opinion, analysis and interview pieces carry an attribution prefix (`Cramer: …`, `Analysis: …`).
- Hedges are preserved.

**Summary:**
- 3 sentences: S1 says what happened, S2 gives context, S3 says why it matters, concretely.
- Full summary 45–62 words (target 52). Short summary (S1+S2) 28–42 words, written to stand alone.
- Plain text; each fact is stated once.

**`notif_line`:** one sentence, ≤ 110 characters, used as the push body (R3).

**Image** (served from the edge, section 4):
- **Sources:** `og:image`, then the feed `media:content`/`enclosure`. Reject images under 600 px wide, with an aspect outside 1.2–2.2, or under 8 KB. Sources marked `images: generated` always use the generated image.
- **Variants** (16:10 centre crop):
  - `hero`: 1290×806 WebP, q75, about 110 KB, for devices 390 pt or wider;
  - `std`: 720×450 progressive JPEG, q70, for SE, the v1 edition and push attachments (NSE needs JPEG or PNG);
  - `thumb`: 360×225 WebP.
- **Generated image** when nothing is usable: a Pillow-rendered desaturated topic gradient with the source logo and a topic glyph, no text, and `generated: true`.
- **Retention:** 14 days by mtime. Saved items keep their own copy on the device.

---

## 4. Delivery and edge (R2)

**Decision:** a Cloudflare Worker (`edge/`, free tier) on `techgist.<account>.workers.dev`, backed by R2 (10 GB free, no egress fees).
- The GitHub `edition` branch remains a **mirror** and keeps serving the v1 `edition.json` that 1.2 hardcodes.
- **Content addressing:**
  - `v2/manifest.json` is the only mutable file (`Cache-Control: max-age=60`).
  - Tab files are immutable, `v2/tabs/top.<sha8>.json`, with `max-age=31536000, immutable`.
  - Images are `img/<sha8>_{hero,std,thumb}.<ext>`, also immutable.
  - A manifest therefore always points to one self-consistent edition.
- **GitHub mirror:**
  - JSON is force-pushed at most every 10 min.
  - Images go to `img/` as **append-only commits**, squashed weekly by the worker.
- **Client failover:** the manifest lists `bases: ["https://techgist….workers.dev/", "https://raw.githubusercontent.com/pranjaldoshi96/techshorts/edition/"]`. The client fails over on error or a 429.
- **Request budget:**
  - The free Worker allows 100k requests/day. `health.json.edge.requests24h` tracks usage, and ntfy alerts at 70k.
  - Escape hatches, in order: (1) serve images from the GitHub mirror base (a manifest flag, no app change); (2) Workers Paid at $5/mo, which is an owner decision.
- **Other routes on the same Worker:**
  - `/e/<eventId>` is a static share page generated at publish time. It holds the headline, summary, lead link, an Open Graph image and `<meta name="apple-itunes-app">`.
  - `/.well-known/apple-app-site-association` provides Universal Links for `/e/*`.
  - `/v1/report`: `POST {eventId, reason}`, rate-limited per IP at 30/h. Three distinct reports in 24 h auto-hide the AI text: the event republishes with the publisher title and `og:description`, and the owner gets an ntfy alert.
  - `/v1/metric`: `POST {kind: push_open|share|gist_open, eventId?, appVersion}`. It writes daily counts only, with no device id.
  - `/v1/register` arrives in R3.

### 4.1 Edition schema v2
**`v2/manifest.json`:**
```json
{"schemaVersion": 2, "editionId": "2026-11-11T03:35:00Z-7c1e", "publishedAt": "…", "degraded": false,
 "minAppVersion": "2.0", "banner": null, "bases": ["…workers.dev/", "…raw…/edition/"],
 "imagesBase": 0,
 "tabs": [{"id": "top", "title": "Top", "file": "v2/tabs/top.9f2c1a7b.json", "count": 28}, "…ai, startups, india, dev, reads"]}
```

**Event object** (in `v2/tabs/<id>.<sha8>.json`):
```json
{"id": "ev_3fa91c0b2d", "aliases": [], "parentId": null, "rank": 1, "score": 1.058,
 "headline": "…", "summary": "…full…", "summaryShort": "…S1+S2…", "aiText": true,
 "type": "news", "badge": "developing", "importance": 10, "topics": ["ai","startups_funding"], "region": "global",
 "publishedAt": "2026-11-10T13:31:00Z", "updatedAt": "2026-11-11T02:58:00Z",
 "image": {"hero": "…", "std": "…", "thumb": "…", "generated": false},
 "lead": {"source": "guardian", "name": "The Guardian", "title": "…", "url": "…", "publishedAt": "…"},
 "sources": [{"source": "bbc", "name": "BBC", "title": "…", "url": "…", "publishedAt": "…"}],
 "coverage": 10, "onTechmeme": true, "hn": {"points": 715, "comments": 412, "url": "…"},
 "shareUrl": "https://techgist….workers.dev/e/ev_3fa91c0b2d"}
```
- **`badge`:**
  - `breaking` when the event is breaking-qualified (section 6.2) and `publishedAt` is under 3 h ago;
  - `developing` when ≥ 2 new publishers joined in the last 3 h and the event is older than 2 h;
  - otherwise `null`.
- `sources`: up to 12 entries, excluding the lead.
- No `content` or `excerpt` fields. A tab file is 30–60 KB.

---

## 5. App 2.0 (SwiftUI, iOS 17+)

### 5.1 Data layer
- **New files:** `Models/Event.swift`, `Services/EditionClient.swift` (manifest poll with ETag; fetch only changed tab hashes; base failover), `Services/ReadState.swift`, `Services/ImageLoader.swift`.
- **Deleted:** `Summarizer.swift`, `PageMetadata.swift`, `BreakingNews.swift`. `DailyDigest.swift` is deleted in R3.
- **Kept as `Services/LegacyFallback/`:** `FeedService`, `FeedParser`, `Ranker`, `HTMLText` and a slimmed `FeedSource` with 12 sources and no summaries.
  - Used **only** when both bases fail, or the manifest is more than 3 h old and not `degraded`.
  - Shows titles and excerpts, `aiText: false` styling, and a banner.
  - Deleted after 30 days of `health.json` showing ≥ 99.5% of 5-min windows fresh.
- **Ordering:** the client applies exactly 4 transforms, in order.
  1. **Top tab only:** score × (region == userRegion ? 1.25 : 1) × (topics ∩ userTopics ≠ ∅ ? 1.15 : 1), then a stable re-sort. `both` applies no region multiplier.
  2. **User controls:** muted sources are removed. Each "Show fewer like this" multiplies the event's primary topic by 0.7, with a floor of 0.35. Stored locally.
  3. **Unread first:** a stable partition sinks read events. "Read" means on screen ≥ 1.5 s or opened; the read set is kept 7 days.
  4. **Applying a new edition** happens only on a pill tap, pull-to-refresh, a tab change, or a foreground after ≥ 10 min in background. The array is replaced and `scrollPosition` is set explicitly in the same `Transaction`. **The list never re-orders under the user's finger.**

### 5.2 Design tokens (`Theme.swift`)
- **Colors:**
  - `CardBackground` = systemBackground.
  - Image scrim: black, alpha 0 → 0.45.
  - `BadgeBreaking` = systemRed; `BadgeDeveloping` = systemOrange; `BadgeNew` = AccentColor.
  - Hairline: 8% black in light mode, 10% white in dark mode.
- **Type:**
  - Headline: `.title2` serif bold, `lineSpacing(2)`, `lineLimit(3)`.
  - Body: `.body` with `lineSpacing(5)`, `primary.opacity(0.85)`.
  - Meta: `.footnote` secondary.
  - Badge: `.caption2.heavy`, tracking 0.6.
- **Spacing:** horizontal 20; image→meta 14; meta→headline 8; headline→body 10; body→coverage 12.
- **Time (`RelativeTime.swift`)** is based on `publishedAt`, clamped to ≤ now. Format: `now`, `12m`, `3h`, `Yesterday`, `Nov 4`. When `updatedAt − publishedAt > 1 h`, show `Updated 2h` instead.

### 5.3 Card (`CardView.swift` + `CardLayout: Layout`)
**Size contract:**
1. Measure the text block (meta, headline, **full** summary, coverage) at the card width. Set `imageH = containerH − textH − footer(48) − spacing`.
2. If `imageH < 170`, re-measure with `summaryShort`.
3. If `imageH` is still under 170, show the image as a 72 pt thumbnail in the meta row with no hero.
4. Clamp `imageH` to ≤ width × 0.625.
5. No `minimumScaleFactor`, no font change.

**Budget on iPhone SE** (container ≈ 550 pt): the short summary is about 7 lines (≈ 154 pt) and the headline ≈ 84 pt, giving a hero image of ≈ 175 pt. iPhone 16 shows the full summary with a ≈ 225 pt image. Pro Max shows the full summary with the image capped at ≈ 269 pt.

**Image:**
- Full-bleed with the bottom corners rounded 12.
- The scrim carries the source badge (logo 20 + name, white `.caption.semibold`).
- `hero` is used on widths ≥ 390 pt, `std` otherwise.
- **No parallax.**

**Meta row:**
- Badge pill. `BREAKING` is static; it pulses twice on first appearance only, and not with Reduce Motion.
- Type label for non-news (`ANALYSIS`, `OPINION`, `INTERVIEW`, `RELEASE`).
- Time.
- `sparkles` 11 pt (label "Summarized by AI"), or "Summary pending" when `!aiText`.

**Coverage row** (shown when coverage ≥ 2): up to 4 overlapping logos, then "TechCrunch + 6 more" and "· 715 pts on HN". Tapping opens Full coverage.

**Footer** (48 pt, `.ultraThinMaterial`):
- "Read more at The Guardian ›".
- Save, with symbol replace, bounce and a `.success` haptic.
- Share.

**Gestures:**
- Tapping the text opens the Reader.
- Long-press menu: Save, Share, Full coverage, Copy link, **Show fewer like this**, **Mute <source>**, **Report summary problem** (posts to `/v1/report`; no mailto).
- **Swipe left** opens the Reader, attached as `.simultaneousGesture(DragGesture(minimumDistance: 30))` with `|dx| > 80 && |dx| > 2|dy|`. It is gated by a 1-day spike: if any vertical-paging UI test fails with it attached, it is cut and the footer link stays the only path.

**Haptics:** only on chip change (`.selection`), save (`.success`), refresh success (`.success`) and pill appearance (`.impact(.light)`). **None on page settle.**

### 5.4 Feed screen (`ContentView.swift`)
- **Nav bar:** inline logo + "Techgist", with Search (`magnifyingglass`), Saved and Settings.
- **Search** covers all cached tabs and Saved locally (headline, summary, sources, entities).
- **Chips:**
  - Capsule chips with a `matchedGeometryEffect` selection and `.spring(response: 0.35, dampingFraction: 0.85)`.
  - Unread dot.
  - Order follows region: Bengaluru users get Top, India, AI, Startups, Dev, Reads; Bay Area users get Top, AI, Startups, Dev, Reads, India.
  - A temporary **Gist** chip appears first when the app is opened from the morning gist (R3).
- **Progress bar:** 2 pt.
- **Paging:** `ScrollView` + `LazyVStack` + `.scrollTargetBehavior(.paging)` + `.scrollPosition(id:)`, with `containerRelativeFrame(.vertical)`. Depth uses the existing `scrollTransition` (scale 0.94 and opacity 0.5 above the current card, offset 24 below), disabled with Reduce Motion.
- **Tabs:**
  - The horizontal `TabView` (`ContentView.swift:22-28`) is removed; switching happens only through the chips, with a push transition.
  - Each tab's position is restored if the app was used in the last 30 min.
- **New-stories pill:**
  - The manifest is polled on foreground and every 5 min, using the URLCache default and removing `.reloadIgnoringLocalCacheData`.
  - The pill shows when ≥ 1 unseen event appears in the current tab's top 10. Label: `↑ 3 new stories`.
  - It springs in, auto-hides after 8 s, and a tap applies the edition and scrolls to the top.
- **Pull-to-refresh:** `.refreshable`.
- **End-of-tab card:** "You're all caught up", count, updated time, "Back to Top" and "Saved".
- **States:**
  - Offline with cache: banner `Offline · stories from 2h ago`.
  - `degraded` or legacy fallback: `Summaries catching up · showing headlines`.
  - Manifest more than 2 h old: `Updated 3h ago`.
  - `minAppVersion` and `banner` banners.
- **Cold start with no cache:** a redacted skeleton (`.redacted(.placeholder)` + shimmer). **No bundled seed content.** Live data crossfades in over 0.25 s.

### 5.5 Images
- **`ImageLoader` actor:** `NSCache` (80 MB) plus a disk cache in Caches/ kept 7 days.
- **Off-main work:** downsampling with `byPreparingThumbnail`, and WebP decode through ImageIO.
- **Prefetch:** index+1…+3.
- **Loading state:** a shimmer block, then a 0.25 s fade-in.
- The old `StoryCover` (`CardView.swift:106-135`) is deleted.

### 5.6 Secondary screens
- **Full coverage sheet** (medium/large detents):
  - Header: "Covered by 10 outlets · first reported 14h ago by The Guardian", plus a link to the parent or follow-up event.
  - Rows: logo, outlet, original title, time.
- **Reader:** `SFSafariViewController` presented from UIKit as a page sheet, entering Reader mode when available, with AccentColor tint. Replaces the `.sheet` at `ContentView.swift:62` and `SupportViews.swift:47`. Opening marks the event read.
- **Saved:**
  - Stores the full event JSON and the thumb.
  - Search, swipe to delete, Clear all.
  - Refreshes text when the id (or its alias) is still live.
- **Settings:**
  - Region (Bengaluru / Bay Area / Both) and topics.
  - Muted sources (list with unmute) and reset "Show fewer".
  - Reader on/off.
  - About.
  - Notifications section arrives in R3.
  - The per-source toggles are removed, replaced by long-press Mute.
- **Share** (`ShareCard.swift`):
  - The image card plus `shareUrl` (`/e/<id>`); WhatsApp and iMessage show the OG preview.
  - Universal Links open the card in the app.
- **Onboarding:**
  - 3 pages: (1) a live sample card with "Tech news in 60 words. Ranked by what matters."; (2) region, defaulted from the time zone, plus topics; (3) done.
  - No notification prompt.
  - The 20 s timer (`ContentView.swift:74-81`) is removed.

### 5.7 Accessibility
- **Dynamic Type:** at ≥ `.accessibility1` the card uses the thumbnail layout with the text block in an inner `ScrollView`.
- **VoiceOver:** a combined label `"\(badge), \(headline). \(summary). \(lead), \(time)"` with actions Save, Share, Read more, Full coverage, Show fewer, Mute.
- **Reduce Motion:** no scale or offset, and opacity transitions only.
- **Hit targets:** all ≥ 44×44.

---

## 6. Notifications (R3, app 2.1)

### 6.1 Architecture
- **Sender:** APNs token-based push sent from the box (`server/worker/push.py`, `httpx[http2]` + `PyJWT`, ES256 JWT refreshed every 40 min).
- **Registry:** the device registry lives in the same Cloudflare Worker, with D1.
- **Rich content:** a Notification Service Extension attaches the image.
- **Local notifications deleted:** `BreakingNews.swift` and `DailyDigest.swift` go; `BGAppRefresh` only warms the cache.
- **Rejected options:** FCM (SDK plus a data processor), CloudKit (needs iCloud), GitHub (secret in the app).

### 6.2 Breaking rules
**An event qualifies when all of these hold:**
- Clamped event importance ≥ 8 (section 2.10), relevance 3, and **≥ 2 members flagged `breaking`**, **or** 1 tier-1 member flagged plus `on_techmeme`.
- `event_published_at` is under 3 h ago. Follow-up events use their own time.
- The headline passed the entailment gate.
- If `sensitive`, the push title is the lead's original title (trimmed to 72 characters at a word boundary), never AI text.
- It has not already been pushed (aliases count).

**India path:** a `region=in` event qualifies with 2 Indian publishers including 1 India tier-1, at importance ≥ 8.

**Owner hold:** on qualification, the box sends the owner an ntfy message (original title → push title, `tg kill <id>`) and **auto-sends after 5 min** unless killed.

**Per-device conditions:**
- Breaking is enabled and the device is not provisional.
- Local time is outside 22:00–07:30. Inside quiet hours the event becomes a gist candidate.
- Fewer than 2 breaking pushes so far on the local date.
- At least 3 h since the device's last breaking push.
- Region gate: `in`/`us` events go only to matching or `both` devices unless importance ≥ 9. `global` events go to everyone.

**Latency:** `health.json` records `feed_published_at → apns_sent` (p50/p95). The promise is set from 30 days of data. Expected: under 30 min after second-source confirmation.

### 6.3 Morning gist
- **Timing:** once per local date, in `[briefMinute, briefMinute + 15 min)`. Default 08:00 (02:30Z for IST, 16:00Z for PST).
- **Content:** the top 5 Top events by region-adjusted score with `event_published_at` in the last 18 h, excluding events pushed to that device as breaking in the last 12 h.
- **Weekends:** included.
- **Overall cap:** 3 pushes per device per local day.

### 6.4 Payloads
**Breaking:**
```json
{"aps": {"alert": {"title": "<event headline>", "body": "<notif_line>"},
         "sound": "techgist.caf", "mutable-content": 1, "thread-id": "breaking", "category": "STORY",
         "relevance-score": 1.0, "interruption-level": "active", "target-content-id": "ev_…"},
 "eventId": "ev_…", "tab": "top", "image": "<std 720 jpg>", "event": {"…": "compact event JSON"}}
```
No "Breaking" subtitle: the headline is the title and the body is new information.

**Gist:**
- `title` = the #1 headline.
- `subtitle` = "Morning gist · 5 stories".
- `body` = `"• h2\n• h3\n• h4"`.
- `thread-id: brief`, image of #1, `gistIds: [5 ids]`.
- A tap opens the **Gist** chip with exactly those 5 cards.

**Common rules:**
- Payloads stay under 4 KB.
- No `time-sensitive`.
- `apns-collapse-id` = event id.
- `apns-expiration`: +3 h for breaking, +2 h for the gist.

**NSE** (`ios/TechgistNotificationService/`):
- Downloads `image` within an 8 s timeout and attaches it with a centre clipping rect.
- On timeout it delivers the notification without the image.

**Category `STORY`:**
- Actions: Save (background; writes the `event` to Saved via the App Group) and Share (foreground).
- `hiddenPreviewsBodyPlaceholder`.
- `categorySummaryFormat`.

**Sound:** `techgist.caf`, a 0.6 s two-note chime (E6→B6), IMA4 at 44.1 kHz, −6 dBFS. Generated by `ios/scripts/make_chime.sh` (sox + afconvert) and checked in.

**Tap:**
- `didReceive` calls `store.focus(eventId:tab:)`: refresh if the edition is more than 5 min old, resolve aliases, switch tab, scroll, and run a 1.2 s highlight stroke.
- If the event is missing from the feed, it is inserted at index 0 from the payload without shifting the visible id.
- A `push_open` metric is posted.

**Foreground:** `[.banner, .list, .sound]`, or `[.list]` when the event is the card on screen.

### 6.5 Registry and sender
**Endpoints:**
- `POST /v1/register` `{token, env, tz, region, brief, briefMinute, breaking, provisional, appVersion}`:
  - validates the token;
  - rate-limits **per token** at 30/min;
  - caps the table at 200k rows (beyond that it returns 507 and alerts the owner);
  - upserts the row.
- `POST /v1/unregister`.
- `GET /v1/devices?since=<updated_at>` (bearer secret) is **incremental**. The box keeps its own copy in state.db.
- `DELETE /v1/devices/:token` (bearer secret).

**Environment:** `env` is set at compile time (`#if DEBUG` → sandbox, else production). On `BadDeviceToken`, the sender retries once on the other host before deleting. A 410 deletes immediately.

**When the app re-registers:**
- when it receives a token;
- on any notification-setting change;
- on `NSSystemTimeZoneDidChange`;
- on every foreground where the time zone differs from the registered one;
- otherwise every 7 days.

**Provisional devices** register with `breaking=false, brief=true`.

### 6.6 Permission flow
- **Trigger:** the 8th card view in a session or the first Save, after the scroll has settled.
- **Sheet:** a mock notification rendered from today's #1 event, the text "One morning gist at 8:00 and at most 2 breaking alerts a day.", and a time picker.
- **Turn on:** `requestAuthorization([.alert, .sound])` → `registerForRemoteNotifications` → register.
- **Not now:**
  - `.provisional` (quiet delivery of the gist only).
  - The offer returns after ≥ 3 days and ≥ 3 sessions, at most 2 more times.
- **`notificationsOffered`** is set only after the user chooses.
- No badge.

### 6.7 Privacy and App Review
**Owner step 0, before any R2 or R3 submission:** read the 1.1 rejection reason in App Store Connect Resolution Center and add it to the checklist.

**Privacy label:**
- Identifiers → Device ID (push token), App Functionality, not linked, no tracking.
- No analytics SDK. `/v1/metric` holds aggregate counts with no identifiers. Retention comes from App Store Connect App Analytics.

**Policy and settings text:**
- Add the policy line: "We store a push token, time zone, region and notification preferences to deliver notifications. Turn notifications off to delete them."
- Toggling notifications off unregisters the device.
- Settings footer replaces the text at `SupportViews.swift:~111`.

**Review notes:**
- Guideline 4.5.4: push is opt-in, never marketing, and can be turned off in the app.
- AI labeling: the sparkles label on every card, publisher attribution and link, report action.

**Content:** cards carry ≤ 62-word summaries with a prominent link to the publisher. No full-text re-hosting. Images can be switched to generated per source (`images: generated`) on any complaint.

### 6.8 Owner one-time steps
1. Apple Developer → Keys → APNs key → `AuthKey.p8` to `~/.techshorts/apns/` with `chmod 600`. Set `APNS_KEY_ID`, and `APNS_TEAM_ID=A4HLS6BV3C`.
2. Xcode: add the Push capability, the NSE target `com.pranjald.techgist.NotificationService`, the App Group `group.com.pranjald.techgist`, and Associated Domains `applinks:techgist.<account>.workers.dev`.
3. Cloudflare (needed at R2):
   - `wrangler login`;
   - `wrangler r2 bucket create techgist`;
   - `wrangler d1 create techgist`;
   - `wrangler secret put WORKER_ADMIN_SECRET`;
   - `wrangler deploy`.
   Put the secret and an R2 API token in `~/.techshorts/env`.
4. ntfy: create a random topic, install the app, and set `NTFY_TOPIC`.
5. Box: `loginctl enable-linger`, `systemctl --user enable --now techgist-worker`, and remove the old crontab line.
6. GitHub: add the `NVIDIA_API_KEY` repo secret, which safe-mode Actions does not need but the v1 decoder build does. The safe-mode workflow is enabled.
7. App Store Connect: privacy label, policy text, review notes.

---

## 7. Server layout and observability

**Layout:**
- `server/worker/`: `main.py`, `config.py`, `db.py`, `sources.py`, `fetch.py`, `prefilter.py`, `text.py`, `attribution.py`, `llm.py`, `validate.py`, `entail.py`, `embed.py`, `cluster.py`, `importance.py`, `rank.py`, `images.py`, `publish_v1.py`, `publish_v2.py`, `push.py`, `health.py`.
- `server/config.yaml` and `server/sources.yaml`.
- `server/tools/`: `check_sources.py`, `check_edition.py`, `label_pairs.py`, `v1decode/`.
- `server/tg` CLI: `pin`, `bury`, `push`, `kill`, `override`, `status`.
- `edge/` (Worker + `schema.sql`).

**Cadence:**
- 60 s tick.
- 3 or 10 min polls per source.
- v2 publishes when a tab hash changes, at most every 5 min, or immediately on a breaking qualification.
- The GitHub mirror is updated at most every 10 min.

**Editorial file:** `~/.techshorts/editorial.yaml`, reloaded each tick, supports `pin`, `bury`, `boost`, `override`, `push`, `kill` and `block`.

**`health.json`** (written on every publish):
- **Freshness:** `editionAgeMin`, publisher→app lag p50/p95 (`feed_published_at → publishedAt`), push latency p50/p95.
- **Pipeline:**
  - per-source status;
  - prefilter drop counts by rule;
  - LLM calls, p50/p95, 429s, fallback calls, and the daily token budget used vs `config.llm.daily_budget`;
  - validation pass rate and skip reasons (`too_long`, `ungrounded`, `hedge_dropped`, `entail_fail`, `no_text`).
- **Content:**
  - raw vs clamped importance histogram;
  - tab counts, median age and max per publisher;
  - number of degraded events;
  - reports in 24 h.
- **Delivery:** edge requests in 24 h; push devices, sends and APNs errors.

**ntfy alerts** (at most 1 per condition per 6 h):
- no publish for 20 min;
- a source fails 6 polls in a row;
- validation pass rate below 80% over 1 h;
- 429s above 20/h, or any 401/403;
- Top count below 12;
- more than 10% of raw importance ≥ 8;
- the v1 decode check fails;
- edge requests above 70k/day;
- an APNs 403;
- worker restarts above 3/h.

**Daily owner digest** at 09:00 IST via ntfy:
- the Top 10 as original title → AI headline pairs;
- entailment-judge failures on a random sample of 50 shipped items;
- reports.

---

## 8. Build plan

### R1: server v1.3 edition (live 1.2 improves, no App Review)

**WP1: Worker skeleton, sources, fetch, timestamps, prefilter.**
- Files: `server/worker/{main,config,db,sources,fetch,prefilter}.py`, `sources.yaml`, `config.yaml`, `check_sources.py`, `techgist-worker.service`, `requirements.txt`, `tests/test_prefilter.py`, `tests/test_timestamps.py`.
- AC:
  - `check_sources.py` is green, and failed sources are listed and excluded from tier-1.
  - A second run within 3 min shows ≥ 60% 304s.
  - An item with feed date `2026-11-12T00:00Z` first seen at `2026-11-11T03:30Z` gets `published_at` = 03:30Z.
  - An item with feed date 5 days ago first seen now has an age of 5 days.
  - Prefilter **drops**: the Blu-ray lifespan and Roku HDMI titles, "Huupe Mini 2 Review", "Fragments: November 10", "Quoting Nilay Patel", "The Pragmatic Summit 2027", and the Wired "Kitchen Gear From Our Place Is Steeply Discounted".
  - Prefilter **keeps**: "Microsoft closes $69B Activision deal", "US bans Nvidia chip sale to China", "FTC opens antitrust review of OpenAI".

**WP2: Text, attribution, LLM, trim, validation, entailment.**
- Files: `{text,attribution,llm,validate,entail}.py`, `tests/test_validate.py`, `tests/fixtures/edition-2026-11-11.json`.
- AC:
  - **Pass rate** on the 223-item fixture (the live 2026-11-11 v1 edition): ≥ 90% of eligible items pass within one retry. 100% of shipped outputs have full summaries of 45–62 words, short summaries of 28–42 words, headlines ≤ 72 characters and `notif_line` ≤ 110 characters.
  - **Attribution and type:**
    - The Cramer item gives `type: opinion` and a `Cramer:` prefix, decided by `attribution.py` regardless of LLM output.
    - `llm-gemini 0.36` gives `release_notes` with importance ≤ 4.
    - Gilbert + Tobin gives `customer_story`.
  - **Grounding:**
    - "$80 million" grounds `$80M`.
    - "£1.4tn" does not ground `$1.4T`.
    - A headline adding "China" absent from the source fails.
    - "admits affair with Chinese spy", where the source has "suspected", fails with `hedge_dropped`.
  - **Relevance:** the Wired GOP extremists, NYT BlackBerries/Presidents and Guardian Hegseth items get relevance ≤ 1.
  - **Region:** the IPO, CoreWeave earnings and Nvidia probe are `global`.
  - **Boilerplate:** the Stratechery fixture summary contains no "subscribers".
  - **Holding:** a no-text item is held with `attempts` and `retry_at`, never as a permanent skip.
  - **Rate limit:** the token bucket stays ≤ 30/min in a 200-item mocked load.

**WP3a: Embedding calibration.**
- Files: `tools/label_pairs.py`, `tests/fixtures/pairs.jsonl` (≥ 60 same-event, ≥ 250 different-event pairs).
- AC: `T_cand`, `T_join` and `T_signal` are chosen at precision ≥ 0.97 and written to `config.yaml`. The PR includes the precision/recall table.

**WP3b: Clustering, follow-ups, merge, lead, importance.**
- Files: `{embed,cluster,importance}.py`, `tests/test_cluster.py`.
- AC, from a fixture replay **in 3 different arrival orders**:
  - **Positive merges:**
    - The IPO filing members (Guardian, CNBC retail, NYT ×2, BBC, TechCrunch founders, Decoder roadshow) form exactly 1 event.
    - The Huang spy items form 1 event.
    - The CoreWeave items form 1 event.
    - "Introducing Businesses in ChatGPT" and the Decoder directory item form 1 event.
  - **Negative merges:** Glasswing, the RAISE Act and the Australia penalties each stay separate from the IPO. Nvidia earnings and the Nvidia probe stay separate.
  - **Lead:** the IPO lead is a hard-news filing article (not TechCrunch founders, not Cramer).
  - **Follow-ups:** a synthetic "Huang resigns" item 50 h later creates a follow-up event with `parentId`.
  - **IDs:** stable across replays; merged ids resolve through the alias table.
  - **Importance clamps:** a single-source gadget launch rated 7 by the LLM is clamped to 5.

**WP4: Ranking, tabs, images, v1 publish, health, degraded mode, safe mode.**
- Files: `{rank,images,publish_v1,health}.py`, `check_edition.py`, `v1decode/`, `.github/workflows/safe-mode.yml`, `server/tg`, `server/README.md` (rewrite, correct cadence).
- AC:
  - **Scoring:** the section 2.11 numbers are reproduced to ±0.01, and the 24 h yield property test passes.
  - **v1 output:**
    - Every `category` is in {Top, AI, Startups, Dev, Blogs}.
    - The 1.2 decoder copy decodes every publish.
    - Every Top article has `topRank`.
    - 100% of articles have AI headline and summary, or are degraded-flagged.
  - **Top contents:** no Top item has relevance < 2 or an excluded type. The live Toyota kei truck, Disney+ VR and Ring drone items would not reach Top.
  - **Deduplication:** pairwise Top headline cosine stays below the calibrated `T_join`.
  - **Freshness:** every event's age is within its tab window, computed from `event_published_at`.
  - **Degraded mode:** with the LLM key revoked, the next publish is degraded and still has ≥ 15 Top items.
  - **Safe mode:** with the worker stopped for 35 min, the Action publishes a degraded edition and an ntfy alert fires.

**WP5 [OWNER]: Cutover.**
- AC:
  - 72 h green: no ntfy alerts apart from source flakiness.
  - Median publisher→app lag below 12 min.
  - The 1.2 app on a device shows AI summaries on every card and no duplicate IPO cards.

### R2: app 2.0 + edge

**WP6: Edge Worker + v2 publish.**
- Files: `edge/{src/index.ts, schema.sql, wrangler.toml}`, `server/worker/publish_v2.py`.
- AC:
  - Tab files and images are content-hashed and immutable, and the manifest is the only mutable file.
  - The GitHub mirror holds the same v2 data, with images append-only and squashed weekly.
  - `/e/<id>` renders an OG preview in iMessage and WhatsApp.
  - The AASA file validates (Apple's CDN validator).
  - `/v1/report`: 3 reports flip the event to `aiText: false` on the next publish.
  - `/v1/metric` stores no identifiers.

**WP7: App data layer.**
- Files: `Models/Event.swift`, `Services/EditionClient.swift`, `Services/ReadState.swift`, `Services/NewsStore.swift`, `Services/LegacyFallback/*`; delete `Summarizer.swift`, `PageMetadata.swift`, `BreakingNews.swift`.
- AC:
  - Only changed tab hashes are fetched (URLProtocol stub).
  - Failover to the mirror on a 429.
  - Legacy fallback activates only when both bases fail or the manifest is more than 3 h old and not degraded.
  - Ordering unit tests per section 5.1, including mute and show-fewer.
  - UI test: inserting at index 0 while on card 5 leaves the visible id unchanged.

**WP8: Card and typography.**
- Files: `Views/CardView.swift` + `CardLayout`, `Theme.swift`, `RelativeTime.swift`, `Services/ImageLoader.swift`.
- AC:
  - **Fit test:** an XCTest renders every live event's text block with `UIHostingController.sizeThatFits` at widths 335/353/390 and the corresponding container heights for SE, 16 and 16 Pro Max.
    - It asserts that the full or short summary fits, the image is ≥ 170 pt (or the thumbnail layout is used, on SE only) and there is no truncation.
    - The SE full-vs-short ratio is recorded.
  - No `minimumScaleFactor` remains.
  - "Updated 2h" appears when `updatedAt − publishedAt > 1 h`.
  - `hero` loads at width ≥ 390 and `std` on SE.
  - The VoiceOver actions are present.

**WP9: Feed shell, motion, controls.**
- Files: `ContentView.swift`, `ChipBar.swift`, `NewStoriesPill.swift`, `EndOfTabCard.swift`, `StateBanner.swift`, `SearchView.swift`.
- AC:
  - `sensoryFeedback` appears only on chip change, save, refresh and pill (grep).
  - No `repeatForever` except the shimmer.
  - The horizontal `TabView` is removed.
  - Swipe-left either passes the paging UI tests or is removed.
  - Reduce Motion is honoured.
  - Offline, degraded and stale banners all show.
  - Search finds a cached event by source name.

**WP10: Coverage, reader, saved, settings, share, onboarding.**
- AC:
  - "+6 more" lists 7 outlets.
  - The Reader opens as a UIKit page sheet in Reader mode.
  - A saved, pruned event still renders.
  - Mute is reversible in Settings.
  - The share sheet includes `/e/<id>`, and tapping that link on a device opens the card.
  - Onboarding defaults the region from the time zone.
  - The India chip is last for Bay Area users.

**WP11 [OWNER]: Release 2.0.**
- AC:
  - The 1.1 rejection reason is read and addressed.
  - TestFlight passes the WP7–WP10 checklists on a physical SE and a Pro Max.
  - `check_edition.py` is green on submission day.

### R3: app 2.1 push

**WP12 [OWNER: Cloudflare]: Registry endpoints.**
- AC:
  - register → `GET /v1/devices?since=` returns the device.
  - 401 without the secret.
  - 400 on a malformed token.
  - 429 on the 31st request in a minute for the same token.
  - The row cap is enforced.

**WP13 [OWNER: .p8]: Sender and rules.**
- Files: `push.py`, `tests/test_push_rules.py`.
- AC, with a frozen clock:
  - **Timing and caps:**
    - IST 08:00 gist sent between 02:30Z and 02:45Z, once per local date; Los Angeles at 16:00Z in November.
    - A breaking push at 23:00 local is withheld.
    - The 3rd breaking push of a local day is not sent.
    - A breaking push within 3 h of the last one is not sent.
  - **Region gate:** `in` at importance 8 goes to `in` and `both` devices, not `us`. `global` goes to everyone.
  - **Sensitive events:** the push title equals the trimmed publisher title.
  - **Owner hold:** `tg kill` within 5 min cancels the push.
  - **Token errors:**
    - `BadDeviceToken` retries on the other host before deleting.
    - A 410 deletes the device.
  - **Live check:** a sandbox send returns 200 with an `apns-id` header.

**WP14 [OWNER: capability]: App push client, NSE, sound, permission, deep links.**
- Files: `PushRegistration.swift`, `NotificationRouter.swift`, `NotificationsOffer.swift`, `TechgistNotificationService/`, `techgist.caf`, `make_chime.sh`; delete `DailyDigest.swift`.
- AC:
  - On a physical device: the chime plays, the banner shows a thumbnail, long-press shows the full image and body, and Save/Share work.
  - Saving from the notification shows up in Saved.
  - A tap highlights the exact card, whether cached or inserted from the payload.
  - A gist tap opens the 5-card Gist chip.
  - The offer appears after the 8th card or first save. "Not now" gives provisional status and registers with `breaking=false`.
  - A time zone change triggers re-registration.
  - grep finds no `UNCalendarNotificationTrigger`.

**WP15 (2.2): Widget.** systemSmall, systemMedium and accessoryRectangular from the App Group, 30-min timeline, deep link to the card.

**Explicitly cut:** a personalized "For You" model, bullet or ELI5 modes, per-story chat, HN comment summaries, Saved sync, audio, polls, a third-party analytics SDK, and parallax.

---

## Appendix A: Critiques rejected or modified

- **"Two agreeing LLM samples before any push":** rejected. The same guarantee comes from median member importance, the corroboration clamps (section 2.10), ≥ 2 members flagged breaking, the entailment gate and the owner hold. A second sample adds cost and latency without new evidence.
- **"Fast breaking path on HN velocity alone":** rejected. HN velocity lags tier-1 plus Techmeme and over-weights developer drama. The fast path is 1 tier-1 + Techmeme.
- **"TelemetryDeck or another analytics SDK":** rejected. It adds an SDK and a privacy-label burden. Aggregate `/v1/metric` counts plus App Store Connect analytics cover push-open, share and retention.
- **"Hotlink og:images through a resizing proxy instead of re-hosting":** modified. A proxy on the free Worker would spend the request budget and break NSE attachments when publishers block it. Instead: derived crops, 14-day retention, `images: generated` per source on request, and a domain blocklist.
- **"Cut the horizontal swipe outright":** modified. It is the Inshorts gesture the owner expects. It ships only if it passes the paging UI tests after a 1-day spike; otherwise it is cut.
- **"Request two separate summaries (short and long)":** modified. A single 3-sentence output with S1+S2 written to stand alone gives both lengths from one call with deterministic trimming. That halves validation work.
- **"Rate-limit the registry at about 120/min per IP":** replaced with a per-token limit plus a table cap. Carrier-grade NAT makes per-IP limits wrong at any value.
- **"Region boost amplifies the India funding flood":** partly rejected. The ×1.25 boost stays (it is the stated audience promise). The flood is handled at the source by importance clamps (single-source rounds cap at 6) and publisher diversity.
- **"Bay Area users get no local sources":** deferred. There is no free, reliable Bay Area tech-local feed set worth adding. Bay Area users get `us`-region boosting and chip ordering instead.
- **"5-min owner hold will delay breaking":** accepted as a trade-off. It is a cap, not a wait (auto-send) in exchange for catching defamation-grade errors before they reach lock screens.
