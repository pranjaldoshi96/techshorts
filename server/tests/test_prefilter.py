"""Prefilter rules (spec 2.4), including the WP1 acceptance titles from the 2026-11-11 edition."""
import pytest

from worker import prefilter

ARTICLE = "https://example.com/2026/11/story/"


@pytest.mark.parametrize("title, rule", [
    # WP1 acceptance: must drop.
    ("What is the average lifespan of recordable Blu-ray discs?", "title_question"),
    ("Why your Roku might be plugged into the wrong HDMI port", "title_question"),
    ("Huupe Mini 2 Review", "title_review"),
    ("Huupe Mini 2 Review: More Game Than It Can Handle", "title_review"),  # the live Wired title
    ("Fragments: November 10", "title_fragments"),
    ("Quoting Nilay Patel", "title_quoting"),
    ("The Pragmatic Summit 2027", "title_summit"),
    ("Kitchen Gear From Our Place Is Steeply Discounted", "title_shopping"),
    # Other spec rules.
    ("Review: the Pixel 11 is a quiet upgrade", "title_review"),
    ("Pixel 11: review", "title_review"),
    ("How to get this Siri camera upgrade for your iPad", "title_question"),
    ("When do Black Friday sales start", "title_question"),
    ("Headphones are 40% off today", "title_shopping"),
    ("Use this promo code for cheaper Kindles", "title_shopping"),
    ("Sponsored: build agents faster", "title_sponsored"),
    ("Hard Fork episode 112: agents everywhere", "title_podcast"),
    ("Our 2026 gift guide for gamers", "title_gift_guide"),
    ("The best laptops to buy right now", "title_best_to_buy"),
    ("The best phones for 2027", "title_best_to_buy"),
    # Ported from the 1.2 ContentFilter.
    ("Best Early Amazon Black Friday Deals (2026)", "v1_title"),
    ("The 12 best mechanical keyboards", "v1_title"),
    ("StrictlyVC comes to Bengaluru", "v1_title"),
])
def test_drops_with_named_rule(title, rule):
    assert prefilter.check(title, ARTICLE) == rule


@pytest.mark.parametrize("title", [
    # WP1 acceptance: must keep (bare deal / sale / review are news words).
    "Microsoft closes $69B Activision deal",
    "US bans Nvidia chip sale to China",
    "FTC opens antitrust review of OpenAI",
    # Live 2026-11-11 news that is close to a rule but must pass.
    "Anthropic targets valuation of up to $1.4tn in stock market flotation",
    "Who is the suspected Chinese spy at centre of the Nvidia investigation?",
    "What happens to mathematics when AI becomes an oracle?",
    "How SCSKiller is trying to fix shader stuttering in PC games",
    "Jim Cramer says Anthropic's explosive growth could justify a record-breaking IPO",
    "Oura is acquiring medical AI chatbot startup Counsel Health",
    "Seagate's new hybrid drive raises some old questions",
    "Proximal Ventures raises debut fund to back startups bringing AI to drug development",
    "Reviewers flag problems in Glasswing's CVE findings",
])
def test_keeps_news(title):
    assert prefilter.check(title, ARTICLE) is None


@pytest.mark.parametrize("url, rule", [
    ("https://www.engadget.com/deals/the-best-headphones/", "url_deals"),
    ("https://www.wired.com/coupons/dell", "url_coupons"),
    ("https://www.nytimes.com/wirecutter/shopping/thing.html", "url_shopping"),
    ("https://example.com/gift-guide/2026/", "url_gift-guide"),
    ("https://example.com/sponsored/acme-cloud", "url_sponsored"),
    ("https://economictimes.indiatimes.com/tech/insights/thing/12345.cms", "url_insights"),
    ("https://example.com/brandstory/acme", "url_brandstory"),
    ("https://example.com/partner-content/acme", "url_partner-content"),
    ("https://example.com/podcast/ep-12", "url_podcast"),
    ("https://inc42.com/events/fast42", "url_events"),
    ("https://www.wired.com/review/huupe-mini-2/", "url_review"),
    ("https://example.com/reviews/pixel-11", "url_reviews"),
    ("https://example.com/podcast", "url_podcast"),
    ("https://example.com/tech/black-friday-deal-sale-2026", "v1_path"),
])
def test_url_drops(url, rule):
    assert prefilter.check("Ordinary news headline", url) == rule


def test_url_rules_need_a_path_segment():
    # "/events/" must be a segment, not part of a slug like "major-events-reshape".
    assert prefilter.check("Ordinary news headline", "https://example.com/2026/11/major-events-reshape/") is None


def test_odd_whitespace_is_normalized():
    assert prefilter.check("Why\u00a0your  Mac needs this", ARTICLE) == "title_question"
