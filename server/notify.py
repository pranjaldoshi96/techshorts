#!/usr/bin/env python3
"""Sends a big-story alert after each publish.

The alert is an `Alert` record (title, body, url) saved to the app's public CloudKit database; iCloud pushes it to
every phone that turned on "Big story alerts". A story qualifies when it's in the Top tab, covered by 3+ outlets
(Reddit counts as one) and under 6 hours old, or when it's pinned in editorial.json with "notify": true.
At most 1 alert per run and 3 per 24 hours, never the same story twice. On the very first run, current big stories
are recorded as sent without alerting, so nobody gets a burst of old news.

Usage: notify.py           (after publish.sh writes the edition)
       notify.py --test    (sends a test alert to development devices only)
"""
import base64, datetime as dt, hashlib, json, os, sys
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

STATE = os.environ.get("TECHSHORTS_STATE", os.path.expanduser("~/.techshorts"))
CONTAINER = "iCloud.com.pranjald.techgist"
KEY_PEM = os.path.join(STATE, "cloudkit_key.pem")
KEY_ID_FILE = os.path.join(STATE, "cloudkit_key_id")
SENT_FILE = os.path.join(STATE, "alerts.json")
MAX_PER_DAY, MAX_PER_RUN, MIN_OUTLETS, MAX_AGE_HOURS = 3, 1, 3, 6


def save_alert(env, title, body, url):
    """Creates one Alert record with CloudKit Web Services, signed with the server-to-server key."""
    key_id = open(KEY_ID_FILE).read().strip()
    key = serialization.load_pem_private_key(open(KEY_PEM, "rb").read(), password=None)
    subpath = f"/database/1/{CONTAINER}/{env}/public/records/modify"
    body_json = json.dumps({"operations": [{"operationType": "create", "record": {"recordType": "Alert", "fields": {
        "title": {"value": title}, "body": {"value": body}, "url": {"value": url}}}}]})
    date = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha256(body_json.encode()).digest()).decode()
    signature = base64.b64encode(key.sign(f"{date}:{digest}:{subpath}".encode(), ec.ECDSA(hashes.SHA256()))).decode()
    r = requests.post("https://api.apple-cloudkit.com" + subpath, data=body_json, timeout=30, headers={
        "Content-Type": "application/json",
        "X-Apple-CloudKit-Request-KeyID": key_id,
        "X-Apple-CloudKit-Request-ISO8601Date": date,
        "X-Apple-CloudKit-Request-SignatureV1": signature})
    ok = r.status_code == 200 and not any("serverErrorCode" in rec for rec in r.json().get("records", []))
    print(f"  {env}: {'sent' if ok else 'failed'} ({r.status_code}) {'' if ok else r.text[:300]}")
    return ok


def send(title, body, url, envs=("production", "development")):
    # Production reaches App Store and TestFlight installs; development reaches builds installed from Xcode.
    return any([save_alert(env, title, body, url) for env in envs])


def main():
    if not (os.path.exists(KEY_PEM) and os.path.exists(KEY_ID_FILE)):
        print("Alerts: no CloudKit key configured yet, skipping")
        return
    if "--test" in sys.argv:
        print("Alerts: sending a test alert")
        send("Techgist test alert", "Big story alerts are working.", "https://pranjaldoshi96.github.io/apps/techgist/",
             envs=("development",))
        return

    now = dt.datetime.now(dt.timezone.utc)
    edition = json.load(open(os.path.join(STATE, "site", "edition.json")))
    editorial = json.load(open(os.path.join(STATE, "editorial.json"))) if os.path.exists(os.path.join(STATE, "editorial.json")) else {}
    first_run = not os.path.exists(SENT_FILE)
    sent = [] if first_run else json.load(open(SENT_FILE))
    sent_urls = {s["url"] for s in sent}
    recent = [s for s in sent if now - dt.datetime.fromisoformat(s["at"]) < dt.timedelta(hours=24)]

    def age_hours(a):
        return (now - dt.datetime.fromisoformat(a["publishedAt"].replace("Z", "+00:00"))).total_seconds() / 3600

    pinned = [p for p in editorial.get("pinned", []) if p.get("notify")]
    big = sorted((a for a in edition["articles"] if a.get("topRank") is not None
                  and 1 + len(a.get("alsoCoveredBy") or []) >= MIN_OUTLETS and age_hours(a) < MAX_AGE_HOURS),
                 key=lambda a: a["topRank"])
    candidates = [{"url": p["url"], "title": p["title"], "source": p.get("source", "Techgist"), "others": 0} for p in pinned] + \
                 [{"url": a["url"], "title": a["title"], "source": a["source"], "others": len(a.get("alsoCoveredBy") or [])} for a in big]
    candidates = [c for c in candidates if c["url"] not in sent_urls]

    if first_run:
        json.dump([{"url": c["url"], "at": now.isoformat()} for c in candidates], open(SENT_FILE, "w"))
        print(f"Alerts: first run, recorded {len(candidates)} current big stories without alerting")
        return

    budget = min(MAX_PER_RUN, MAX_PER_DAY - len(recent))
    if budget <= 0 or not candidates:
        print(f"Alerts: nothing sent ({len(candidates)} new big stories, {len(recent)} alerts in the last 24 h)")
        return
    for c in candidates[:budget]:
        title = c["source"] if c["others"] == 0 else f"{c['source']} + {c['others']} more"
        print(f"Alerts: sending \"{c['title'][:80]}\"")
        if send(title, c["title"], c["url"]):
            sent.append({"url": c["url"], "at": now.isoformat()})
    json.dump(sent[-500:], open(SENT_FILE, "w"))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # Alerts are best effort; never fail the publish.
        print(f"Alerts: error {e!r}")
