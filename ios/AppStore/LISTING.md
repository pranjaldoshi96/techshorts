# App Store listing — Techgist 2.0

Paste these into App Store Connect → the app → version 2.0. Everything here describes 2.0 as built on
16 Nov 2026 (server push on, AI summaries written on Techgist's server). Text for 1.x is in git history.

**Name** (30 chars max): `Techgist: AI & Startup News`
"TechShorts: 60-Word Tech News" was refused as already in use on 3 Nov 2026, so the app is named Techgist.

**Subtitle** (30 chars max): `Tech news in 60 words a story`

**Category:** Primary *News*, Secondary *Developer Tools*

**Bundle ID:** `com.pranjald.techgist` · **SKU:** `techgist-ios`

**Promotional text** (170 chars, can change without review):
> Today's tech in 60 words a story. AI, startups, dev tools and the best long reads, ranked by what matters. No account, no ads, no tracking.

**Keywords** (100 chars, comma-separated, no spaces after commas):
```
tech news,ai news,startup,developer,programming,hacker news,engineering,summary,daily,silicon valley
```

**What's New in This Version:**
```
Techgist 2.0 is a new app on the same idea: one story per card, sixty words each.

• A new full-screen card and a feed that moves to the next story with any short swipe
• Stories ranked by Techgist's newsroom engine, with the day's biggest first in every tab
• Short AI summaries, written from the publisher's story and checked against it, labelled on every card
• Big story alerts with the story's picture, sent as the news breaks, a few a day at most
• Search across today's stories and everything you saved
• Full coverage: see every outlet reporting a story
• Read the full story in a Reader view without leaving the app
• Larger text sizes, VoiceOver and Reduce Motion are supported throughout
```

**Description:**
```
Techgist is the fastest way to keep up with tech. One story per card, sixty words each, from the outlets and blogs people who build things actually read.

THE STORIES THAT MATTER, FIRST
Stories covered by several outlets or popular on Hacker News rise to the top. The same story from different publishers is merged into one card, so you never read it twice. Deals, buying guides and sponsored posts are filtered out.

FIVE TABS
• Top — the day's biggest stories across tech
• AI — models, labs, research and the companies building on them
• Startups — funding, launches and founders
• Dev — tools, languages, platforms and open source
• Reads — longer essays and analysis worth your time

Stories come from more than 50 publishers and blogs, including TechCrunch, The Verge, Ars Technica, Wired, BBC, The Guardian, MIT Technology Review, GitHub, Cloudflare and the Hacker News front page.

SIXTY-WORD SUMMARIES
Each card carries a short summary written by Techgist's AI from the publisher's own story and checked against it. Cards with an AI summary are labelled. Every card names its publisher and links to the original article.

MADE FOR A QUICK READ
• Swipe up for the next story; tap a card or swipe left to read it in full
• The full story opens inside the app, in Reader view when the page supports it
• Search today's stories and your saved ones
• See every outlet covering a story
• Save stories for later
• Share a story as a clean image card, ready for any chat
• Mute any source you don't want
• A morning gist notification at the time you choose
• Optional big story alerts, a few a day at most, never marketing
• Works offline with the latest stories

PRIVATE BY DESIGN
No account. No ads. No tracking. What you read and save stays on your iPhone. If you turn on big story alerts, Techgist's server keeps your phone's notification address so it can send them, and removes it when you turn them off.

Every story links to the original article and credits its publisher.
```

**Marketing URL:** https://pranjaldoshi96.github.io/apps/techgist/
**Support URL:** https://pranjaldoshi96.github.io/apps/techgist/support/
**Privacy Policy URL:** https://pranjaldoshi96.github.io/apps/techgist/privacy/
The privacy page must carry the 2.0 text (source in `../PRIVACY.md`) before 2.0 is submitted.

**App Privacy (nutrition label)** — set in the App Store Connect web UI (the API cannot set it):
choose **"Yes, we collect data from this app"**, then:
- Data type: **Identifiers → Device ID** (the push notification token).
- Purpose: **App Functionality** only.
- Linked to the user's identity: **No**.
- Used for tracking: **No**.

Nothing else is collected. Why this answer: with Big story alerts on, the app sends the APNs device token,
the time zone, the language and region, and the app version to Techgist's server so alerts can be sent at a
sensible local time. There is no account, no analytics and no third-party SDK. `Techgist/PrivacyInfo.xcprivacy`
declares the same thing.

**Age rating:** this is a live news feed, and tech news sometimes covers crime, abuse, violence, drugs and
health. Answer the questionnaire for that content, not for the app's own text:
- *Mature or suggestive themes*: **Infrequent/Mild**
- *Realistic violence* (news reports, no imagery made by the app): **Infrequent/Mild**
- *Medical or treatment information*: **Infrequent/Mild**
- *Profanity or crude humor*, *Alcohol, tobacco or drug use or references*: **Infrequent/Mild** if asked about
  news text; everything else **None**
- **Unrestricted Web Access: No.** The app only opens the publisher's article for a story you tap, in Safari's
  in-app view, with no address bar or search.
- Don't tick "Made for Kids".
Expect 12+ (or the nearest tier App Store Connect computes). 1.x was rated 4+ with every answer "None".

**Availability:** all countries except **China mainland**, which requires a government licence for news apps.

**App Review notes:**
```
Techgist is a news reader. No login, no in-app purchases, no ads, no third-party SDKs.

What changed in 2.0: a new card and feed, server-ranked stories, AI summaries written on our server (the on-device Apple Intelligence summaries of 1.x are gone), search, a full-coverage sheet, and big story alerts sent by push from our own server (1.x alerts were local).

Content: headlines and sixty-word summaries of stories from more than 50 publishers' public RSS/Atom feeds. Each card names its publisher and links to the original article, which opens in SFSafariViewController. Our server builds a ranked edition every few minutes and publishes it at https://raw.githubusercontent.com/pranjaldoshi96/techshorts/edition/edition.json; if that is unavailable or more than 8 hours old the app reads the publishers' feeds directly. Summaries are written by a language model on our server from the publisher's story and automatically checked against it; cards with one are labelled "AI summary". Pictures are the publishers' story images, resized and served from the same GitHub branch, or a cover drawn by the app when a story has none.

Notifications: the app asks for permission only after its own explanation screen (shown after eight stories or the first save) or from the switches in Settings. "Morning gist" is a local notification. "Big story alerts" are remote notifications sent by our server (https://techgist.213-210-36-218.sslip.io) to devices that turned them on: news only, at most 2 a day (3 on an exceptional day), none between 22:00 and 07:30 local time. No marketing or promotional pushes are sent.

Data: with Big story alerts on, the app sends the APNs device token, time zone, language/region and app version to our server, used only to deliver alerts and deleted when alerts are turned off. Nothing else leaves the device. This is declared in App Privacy (Identifiers > Device ID, App Functionality, not linked, not used for tracking), in the privacy manifest and in the privacy policy, which is also linked in the app under Settings > About.

To try it: swipe up through stories; tap a chip (Top, AI, Startups, Dev, Reads) to change tabs; tap a card or swipe left to read the full story; tap the magnifying glass to search; tap the bookmark to save and the share icon to share a story as an image; the "..." button on a card shows full coverage, copy link and mute source. Settings (gear icon) has the notification switches, Reader view, muted sources, and the Privacy Policy and Support links. "Preview an alert" in Settings shows what an alert looks like 5 seconds later.
```

**Screenshots:** `AppStore/screenshots/` — 6.9" (1320×2868), taken from the 2.0 build on 16 Nov 2026. Upload in this order:
1. `01-top.png` Top tab: a story card with its AI summary and coverage row
2. `02-ai.png` AI tab
3. `03-coverage.png` Full coverage: every outlet reporting a story
4. `04-search.png` Search
5. `05-settings.png` Settings: alerts, Reader view, sources, privacy links

These show real stories, so publishers' photos appear on the cards. If App Review objects to third-party
images, retake them on cards that use Techgist's own drawn cover.

**Before submitting:** read the 1.1 rejection in App Store Connect → App Review (Resolution Center). The API
does not expose the reason. 1.1's only change was the share card's "Download free on the App Store" footer and
link; 2.0's share card now says "Read it on Techgist" and the share text only carries the link.
