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
- [x] **Submitted for App Review on 3 Nov 2026** with build 20261103.2052 — status *Waiting for Review*, releases automatically when approved
- [x] 6.9" App Store screenshots in `AppStore/screenshots/`
- [x] Store listing, review notes, privacy and age-rating answers in `AppStore/LISTING.md`
- [x] Product, privacy and support pages live: https://pranjaldoshi96.github.io/apps/techgist/

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
7. When it's live, change "Coming to the App Store" on https://pranjaldoshi96.github.io/apps/techgist/ (and the home page card) to the App Store link, as was done for Roomerang.

## Next versions (not in v1)
- Home and Lock Screen widget with today's top story (WidgetKit)
- iCloud sync of saved stories (CloudKit)
- "What's in tech today?" for Siri and Shortcuts (App Intents)
- Unit tests for the parser, filter and ranking
