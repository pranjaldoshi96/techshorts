"""Regression tests for the third review of the VPS test run (revision 222e760, the 22:10 edition of 2026-11-15,
115 cards). Each test names the card, event or row it guards against. No network: the LLM is a scripted client.
"""
import json
from datetime import timedelta

import pytest

from worker import validate
from test_spice_and_truth import NOW, SEBI, SEBI_SRC, SEBI_TITLE, check, out  # noqa: F401


# 11. The tense rule: any capitalization, and plan -> done drift on the same verb wherever the plan is stated

@pytest.mark.parametrize("title", ["Sebi To Issue AI Usage Guidelines For Capital Markets",
                                   "SEBI TO ISSUE AI USAGE GUIDELINES", "Sebi to Issue AI usage guidelines"])
def test_a_title_case_plan_binds_the_headline(title):
    assert validate.future_marker(title, validate.PLAN_MARK) == "to issue"
    assert validate.tense_problem("Sebi issues AI usage guidelines", title) == "to issue"
    v = check(SEBI, title=title, headline="Sebi issues AI usage guidelines for capital markets")
    assert not v.ok and "tense_drift" in v.reasons


def test_a_prospective_word_elsewhere_does_not_excuse_the_done_verb():
    # "ahead of" counted as keeping the plan, so "Sebi issues ... ahead of" passed the title rule.
    v = check(SEBI, headline="Sebi issues AI guidelines ahead of market overhaul",
              src=SEBI_SRC + " The rules come ahead of a wider market overhaul.")
    assert not v.ok and "tense_drift" in v.reasons
    assert "issues" in next(f for f in v.feedback if "still to come" in f)


IPO_TITLE = "Anthropic launches roadshow for its initial public offering"
IPO_SRC = ("Anthropic began its roadshow on Monday and is expected to price its shares at $70 to $78 each, people "
           "familiar with the matter said. The offering will be priced on Wednesday evening and the stock is set "
           "to list on Nasdaq on Thursday, according to the timetable.")


@pytest.mark.parametrize("line,drift", [
    ("Anthropic IPO priced at $70 to $78 a share", "priced"),             # "priced" before pricing
    ("Anthropic prices IPO at $70 to $78", "prices"),
    ("Anthropic lists on Nasdaq after roadshow", "lists"),
    ("Anthropic expected to price IPO at $70 to $78", None),
    ("Anthropic IPO to be priced at $70 to $78 on Wednesday", None),
    ("Anthropic IPO pricing expected at $70 to $78", None),
    ("Anthropic began its roadshow on Monday", None),
    ("If priced at the top, Anthropic would raise the most", None),
])
def test_a_plan_in_the_text_must_not_be_stated_as_done(line, drift):
    got = validate.done_before_due(line, IPO_TITLE, IPO_SRC)
    assert (got[0].lower() if got else None) == drift


def test_the_source_reporting_it_done_clears_the_verb():
    src = IPO_SRC + " The company priced a smaller debt offering in June."
    assert validate.done_before_due("Anthropic priced a debt offering in June", IPO_TITLE, src) is None
    # The alert and the summary are held to the same rule as the headline: it names a verb, so it does not
    # trip on an alert that states another, present fact (the old wide rule did and was taken off them).
    v = check(SEBI, notif_line="Sebi issued guidelines that keep critical market decisions under human oversight.")
    assert not v.ok and v.reasons == ["tense_drift"]
    assert check(SEBI).ok


@pytest.mark.parametrize("later,verb", [
    ("Elon Musk signs petition to reinstate Jensen Huang as Nvidia CEO", "sign"),
    ("Elon Musk, other tech execs sign petition calling on Nvidia to reinstate Jensen Huang", "sign"),
    ("Jensen Huang return petition crosses 1.4 lakh signatures as big tech leaders join campaign", None),
    ("Musk says he is ready to sign the Huang petition", None),
    ("Musk has not signed the petition yet", None),
])
def test_a_later_title_that_reports_the_plan_done(later, verb):
    planned = "Musk backs petition calling for Jensen Huang’s return, says he will sign it"
    assert validate.realized_by(planned, later) == verb
