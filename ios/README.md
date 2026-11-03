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

### Steps only you can do

1. **Join the Apple Developer Program** ($99/year) at developer.apple.com/programs. You can join as an individual, where your own name is shown as the seller, or as an organization, which needs a D-U-N-S number.
2. **Pick a bundle ID** you control, such as `com.yourname.techshorts`. In Xcode, open the target's **Signing & Capabilities**, choose your Team and set the bundle ID. The project currently uses the placeholder `com.example.techshorts`.
3. **Create the app record** in App Store Connect under **Apps**, then **+**, then **New App**. The name "TechShorts" may already be taken, so have a backup ready (for example "TechShorts: 60-Word Tech News").

### Upload a build

```bash
TEAM_ID=ABCDE12345 BUNDLE_ID=com.yourname.techshorts scripts/archive.sh
```

You can also use **Product › Archive** and then **Distribute App** in Xcode. The build appears in **TestFlight** about 10–30 minutes later. Internal testers (up to 100 people on your team) can install it immediately. External testers need a quick Beta App Review first.

For automatic builds, Xcode Cloud is included with the membership (25 compute hours a month). It can build and send every push on `main` to TestFlight once the code is in a GitHub, GitLab or Bitbucket repository.

### Store listing checklist

- **Screenshots**: required at 6.9″ (1320 × 2868). Take them from the *iPhone 17 Pro Max* simulator with ⌘S.
- **Description, keywords, category** (News, with Technology as secondary), and a **support URL**.
- **Privacy policy URL**: required even though the app collects nothing. A draft is in `PRIVACY.md`, and you can host it on GitHub Pages.
- **App Privacy**: answer **Data Not Collected**. This is accurate because there are no analytics, no accounts and no server. `PrivacyInfo.xcprivacy` already declares this.
- **Age rating**: answer the questionnaire honestly. The in-app reader can open any web page, which can count as unrestricted web access.
- **Export compliance**: already answered in `Info.plist` (`ITSAppUsesNonExemptEncryption = NO`).

### Review risks

- **Guideline 5.2 (intellectual property).** News aggregators sometimes get asked whether they have the right to show publishers' content. The app keeps to the safer pattern: headline, short excerpt or summary, clear source label, and a link to the original. If you're asked, explain this in the review notes. Drop any source that objects.
- **Guideline 4.2 (minimum functionality).** Apps that only wrap web content get rejected. The native features (summaries, digest, saving, source settings, offline cache) help here, and the review notes should mention them.
- **AI summaries** are labeled as such and only made when the feed contains enough of the article to summarize.

## Next steps

- **Home and Lock Screen widget** showing today's top story (WidgetKit).
- **iCloud sync** of saved stories across devices (CloudKit, which needs the paid account).
- **Siri and Shortcuts**: "What's in tech today?" (App Intents).
- **Unit tests** for the parser and filter, beyond the live `feedcheck.sh`.
