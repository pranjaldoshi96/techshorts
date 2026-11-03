# Techgist

Published on the App Store as **Techgist: AI & Startup News**. (The repo and Xcode project keep the working name TechShorts.)

Inshorts-style tech news: one story per card, about 60 words each, covering AI, startups, dev tools and good engineering blogs.

| Folder | What it is |
|---|---|
| [`ios/`](ios/) | The iPhone app (SwiftUI). It fetches feeds on the device and writes on-device AI summaries with Apple Intelligence. **This is the main product.** See [ios/README.md](ios/README.md) for how it works and how to publish it to the App Store. |
| [`server/`](server/) | The publisher. Every 2 hours it builds a ranked edition with the app's own Swift code and publishes it to the `edition` branch of this repo. It can also pin your own stories and block links. The app uses that edition, and fetches the feeds itself if the edition is more than 8 hours old. |
| [`web/`](web/) | The original web prototype: a small Node server plus a single-page swipe UI. It isn't needed by the iOS app. Run it with `cd web && npm start` and open http://localhost:3000. |

## Quick start (iOS)

```bash
open ios/Techgist.xcodeproj
```

Choose an iPhone simulator and press Run. To check that every news feed still parses:

```bash
ios/scripts/feedcheck.sh
```
