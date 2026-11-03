# TechShorts

Inshorts-style tech news for iPhone. One story per card, about 60 words each, across Top, AI, Startups, Dev and Blogs.

## How it works

The app loads a ranked **edition** that the TechShorts publisher (`../server/`) posts to GitHub Pages every 2 hours. If that edition is more than 8 hours old or can't be loaded, the app fetches the publishers' feeds and ranks them itself, using the same code, so it keeps working without the server.

| Piece | What it does | Apple tech |
|---|---|---|
| `Services/FeedService.swift`, `FeedParser.swift` | Fetches 22 RSS/Atom feeds in parallel, merges repeated links, drops deals, buying guides and sponsored posts | URLSession, XMLParser |
| `Services/Ranker.swift` | Ranks stories by importance: how many outlets cover the same story (matched by headline), Hacker News points, source weight, and age. Merges same-story duplicates into one card marked "+N more", and stops one source from filling the top of a tab | |
| `Services/PageMetadata.swift` | Fills in a missing image or excerpt from the article's `og:` tags | |
| `Services/Summarizer.swift` | Writes 50–60 word summaries on the device and labels them "Summarized on device" | Foundation Models (Apple Intelligence, iOS 26+) |
| `Services/DailyDigest.swift` | Sends a daily notification with the top headline, and refreshes stories in the background | UserNotifications, BackgroundTasks |
| `Services/NewsStore.swift` | App state, offline cache, saved stories, source toggles | SwiftUI, JSON on disk |
| `Views/` | Paging card feed, in-app Safari reader, share sheet, saved list, settings | SwiftUI, SafariServices |

The app runs on iOS 17 and later. On iPhones without Apple Intelligence it shows the publisher's excerpt instead of an AI summary.

## Run

Open `TechShorts.xcodeproj` in Xcode and press Run, or use the command line:

```bash
xcodebuild -project TechShorts.xcodeproj -scheme TechShorts -sdk iphonesimulator -derivedDataPath build build
```

## Check the feeds

Feeds change or break without warning. This script builds the app's parser as a Mac command-line tool and runs it against every live feed:

```bash
scripts/feedcheck.sh
```

To add a source, add one line to `Models/FeedSource.swift`, then run the check.

## Publishing to the App Store

Everything for the store is ready in this folder. Follow [`STORE_CHECKLIST.md`](STORE_CHECKLIST.md) for the remaining steps.

- **Signing:** team `A4HLS6BV3C`, bundle ID `com.pranjald.techshorts`, automatic signing.
- **Listing text, review notes, privacy and age-rating answers:** [`AppStore/LISTING.md`](AppStore/LISTING.md)
- **Screenshots:** [`AppStore/screenshots/`](AppStore/screenshots/), 6.9" (1320 × 2868)
- **Product, support and privacy pages:** <https://pranjaldoshi96.github.io/apps/techshorts/>, in the `pranjaldoshi96.github.io` repo with your other apps
- **Upload a build:** `scripts/archive.sh` archives, signs and uploads to App Store Connect. It then appears in TestFlight after 10–30 minutes.
- **Export compliance:** already answered in `Info.plist` (`ITSAppUsesNonExemptEncryption = NO`).

### Review risks

- **Guideline 5.2 (intellectual property).** News aggregators sometimes get asked whether they have the right to show publishers' content. The app keeps to the safer pattern: headline, short excerpt or summary, clear source label, and a link to the original. If you're asked, explain this in the review notes. Drop any source that objects.
- **Guideline 4.2 (minimum functionality).** Apps that only wrap web content get rejected. The native features (summaries, digest, saving, source settings, offline cache) help here, and the review notes should mention them.
- **AI summaries** are labeled as such and only made when the feed contains enough of the article to summarize.

## Next steps

- **Home and Lock Screen widget** showing today's top story (WidgetKit).
- **iCloud sync** of saved stories across devices (CloudKit, which needs the paid account).
- **Siri and Shortcuts**: "What's in tech today?" (App Intents).
- **Unit tests** for the parser and filter, beyond the live `feedcheck.sh`.
