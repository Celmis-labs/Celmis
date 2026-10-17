"""HTTP middleware (Stage 21) — rate limiting + request-size limits.

Rate limiting:
    FIXED-window counters per (client_ip, path_class) — both backends floor
    the clock to the window and count inside it, so a caller can spend the
    whole allowance at the end of one window and the whole allowance at the
    start of the next. Twice the limit across a boundary is by design here;
    this said "sliding", which promises the opposite. In-memory by default
    (single-instance deploys); if CELMIS_REDIS_URL is set the limiter uses
    Redis INCR+EXPIRE so multiple API replicas share state.

    Path classes + default limits (per minute, override via env):
        auth      (/oauth/token, /api/auth/login)      CELMIS_RL_AUTH=10
        review    (/api/reviews/trigger)               CELMIS_RL_REVIEW=30
        llm_test  (/api/llm/test-agent: a real, paid call) CELMIS_RL_LLM_TEST=12
        mcp       (/mcp/*)                             CELMIS_RL_MCP=120
        default   (everything else under /api, /oauth) CELMIS_RL_DEFAULT=240

    Exempt: /healthz, /readyz, /metrics, /docs, /openapi.json,
    /.well-known/, and the three GIT webhook routes — /webhook/github,
    /webhook/gitlab, /webhook/bitbucket — which have HMAC verification and
    dedup already, so rate-limiting them risks dropping legitimate burst
    deliveries from GitHub.

    NOT "webhooks", which is what this said. `/webhook/alerts/{token}` has
    neither a signature over the body nor dedup: the token in the path is
    compared and every delivery is stored and fanned out. It is rate-limited
    like anything else, and the list in `_EXEMPT_PREFIXES` names the routes
    that were actually reasoned about rather than a prefix that swept in a
    fourth.

Request size:
    Requests with Content-Length above the per-class cap are rejected
    with 413 before the body is read. The cap is ALSO enforced on the bytes
    that actually arrive (the header is only a claim: a proxy, or an HTTP
    parser that lets Transfer-Encoding win over Content-Length, makes it a
    wrong one): once the running total passes the class limit the body is cut
    off and the answer is 413, whatever the headers said. The one-request and
    pack uploads (the classes that demand a length) also refuse
    Transfer-Encoding altogether (411), so the declared length is the real one.

        auth/default   1 MB     CELMIS_MAX_BODY_BYTES=1048576
        review/mcp     5 MB     CELMIS_MAX_BODY_BYTES_LARGE=5242880
        upload         250 MB   CELMIS_MAX_UPLOAD_BYTES=262144000
                                (POST /api/repos/upload only: one-request archive upload)
        upload_part    chunk + 1 MB  CELMIS_UPLOAD_CHUNK_BYTES (default 32 MiB, under 100 MB)
                                (PUT /api/repos/upload/sessions/{id}/parts/{n})
        pack_upload    5 MB     CELMIS_MAX_BODY_BYTES_LARGE (the knowledge-pack upload: a zip or
                                the files of a pack; POST /api/codebase/packs/{id}/versions and
                                POST /api/codebase/publish), rate-limited like `upload`
                                (CELMIS_RL_UPLOAD); the GETs of the publish routes (status,
                                pull) are ordinary reads of the default class

429 responses carry Retry-After (seconds until window reset).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

_WINDOW_SECONDS = 60

_EXEMPT_PREFIXES = (
    "/healthz", "/readyz", "/metrics", "/docs", "/openapi.json",
    # HMAC + dedup already guard these — TRUE OF THE GIT WEBHOOKS AND OF
    # NOTHING ELSE. `/webhook/alerts/{token}` has no signature over the body
    # and no dedup: the token in the path is compared, and every delivery is
    # accepted and stored. Exempting it let a monitoring system in a loop —
    # or anyone holding the token — write unboundedly into a table and fan
    # each one out to a chat room. The prefix names the provider routes it
    # was reasoned about.
    "/webhook/github",
    "/webhook/gitlab",
    "/webhook/bitbucket",
    "/.well-known/",
)


def _is_exempt(path: str) -> bool:
    """Whether this path skips rate limiting.

    A function rather than an inline `any(...)` so a test can ask the same
    question the middleware asks. The list it consults used to hold the bare
    prefix `/webhook/`, which silently covered `/webhook/alerts/` — a route
    with none of the protections the exemption was reasoned from.
    """
    return any(path.startswith(prefix) for prefix in _EXEMPT_PREFIXES)


def _limits() -> dict[str, int]:
    return {
        "auth": int(os.environ.get("CELMIS_RL_AUTH", "10")),
        "review": int(os.environ.get("CELMIS_RL_REVIEW", "30")),
        "mcp": int(os.environ.get("CELMIS_RL_MCP", "120")),
        "llm_test": int(os.environ.get("CELMIS_RL_LLM_TEST", "12")),
        "default": int(os.environ.get("CELMIS_RL_DEFAULT", "240")),
        "upload": int(os.environ.get("CELMIS_RL_UPLOAD", "10")),
        "pack_upload": int(os.environ.get("CELMIS_RL_UPLOAD", "10")),
        "upload_part": int(os.environ.get("CELMIS_RL_UPLOAD_PART", "600")),
    }


def _body_caps() -> dict[str, int]:
    small = int(os.environ.get("CELMIS_MAX_BODY_BYTES", str(1024 * 1024)))
    large = int(os.environ.get("CELMIS_MAX_BODY_BYTES_LARGE", str(5 * 1024 * 1024)))
    return {"auth": small, "default": small, "llm_test": small,
            "review": large, "mcp": large,
            "upload": _upload_cap(), "upload_part": _part_cap(),
            "pack_upload": large}


def _upload_cap() -> int:
    """The archive limit plus a megabyte for the multipart envelope."""
    from src.repos.upload import max_archive_bytes

    return max_archive_bytes() + 1024 * 1024


def _part_cap() -> int:
    """One part of a chunked upload: the chunk size plus a small margin."""
    from src.repos.upload_sessions import part_body_cap

    return part_body_cap()


#: A knowledge pack's new version: exactly this path, and only the POST that
#: carries the files (listing the versions is a plain GET).
_PACK_UPLOAD = re.compile(r"^/api/codebase/packs/[^/]+/versions$")
#: The publish route of a pack token (a laptop or a pipeline): only the POST
#: carries files. Its GETs (status, pull) are ordinary authenticated reads and
#: draw from the default bucket, so a pipeline that checks status before and
#: after a push is not locked out by its own pushes.
_PACK_PUBLISH = re.compile(r"^/api/codebase/publish$")


def _classify(path: str, method: str = "") -> str:
    if path == "/api/repos/upload":
        return "upload"
    if method == "POST" and (_PACK_UPLOAD.match(path) or _PACK_PUBLISH.match(path)):
        return "pack_upload"
    if path.startswith("/api/repos/upload/sessions/") and "/parts/" in path:
        return "upload_part"
    if path.startswith("/oauth/token") or path.startswith("/api/auth/login"):
        return "auth"
    if path.startswith("/api/reviews/trigger"):
        return "review"
    if path.startswith("/api/llm/test-agent"):
        return "llm_test"
    if path.startswith("/mcp"):
        return "mcp"
    return "default"


class _MemoryWindow:
    """Fixed-window counter per key. Thread-safe; prunes lazily."""

    def __init__(self) -> None:
        self._buckets: dict[str, tuple[int, int]] = {}  # key -> (window_start, count)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        now = int(time.time())
        window = now - (now % _WINDOW_SECONDS)
        with self._lock:
            start, count = self._buckets.get(key, (window, 0))
            if start != window:
                start, count = window, 0
            count += 1
            self._buckets[key] = (start, count)
            # Lazy prune every ~1k keys to bound memory.
            if len(self._buckets) > 10_000:
                cutoff = window - _WINDOW_SECONDS
                for k in [k for k, (s, _) in self._buckets.items() if s < cutoff]:
                    del self._buckets[k]
        if count > limit:
            return False, _WINDOW_SECONDS - (now % _WINDOW_SECONDS)
        return True, 0


class _RedisWindow:
    def __init__(self, url: str) -> None:
        import redis
        # Bounded timeouts so a slow/dead Redis can't block the event
        # loop indefinitely; a health check on construction surfaces a
        # dead endpoint immediately so _build_window can fall back to
        # memory (redis-py connects lazily otherwise).
        self._r = redis.Redis.from_url(
            url, decode_responses=True,
            socket_timeout=0.25, socket_connect_timeout=0.25,
        )
        self._r.ping()

    def hit(self, key: str, limit: int) -> tuple[bool, int]:
        now = int(time.time())
        window = now - (now % _WINDOW_SECONDS)
        rkey = f"rl:{key}:{window}"
        pipe = self._r.pipeline()
        pipe.incr(rkey)
        pipe.expire(rkey, _WINDOW_SECONDS * 2)
        count, _ = pipe.execute()
        if int(count) > limit:
            return False, _WINDOW_SECONDS - (now % _WINDOW_SECONDS)
        return True, 0


def _build_window():
    url = os.environ.get("CELMIS_REDIS_URL", "").strip()
    if url:
        try:
            w = _RedisWindow(url)
            logger.info("rate_limiter_backend=redis")
            return w
        except Exception as exc:  # noqa: BLE001
            logger.warning("rate_limiter_redis_failed err=%s — falling back to memory", exc)
    logger.info("rate_limiter_backend=memory")
    return _MemoryWindow()


class _CountedBody:
    """Counts the request body as it is read and answers 413 past the class cap.

    ``receive`` hands the application the body until the running total passes
    the cap; from then on it reports a disconnect, so a form parser or a stream
    reader stops at once. ``send`` then drops whatever the application answers
    (a parser that was cut off says 400) and puts one 413 in its place — unless
    the response had already started, which cannot be taken back.
    """

    def __init__(self, scope: Scope, receive: Receive, send: Send, cls: str) -> None:
        self._scope = scope
        self._receive = receive
        self._send = send
        self._cls = cls
        self._cap: int | None = None
        self.seen = 0
        self.exceeded = False
        self._started = False
        self._answered = False

    async def receive(self) -> Message:
        if self.exceeded:
            return {"type": "http.disconnect"}
        message = await self._receive()
        if message["type"] == "http.request":
            self.seen += len(message.get("body", b""))
            if self._cap is None:                   # resolved on the first body, not per request
                self._cap = _body_caps()[self._cls]
            if self.seen > self._cap:
                self.exceeded = True
                return {"type": "http.disconnect"}
        return message

    async def send(self, message: Message) -> None:
        if self._answered:                          # the 413 is the whole answer
            return
        if self.exceeded and not self._started:
            await self.answer()
            return
        if message["type"] == "http.response.start":
            self._started = True
        await self._send(message)

    async def answer(self) -> None:
        """The 413, once."""
        if self._answered or self._started:
            return
        self._answered = True
        self._started = True

        async def no_body() -> Message:
            return {"type": "http.disconnect"}

        await JSONResponse(
            {"detail": f"request body too large (limit {self._cap} bytes)"},
            status_code=413,
        )(self._scope, no_body, self._send)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app) -> None:  # noqa: ANN001
        super().__init__(app)
        self._window = _build_window()
        self._enabled = os.environ.get("CELMIS_RATE_LIMIT_ENABLED", "1").strip() != "0"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._enabled:
            await super().__call__(scope, receive, send)
            return
        # The cap holds for the bytes that arrive, whatever the headers claim.
        counted = _CountedBody(scope, receive, send, _classify(scope["path"], scope["method"]))
        try:
            await super().__call__(scope, counted.receive, counted.send)
        except Exception:
            if not counted.exceeded:
                raise
            # The application was cut off in the middle of reading and failed
            # (a stream reader says "client disconnected"); the cause is the cap.
        if counted.exceeded:                        # unless it had already answered, say why
            await counted.answer()

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not self._enabled:
            return await call_next(request)

        cls = _classify(path, request.method)
        exempt = _is_exempt(path)

        # ── Body size — ALWAYS enforced, even on rate-limit-exempt paths.
        # Webhooks buffer the whole body before HMAC verification, so an
        # unbounded body is a pre-auth memory-exhaustion DoS.
        cl = request.headers.get("content-length")
        if cls in ("upload", "pack_upload") and request.method == "POST" \
                and request.headers.get("transfer-encoding"):
            # A chunked body has no length to check, and where a parser lets
            # Transfer-Encoding win over a small Content-Length the header is
            # a lie. Uploads are sent with the length they have.
            return JSONResponse(
                {"detail": "Transfer-Encoding is not accepted for archive uploads; "
                           "send a Content-Length"},
                status_code=411,
            )
        if cls in ("upload", "pack_upload") and request.method == "POST" and not cl:
            # The multipart body of the one-request upload is parsed (spooled
            # to disk) BEFORE authentication runs; without a declared length
            # the cap below could not apply, so an anonymous client could
            # stream an unbounded body to disk. Browsers always send a length.
            return JSONResponse(
                {"detail": "Content-Length is required for archive uploads"},
                status_code=411,
            )
        if cl:
            try:
                if int(cl) > _body_caps()[cls]:
                    return JSONResponse(
                        {"detail": f"request body too large (limit {_body_caps()[cls]} bytes)"},
                        status_code=413,
                    )
            except ValueError:
                pass

        # Webhooks (and other exempt paths) skip only rate limiting.
        if exempt:
            return await call_next(request)

        # ── Rate limit ──
        client_ip = request.client.host if request.client else "unknown"
        # Single trusted reverse proxy: the real client IP is the LAST
        # X-Forwarded-For entry (the proxy appends the peer IP). The
        # leftmost entry is attacker-controlled, so using it would let an
        # attacker rotate it to bypass the limit or forge a victim's IP.
        if os.environ.get("CELMIS_TRUST_PROXY", "").strip() == "1":
            fwd = request.headers.get("x-forwarded-for")
            if fwd:
                parts = [p.strip() for p in fwd.split(",") if p.strip()]
                if parts:
                    client_ip = parts[-1]

        # Fail-OPEN on limiter backend failure (e.g. Redis outage) — a
        # broken limiter must not 500 every request. We log and allow.
        try:
            allowed, retry_after = self._window.hit(f"{client_ip}:{cls}", _limits()[cls])
        except Exception as exc:  # noqa: BLE001
            logger.warning("rate_limiter_backend_error err=%s — allowing request", exc)
            return await call_next(request)

        if not allowed:
            logger.warning("rate_limited ip=%s class=%s path=%s", client_ip, cls, path)
            return JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)


__all__ = ["RateLimitMiddleware"]
