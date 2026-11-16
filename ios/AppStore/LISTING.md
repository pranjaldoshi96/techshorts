# App Store listing — Techgist 2.0

This is the text in App Store Connect for version 2.0 (set through the API on 16 Nov 2026 from this file).
Everything here describes 2.0 as built: server push on, AI summaries written on Techgist's server. Text for
1.x is in git history.

**Name** (30 chars max): `Techgist: AI & Startup News`
"TechShorts: 60-Word Tech News" was refused as already in use on 3 Nov 2026, so the app is named Techgist.

**Subtitle** (30 chars max): `Tech news in 60 words a story`

**Category:** Primary *News*, Secondary *Developer Tools*

**Bundle ID:** `com.pranjald.techgist` · **SKU:** `techgist-ios`

**Promotional text** (170 chars, can change without review):
```
Fast, sharp tech news from Bengaluru to Silicon Valley. AI, startups, funding and dev tools in 60 words a story, ranked by what matters. No account, no ads, no tracking.
```

**Keywords** (100 chars, comma-separated, no spaces after commas):
```
tech news,ai,startups,india,bengaluru,silicon valley,funding,developer,hacker news,short news,daily
```

**What's New in This Version:**
```
Techgist 2.0 is a new app on the same idea: one story per card, sixty words each.

• A new full-screen card, and a feed that moves to the next story with any short swipe
• Stories ranked all day by Techgist's newsroom engine, with the biggest first in every tab
• Wider coverage: Indian tech and startups alongside Silicon Valley, from more than 50 publishers
• Sharper headlines and short AI summaries, written from the publisher's story, checked against it and labelled
• Big story alerts as the news breaks, with the story's picture: a few a day at most, none at night
• Full coverage: see every outlet reporting a story
• Search today's stories and everything you saved
• A new Reads tab for longer essays and analysis
• Read the full story in Reader view without leaving the app
• Larger text sizes, VoiceOver and Reduce Motion are supported throughout

Big story alerts are now sent by Techgist's server, which keeps your phone's notification address to do it. If you already had alerts on, the app asks you once before anything is sent, and you can turn them off there.
```

**Description:**
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
• The full story opens inside the app, in Reader view when the page supports it
• See every outlet covering a story
• Search today's stories and your saved ones
• Save stories for later
• Share a story as a clean image card, ready for any chat
• Mute any source you don't want
• Works offline with the latest stories

ALERTS YOU CHOOSE
• Big story alerts as the news breaks, with the story's picture: a few a day at most, none at night, never marketing
• A morning gist with the day's top story, at the time you pick
Both are off until you turn them on.

PRIVATE BY DESIGN
No account. No ads. No tracking. What you read and save stays on your iPhone. If you turn on big story alerts, Techgist's server keeps your phone's notification address so it can send them, and removes it when you turn them off.

Every story links to the original article and credits its publisher.
```

**Marketing URL:** https://pranjaldoshi96.github.io/apps/techgist/
**Support URL:** https://pranjaldoshi96.github.io/apps/techgist/support/
**Privacy Policy URL:** https://pranjaldoshi96.github.io/apps/techgist/privacy/
The three pages were updated for 2.0 on 16 Nov 2026 (privacy text: `../PRIVACY.md`). They describe 2.0 and say
what was different in 1.x, because 1.2 stays on sale until 2.0 is released.

**App Privacy (nutrition label)** — must be changed by hand in the App Store Connect web UI before 2.0 is
submitted (the API can neither read nor set it; 1.x was published as "Data Not Collected"):
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

What changed in 2.0: a new card and feed, server-ranked stories, AI summaries written on our server (the on-device Apple Intelligence summaries of 1.x are gone), search, a full-coverage sheet, and big story alerts sent by push from our own server (1.x alerts were local). People who turned alerts on in 1.x see a one-time notice on first launch, and nothing is registered until they choose "Keep alerts on".

Content: headlines and sixty-word summaries of stories from more than 50 publishers' public RSS/Atom feeds. Each card names its publisher and links to the original article, which opens in SFSafariViewController. Our server builds a ranked edition every few minutes and publishes it at https://raw.githubusercontent.com/pranjaldoshi96/techshorts/edition/edition.json; if that is unavailable or more than 8 hours old the app reads the publishers' feeds directly. Summaries are written by a language model on our server from the publisher's story and automatically checked against it; cards with one are labelled "AI summary". Pictures are the publishers' story images, resized and served from the same GitHub branch, or a cover drawn by the app when a story has none.

Notifications: the app asks for permission only after its own explanation screen (shown after eight stories or the first save) or from the switches in Settings. "Morning gist" is a local notification. "Big story alerts" are remote notifications sent by our server (https://techgist.213-210-36-218.sslip.io) to devices that turned them on: news only, at most 2 a day (3 on an exceptional day), none between 22:00 and 07:30 local time. No marketing or promotional pushes are sent.

Data: with Big story alerts on, the app sends the APNs device token, time zone, language/region and app version to our server, used only to deliver alerts and deleted when alerts are turned off. Nothing else leaves the device. This is declared in App Privacy (Identifiers > Device ID, App Functionality, not linked, not used for tracking), in the privacy manifest and in the privacy policy, which is also linked in the app under Settings > About.

To try it: swipe up through stories; swipe sideways or tap a chip (Top, AI, Startups, Dev, Reads) to change tabs; tap a card to read the full story; tap the magnifying glass to search; tap the bookmark to save and the share icon to share a story as an image; the "..." button on a card shows full coverage, copy link and mute source. Settings (gear icon) has the notification switches, Reader view, muted sources, and the Privacy Policy and Support links. "Preview an alert" in Settings shows what an alert looks like 5 seconds later.
```

**Screenshots:** `AppStore/screenshots/2.0/` — 6.9" (1320×2868, the size App Store Connect asks for in the
iPhone 6.9" class, `APP_IPHONE_67`), taken from the 2.0 build on 16 Nov 2026 with that morning's edition. In
this order:
1. `01-top.png` Top tab: a story card with its AI summary and coverage row
2. `02-hot.png` A card with the HOT badge
3. `03-coverage.png` Full coverage: every outlet reporting a story
4. `04-dark.png` The feed in Dark Mode
5. `05-alert.png` A big story alert arriving
6. `06-reads.png` Reads tab: longer essays and analysis

These show real stories, so publishers' photos appear on the cards (as they did in the approved 1.2
screenshots). If App Review objects to third-party images (5.2), retake them on cards that use Techgist's own
drawn cover, and set `images: generated` for any source that objects.

**The 1.1 rejection is still unread.** App Store Connect → the app → App Review (Resolution Center) has the
reason; the API does not expose it. What is known: the version record created on 3 Nov 2026 19:13Z was submitted
as 1.1 (build 20261104.39) two minutes later and rejected; the same record was renamed 1.2, resubmitted on
5 Nov with build 20261105.2312 and approved. 1.2 shipped the share footer "Download free on the App Store" and
the App Store link in the share text, so the footer was **not** the reason (an earlier note here said it might
be). 2.0's share card says "Read it on Techgist", which is a wording choice, not a fix for anything. If the
reason was third-party content (5.2): every card credits and links its publisher, and the screenshots can be
retaken on drawn covers.
