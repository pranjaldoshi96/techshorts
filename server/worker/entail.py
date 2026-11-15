"""Entailment gate (spec 2.8): a second LLM call, on the judge model, that asks whether the source supports the
card. It replaces the old cosine "faithfulness" check, which passed fluent headlines that added facts.

What is checked: every reader-facing line of the card (headline, the three summary sentences, the push alert's
title and hook), not only the headline and S1. The 2026-11-15 VPS test run shipped cards whose headline and S1 were
fine while a later sentence was not: "may have contributed to the company's payroll crisis" (an inference the
source never drew), "matching the performance of Opus 5 ... while costing less per token" (the source said the
price was higher), "IPO priced at $70 to $78" (the source said "expected"). The prompt names the ways a fluent
card goes wrong (tense and certainty, numbers moved to another thing, causes and superlatives added, allegations
stated as fact, dropped sourcing, facts from two digest items joined), because a bare "is it entailed?" let them by.

The gate is spec 2.8 as written: importance >= 6, OR relevance >= 2, OR breaking. A card needs relevance >= 2 to
ship at all (rank.never_shipped), so every shipped card has passed this check.

What the judge sees: the whole text the writer saw (capped at 800 words). The headline of a news item must still
come from its opening, which the hedge and tense rules in validate.py enforce on the title and the lede.
"""
import json
import re
from dataclasses import dataclass

PROMPT = """You are the fact-checker of a news app. Check a card against its source. The card is thrown away \
unless every line of it is supported, so be strict and literal.

A line is supported when ORIGINAL TITLE + SOURCE TEXT states it or it follows directly. Paraphrase, rounding \
("$1.405T" -> "$1.4T") and dropping detail are fine. A headline starting with "Name:" or "Analysis:" reports \
what the author argues or says; it is supported when the author argues or says it. "Report:" or "sources say" \
is supported when the source attributes the claim to a report or to unnamed sources.

Reject (entailed "no") if any line does any of these:
1. Adds a fact, name, number, date or quote that is not in the source.
2. Changes tense or certainty: states as done what the source says is planned, expected, proposed, in talks or \
"to" happen ("Sebi to issue guidelines" -> "Sebi issues guidelines"; "expected to price at $70 to $78" -> \
"priced at $70 to $78"; an invitation to buy -> "sells"); or drops a hedge (suspected, alleged, reportedly).
3. Presents something the source dates in the past (last year, in June 2025) as happening now.
4. Moves a number to a different thing than the source measures, or changes what it compares ("performs like \
models from a few months ago" -> "matches current models"; "higher per-token price" -> "costs less").
5. Adds a superlative or scale the source does not state ("record", "largest", "first", "millions").
6. Adds a cause, motive, blame or consequence the source does not state ("may have contributed to", "which led \
to", "forcing").
7. States an allegation or unnamed-source claim about a named person or company as established fact, or leaves \
out who is alleging it.
8. Joins facts from two separate items of a digest or list into one claim.
9. Is a teaser that withholds the fact ("one key quality", "here is why") instead of stating it.

Reply with only JSON: {"entailed": "yes" or "no", "line": "which line fails, or empty", \
"unsupported": "the unsupported claim and what the source actually says, in one sentence, or empty"}

ORIGINAL TITLE: <<title>>
SOURCE TEXT:
<<lead>>

CARD
HEADLINE: <<headline>>
S1: <<s1>><<rest>>"""


@dataclass
class Entailment:
    ok: bool
    unsupported: str = ""
    line: str = ""

    def feedback(self):
        claim = self.unsupported or "a claim"
        where = f" ({self.line})" if self.line else ""
        return (f"The fact-check failed{where}: {claim} Rewrite that line with only what the source states, "
                f"keeping its tense, hedges and attribution.")


# Types whose headline must come from the lede (validate.py's hedge and tense scope).
NEWS_TYPES = {"news", "launch", "funding", "acquisition", "earnings", "policy", "security_incident", "deal"}


def needed(output):
    return output.importance >= 6 or output.relevance >= 2 or output.breaking


def context(output, text, attribution=None):
    """The source text the judge sees: everything the writer saw (see module docstring)."""
    return text.text


def fill(template, **values):
    """One-pass placeholder fill, so a title containing a placeholder-like word can't be substituted twice."""
    return re.sub(r"<<(\w+)>>", lambda m: str(values[m.group(1)]), template)


def build(title, lead, headline, s1, s2="", s3="", notif_title="", notif_line=""):
    rest = "".join(f"\n{label}: {value}" for label, value in (
        ("S2", s2), ("S3", s3), ("ALERT TITLE", notif_title), ("ALERT", notif_line)) if value)
    return fill(PROMPT, title=title, lead=lead, headline=headline, s1=s1, rest=rest)


def parse(content):
    m = re.search(r"\{.*\}", content or "", re.S)
    data = json.loads(m.group(0)) if m else {}
    verdict = str(data.get("entailed", "")).strip().lower()
    if verdict not in ("yes", "no", "true", "false"):
        raise ValueError("entailment reply without a yes/no")
    return Entailment(verdict in ("yes", "true"), str(data.get("unsupported") or "").strip(),
                      str(data.get("line") or "").strip())


def check(client, model, title, lead, headline, s1, s2="", s3="", notif_title="", notif_line=""):
    content = client.chat(model, [{"role": "user", "content": build(title, lead, headline, s1, s2, s3,
                                                                    notif_title, notif_line)}],
                          max_tokens=700, kind="entail", temperature=0.0)
    return parse(content)
