"""The device registry's HTTP side. Listens on localhost only; nginx terminates TLS and proxies /v1/ to it.

  POST   /v1/devices            {"token", "env", "tz", "locale", "appVersion", "topics"?}  -> {"ok": true}
  DELETE /v1/devices/<token>    opt out; the same answer whether or not the token was known
  GET    /v1/health             {"ok": true} while the sender loop is alive, 503 otherwise

There is no way to list or look up devices over HTTP. Answers never carry a token back, and the request log shows
a token's first six characters at most.
"""
import json
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import store as store_mod

TOKEN = re.compile(r"^[0-9a-f]{64,200}$")
TZ = re.compile(r"^[A-Za-z0-9_+\-/:]{1,64}$")
LOCALE = re.compile(r"^[A-Za-z0-9_\-@.]{1,35}$")
VERSION = re.compile(r"^[0-9A-Za-z.\-+() ]{1,24}$")
TOPIC = re.compile(r"^[A-Za-z0-9_\- &]{1,32}$")
ENVS = ("sandbox", "production")
FIELDS = {"token", "env", "tz", "locale", "appVersion", "topics"}


class Invalid(Exception):
    pass


def validate(data):
    """The device row for a registration body, or Invalid(field). Unknown fields are ignored; nothing is
    truncated to fit: a value out of bounds is refused."""
    if not isinstance(data, dict):
        raise Invalid("body")
    token = data.get("token")
    if not isinstance(token, str) or not TOKEN.match(token.lower()):
        raise Invalid("token")
    if data.get("env") not in ENVS:
        raise Invalid("env")
    tz = data.get("tz")
    if not isinstance(tz, str) or not TZ.match(tz):
        raise Invalid("tz")
    locale = data.get("locale")
    if locale is not None and (not isinstance(locale, str) or not LOCALE.match(locale)):
        raise Invalid("locale")
    version = data.get("appVersion")
    if version is not None and (not isinstance(version, str) or not VERSION.match(version)):
        raise Invalid("appVersion")
    topics = data.get("topics")
    if topics is not None:
        if (not isinstance(topics, list) or len(topics) > 32
                or not all(isinstance(t, str) and TOPIC.match(t) for t in topics)):
            raise Invalid("topics")
    return {"token": token.lower(), "env": data["env"], "tz": tz, "locale": locale, "app_version": version,
            "topics": topics}


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
        self.by_token = RateLimit(self.cfg["token_per_min"])
        self.local = threading.local()

    def store(self):
        if not hasattr(self.local, "store"):
            self.local.store = self.open_store()
        return self.local.store

    def handle(self, method, path, body, ip):
        path = path.split("?", 1)[0]
        if method == "GET" and path == "/v1/health":
            return (200, {"ok": True}) if self.alive() else (503, {"ok": False})
        if not self.by_ip.allow(ip):
            return 429, {"ok": False, "error": "rate_limited"}
        if method == "POST" and path == "/v1/devices":
            try:
                dev = validate(json.loads(body.decode("utf-8")))
            except Invalid as e:
                return 400, {"ok": False, "error": "invalid", "field": str(e)}
            except (ValueError, UnicodeDecodeError):
                return 400, {"ok": False, "error": "invalid", "field": "body"}
            if not self.by_token.allow(dev["token"]):
                return 429, {"ok": False, "error": "rate_limited"}
            ok = self.store().register(dev["token"], dev["env"], dev["tz"], dev["locale"], dev["app_version"],
                                       dev["topics"], self.clock(), self.cfg["max_devices"])
            return (200, {"ok": True}) if ok else (503, {"ok": False, "error": "full"})
        if method == "DELETE" and path.startswith("/v1/devices/"):
            token = path[len("/v1/devices/"):].lower()
            if not TOKEN.match(token):
                return 400, {"ok": False, "error": "invalid", "field": "token"}
            if not self.by_token.allow(token):
                return 429, {"ok": False, "error": "rate_limited"}
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

        do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = _do

        def log_message(self, *args):       # the default log line carries the full path, token included
            pass

    return Handler


def serve(cfg, open_store, alive, log=print):
    app = App(cfg, open_store, alive)
    httpd = ThreadingHTTPServer((cfg["api"]["host"], cfg["api"]["port"]), make_handler(app, log))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="api", daemon=True).start()
    return httpd
