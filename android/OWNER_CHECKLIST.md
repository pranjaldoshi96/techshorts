# What the owner needs to do for Android

Nothing here is needed to build or try the app. Each part unlocks one thing. Do them in this order.

## 1. Firebase project (unlocks real push alerts in the app) — about 10 minutes, free

1. Open https://console.firebase.google.com and sign in with the Google account that should own Techgist.
2. **Create a project**. Name it `Techgist`. When it asks about Google Analytics, turn it **off**.
3. On the project's home page click the **Android** icon ("Add app").
   - Android package name: `com.pranjald.techgist` (exactly this)
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

1. Check it opens a **developer** account (a Play Store or Play Pass subscription is not one). If it asks you to
   sign up: choose **Yourself**, pay the one-time US$25, finish the identity check.
2. **Create app**: name `Techgist: AI & Startup News`, language English, **App**, **Free**, tick the two
   declarations.
3. **Dashboard → Set up your app**: fill each form with the answers in `android/play/LISTING.md`
   ("Forms in the Play Console").
4. **Grow → Store presence → Main store listing**: paste the texts, upload the icon, feature graphic, screenshots.
5. **Testing → Closed testing → Create track**: add the testers' Gmail addresses, **Create new release**, upload
   `app-release.aab`, accept Play App Signing, save, **Send for review**.
6. New personal accounts must keep **12 testers opted in for 14 days**, then **Apply for production access**.

## 4. Small things worth knowing

- The privacy page (https://pranjaldoshi96.github.io/apps/techgist/privacy/) should say that on Android alerts are
  delivered through Google's Firebase Cloud Messaging. It is one sentence; ask for it when the Play listing is prepared.
- The share picture links to `https://play.google.com/store/apps/details?id=com.pranjald.techgist`, which starts
  working when the app is public.
