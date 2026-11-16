# Techgist for iPhone

Inshorts-style tech news. One story per card, about 60 words each, across Top, AI, Startups, Dev and Reads. SwiftUI, iOS 17 and later, no third-party code.

## How it works

The app loads a ranked **edition** that the Techgist server (`../server/`) publishes to this repo's `edition` branch. If that edition is more than 8 hours old or can't be loaded, the app fetches the publishers' feeds and ranks them itself, so it keeps working without the server.

| Piece | What it does |
|---|---|
| `Models/Article.swift`, `Edition.swift` | The edition's shape. The v1 fields decode exactly as in 1.2; the 2.0 fields are optional and lenient (see below). |
| `Services/FeedOrdering.swift` | Which stories a tab shows and in what order: the server's rank, muted sources removed, read stories below unread ones. |
| `Services/NewsStore.swift` | App state. Shows the cached edition at once, fetches a newer one, and applies it only when nobody is mid-read (launch, the new-stories pill, pull to refresh). |
| `Services/ReadState.swift` | Which stories were read (on screen 1.5 s, or opened), kept 7 days on the device. |
| `Services/ImageLoader.swift` | Downloads, downsamples and decodes pictures off the main thread; memory cache plus a 7-day disk cache; prefetches the next three cards. |
| `Services/FeedService.swift`, `FeedParser.swift`, `Ranker.swift`, `HTMLText.swift`, `PageMetadata.swift` | The direct-feed fallback. The server's safe-mode publisher compiles these same files (plus the two models and `Disk.swift`) on Linux, so they must stay Foundation-only and keep their paths. |
| `Services/StoryNotification.swift`, `BreakingNews.swift`, `DailyDigest.swift` | Local alerts: the morning gist and big-story alerts, with the story picture, Techgist's chime, and Read and Save buttons. |
| `Services/PushRegistration.swift` | Server push to Techgist's registry at `PushConfig.registerURL` (see "Server push" below). |
| `TechgistNotificationService/` | Notification Service Extension: attaches the picture named in a push's `image` key and gives the push the Read and Save buttons. |
| `Views/` | `CardView` (the card and its layouts), `FeedView` (paging), `ChipBar`, `ContentView` (shell), `SupportViews` (reader, saved, search, coverage, settings), `Theme` (design tokens). |
| `TechgistTests/` | Decoding against snapshots of the live v1 edition and the engine's edition, ordering, alert content, the push wire format, the paging rule. |
| `TechgistUITests/` | Drives the app in the simulator: pages through every tab with short drags, flicks and held presses; the one-time server-alerts notice; accessibility audits of each screen; a hitch measurement. |

### Edition fields the app reads

Version 1 (required unless marked): `url`, `title`, `excerpt`, `content`, `source`, `category` (`Top`, `AI`, `Startups`, `Dev`, `Blogs`), `publishedAt`; optional `image`, `points`, `alsoCoveredBy`, `topRank`, `headline`, `summary`.

Added for 2.0, all optional. A missing or mistyped value is ignored, never fatal:

| Field | Type | Use |
|---|---|---|
| `rank` | int | 0-based position inside the story's own tab. Without it, tabs other than Top are newest first. |
| `summaryShort` | string | Shown when the full summary doesn't fit. Without it the app cuts the summary to two sentences. |
| `notification` | `{title, body}` | The alert's title (40 characters at most) and body (110). Without it the alert uses the headline and `alertLine`. |
| `alertLine` (or `notifLine`) | string | Older name for the alert body. Without either, the summary's first sentence is used. |
| `spice` | int, 1 to 10 (a 0 to 1 fraction is read as tenths) | 8 and above shows a HOT badge and is preferred for alerts. |
| `images` | `{hero, std, thumb, generated}` | `hero` on phones 390 pt and wider, `std` otherwise and for alerts, `thumb` in lists. Falls back to `image`. |
| `badge` | `breaking` or `developing` | Badge on the card; `breaking` stories can alert with fewer than 3 outlets. |
| `type` | string | `analysis`, `opinion`, `interview`, `release_notes` and `tutorial` get a label. |
| `updatedAt` | ISO 8601 date | "Updated 2h" when more than an hour after `publishedAt`. |
| `aiText` | bool | `false` shows "Summary pending" instead of the AI label. |
| `eventId` | string | The server's id for the event. A push names its story by it. |
| `degraded` (on the edition) | bool | Shows the "Summaries catching up" banner. |

An article that fails to decode (an unknown `category`, say) is dropped on its own; 1.2 rejects the whole edition in that case.

## Run and test

Open `Techgist.xcodeproj` in Xcode and press Run, or use the command line:

```bash
xcodebuild -project Techgist.xcodeproj -scheme Techgist -destination 'platform=iOS Simulator,name=iPhone 17 Pro' -derivedDataPath build test
```

That runs the unit tests and the UI tests (about 15 minutes). Add `-only-testing:TechgistTests` for the unit tests alone (seconds), or `-only-testing:TechgistUITests/FeedScrollingUITests` for the paging tests.

The accessibility audits read the text size from the environment, so they can be repeated at any size, and they follow the simulator's contrast and appearance settings:

```bash
TEST_RUNNER_CONTENT_SIZE=UICTContentSizeCategoryAccessibilityXXXL xcodebuild … test -only-testing:TechgistUITests/AccessibilityUITests
xcrun simctl ui booted increase_contrast enabled
```

In a Debug build, `SIMCTL_CHILD_EDITION_URL=file:///path/to/edition.json xcrun simctl launch booted com.pranjald.techgist` points the app at another edition, and `SIMCTL_CHILD_UITEST_RESET=1` starts it as on a first launch. `SIMCTL_CHILD_EDITION_DELAY=1.5` makes that edition answer like a slow network (and fail when the request is cancelled).

In the Simulator, move through the feed by clicking and dragging upward, as a finger would. A short drag is enough: the feed moves to the next story once it has travelled 36 points. A two-finger trackpad scroll is not a touch; it has not been tested here (this Xcode install has no Simulator.app to send scroll-wheel events to), so if it does nothing, drag instead. Quick flicks one after another each move one card, including a flick made while the last one is still moving.

## Server push

`PushConfig.registerURL` (one constant in `Services/PushRegistration.swift`) is the registry's HTTPS base URL, `https://techgist.213-210-36-218.sslip.io` (the VPS, see `server/push/README.md` on branch `push-r3`). Set it to nil and the app never asks iOS for a token and keeps its own local alerts. Test runs (unit and UI tests) never register. `locale` is sent as language and region only (`en_US`, not iOS's `en_US@rg=inzzzz`). Checked end to end on a simulator on 2026-11-16: registration, a story alert from the engine's queue, the picture attached by the service extension, a tap opening the story (app running and not running), opt-out. The simulator does not draw the picture's thumbnail (its thumbnailer fails), so that still needs a look on a phone.

- **Register:** `POST <registerURL>/v1/devices` with `{"token", "env": "sandbox"|"production", "tz", "locale", "appVersion"}`. Sent after the reader allows notifications with Big story alerts on, on every launch after that, and when iOS issues a new token (the old one is deleted first).
- **Opt out:** `DELETE <registerURL>/v1/devices/<token>` when Big story alerts is turned off in Settings or the permission is taken away. If the server can't be reached it is tried again on the next launch.
- **Payload:** `{"aps": {"alert": {"title", "body"}, "sound": "techgist.caf", "mutable-content": 1, "thread-id": eventId}, "eventId", "url", "image"}`. The extension attaches `image`; tapping the alert shows the story's card, found by `url` or `eventId`, loading the newer edition first when the story isn't in the one on screen.
- **People who turned alerts on in 1.x** (where alerts were local and the policy said nothing went to a server) see a one-time notice on the first launch of 2.0: "Keep alerts on" registers, "Turn off alerts" turns the switch off. Nothing is sent before the answer; until then the app's own local alerts carry on. Turning alerts on in 2.0 counts as an answer (the offer and Settings both say alerts come from the server).
- While the server is pushing to a device, the app's own background big-story alerts are off for it, so nobody gets the same story twice. The morning gist stays local.
- The permission prompt only ever follows the in-app offer (after eight stories or the first save) or a switch in Settings; never at launch.
- `env` is `sandbox` in Debug builds and `production` in Release builds.

`scripts/make_chime.sh` regenerates the notification sound (`Techgist/techgist.caf`). It is synthesised from sine waves in the script; nothing is sampled.

## Check the feeds

Feeds change or break without warning. This script builds the app's parser as a Mac command-line tool and runs it against every live feed:

```bash
scripts/feedcheck.sh
```

To add a source, add one line to `Models/FeedSource.swift`, then run the check.

## Publishing to the App Store

Follow [`STORE_CHECKLIST.md`](STORE_CHECKLIST.md).

- **Signing:** team `A4HLS6BV3C`, bundle ID `com.pranjald.techgist`, automatic signing. The extension is `com.pranjald.techgist.NotificationService`; both come from the `APP_BUNDLE_ID` build setting.
- **Listing text, review notes, privacy and age-rating answers:** [`AppStore/LISTING.md`](AppStore/LISTING.md)
- **Screenshots:** [`AppStore/screenshots/2.0/`](AppStore/screenshots/2.0/), 6.9" (1320 × 2868), from the 2.0 build
- **Product, support and privacy pages:** <https://pranjaldoshi96.github.io/apps/techgist/>. The privacy page's 2.0 text is in [`PRIVACY.md`](PRIVACY.md).
- **Upload a build:** `scripts/archive.sh` archives, exports a signed IPA and validates it; `scripts/archive.sh --upload` also uploads it to App Store Connect (see the checklist for the credentials each step uses).
- **Export compliance:** already answered in `Info.plist` (`ITSAppUsesNonExemptEncryption = NO`).

### Review risks

- **Guideline 5.2 (intellectual property).** The app keeps to the safer pattern: headline, short summary, clear source label, and a link to the original. Drop any source that objects.
- **Guideline 4.2 (minimum functionality).** The native features (ranked edition, saving, search, alerts, offline cache) help here, and the review notes should mention them.
- **AI summaries** are labelled on every card that has one.
