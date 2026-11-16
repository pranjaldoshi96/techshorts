# Techgist — launch checklist

## Done ✅
- [x] iOS app (SwiftUI, iOS 17+), iPhone, portrait
- [x] Ranked feed: wider coverage and Hacker News points first, duplicates merged into one card ("+N more")
- [x] Five tabs, 22 sources, deals and sponsored posts filtered out
- [x] On-device summaries with Apple Intelligence (iOS 26+), labelled; publisher excerpt elsewhere
- [x] Saved stories, share, source toggles, daily digest notification, offline cache
- [x] Branded covers for stories without a photo
- [x] Publishing server: builds a ranked edition every 2 hours and after reboots; app falls back to direct feeds if it's over 8 hours old
- [x] Privacy: no accounts, analytics, ads or third-party SDKs; privacy manifest (`PrivacyInfo.xcprivacy`)
- [x] App icon, accent colour, light + dark mode
- [x] Team `A4HLS6BV3C`, bundle ID `com.pranjald.techgist`; signed Release archive succeeds
- [x] App record "Techgist: AI & Startup News" created (plain "Techgist" and "TechShorts" belong to other developers)
- [x] Build 1.0 (20261103.2052) uploaded on 3 Nov 2026 with `scripts/archive.sh`: AI summaries are fact-checked against the article. 
- [x] Listing, screenshots, age rating (4+), free pricing in 174 territories (not China mainland), review details and App Privacy ("Data Not Collected") set
- [x] **Submitted for App Review on 3 Nov 2026**; swapped to build 20261103.2323 (swipe tabs, wider Top, image sharing, on-device big story alerts) the same evening — approved and **live on the App Store on 4 Nov 2026**: https://apps.apple.com/app/techgist-ai-startup-news/id6830218043
- [x] **1.1** (build 20261104.0039) submitted 4 Nov 2026 — **rejected** (reason in App Store Connect → App Review; not readable via the API)
- [x] **1.2** (build 20261105.2312) submitted 5 Nov 2026: newest-first tabs, new-stories button, server LLM headlines/summaries (Nemotron 3.5 Lightning), lighter images, one-tap notifications, App Store share link
- [x] 6.9" App Store screenshots in `AppStore/screenshots/`
- [x] Store listing, review notes, privacy and age-rating answers in `AppStore/LISTING.md`
- [x] Product, privacy and support pages live: https://pranjaldoshi96.github.io/apps/techgist/

## 2.0 (branch `app-2.0`, not submitted)

Built and checked in the iOS Simulator only. Version 2.0, build number set by `scripts/archive.sh` as before.

- [x] New card and feed, server rank order, image pipeline, search, coverage sheet, Reader-mode sheet, accessibility pass
- [x] Alerts with the story picture, Techgist's own chime (`techgist.caf`) and Read and Save buttons
- [x] Notification Service Extension target `com.pranjald.techgist.NotificationService` (for server push later)
- [x] Unit tests (`TechgistTests`), including the live edition as a fixture

Before submitting 2.0, needs you:
1. **Read the 1.1 rejection reason** in App Store Connect and check 2.0 against it.
2. **Run it on a real iPhone** (TestFlight): the chime on a real alert, the picture in the banner and on a long press, the Read and Save buttons, paging feel, VoiceOver, and the largest text size. Settings → "Send a sample alert" fires one in 5 seconds.
3. **The first archive registers the extension's App ID.** `scripts/archive.sh` uses `-allowProvisioningUpdates`, so Xcode creates `com.pranjald.techgist.NotificationService` and its profile; nothing to do by hand unless it fails.
4. **Listing text is out of date** (`AppStore/LISTING.md`): the "Summaries written on your iPhone" paragraph, the review notes about Apple Intelligence, "swipe left/right between tabs", the source toggles and the screenshots all describe 1.0. On-device summaries are gone in 2.0; tabs change with the chips; swiping left opens the story.
5. **Server push is wired but off.** The app target has the Push Notifications entitlement (`Techgist.entitlements`, `aps-environment`); the first archive with `-allowProvisioningUpdates` turns the capability on for the App ID. To switch push on, set `PushConfig.registerURL` in `Techgist/Services/PushRegistration.swift` to the registry's HTTPS base URL. With it nil the app never asks iOS for a token and sends nothing.
6. **Privacy answers change when push is switched on.** While `registerURL` is nil the answer stays "Data Not Collected". Once it is set, the app sends the APNs device token, time zone, locale and app version to Techgist's server for readers who turn on Big story alerts: declare *Identifiers → Device ID*, used for *App Functionality*, not linked to identity, not used for tracking, and say so in the privacy policy page.

## Before you submit — needs you ⚠️
1. **Accept Apple's updated Program License Agreement** at developer.apple.com → Account. Until then Xcode can't create the App Store profile, and the upload fails with "PLA Update available". (Same blocker as Firstnest.)
2. **Register the bundle ID and create the app record** (below). Register `com.pranjald.techgist` first at developer.apple.com/account/resources/identifiers/add/bundleId (App IDs → App → Explicit, no capabilities), or it won't appear in App Store Connect's bundle ID list. The store name is "Techgist: AI & Startup News" ("TechShorts: 60-Word Tech News" was already taken).
3. **Test on your iPhone via TestFlight** for a day or two. Not yet checked on a real device: the daily digest notification and background refresh.

## Submit steps
1. App Store Connect → Apps → **+** → New App → iOS, name from `AppStore/LISTING.md`, bundle ID `com.pranjald.techgist`, SKU `techgist-ios`.
2. Upload a build: `ios/scripts/archive.sh` (or Xcode → Any iOS Device → Product → Archive → Distribute App → App Store Connect → Upload).
3. Fill in the listing, keywords, screenshots, URLs, App Privacy ("Data Not Collected") and age rating from `AppStore/LISTING.md`.
4. Availability: everywhere except China mainland.
5. TestFlight: add yourself as an internal tester, install, use it for a couple of days. For friends, create a public link (external testers need a short Beta App Review for the first build).
6. Submit for review.
7. ~~Link the website to the App Store~~ Done on 4 Nov 2026.

## Next versions (not in v1)
- Home and Lock Screen widget with today's top story (WidgetKit)
- iCloud sync of saved stories (CloudKit)
- "What's in tech today?" for Siri and Shortcuts (App Intents)
- Unit tests for the parser, filter and ranking
