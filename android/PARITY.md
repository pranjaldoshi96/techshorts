# Parity with the iOS app 2.0

The inventory was made from the iOS sources on branch `app-2.0` (`ios/Techgist`), file by file. Each line says
where the Android equivalent is and its state:

- **Done**: built, and seen working in the emulator or covered by a unit test (which one is said).
- **Adapted**: done the Android way because the iOS mechanism does not exist on Android; the difference is named.
- **Not done**: with the reason.

## Stories and loading (`FeedService`, `Edition`, `Article`, `NewsStore`)

| iOS 2.0 | Android | State |
|---|---|---|
| Reads `edition.json` from the `edition` branch; 10 s timeout; revalidated with the server's ETag | `FeedService.fetchEdition`, `OkHttpGet` (always revalidates, so an unchanged edition costs a few hundred bytes) | Done (emulator: live news) |
| v1 fields required; 2.0 fields optional and lenient (`summaryShort`, `alertLine`/`notifLine`, `spice` 1-10 or 0-1, `rank`, `images`, `badge`, `type`, `updatedAt`, `aiText`, `eventId`, `notification`, `coverageLinks`) | `Article.fromJson` | Done (tests, against a real engine edition) |
| One article that does not decode is dropped, the rest kept | `Edition.decode` | Done (test) |
| Edition used while under 8 hours old; otherwise the 27 publisher feeds are fetched on the phone, filtered (deals, sponsored), de-duplicated, clustered and ranked | `FeedService.fetchAll`, `DirectFeeds.kt` (`FeedParser`, `HtmlText`, `ContentFilter`, `Ranker`) | Done (tests for parsing, filtering, merging, ranking; the live fallback itself was not exercised because the edition was fresh) |
| First launch with no edition: stories appear as each feed answers | `NewsStore.showPartial` | Done (code; not exercised live) |
| Headline and summary shown exactly as the edition gives them (`headline` else `title`; `summary` else `excerpt`) | `Article.displayTitle`, `displaySummary` | Done (test) |
| Last stories cached (`articles.json`, cache directory) and on screen at the next launch, online or not | `Disk`, `ArticleCache` | Done (test; emulator: relaunch in airplane mode) |
| Refresh on launch and on coming to the front when stories are over 10 minutes old; after a long break the feed goes back to the top | `NewsStore.becameActive` | Done (code port; test for the parts) |
| Check for a newer edition every 5 minutes while open; never re-order under the reader | `ContentScreen`, `NewsStore.checkForNewStories` | Done (test) |
| "N new stories" pill, hides after 8 seconds, tap brings them in | `NewStoriesPill` | Done (test for the count; the pill needs a new edition to arrive while reading, seen once in the emulator if noted below) |
| A refresh once asked for runs to its end; two at once fetch once; a pull holds a fast result 0.6 s | `NewsStore.refresh` | Done (tests) |
| Offline: banner "Offline · stories from …", cached stories stay | `banner()` | Done (test; emulator) |
| Banners: "Summaries catching up · showing headlines" (degraded or direct), "Updated … ago" (edition over 2 h old) | `banner()` | Done (test) |
| Tab order: Top by `topRank`, other tabs by `rank` then newest; muted sources removed; read stories sink when a new edition is applied | `FeedOrdering` | Done (tests) |
| Read state: on screen 1.5 s or opened; kept 7 days (`read.json`) | `ReadState`, `FeedView` | Done (test; emulator: unread dots clear) |

## The feed (`ContentView`, `FeedView`, `CardView`, `ChipBar`)

| iOS 2.0 | Android | State |
|---|---|---|
| Top bar: logo + "Techgist" in bold serif (tap: back to first story); Search, Saved, Settings | `TopBar` | Done (emulator) |
| Tabs Top, AI, Startups, Dev, Reads as chips; selected chip filled with the accent; dot on tabs with unread stories; tap the current tab to go to the top | `ChipBar` | Done (emulator). The selected capsule changes colour in place instead of sliding between chips |
| Thin progress line under the tabs | `ProgressLine`, `progress()` | Done (test; emulator) |
| One card per screen; the page follows the finger and snaps in about 0.24 s; never more than one card per swipe; 36 pt commits, a flick counts; rubber band at both ends | `FeedPager`, `FeedPaging` | Done (tests for the rules; emulator) |
| Pull down on the first card (90 pt) to refresh | `FeedPager` | Done (emulator) |
| Sideways swipe changes tab (50 pt, clearly horizontal); new tab slides in from that side | `FeedPager`, `AnimatedContent` | Done (test; emulator) |
| Each tab remembers its card | `positions` in `ContentScreen` | Done (emulator) |
| Card picks the richest layout that fits, text never shrinks: hero + full summary, hero + short, thumbnail + full, thumbnail + short, text only (ellipsis allowed only there) | `CardView` (`FirstThatFits`, `HeroPolicy`) | Done (emulator) |
| Hero picture 170 pt to 16:10 with rounded bottom corners, scrim and white source label; shimmer while loading; drawn gradient cover per tab when there is no picture | `StoryImage`, `StoryCover` | Done (emulator) |
| Meta row: BREAKING (two pulses) / DEVELOPING / HOT (spice 8+) badge, story type, age or "Updated …", "AI summary" or "Summary pending" | `MetaRow` | Done (emulator: HOT and AI summary; BREAKING and DEVELOPING depend on the day's edition) |
| Source monograms with the same colour per name as on iOS | `SourceMonogram`, `monogramColor` | Done (test; emulator) |
| "Source + N more · X pts on HN ›" opens Full coverage; "N points on Hacker News" when alone | `CoverageRow` | Done (emulator) |
| Footer: "Read more at Source ›", Save, Share, More (Full coverage, Copy link, Mute source) | `Footer` | Done (emulator) |
| Tap the card: full story in the in-app browser | `Reader` (Chrome Custom Tab) | Adapted: Custom Tabs in place of in-app Safari |
| Last page "You're all caught up", count and update time, Back to top, Saved stories | `EndOfTabCard` | Done (emulator) |
| Skeleton card while the first edition loads; "No stories … Try again" when nothing could be loaded | `SkeletonCard`, `EmptyFeed` | Done (emulator) |
| Highlight border for 1.6 s on a card opened from an alert or search | `CardView`, `ContentScreen` | Done (emulator) |
| At accessibility text sizes the feed becomes a scrolling list of full-length cards with pull to refresh | `FeedList`, from font scale 1.6 | Done (emulator at the largest font size) |
| VoiceOver: one spoken label per card, actions for Save, Share, Full coverage, Copy link, Mute | TalkBack: `spokenLabel`, custom actions | Done (test for the label; not tried with TalkBack on) |
| Haptics on tab change, pill, save, refresh | `LocalHapticFeedback` | Done (code; an emulator cannot show it) |
| Light and dark appearance, accent blue `#215CED` / `#6C97FF`, black and red icon | `Theme.kt`, adaptive icon from the iOS artwork | Done (emulator, both) |
| Portrait only | manifest | Done |

## Sheets (`SupportViews`)

| iOS 2.0 | Android | State |
|---|---|---|
| Full coverage: "Covered by N outlets · first reported … by …", lead outlet with its title, every other outlet opens its own report (`coverageLinks`), outlets without a link are plain, HN points; half-height sheet | `CoverageSheet` (bottom sheet) | Done (emulator) |
| Saved: list, tap opens the story, swipe to remove, search within saved, Clear all with confirmation, empty state; kept in backed-up storage (`saved.json`) | `SavedScreen` | Done (test; emulator) |
| Search across the edition and Saved by headline, summary or outlet; keyboard up on opening; a result shows its card, or opens the story when it has left the edition | `SearchScreen`, `StorySearch` | Done (test; emulator) |
| Settings: Morning gist + time, Big story alerts, Alert sound (News flash, News wire, Chime, Soft, phone default) with preview, "Preview an alert" (arrives after 5 s), explanatory footers | `SettingsScreen` | Done (emulator) |
| Settings: Sources (mute per source, Unmute all), "All on" / "N muted" | `SourcesScreen` | Done (test; emulator) |
| Settings: About (version, where the stories came from, Privacy Policy, Support) | `SettingsScreen` | Done (emulator) |
| Settings: "Open stories in Reader view" | none | **Not done.** Chrome Custom Tabs has no reader mode an app can switch on, so the switch would do nothing. A reader of our own is an Android-specific feature for later |
| Share: a picture of the card (photo, source, headline, summary, Techgist footer) plus the headline and a link to the app | `StoryShare` | Done (emulator). The link is the Play Store's, which works once the app is published |
| Sheets close with Done | system Back gesture or the back arrow | Adapted |

## Notifications and push (`StoryNotification`, `AlertSound`, `DailyDigest`, `BreakingNews`, `PushRegistration`, `AppDelegate`)

| iOS 2.0 | Android | State |
|---|---|---|
| Permission asked once at first launch; yes turns on the morning gist and big story alerts | `ContentScreen` (Android 13+ dialog; earlier versions allow by default) | Done (emulator) |
| Switching either on later asks again, or explains how to allow it in Settings | `SettingsScreen` | Done (emulator) |
| Alert: title and body exactly as the edition's `notification` (else headline and alert line), picture, chosen sound, Read and Save buttons | `StoryNotification.post` | Done (emulator, with "Preview an alert") |
| Tap an alert: the story's card comes on screen, highlighted, loading the newest edition first if needed; an old story opens its page | `MainActivity`, `NewsStore.showStory` | Done (test; emulator) |
| Read button opens the story; Save keeps it without opening the app | `MainActivity`, `SaveStoryReceiver` | Done (emulator) |
| Five alert sounds; the choice is sent to the server | `AlertSound`: the same sound files as WAV; one notification channel, re-made for the chosen sound | Adapted (Android ties a sound to a channel). Done (emulator: channel and preview; an emulator's audio was not listened to) |
| Morning gist at the chosen time with the top story and picture | `DailyDigest` (alarm, also after a restart) | Adapted: Android may deliver it a few minutes late; the story is the top one at that moment. Done (code and test of the time; not waited for overnight) |
| Hourly background refresh; without server push, the app's own big-story alerts (breaking or 3+ outlets, under 6 h, 3 a day, never twice, not stories already seen) | `BackgroundRefresh` (WorkManager), `BreakingNews` | Done (tests for the rules; the hourly job was not waited for) |
| Server push: register after permission + switch, on every launch, and on a new token; unregister when off; token only over HTTPS; body `token, env, tz, locale, appVersion, sound` | `PushRegistration` + `"platform": "android"`, FCM token | Done in code and tests. **Not live**: needs the owner's Firebase file and the server change deployed |
| Server rules: spice 7+, gap, quiet hours in the device's time zone, never twice | `server/push` (same `rules.decide` for both platforms) | Done, tested, **not deployed** |
| Picture attached to a server push (notification service extension) | the app builds the notification from the data message | Adapted |
| One-time notice for people who had alerts on in 1.x | none | Not applicable: there is no Android 1.x |
| Alerts shown while the app is open | Android shows them by default | Done |

## Not in either app

Ads, analytics, accounts. Nothing was added that the iOS app does not have.
