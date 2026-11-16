# Techgist — launch checklist

## History (1.x)
- [x] 1.0 (build 20261103.2323) approved and **live on 4 Nov 2026**: https://apps.apple.com/app/techgist-ai-startup-news/id6830218043
- [x] 1.1 (build 20261104.39) submitted 3 Nov 2026 19:15Z — **rejected**. The reason is in App Store Connect → App Review; the API does not expose it and nobody has read it yet. It was not the share footer: 1.2 shipped the same footer and was approved (see `AppStore/LISTING.md`).
- [x] 1.2 (build 20261105.2312) live: the same version record as 1.1, renamed and resubmitted on 5 Nov. Newest-first tabs, new-stories button, server-written headlines and summaries. 1.2 reads the same `edition.json` the 2.0 engine now publishes.
- [x] App record "Techgist: AI & Startup News", app id 6830218043, team `A4HLS6BV3C`, bundle ID `com.pranjald.techgist`, free in 174 territories (not China mainland)
- [x] Product, privacy and support pages: https://pranjaldoshi96.github.io/apps/techgist/

## 2.0 (branch `app-2.0`) — what is in the build
- [x] New card and feed (any short swipe moves one story; quick flicks one after another each move one), server rank order in every tab, image pipeline, search, full-coverage sheet, Reader-view sheet, accessibility pass
- [x] Five tabs (Top, AI, Startups, Dev, Reads); the engine reads about 60 sources and publishes an edition every few minutes; the app falls back to the publishers' feeds when the edition is over 8 hours old
- [x] AI summaries are written on Techgist's server and checked against the article; labelled on the card. There are **no on-device (Apple Intelligence) summaries** in 2.0
- [x] **Server push is on.** `PushConfig.registerURL` is `https://techgist.213-210-36-218.sslip.io`. With Big story alerts on, the app sends the APNs token, time zone, language/region and app version, and deletes them on opt-out. Checked end to end on a simulator (sandbox APNs); not yet on a phone or with a production token
- [x] **People who had alerts on in 1.x are asked once** before anything is sent ("Keep alerts on" / "Turn off alerts"); the alerts offer says alerts come from Techgist's server
- [x] Alerts carry the story picture (Notification Service Extension `com.pranjald.techgist.NotificationService`), Techgist's chime and Read and Save buttons
- [x] Privacy manifest declares Device ID (App Functionality, not linked, not tracking), UserDefaults (CA92.1) and file timestamps (C617.1)
- [x] Privacy Policy and Support links in Settings → About
- [x] Unit tests (77) and UI tests (23) pass from a clean build on iPhone 17; the paging tests also pass on iPhone 17 Pro, and two of them in the iPhone window on an iPad Pro 11" simulator (16 Nov 2026)

## Done in App Store Connect and on the web (16 Nov 2026)
- [x] **Privacy policy, support and product pages replaced** (repo `pranjaldoshi96.github.io`, commit d4d9ea9; source text `PRIVACY.md`). They describe 2.0 and say what was different in 1.x, because 1.2 stays on sale until 2.0 is released. The policy names the host as Hostinger, India, from the server address's registration: **check it against the hosting invoice.**
- [x] **Build 2.0 (20261116.1321) uploaded**, processed (`VALID`), available to internal TestFlight testers
- [x] **Version 2.0 created** (`PREPARE_FOR_SUBMISSION`, **manual release**), with the description, keywords, promotional text, What's New and review notes from `AppStore/LISTING.md`, the six 6.9" screenshots from `AppStore/screenshots/2.0/`, and build 20261116.1321 attached
- [ ] Nothing has been submitted for review

## Before pressing Submit — only possible by hand ⚠️
1. **App Privacy answers** (App Store Connect → App Privacy → Edit; the API can neither read nor set them, 1.x was published as "Data Not Collected"): "Yes, we collect data" → Identifiers → Device ID → App Functionality → not linked to the user → not used for tracking → Publish. The build's privacy manifest, the policy and the review notes all say this; the label is the one thing still contradicting them.
2. **Age rating** (App Information → Age Rating → Edit): still 4+ with every answer "None", which is wrong for a live news feed. Answers in `AppStore/LISTING.md` (infrequent/mild mature themes, realistic violence and medical information). Expect 12+. Left untouched on purpose: changing it changes who can download the app.
3. **Read the 1.1 rejection** (App Store Connect → the app → App Review) and check 2.0 against it. Two minutes; nobody has done it.
4. **Release choice.** The version is set to manual release, so after approval it waits for "Release this version". That leaves room for step 5. Switch to automatic on the version page if the wait is not wanted.
5. **Run it on a real iPhone** (TestFlight, build 20261116.1321): turn on Big story alerts, send a test from the VPS (`pushctl test <token prefix> --event <id>`), and check the picture, the chime, the Read and Save buttons and the tap-through. Server push has only ever run against sandbox APNs on a simulator. Also worth a look there: paging feel, VoiceOver, the largest text size.
6. **Push hostname (decide knowingly).** The build hard-codes `techgist.213-210-36-218.sslip.io`: the VPS's IP address inside a free wildcard DNS name run by a third party. If the VPS gets a new IP, or sslip.io stops answering, registration and opt-out stop working for every installed copy until an app update (alerts already registered keep arriving; the feed is unaffected). The fix is a hostname the owner controls pointed at the VPS, a certificate for it, and one constant in `Techgist/Services/PushRegistration.swift`.

## Upload a build
`scripts/archive.sh` archives and exports a signed IPA using the Apple ID signed in to Xcode, then validates it
with App Store Connect; `scripts/archive.sh --upload` also uploads it. Validation and upload need the App Store
Connect API key (`ASC_KEY_ID`, `ASC_ISSUER_ID`, optionally `ASC_KEY_PATH`). The export must not be given the API
key: its role cannot use cloud-managed distribution certificates.

Build numbers are the date and the time, `YYYYMMDD.HHMM` without leading zeros after the dot, which is how App
Store Connect shows them (20261104.0039 is listed as 20261104.39). A newer build replaces the attached one on
the version page (or `PATCH /v1/appStoreVersions/<id>/relationships/build`).

After upload Apple emails about any missing privacy-manifest reason (ITMS-91053); none is expected.

## Submit steps
1. Finish items 1–3 above (4–6 are choices).
2. App Store Connect → the app → 2.0 → Add for Review → Submit.
3. Availability stays as it is: everywhere except China mainland.

## Review risks that stay
- **5.2, third-party content.** Publishers' photos are resized and served by Techgist and appear in the screenshots, as in the approved 1.2. If review objects: retake the screenshots on drawn covers, set `images: generated` for the source in the engine, drop any source that objects.
- **4.2, minimum functionality.** The review notes list the native features.

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
