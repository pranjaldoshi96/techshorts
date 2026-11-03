# App Store listing — Techgist

Paste these into App Store Connect → your app → App Information / Version page.

**Name** (30 chars max): `Techgist: AI & Startup News`
"TechShorts: 60-Word Tech News" was refused as already in use on 3 Nov 2026, so the app is named Techgist.

**Subtitle** (30 chars max): `Tech news in 60 words a story`

**Category:** Primary *News*, Secondary *Developer Tools*

**Bundle ID:** `com.pranjald.techgist` · **SKU:** `techgist-ios`

**Promotional text** (170 chars, can change without review):
> Today's tech in 60 words a story. AI, startups, dev tools and the best engineering blogs, ranked by what matters. No account, no ads, no tracking.

**Keywords** (100 chars, comma-separated, no spaces after commas):
```
tech news,ai news,startup,developer,programming,hacker news,engineering,summary,daily,silicon valley
```

**Description:**
```
Techgist is the fastest way to keep up with tech. One story per card, sixty words each, from the outlets and blogs people who build things actually read.

THE STORIES THAT MATTER, FIRST
Stories covered by several outlets or popular on Hacker News rise to the top. The same story from different publishers is merged into one card, so you never read it twice. Deals, buying guides and sponsored posts are filtered out.

FIVE TABS
• Top — TechCrunch, The Verge, Ars Technica, Wired and the Hacker News front page
• AI — MIT Technology Review, The Decoder, OpenAI, Google AI, Latent Space
• Startups — TechCrunch Startups, Product Hunt, Lenny's Newsletter
• Dev — GitHub, Cloudflare, Changelog, DEV Community
• Blogs — Simon Willison, The Pragmatic Engineer, Stratechery, Martin Fowler, Julia Evans

SUMMARIES WRITTEN ON YOUR IPHONE
On iPhones with Apple Intelligence, Techgist writes a short summary of each story on your device. Nothing is sent to a server. On other iPhones you get the publisher's own excerpt.

MADE FOR A QUICK READ
• Swipe up for the next story
• Tap through to read the full article in Safari
• Save stories for later and share them
• Turn off any source you don't want
• One daily digest notification at the time you choose
• Works offline with the latest stories

PRIVATE BY DESIGN
No account. No ads. No tracking. What you read and save stays on your iPhone.

Every story links to the original article and credits its publisher.
```

**Marketing URL:** https://pranjaldoshi96.github.io/apps/techgist/
**Support URL:** https://pranjaldoshi96.github.io/apps/techgist/support/
**Privacy Policy URL:** https://pranjaldoshi96.github.io/apps/techgist/privacy/

**App Privacy (nutrition label):** choose **"No, we do not collect data from this app."** → result: *Data Not Collected*.
This is accurate: there are no analytics, accounts or third-party SDKs, and the only network requests are for public news feeds and article pages.

**Age rating:** answer "None" to every content item. For **Unrestricted Web Access**, answer **No**: the app only opens the publisher's article for a story you tap, in Safari's in-app reader, with no address bar or search. Don't tick "Made for Kids".

**Availability:** all countries except **China mainland**, which requires a government licence for news apps.

**App Review notes:**
```
Techgist is a news reader. No login, no in-app purchases, no third-party SDKs.

Content: headlines and short excerpts from 22 publishers' public RSS/Atom feeds, each credited and linked to the original article, which opens in SFSafariViewController. A ranked list of today's stories is published every 2 hours to https://raw.githubusercontent.com/pranjaldoshi96/techshorts/edition/edition.json; if it's unavailable the app reads the feeds directly.

On-device summaries use Apple's Foundation Models framework and appear only on Apple Intelligence devices (iOS 26+), labelled "Summarized on device". Other devices show the publisher's excerpt.

To try it: swipe up through stories, switch tabs (Top, AI, Startups, Dev, Blogs), tap the bookmark icon to save, and open Settings (gear icon) to turn sources off or enable the daily digest notification.
```

**Screenshots:** `AppStore/screenshots/` — 6.9" (1320×2868), upload in this order:
1. `01-startups.png` Story card with photo
2. `02-ai-summary.png` On-device AI summary, "+1 more" coverage
3. `03-blogs.png` Blogs tab
4. `04-settings.png` Settings: digest, summaries, sources

These show real stories, so a publisher's photo appears in screenshot 1. If App Review objects to third-party images, retake it on a card that uses Techgist's own cover.
