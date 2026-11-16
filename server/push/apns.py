"""APNs over HTTP/2 with a provider token (ES256 JWT signed with the team's .p8 key).

The key file is read when a token is signed and is never logged; neither is the token. A device token appears in a
log line as its first six characters at most (store.short).
"""
import json
import threading
import time
from dataclasses import dataclass

HOSTS = {"sandbox": "https://api.sandbox.push.apple.com", "production": "https://api.push.apple.com"}
# Apple's reasons that mean "this token will never work for this app": the device is removed.
GONE_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic", "ExpiredToken"}
AUTH_REASONS = {"ExpiredProviderToken", "InvalidProviderToken", "MissingProviderToken"}


@dataclass
class Result:
    outcome: str            # ok | gone | retry (nothing delivered, try later) | auth (our key or token) | bad
    status: int = 0
    reason: str = ""
    apns_id: str = ""
    env: str = ""           # the host that answered last; differs from the device's when the other host took it
    attempts: int = 0


def _other(env):
    return "production" if env == "sandbox" else "sandbox"


class Apns:
    def __init__(self, cfg, client=None, clock=time.time, sleep=time.sleep, signer=None):
        self.cfg = cfg
        self.clock, self.sleep = clock, sleep
        self._client = client
        self._signer = signer
        self._jwt, self._jwt_at = None, 0.0
        self._lock = threading.Lock()

    # Provider token

    def _sign(self, issued_at):
        if self._signer is not None:
            return self._signer(issued_at)
        import jwt
        with open(self.cfg["key_file"]) as f:
            key = f.read()
        return jwt.encode({"iss": self.cfg["team_id"], "iat": int(issued_at)}, key, algorithm="ES256",
                          headers={"kid": self.cfg["key_id"]})

    def provider_token(self, fresh=False):
        """One token for all sends, renewed after jwt_ttl_s (Apple: no more than once per 20 min, valid 60)."""
        with self._lock:
            now = self.clock()
            if fresh and now - self._jwt_at < 1200:
                fresh = False       # Apple answers TooManyProviderTokenUpdates to a faster renewal
            if self._jwt is None or fresh or now - self._jwt_at >= self.cfg["jwt_ttl_s"]:
                self._jwt, self._jwt_at = self._sign(now), now
            return self._jwt

    # Transport

    def client(self):
        with self._lock:
            if self._client is None:
                import httpx
                self._client = httpx.Client(http2=True, timeout=self.cfg["timeout_s"])
            return self._client

    def _post(self, env, token, headers, body):
        r = self.client().post(f"{HOSTS[env]}/3/device/{token}", headers=headers, content=body)
        reason = ""
        if r.status_code != 200:
            try:
                reason = (r.json() or {}).get("reason", "")
            except ValueError:
                reason = ""
        return r.status_code, reason, r.headers.get("apns-id", ""), r.http_version

    def send(self, token, env, payload, collapse_id=None, expiration=None, priority=10):
        """One notification to one device. Retries 429, 5xx and network errors with backoff; a BadDeviceToken is
        tried once on the other host (a build can report the wrong environment) before the device counts as gone."""
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        if len(body) > 4096:
            return Result("bad", 413, "PayloadTooLarge", env=env)
        env = env if env in HOSTS else "production"
        tried_other = renewed = False
        attempts = 0
        backoff = list(self.cfg["backoff_s"])
        while True:
            attempts += 1
            headers = {"authorization": "bearer " + self.provider_token(), "apns-topic": self.cfg["topic"],
                       "apns-push-type": "alert", "apns-priority": str(priority)}
            if collapse_id:
                headers["apns-collapse-id"] = collapse_id[:64]
            if expiration is not None:
                headers["apns-expiration"] = str(int(expiration))
            try:
                status, reason, apns_id, _ = self._post(env, token, headers, body)
            except Exception as e:        # connection reset, timeout, DNS: nothing was delivered for certain
                status, reason, apns_id = 0, type(e).__name__, ""
            if status == 200:
                return Result("ok", 200, "", apns_id, env, attempts)
            if status == 410 or (status == 400 and reason in GONE_REASONS):
                if reason == "BadDeviceToken" and not tried_other:
                    tried_other, env = True, _other(env)
                    continue
                return Result("gone", status, reason, apns_id, env, attempts)
            if status == 403 and reason in AUTH_REASONS and not renewed:
                renewed = True
                self.provider_token(fresh=True)
                continue
            if status == 403:
                return Result("auth", status, reason, apns_id, env, attempts)
            if status in (0, 429) or status >= 500:
                if attempts >= self.cfg["attempts"]:
                    return Result("retry", status, reason, apns_id, env, attempts)
                self.sleep(backoff[min(attempts - 1, len(backoff) - 1)] if backoff else 0)
                continue
            return Result("bad", status, reason, apns_id, env, attempts)


# The reader's choice of alert sound -> the file in the app bundle ("default" is the iPhone's own sound).
SOUNDS = {"flash": "techgist-flash.caf", "wire": "techgist-wire.caf", "chime": "techgist.caf",
          "soft": "techgist-soft.caf", "default": "default"}


def sound_for(device, fallback):
    """The sound name for this device's alerts: its own choice, else the service's."""
    choice = device["sound"] if device is not None and "sound" in device.keys() else None
    return SOUNDS.get(choice, fallback)


def story_payload(alert, article, sound):
    """The PUSH CONTRACT payload. Title and body are the card's notification text, unchanged (the caller takes
    them from the edition at send time: sender.alert_text)."""
    p = {"aps": {"alert": {"title": alert["title"], "body": alert["body"]}, "sound": sound, "mutable-content": 1,
                 "thread-id": alert["event_id"]},
         "eventId": alert["event_id"]}
    url = (article or {}).get("url") or (alert.get("payload") or {}).get("url")
    if url:
        p["url"] = url
    if (article or {}).get("image"):
        p["image"] = article["image"]
    return p


def server_payload(title, message):
    """An operational alert for the owner's own devices: titled so it cannot be taken for a story."""
    return {"aps": {"alert": {"title": ("Server alert: " + (title or "Techgist"))[:120], "body": (message or "")[:600]},
                    "sound": "default", "thread-id": "server"},
            "kind": "server"}
