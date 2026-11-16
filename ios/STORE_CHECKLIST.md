# Techgist — launch checklist

## History (1.x)
- [x] 1.0 (build 20261103.2323) approved and **live on 4 Nov 2026**: https://apps.apple.com/app/techgist-ai-startup-news/id6830218043
- [x] 1.1 (build 20261104.39) submitted 4 Nov 2026 — **rejected**. The reason is in App Store Connect → App Review; the API does not expose it and nobody has read it yet. 1.1's only code change was the share card footer "Download free on the App Store" and the App Store link in the share text.
- [x] 1.2 (build 20261105.2312) live: newest-first tabs, new-stories button, server-written headlines and summaries, App Store share link. 1.2 reads the same `edition.json` the 2.0 engine now publishes.
- [x] App record "Techgist: AI & Startup News", app id 6830218043, team `A4HLS6BV3C`, bundle ID `com.pranjald.techgist`, free in 174 territories (not China mainland)
- [x] Product, privacy and support pages: https://pranjaldoshi96.github.io/apps/techgist/

## 2.0 (branch `app-2.0`) — what is in the build
- [x] New card and feed (any short swipe moves one story), server rank order in every tab, image pipeline, search, full-coverage sheet, Reader-view sheet, accessibility pass
- [x] Five tabs (Top, AI, Startups, Dev, Reads); the engine reads about 60 sources and publishes an edition every few minutes; the app falls back to the publishers' feeds when the edition is over 8 hours old
- [x] AI summaries are written on Techgist's server and checked against the article; labelled on the card. There are **no on-device (Apple Intelligence) summaries** in 2.0
- [x] **Server push is on.** `PushConfig.registerURL` is `https://techgist.213-210-36-218.sslip.io`. With Big story alerts on, the app sends the APNs token, time zone, language/region and app version, and deletes them on opt-out. Checked end to end on a simulator (sandbox APNs); not yet on a phone or with a production token
- [x] Alerts carry the story picture (Notification Service Extension `com.pranjald.techgist.NotificationService`), Techgist's chime and Read and Save buttons
- [x] Privacy manifest declares Device ID (App Functionality, not linked, not tracking), UserDefaults (CA92.1) and file timestamps (C617.1)
- [x] Privacy Policy and Support links in Settings → About
- [x] Pull to refresh works over the network (it used to end in a false "Offline")
- [x] Unit tests and UI tests (`xcodebuild … test`, see README)
- [x] Listing text, What's New, review notes, privacy and age-rating answers for 2.0: `AppStore/LISTING.md`
- [x] 6.9" screenshots from the 2.0 build: `AppStore/screenshots/`

## Before submitting 2.0 — needs the owner ⚠️
1. **Publish the new privacy policy.** The live page still says nothing is sent to a server. Replace it with the text in `PRIVACY.md` (repo `pranjaldoshi96.github.io`, page `/apps/techgist/privacy/`), and fill in the hosting provider. Submitting 2.0 with the old page is a 5.1.1 rejection.
2. **Change the App Privacy answers** in the App Store Connect web UI (not possible through the API): "Yes, we collect data" → Identifiers → Device ID → App Functionality → not linked → not used for tracking. See `AppStore/LISTING.md`.
3. **Read the 1.1 rejection** (App Store Connect → the app → App Review) and check 2.0 against it. The share footer was already changed to neutral wording in case that was the reason.
4. **Age rating:** redo the questionnaire as in `AppStore/LISTING.md` (infrequent/mild mature themes, violence and medical information in news). Expect 12+.
5. **Create version 2.0** in App Store Connect and paste the description, What's New, promotional text and review notes from `AppStore/LISTING.md`; replace all screenshots with the five in `AppStore/screenshots/`.
6. **Push hostname (decide knowingly).** The build hard-codes `techgist.213-210-36-218.sslip.io`: the VPS's IP address inside a free wildcard DNS name run by a third party. If the VPS gets a new IP, or sslip.io stops answering, registration and opt-out stop working for every installed copy until an app update (alerts already registered keep arriving; the feed is unaffected). The fix is a hostname the owner controls pointed at the VPS, a certificate for it, and one constant in `Techgist/Services/PushRegistration.swift`.
7. **Run it on a real iPhone** (TestFlight) before or right after release: a real alert with its picture and chime, the Read and Save buttons, paging feel and frame pacing (only measured in a Debug simulator build: about 1.6–1.8% of frames late), VoiceOver, the largest text size.

## Upload a build
`scripts/archive.sh` archives and exports a signed IPA using the Apple ID signed in to Xcode, then validates it
with App Store Connect; `scripts/archive.sh --upload` also uploads it. Validation and upload need the App Store
Connect API key (`ASC_KEY_ID`, `ASC_ISSUER_ID`, optionally `ASC_KEY_PATH`). The export must not be given the API
key: its role cannot use cloud-managed distribution certificates.

Build numbers are the date and the time, `YYYYMMDD.HHMM` without leading zeros after the dot, which is how App
Store Connect shows them (20261104.0039 is listed as 20261104.39).

After upload Apple emails about any missing privacy-manifest reason (ITMS-91053); none is expected.

## Submit steps
1. Finish items 1–5 above.
2. `scripts/archive.sh --upload`, wait for processing (10–30 min), then select the build on the 2.0 version page.
3. Availability: everywhere except China mainland (unchanged).
4. Submit for review. Release manually or automatically as preferred.

## Known limits of 2.0 (not blockers)
- After a slow drag that ends at rest the card takes about a second to settle (the system's own animation).
- On the direct-feed fallback, cards for link posts (Hacker News) can have a headline and no summary.
- Switching to an accessibility text size while reading loses the place in the tab.
- Stories without a publisher picture show a cover drawn by the app.
- When there is no recent top story the morning gist uses a generic line ("Today's tech, AI and startup news, 60 words a story."). It is local and opt-in.

## Next versions
- Home and Lock Screen widget with today's top story (WidgetKit)
- iCloud sync of saved stories (CloudKit)
- "What's in tech today?" for Siri and Shortcuts (App Intents)
