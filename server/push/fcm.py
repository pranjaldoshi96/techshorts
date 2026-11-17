"""Android: Firebase Cloud Messaging over its HTTP v1 API, authorised by a Google service account.

The service account file is read when an access token is needed (about once an hour) and is never logged; neither
is the access token. A device token appears in a log line as its first six characters at most (store.short).

`Fcm.send` has the signature and the outcomes of `Apns.send` (apns.Result), so the sender settles both alike.

The message is data-only (no `notification` block): the Android app builds the notification itself, with its own
channel per alert sound, the picture, and a tap that opens the story's card.

  {"data": {"kind": "story", "title", "body", "eventId", "url"?, "image"?, "sound"?},
   "android": {"priority": "HIGH", "ttl": "<seconds>s", "collapse_key": <the queue row's collapse id>}}

Every data value is a string. `sound` is the reader's choice as the app registered it (flash, wire, chime, soft,
default), absent when there is none. `kind` is `story`, `server` (an operational alert for the owner) or `test` (pushctl test).
"""
import json
import os
import re
import threading
import time
import urllib.parse

from .apns import Result

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
SEND_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
RENEW_BEFORE_S = 300        # an access token is replaced this long before Google stops accepting it
MAX_TTL_S = 2419200         # FCM keeps a message 28 days at most
MAX_DATA_BYTES = 4096       # FCM's limit for a message's data
_CODE = re.compile(r"^[A-Za-z0-9_.]{1,48}$")


class Credentials(Exception):
    """The service account cannot be used (file, key, or Google refused it): outcome `auth`, not a retry."""

    def __init__(self, reason, status=0):
        super().__init__(reason)
        self.reason, self.status = reason, status


def from_config(cfg):
    """An Fcm for this configuration, or None while the service account file is not there to read: Android
    devices then wait, and nothing else changes."""
    f = cfg.get("fcm") or {}
    return Fcm(f) if f.get("credentials_file") and os.access(f["credentials_file"], os.R_OK) else None


def _code(value):
    """A word of Google's (an error code) fit for a log line; never free text, which can quote a token."""
    return value if isinstance(value, str) and _CODE.match(value) else ""


class Fcm:
    def __init__(self, cfg, client=None, clock=time.time, sleep=time.sleep, fetcher=None, account=None):
        """`fetcher(now) -> (access_token, expires_in_s)` and `account() -> the service account as a dict` replace
        Google and the file in tests."""
        self.cfg = cfg
        self.clock, self.sleep = clock, sleep
        self._client = client
        self._fetcher, self._account = fetcher, account
        self._token, self._token_until = None, 0.0
        self._project = cfg.get("project_id") or None
        self._lock, self._client_lock = threading.Lock(), threading.Lock()

    # Access token

    def account(self):
        if self._account is not None:
            return self._account()
        try:
            with open(self.cfg["credentials_file"]) as f:
                acc = json.load(f)
        except (OSError, ValueError) as e:
            raise Credentials("CredentialsFile" + type(e).__name__)
        if not isinstance(acc, dict) or not all(isinstance(acc.get(k), str) and acc[k]
                                                for k in ("client_email", "private_key", "project_id")):
            raise Credentials("CredentialsFileIncomplete")
        return acc

    def _fetch(self, now):
        """Signs the service account's assertion and exchanges it at Google's token endpoint."""
        if self._fetcher is not None:
            return self._fetcher(now)
        acc = self.account()
        uri = acc.get("token_uri") or "https://oauth2.googleapis.com/token"
        if not uri.startswith("https://"):
            raise Credentials("CredentialsFileTokenUri")
        import jwt
        try:
            assertion = jwt.encode({"iss": acc["client_email"], "scope": SCOPE, "aud": uri, "iat": int(now),
                                    "exp": int(now) + 3600}, acc["private_key"], algorithm="RS256")
        except Exception as e:                  # not a usable RSA key
            raise Credentials("CredentialsFileKey" + type(e).__name__)
        r = self.client().post(uri, headers={"content-type": "application/x-www-form-urlencoded"},
                               content=urllib.parse.urlencode({"grant_type": GRANT, "assertion": assertion}).encode())
        try:
            doc = r.json() or {}
        except ValueError:
            doc = {}
        if r.status_code == 200 and isinstance(doc.get("access_token"), str) and doc["access_token"]:
            if self._project is None:
                self._project = acc["project_id"]
            return doc["access_token"], int(doc.get("expires_in") or 3600)
        if r.status_code == 429 or r.status_code >= 500:
            raise ConnectionError(f"token endpoint {r.status_code}")        # Google's trouble: tried again
        raise Credentials(_code(doc.get("error")) or "TokenRefused", r.status_code)

    def access_token(self, fresh=False):
        """One token for all sends, renewed RENEW_BEFORE_S before it runs out (Google gives it an hour)."""
        with self._lock:
            now = self.clock()
            if self._token is None or fresh or now >= self._token_until:
                self._token = None
                token, lasts = self._fetch(now)
                self._token, self._token_until = token, now + max(60, lasts - RENEW_BEFORE_S)
            return self._token

    def project(self):
        if self._project is None:
            self._project = self.account()["project_id"]
        return self._project

    # Transport

    def client(self):
        with self._client_lock:
            if self._client is None:
                import httpx
                self._client = httpx.Client(http2=True, timeout=self.cfg["timeout_s"])
            return self._client

    def _post(self, access_token, body):
        r = self.client().post(SEND_URL.format(project=urllib.parse.quote(self.project(), safe="")),
                               headers={"authorization": "Bearer " + access_token,
                                        "content-type": "application/json; charset=utf-8"}, content=body)
        try:
            doc = r.json() or {}
        except ValueError:
            doc = {}
        if not isinstance(doc, dict):
            doc = {}
        if r.status_code == 200:
            return 200, "", doc.get("name") if isinstance(doc.get("name"), str) else ""
        err = doc.get("error") if isinstance(doc.get("error"), dict) else {}
        details = err.get("details") if isinstance(err.get("details"), list) else []
        code = next((_code(d.get("errorCode")) for d in details if isinstance(d, dict) and d.get("errorCode")), "")
        return r.status_code, code or _code(err.get("status")), ""

    def send(self, token, env, payload, collapse_id=None, expiration=None, priority=10):
        """One message to one device. `payload` is story_message() or server_message(); the token, the time to
        live (from `expiration`, a unix time, else ttl_s) and the collapse key are added here. `env` means nothing
        to FCM and comes back unchanged. Retries 429, 5xx and network errors with backoff."""
        if len(json.dumps(payload.get("data") or {}, ensure_ascii=False).encode()) > MAX_DATA_BYTES:
            return Result("bad", 413, "PayloadTooLarge", env=env)
        ttl = self.cfg["ttl_s"] if expiration is None else int(expiration - self.clock())
        android = dict(payload.get("android") or {}, priority="HIGH" if priority >= 10 else "NORMAL",
                       ttl=f"{min(max(ttl, 0), MAX_TTL_S)}s")
        if collapse_id:
            android["collapse_key"] = collapse_id[:64]
        body = json.dumps({"message": dict(payload, token=token, android=android)}, ensure_ascii=False,
                          separators=(",", ":")).encode()
        renewed = False
        attempts = 0
        backoff = list(self.cfg["backoff_s"])
        while True:
            attempts += 1
            try:
                status, reason, name = self._post(self.access_token(), body)
            except Credentials as e:
                return Result("auth", e.status, e.reason, "", env, attempts)
            except Exception as e:        # connection reset, timeout, DNS: nothing was delivered for certain
                status, reason, name = 0, type(e).__name__, ""
            if status == 200:
                return Result("ok", 200, "", name, env, attempts)
            if status == 404 or reason == "UNREGISTERED" or (status == 403 and reason == "SENDER_ID_MISMATCH"):
                return Result("gone", status, reason, "", env, attempts)    # the token will never work for this app
            if status == 401 and not renewed:
                renewed = True
                try:
                    self.access_token(fresh=True)
                except Exception:
                    pass                    # the next turn of the loop reports it
                continue
            if status in (401, 403):
                return Result("auth", status, reason, "", env, attempts)
            if status in (0, 429) or status >= 500:
                if attempts >= self.cfg["attempts"]:
                    return Result("retry", status, reason, "", env, attempts)
                self.sleep(backoff[min(attempts - 1, len(backoff) - 1)] if backoff else 0)
                continue
            return Result("bad", status, reason, "", env, attempts)


def choice_of(device):
    """The reader's alert sound as the app registered it (the app maps it to a channel), or None."""
    return device["sound"] if device is not None and "sound" in device.keys() else None


def _message(data):
    return {"data": {k: str(v) for k, v in data.items() if v is not None and v != ""},
            "android": {"priority": "HIGH"}}


def story_message(alert, article, sound_choice=None):
    """The story as the Android app receives it. Title and body are the card's notification text, unchanged (the
    caller takes them from the edition at send time: sender.alert_text). No token: send() adds it."""
    return _message({"kind": "story", "title": alert["title"], "body": alert["body"], "eventId": alert["event_id"],
                     "url": (article or {}).get("url") or (alert.get("payload") or {}).get("url"),
                     "image": (article or {}).get("image"), "sound": sound_choice})


def server_message(title, message):
    """An operational alert for the owner's own devices: titled so it cannot be taken for a story."""
    return _message({"kind": "server", "title": ("Server alert: " + (title or "Techgist"))[:120],
                     "body": (message or "")[:600]})


def trial_message(title, body, sound_choice=None):
    """pushctl's labelled test notification."""
    return _message({"kind": "test", "title": title, "body": body, "sound": sound_choice})
