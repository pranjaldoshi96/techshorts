"""Entailment gate (spec 2.8): a second LLM call that asks whether the source's own opening supports the headline
and S1. It replaces the old cosine "faithfulness" check, which passed fluent headlines that added facts.

The gate is spec 2.8 as written: importance ≥ 6, OR relevance ≥ 2, OR breaking. A narrower reading (importance
and relevance together) was tried and rejected in review: event importance is the median of its members (2.10),
so a lead scored 5 can still head a Top event, and it let wrong essay headlines ship to the AI/Dev/Reads tabs.

What the judge sees: news-shaped items get the opening (title + first paragraphs, spec 2.8), because a news
headline must come from the lede. Essays, interviews and other non-news types get the whole text the writer saw
(capped at 800 words): their point is rarely in the first paragraphs, and judging them on the opening alone failed
correct headlines ("Neil Kakkar compares two eras of shipping...") while adding nothing for accuracy.
"""
import json
import re
from dataclasses import dataclass

PROMPT = """You check a news app's headline and first summary sentence against the source.

Does ORIGINAL TITLE + SOURCE TEXT entail HEADLINE and S1? A claim is entailed when the source states it or it \
follows directly. Paraphrase, rounding ("$1.405T" -> "$1.4T") and dropping detail are fine. Adding a fact, a \
number, a cause, certainty the source hedges ("suspected", "reportedly"), or presenting an opinion as fact is not. \
A headline starting with "Name:" or "Analysis:" reports what the author argues or says; it is entailed when the \
author argues or says it.

Reply with only JSON: {"entailed": "yes" or "no", "unsupported": "the unsupported claim, or empty"}

ORIGINAL TITLE: <<title>>
SOURCE TEXT:
<<lead>>

HEADLINE: <<headline>>
S1: <<s1>>"""


@dataclass
class Entailment:
    ok: bool
    unsupported: str = ""

    def feedback(self):
        claim = self.unsupported or "a claim"
        return f"The source's opening does not support: {claim}. Use only what the source states."


# Types whose headline must come from the lede; everything else is judged against the whole text.
NEWS_TYPES = {"news", "launch", "funding", "acquisition", "earnings", "policy", "security_incident", "deal"}


def needed(output):
    return output.importance >= 6 or output.relevance >= 2 or output.breaking


def context(output, text, attribution=None):
    """The source text the judge sees (see module docstring)."""
    labelled = attribution is not None or output.headline.split(":", 1)[0] in ("Analysis", "Opinion", "Interview")
    if output.type in NEWS_TYPES and output.is_news and not labelled:
        return text.lead()
    return text.text


def fill(template, **values):
    """One-pass placeholder fill, so a title containing a placeholder-like word can't be substituted twice."""
    return re.sub(r"<<(\w+)>>", lambda m: str(values[m.group(1)]), template)


def build(title, lead, headline, s1):
    return fill(PROMPT, title=title, lead=lead, headline=headline, s1=s1)


def parse(content):
    m = re.search(r"\{.*\}", content or "", re.S)
    data = json.loads(m.group(0)) if m else {}
    verdict = str(data.get("entailed", "")).strip().lower()
    if verdict not in ("yes", "no", "true", "false"):
        raise ValueError("entailment reply without a yes/no")
    return Entailment(verdict in ("yes", "true"), str(data.get("unsupported") or "").strip())


def check(client, model, title, lead, headline, s1):
    content = client.chat(model, [{"role": "user", "content": build(title, lead, headline, s1)}],
                          max_tokens=200, kind="entail")
    return parse(content)
