"""Entailment gate (spec 2.8): a second LLM call that asks whether the source's own opening supports the headline
and S1. It replaces the old cosine "faithfulness" check, which passed fluent headlines that added facts.

Only items that can reach Top or a push are judged (spec 2.1 "entailment gate if Top/push-eligible"): importance
≥ 6 with relevance ≥ 2, or breaking. Top needs both, and a push needs importance ≥ 8, so spec 2.8's "importance ≥ 6,
relevance ≥ 2 or breaking" is read that way. Read as three alternatives, relevance ≥ 2 alone would send nearly every
item, essays included, to a judge that only sees the opening, and an essay's point is rarely in its first paragraphs.
"""
import json
import re
from dataclasses import dataclass

PROMPT = """You check a news app's headline and first summary sentence against the source.

Does ORIGINAL TITLE + FIRST PARAGRAPHS entail HEADLINE and S1? A claim is entailed when the source states it or it \
follows directly. Paraphrase, rounding ("$1.405T" -> "$1.4T") and dropping detail are fine. Adding a fact, a \
number, a cause, certainty the source hedges ("suspected", "reportedly"), or presenting an opinion as fact is not.

Reply with only JSON: {"entailed": "yes" or "no", "unsupported": "the unsupported claim, or empty"}

ORIGINAL TITLE: <<title>>
FIRST PARAGRAPHS:
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


def needed(output):
    return (output.importance >= 6 and output.relevance >= 2) or output.breaking


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
