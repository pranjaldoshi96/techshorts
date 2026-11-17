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

## 3. Google Play developer account (needed to publish) — start early, it takes days

1. Open https://play.google.com/console/signup and choose **Yourself** (personal account), unless you want it under a company.
2. Pay the one-time **US$25** fee and finish the identity check (a government ID and, for some countries, a
   phone and address check). Approval can take a few days.
3. Google requires new personal accounts to run a **closed test with at least 12 testers for 14 days** before an
   app can go public. Collect 12 Gmail addresses of people willing to install it (friends, readers).
4. When the account is approved, tell Claude. What follows, and can be prepared for you step by step:
   - a release signing key (made on this Mac, kept out of git; Play App Signing holds the real one)
   - the store listing (the iOS texts and screenshots redone for Android), the content rating form, the
     Data safety form (the app collects only the push token, time zone and language, and only with alerts on;
     no ads, no tracking), the "News app" declaration, the privacy policy link
   - uploading the first build to the closed test

## 4. Small things worth knowing

- The privacy page (https://pranjaldoshi96.github.io/apps/techgist/privacy/) should say that on Android alerts are
  delivered through Google's Firebase Cloud Messaging. It is one sentence; ask for it when the Play listing is prepared.
- The share picture links to `https://play.google.com/store/apps/details?id=com.pranjald.techgist`, which starts
  working when the app is public.
