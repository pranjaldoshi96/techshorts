# What the owner needs to do for Android

Nothing here is needed to build or try the app. Each part unlocks one thing. Do them in this order.

## 1. Firebase project (unlocks real push alerts in the app) — about 10 minutes, free

1. Open https://console.firebase.google.com and sign in with the Google account that should own Techgist.
2. **Create a project**. Name it `Techgist`. When it asks about Google Analytics, turn it **off**.
3. On the project's home page click the **Android** icon ("Add app").
   - Android package name: `com.techshorts.app` (exactly this; the first app, `com.pranjald.techgist`, stays too)
   - App nickname: `Techgist`
   - Leave the SHA-1 box empty. Click **Register app**.
4. Click **Download google-services.json**. Skip the remaining steps on that page (they are already done in the code).
5. Move the downloaded file into the folder `android/app/` of this project. Do not rename it.
   It is ignored by git on purpose: it must not go to GitHub.
6. Tell Claude "the Firebase file is in place". The app is rebuilt and from then on it can receive push.

## 2. FCM server credential (lets the server send to Android phones) — about 5 minutes

1. In the Firebase console: gear icon (top left) → **Project settings** → tab **Service accounts**.
2. Click **Generate new private key** → **Generate key**. A `.json` file downloads
   (its name contains `firebase-adminsdk`). This file is a password: never email it, never put it in the project folder.
3. Move it to your **Desktop**. It must be the only file there with `firebase-adminsdk` in its name.
4. Tell Claude "the FCM key is on my Desktop, deploy push". The deploy copies the new server code and the key to the
   server (`server/push/deploy/deploy_push.sh --fcm`), restarts the push service, and checks it. After it is on the
   server, delete the file from the Desktop.

Until this is done, Android phones that register simply wait; iPhones are not affected in any way.

## 3. Google Play (publishing)

Parts 1 and 2 are done (18 Nov 2026): Firebase project `techgistapp`, server push deployed.

Prepared already, in `android/play/`: the listing text and the answers to every form (`LISTING.md`), the icon,
the feature graphic, five screenshots. The signed release file is
`android/app/build/outputs/bundle/release/app-release.aab`. The upload key and its password are in
`~/.techgist/android/` on this Mac, outside the repository: keep a copy of that folder somewhere safe.

What only the owner can do, in the Play Console (https://play.google.com/console):

The app goes into the existing listing **TechShorts: Tech News, Fast** (`com.techshorts.app`), which has already
passed the 12-tester, 14-day closed test.

1. **Upload key reset** (the old key's password is lost): Test and release → App integrity → App signing →
   Settings → **Request upload key reset** → "I lost my upload key" → upload
   `~/Desktop/techgist_upload_certificate.pem`. Google emails when the new key is active (a day or two).
2. **Grow users → Store presence → Main store listing**: rename to `Techgist: AI & Startup News`, paste the texts
   from `android/play/LISTING.md`, replace the icon, feature graphic and screenshots.
3. **Policy → App content**: check each form against "Forms in the Play Console" in `LISTING.md`.
4. When the key is active: **Closed testing → Alpha → Create new release**, upload `app-release.aab`, send for
   review; then **Apply for production** on the Dashboard.
5. **Declare your trader status** (Home page notice, due February 2027): the owner's own legal declaration.

## 4. Small things worth knowing

- The privacy page (https://pranjaldoshi96.github.io/apps/techgist/privacy/) should say that on Android alerts are
  delivered through Google's Firebase Cloud Messaging. It is one sentence; ask for it when the Play listing is prepared.
- The share picture links to `https://play.google.com/store/apps/details?id=com.techshorts.app`, which starts
  working when the app is public.
