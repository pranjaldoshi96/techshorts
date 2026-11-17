"""The device registry's HTTP side. Listens on localhost only; nginx terminates TLS and proxies /v1/ to it.

  POST   /v1/devices            {"token", "env", "tz", "locale", "appVersion", "topics"?, "sound"?, "platform"?}
                                -> {"ok": true}. platform is "ios" (also when absent: the iOS app sends none) or
                                "android"; the token is then an FCM registration token, kept exactly as sent.
  DELETE /v1/devices/<token>    opt out; the same answer whether or not the token was known. An FCM token may be
                                percent-encoded.
  GET    /v1/health             {"ok": true} while the sender loop is alive, 503 otherwise (HEAD: the same, no body)

There is no way to list or look up devices over HTTP. Answers never carry a token back, and the request log shows
a token's first six characters at most.
"""
import json
import re
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import store as store_mod

TOKEN = re.compile(r"^[0-9a-f]{64,200}$")
# An FCM registration token: case-sensitive, never lowercased. Real ones are 140 to 260 characters or so.
FCM_TOKEN = re.compile(r"^[A-Za-z0-9_\-:]{100,1024}$")
PLATFORMS = ("ios", "android")
TZ = re.compile(r"^[A-Za-z0-9_+\-/:]{1,64}$")
LOCALE = re.compile(r"^[A-Za-z0-9_\-@.]{1,35}$")
VERSION = re.compile(r"^[0-9A-Za-z.\-+() ]{1,24}$")
TOPIC = re.compile(r"^[A-Za-z0-9_\- &]{1,32}$")
ENVS = ("sandbox", "production")
FIELDS = {"token", "env", "tz", "locale", "appVersion", "topics", "sound", "platform"}
# The alert sounds the app ships (apns.SOUNDS has their file names). Anything else is stored as "no choice".
SOUNDS = ("flash", "wire", "chime", "soft", "default")


class Invalid(Exception):
    pass


def validate(data):
    """The device row for a registration body, or Invalid(field). Unknown fields are ignored. A platform, token,
    env, tz or topics value out of bounds is refused; locale and appVersion are kept only when they fit."""
    if not isinstance(data, dict):
        raise Invalid("body")
    platform = data.get("platform", "ios")
    if platform not in PLATFORMS:
        raise Invalid("platform")
    token = data.get("token")
    if platform == "android":
        if not isinstance(token, str) or not FCM_TOKEN.match(token):
            raise Invalid("token")
    elif not isinstance(token, str) or not TOKEN.match(token.lower()):
        raise Invalid("token")
    else:
        token = token.lower()
    if data.get("env") not in ENVS:
        raise Invalid("env")
    tz = data.get("tz")
    if not isinstance(tz, str) or not TZ.match(tz):
        raise Invalid("tz")
    # Locale and app version only describe the device. iOS writes locales such as "en_US@rg=inzzzz" (a region
    # override), so the part after "@" is dropped, and a value that still does not fit is stored as unknown:
    # a device is never turned away over them.
    locale = data.get("locale")
    if isinstance(locale, str):
        locale = locale.split("@", 1)[0]
    if not isinstance(locale, str) or not LOCALE.match(locale):
        locale = None
    version = data.get("appVersion")
    if not isinstance(version, str) or not VERSION.match(version):
        version = None
    topics = data.get("topics")
    if topics is not None:
        if (not isinstance(topics, list) or len(topics) > 32
                or not all(isinstance(t, str) and TOPIC.match(t) for t in topics)):
            raise Invalid("topics")
    sound = data.get("sound") if data.get("sound") in SOUNDS else None
    return {"token": token, "env": data["env"], "tz": tz, "locale": locale, "app_version": version,
            "topics": topics, "sound": sound, "platform": platform}


class RateLimit:
    """A fixed window per key: `per_min` requests a minute. In memory; a restart forgets it."""

    def __init__(self, per_min, clock=time.monotonic, max_keys=100000):
        self.per_min, self.clock, self.max_keys = per_min, clock, max_keys
        self.seen = {}
        self.lock = threading.Lock()

    def allow(self, key):
        now = self.clock()
        with self.lock:
            start, n = self.seen.get(key, (now, 0))
            if now - start >= 60:
                start, n = now, 0
            if len(self.seen) >= self.max_keys and key not in self.seen:
                self.seen = {k: v for k, v in self.seen.items() if now - v[0] < 60}
            self.seen[key] = (start, n + 1)
            return n < self.per_min


class App:
    """The routes, apart from sockets: handle(method, path, body, ip) -> (status, dict)."""

    def __init__(self, cfg, open_store, alive=lambda: True, clock=lambda: datetime.now(timezone.utc)):
        self.cfg = cfg["api"]
        self.open_store = open_store
        self.alive, self.clock = alive, clock
        self.by_ip = RateLimit(self.cfg["ip_per_min"])
        self.by_ip_post = RateLimit(self.cfg["ip_post_per_min"])
        self.by_token = RateLimit(self.cfg["token_per_min"])
        self.local = threading.local()

    def store(self):
        if not hasattr(self.local, "store"):
            self.local.store = self.open_store()
        return self.local.store

    def handle(self, method, path, body, ip):
        path = path.split("?", 1)[0]
        if method in ("GET", "HEAD") and path == "/v1/health":
            return (200, {"ok": True}) if self.alive() else (503, {"ok": False})
        if not self.by_ip.allow(ip):
            return 429, {"ok": False, "error": "rate_limited"}
        if method == "POST" and path == "/v1/devices":
            # Anyone can register any well-formed token, and each one costs APNs calls at the next story: an
            # address gets few registrations a minute (an app registers once per launch at most).
            if not self.by_ip_post.allow(ip):
                return 429, {"ok": False, "error": "rate_limited"}
            try:
                dev = validate(json.loads(body.decode("utf-8")))
            except Invalid as e:
                return 400, {"ok": False, "error": "invalid", "field": str(e)}
            except (ValueError, UnicodeDecodeError):
                return 400, {"ok": False, "error": "invalid", "field": "body"}
            if not self.by_token.allow(dev["token"]):
                return 429, {"ok": False, "error": "rate_limited"}
            ok = self.store().register(dev["token"], dev["env"], dev["tz"], dev["locale"], dev["app_version"],
                                       dev["topics"], self.clock(), self.cfg["max_devices"], sound=dev["sound"],
                                       platform=dev["platform"])
            return (200, {"ok": True}) if ok else (503, {"ok": False, "error": "full"})
        if method == "DELETE" and path.startswith("/v1/devices/"):
            # The path does not say which platform: a hex token is APNs' and stored in lower case; anything else
            # must look like an FCM token and is used as it is (the Android app percent-encodes it).
            raw = urllib.parse.unquote(path[len("/v1/devices/"):])
            tokens = [t for t, ok in ((raw.lower(), TOKEN.match(raw.lower())), (raw, FCM_TOKEN.match(raw))) if ok]
            if not tokens:
                return 400, {"ok": False, "error": "invalid", "field": "token"}
            if not self.by_token.allow(tokens[0]):
                return 429, {"ok": False, "error": "rate_limited"}
            for token in dict.fromkeys(tokens):         # both, for the rare FCM token that is all hex digits
                self.store().remove(token)
            return 200, {"ok": True}
        if path in ("/v1/devices", "/v1/health") or path.startswith("/v1/devices/"):
            return 405, {"ok": False, "error": "method"}
        return 404, {"ok": False, "error": "not_found"}


def masked(path):
    """A request path fit for the log: a device token cut to its first six characters."""
    return re.sub(r"(/v1/devices/)([^/?]{0,6})[^/?]*", lambda m: m.group(1) + m.group(2) + "…", path)[:80]


def make_handler(app, log=print):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "techgist-push"
        sys_version = ""
        timeout = 15

        def _ip(self):
            # Only nginx on this machine can reach the socket; it passes the client's address.
            return (self.headers.get("X-Real-IP") or self.client_address[0])[:64]

        def _answer(self, status, obj):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            if self.path != "/v1/health" or status != 200:
                log(f"api {self.command} {masked(self.path)} {status}")

        def _do(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > app.cfg["max_body"]:
                self.close_connection = True
                return self._answer(413, {"ok": False, "error": "too_large"})
            body = self.rfile.read(length) if length else b""
            try:
                status, obj = app.handle(self.command, self.path, body, self._ip())
            except Exception as e:
                log(f"api error {type(e).__name__}")
                status, obj = 500, {"ok": False, "error": "server"}
            self._answer(status, obj)

        do_GET = do_HEAD = do_POST = do_DELETE = do_PUT = do_PATCH = _do

        def log_message(self, *args):       # the default log line carries the full path, token included
            pass

    return Handler


def serve(cfg, open_store, alive, log=print):
    app = App(cfg, open_store, alive)
    httpd = ThreadingHTTPServer((cfg["api"]["host"], cfg["api"]["port"]), make_handler(app, log))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="api", daemon=True).start()
    return httpd
