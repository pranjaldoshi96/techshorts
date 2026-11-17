# Techgist for Android

The Android app, with the same features as the iOS app 2.0 (`ios/`, branch `app-2.0`). Kotlin and Jetpack Compose,
package `com.pranjald.techgist`, Android 8.0 (API 26) and later. It reads the same public edition file as the iOS
app and talks to the same push registry. No ads, no analytics.

- What matches iOS and what does not: [PARITY.md](PARITY.md)
- What only the owner can do (Firebase, Play Store): [OWNER_CHECKLIST.md](OWNER_CHECKLIST.md)

## Build

```bash
android/build.sh
```

This builds `android/app/build/outputs/apk/debug/app-debug.apk`. It needs a JDK 17 or 21 and the Android SDK
(platform 35); on a Mac with Android Studio installed the script finds both. Elsewhere set `JAVA_HOME` and
`ANDROID_HOME` first. Any arguments are passed to Gradle instead of the default task:

```bash
android/build.sh testDebugUnitTest
```

```bash
android/build.sh installDebug
```

## Layout

| Folder (under `app/src/main/java/com/pranjald/techgist`) | What it holds |
|---|---|
| `model/` | `Article`, `Edition`, the cache: the edition's JSON fields and how leniently each is read |
| `data/` | `FeedService` (edition, then the publishers' feeds as the fallback), `NewsStore` (what is on screen and when it changes), ordering, paging and time rules, storage |
| `notify/` | the notification itself, alert sounds, the app's own big-story alerts, the morning gist, the hourly background refresh |
| `push/` | `PushRegistration` (the registry's wire format) behind `PushTokenSource`; `Firebase.kt` is the only file that touches Firebase |
| `ui/` | the feed pager and card, tabs, Saved, Search, Full coverage, Settings, the share picture |

Unit tests are in `app/src/test` (edition decoding against a real edition, ordering, paging, refresh behaviour,
push registration, the direct-feed fallback).

## Push

Server push needs the owner's Firebase project. Until `app/google-services.json` exists the app builds and runs
without it: Firebase never starts, nothing is registered, and big story alerts are made on the phone after the
hourly background refresh (the iOS app's fallback, same rules). With the file in place the same build registers
with the push server once the reader allows notifications and has Big story alerts on:

```
POST   https://techgist.213-210-36-218.sslip.io/v1/devices
       {"token": <FCM token>, "platform": "android", "env": "production", "tz", "locale", "appVersion", "sound"}
DELETE https://techgist.213-210-36-218.sslip.io/v1/devices/<token>
```

The server sends data messages (`kind`, `title`, `body`, `eventId`, `url`, `image`); the app draws the
notification, so the reader's sound, the picture, Read and Save, and "a tap opens the story's card" all work
whether the app is open or not. The server side is in `server/push` (see its README) and is **not deployed**.

`google-services.json`, keystores and `local.properties` are in `.gitignore`: this repository is public.
