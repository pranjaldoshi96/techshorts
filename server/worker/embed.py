"""Embeddings (spec 2.6): one batched call per tick, up to 50 inputs, outside the chat rate limit.

Items are embedded as `passage` from their original title plus the opening of their text; signal items
(Techmeme and HN headlines with no text of their own) are embedded as `query` from the title alone, since a
headline against an article is the retrieval shape the model was trained for. Vectors are unit-normalised, so a
dot product is the cosine.

Which model and which input shape: only nvidia/nemotron-3.5-embed-1b answers for this key (the e5/llama embed
models return 410). The input shape (title + first `embed.lead_words` words) and size (`embed.dims`) were picked
by tools/label_pairs.py on the hand-labelled fixture pairs; see config.yaml.
"""
import base64
import logging
import time

import numpy as np
import requests

from .llm import AuthError, LLMError, ModelGone, _retry_after

log = logging.getLogger("techgist")

BATCH = 50


class ServerError(LLMError):
    """5xx or 429s on every try."""


class Unavailable(LLMError):
    """The endpoint doesn't answer at all (timeouts, connection errors): the rest of the tick goes without."""


def item_input(title, text, lead_words):
    """The passage for an item: its title, then the first `lead_words` words of its text. The title comes first
    because it is the publisher's one-line statement of the event; a long tail of body text dilutes it."""
    words = (text or "").split()[:lead_words]
    return f"{title.strip()}\n{' '.join(words)}".strip()


def normalize(v):
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n else v


def cosine(a, b):
    return float(np.dot(a, b))


# Storage. SQLite keeps float32 bytes; the test fixture keeps int8 (4x smaller, cosine error about 0.001).

def to_blob(v):
    return np.asarray(v, dtype=np.float32).tobytes()


def from_blob(b):
    return np.frombuffer(b, dtype=np.float32)


def pack_int8(v):
    v = np.asarray(v, dtype=np.float32)
    scale = float(np.abs(v).max()) or 1.0
    q = np.round(v / scale * 127).astype(np.int8)
    return {"s": scale, "q": base64.b64encode(q.tobytes()).decode()}


def unpack_int8(d):
    q = np.frombuffer(base64.b64decode(d["q"]), dtype=np.int8).astype(np.float32)
    return normalize(q * d["s"] / 127)


class Embedder:
    # A healthy call takes about 0.3 s. Two timeouts in a row mean the endpoint is down (2026-11-11 afternoon:
    # every call timed out for 20 min), and halving batches into single retries would stall the 60 s tick for
    # many minutes, so the tick gives up on embeddings instead and clusters what it already has.
    MAX_TIMEOUTS = 2

    def __init__(self, base_url, key, model, dims=None, session=None, timeout=15, sleep=time.sleep):
        self.url = base_url.rstrip("/") + "/embeddings"
        self.key, self.model, self.dims = key, model, dims
        self.session = session or requests.Session()
        self.timeout, self.sleep = timeout, sleep
        self.calls = 0
        self.timeouts = 0

    def embed(self, texts, input_type="passage"):
        """Unit vectors for `texts`, in order. Raises LLMError (or AuthError/ModelGone) when a batch fails."""
        out = []
        self.timeouts = 0
        for i in range(0, len(texts), BATCH):
            out.extend(self._split(texts[i:i + BATCH], input_type))
        return out

    def _split(self, texts, input_type):
        # The hosted endpoint answers 500 "service inference failed" to most multi-input requests some days
        # (2026-11-11: 0/12 batches of 10, 11/12 single inputs), so a failing batch is halved until it goes through.
        try:
            # A multi-input batch gets one try before halving; retries are for the single inputs at the bottom.
            return self._batch(texts, input_type, tries=1 if len(texts) > 1 else 6)
        except ServerError:
            if len(texts) == 1:
                raise
            mid = len(texts) // 2
            return self._split(texts[:mid], input_type) + self._split(texts[mid:], input_type)

    def _batch(self, texts, input_type, tries=3):
        body = {"model": self.model, "input": [t or " " for t in texts], "input_type": input_type,
                "encoding_format": "float", "truncate": "END"}
        if self.dims:
            body["dimensions"] = self.dims
        for attempt in range(tries):
            try:
                r = self.session.post(self.url, json=body, timeout=self.timeout,
                                      headers={"Authorization": f"Bearer {self.key}"})
            except requests.RequestException as e:
                log.info("embed: %s", type(e).__name__)
                self.timeouts += 1
                if self.timeouts >= self.MAX_TIMEOUTS:
                    raise Unavailable(f"embed: {type(e).__name__} x{self.timeouts}")
                self.sleep(2 * (attempt + 1))
                continue
            self.timeouts = 0
            self.calls += 1
            if r.status_code == 429:
                self.sleep(_retry_after(r.headers.get("Retry-After"), 10 * (attempt + 1)))
                continue
            if r.status_code in (401, 403):
                raise AuthError(f"embed HTTP {r.status_code}")
            if r.status_code in (404, 410):
                raise ModelGone(f"{self.model}: HTTP {r.status_code}")
            if r.status_code >= 500:
                self.sleep(attempt + 1)
                continue
            if r.status_code != 200:
                raise LLMError(f"embed HTTP {r.status_code}: {r.text[:200]}")
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            if len(data) != len(texts):
                raise LLMError("embed: wrong number of vectors")
            return [normalize(d["embedding"]) for d in data]
        raise ServerError(f"embed: gave up after {tries} tries")


def make(cfg, key):
    e = cfg.get("embed") or {}
    if not e.get("model") or not key:
        return None
    return Embedder(cfg["llm"]["base_url"], key, e["model"], e.get("dims"), timeout=e.get("timeout_s", 15))
