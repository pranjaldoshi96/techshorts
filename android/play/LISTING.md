# Google Play listing — Techgist 2.0 (Android)

The text follows the App Store listing for 2.0 (`ios/AppStore/LISTING.md` on branch `app-2.0`), changed only
where Android differs: no Reader view, "phone" for "iPhone", and no number of alerts a day (the server has had no
daily cap since 17 Nov 2026).

**App name** (30 chars max): `Techgist: AI & Startup News`

**Short description** (80 chars max):
```
Fast, sharp tech news in 60 words a story. No account, no ads, no tracking.
```

**Full description** (4000 chars max):
```
Techgist is fast, sharp tech news for people who build things, from Bengaluru to Silicon Valley. One story per card, sixty words each, ranked so the biggest story is always first.

THE STORIES THAT MATTER, FIRST
Techgist reads the outlets and blogs the industry reads, all day, and ranks what it finds. A story covered by several outlets, or climbing Hacker News, rises to the top. The same story from different publishers becomes one card, so you never read it twice. Deals, buying guides and sponsored posts are left out.

FIVE TABS
• Top — the day's biggest stories across tech
• AI — models, labs, chips and the companies building on them
• Startups — funding rounds, launches and founders, in India and around the world
• Dev — tools, languages, security and open source
• Reads — longer essays and analysis worth your time

Stories come from more than 50 publishers and blogs, including TechCrunch, The Verge, Bloomberg, Financial Times, ET Tech, Mint, Inc42, YourStory, The Guardian, InfoQ, GitHub, Cloudflare and the Hacker News front page.

SIXTY WORDS, THEN MOVE ON
Each card has a clear headline and a short summary written by Techgist's AI from the publisher's own story and checked against it. Cards with an AI summary are labelled. Every card names its publisher and links to the original article.

MADE FOR A QUICK READ
• Swipe up for the next story, sideways to change tab; tap a card to read it in full
• The full story opens inside the app
• See every outlet covering a story
• Search today's stories and your saved ones
• Save stories for later
• Share a story as a clean image card, ready for any chat
• Mute any source you don't want
• Works offline with the latest stories

ALERTS YOU CHOOSE
• Big story alerts as the news breaks, with the story's picture and a choice of sounds: none at night, never marketing
• A morning gist with the day's top story, at the time you pick
Both are off until you allow notifications.

PRIVATE BY DESIGN
No account. No ads. No tracking. What you read and save stays on your phone. If you turn on big story alerts, Techgist's server keeps your phone's notification address so it can send them, and removes it when you turn them off.

Every story links to the original article and credits its publisher.
```

**Category:** News & Magazines · **Tags:** News
**Contact email:** the owner's support address (shown publicly on the listing)
**Website:** https://pranjaldoshi96.github.io/apps/techgist/
**Privacy policy:** https://pranjaldoshi96.github.io/apps/techgist/privacy/
Before the app goes public the privacy page needs one added sentence: on Android, alerts are delivered through
Google's Firebase Cloud Messaging.

**Graphics** (in this folder):
- `icon-512.png` — app icon, 512×512
- `feature-graphic.png` — 1024×500
- `screenshots/01-top.png` … `05-settings.png` — phone, 1080×1920, taken from the signed release build in the
  emulator on 18 Nov 2026. They show real stories, so publishers' photos appear; retake on cards with
  Techgist's own cover if Google objects.

**Release file:** `android/app/build/outputs/bundle/release/app-release.aab`, made by
`android/build.sh bundleRelease` (version 2.0, code 20), signed with the upload key in `~/.techgist/android/`.
When Play asks, choose **Play App Signing** (the default).

## Forms in the Play Console (App content)

- **Privacy policy:** the link above.
- **Ads:** No, the app contains no ads.
- **App access:** All functionality is available without special access (no login).
- **Content rating** (IARC questionnaire): category *News*. Violence, sexuality, language, controlled
  substances: answer as for news reporting, i.e. the app shows news text and publishers' photos that may
  mention these; the app has no user-generated content, no chat, no purchases, does not share location.
- **Target audience:** 18 and over only (keeps the app out of the Families rules). Not designed for children.
- **News app:** Yes, it is a news app. Publisher: Techgist; contact details: the support page. It is an
  aggregator: every story names its original publisher and links to it.
- **Data safety:**
  - Does the app collect or share user data? **Yes** (collect), **No** (share).
  - Data type: **Device or other IDs** (the Firebase Cloud Messaging token). Collected, not shared; processed
    only to send alerts; **optional** (only with notifications allowed and Big story alerts on); purpose **App
    functionality**. Sent with it: time zone, language and region, app version, chosen alert sound.
  - Data is encrypted in transit: **Yes**. Users can ask for deletion: turning Big story alerts off removes
    the phone from the server.
- **Government app / Financial features / Health:** No.
- **Advertising ID:** the app does not use it.

**Countries:** the same as iOS, i.e. everywhere except China mainland.
