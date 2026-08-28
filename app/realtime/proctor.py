"""
Screen Proctoring Unit — event-driven vision + sparse baseline
===============================================================
Self-contained module (crash_recovery pattern): ALL proctoring logic lives
here; agent.py only calls thin hooks. Owns a small DB engine, optional S3
evidence uploads, and the Claude vision calls.

Two-layer design (Layer 1 triggers Layer 2 — vision only runs on suspicion):

  Layer 1 — browser beacons (continuous, free)
    tab_hidden / tab_visible / window_blur / window_focus / paste /
    share_started / share_stopped / display_surface / multi_monitor
    → every beacon is recorded to interview_proctor_events;
    → suspicious ones TRIGGER a Layer-2 burst.

  Layer 2 — vision analysis (only when triggered + sparse baseline)
    A rolling 1-frame buffer holds the latest decoded frame of the candidate's
    screen-share track (the SDK decodes it anyway — keeping one frame is ~free),
    so the exact screen AT the trigger moment is captured instantly.
    Trigger → burst: frame now + follow-ups; focus-return → one frame.
    Sparse baseline: one frame every PROCTOR_BASELINE_INTERVAL_S seconds
    (default 150; 0 disables) catches passive split-screen cheating that never
    fires a Layer-1 signal.
    Claude Haiku vision classifies each frame; violations store a JPEG to S3
    as evidence and escalate (2nd violation → optional verbal warning).

Guardrails: per-trigger cooldown, hard cap on vision calls per interview,
each failure is a silent no-op — proctoring must never break a live interview.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import pathlib
import re
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_env_path = pathlib.Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=_env_path, override=False)


# ── Config (env) ────────────────────────────────────────────────────────────────

def _env_bool(name: str, default: str = "true") -> bool:
    return os.environ.get(name, default).strip().lower() not in ("false", "0", "no")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


PROCTOR_ENABLED          = _env_bool("PROCTOR_ENABLED")
PROCTOR_VISION_ENABLED   = _env_bool("PROCTOR_VISION_ENABLED")
PROCTOR_VERBAL_WARNINGS  = _env_bool("PROCTOR_VERBAL_WARNINGS")
BASELINE_INTERVAL_S      = _env_float("PROCTOR_BASELINE_INTERVAL_S", 150.0)  # 0 = off
BURST_COOLDOWN_S         = _env_float("PROCTOR_BURST_COOLDOWN_S", 30.0)
MAX_VISION_CALLS         = int(_env_float("PROCTOR_MAX_VISION_CALLS", 30))
BURST_FOLLOWUPS          = 2       # extra frames after the trigger frame
BURST_FOLLOWUP_GAP_S     = 8.0
WARN_AT_VIOLATIONS       = 2       # Sarah warns on the 2nd confirmed violation
MAX_WARNINGS             = 2

VISION_MODEL             = "claude-haiku-4-5-20251001"
VISION_TIMEOUT_S         = 20.0
VISION_MAX_TOKENS        = 200
FRAME_MAX_DIM            = 1024    # downscale evidence frames to this
JPEG_QUALITY             = 70

# Beacon types → (severity, triggers_vision_burst)
BEACON_POLICY: dict[str, tuple[str, bool]] = {
    "share_started":   ("info",   False),
    "share_stopped":   ("high",   False),  # nothing to see — the screen is gone
    "display_surface": ("info",   False),  # payload carries monitor/window/browser
    "tab_hidden":      ("medium", True),
    "tab_visible":     ("info",   True),   # focus-return — catch what they saw
    "window_blur":     ("medium", True),
    "window_focus":    ("info",   True),
    "paste":           ("medium", True),
    "multi_monitor":   ("medium", False),  # other display isn't shared — flag only
}

_VISION_SYSTEM = (
    "You audit a screenshot of a job candidate's shared screen taken DURING a "
    "remote AI screening interview. The interview page itself (an 'AI HR "
    "Assistant' chat/video page) is EXPECTED and never suspicious. Anything "
    "that could help them cheat IS suspicious: AI assistants (ChatGPT, Claude, "
    "Gemini, Copilot, Perplexity...), search engines with interview-related "
    "queries, messaging apps with relevant chats, notes/documents with "
    "prepared answers, or another person's guidance. Reply ONLY with JSON: "
    '{"suspicious": true|false, "apps": ["<visible apps/sites>"], '
    '"reason": "<one short sentence>"}'
)


# ── Tiny own DB engine (crash_recovery pattern) ────────────────────────────────

_engine = None
_session_factory = None


def _get_factory():
    global _engine, _session_factory
    if _session_factory is None:
        db_url = os.environ.get("DATABASE_URL", "")
        if not db_url:
            logger.error("[proctor] DATABASE_URL empty — event persistence disabled")
            return None
        from sqlalchemy.ext.asyncio import (
            AsyncSession, async_sessionmaker, create_async_engine,
        )
        _engine = create_async_engine(db_url, pool_size=2, max_overflow=3, pool_pre_ping=True)
        _session_factory = async_sessionmaker(bind=_engine, class_=AsyncSession, expire_on_commit=False)
    return _session_factory


async def _db_record_event(
    interview_id: str, tenant_id: str, event_type: str,
    severity: str, payload: dict, frame_s3_key: str | None,
) -> None:
    factory = _get_factory()
    if not factory:
        return
    from sqlalchemy import bindparam, text
    from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
    now = datetime.now(timezone.utc)
    try:
        async with factory() as session:
            await session.execute(
                text("""
                    INSERT INTO interview_proctor_events
                        (id, tenant_id, interview_id, event_type, severity,
                         payload, frame_s3_key, created_at, updated_at)
                    VALUES
                        (:id, :tenant_id, :interview_id, :event_type, :severity,
                         :payload, :frame_s3_key, :now, :now)
                """).bindparams(bindparam("payload", type_=PG_JSONB)),
                {
                    "id": str(uuid.uuid4()), "tenant_id": tenant_id,
                    "interview_id": interview_id, "event_type": event_type,
                    "severity": severity, "payload": payload,
                    "frame_s3_key": frame_s3_key, "now": now,
                },
            )
            await session.commit()
    except Exception as e:
        logger.warning(f"[proctor] event save failed (non-fatal): {e}")


# ── S3 evidence upload (optional) ───────────────────────────────────────────────

def _s3_upload_sync(jpeg: bytes, key: str) -> bool:
    import boto3
    bucket = os.environ.get("S3_BUCKET_NAME", "")
    if not bucket:
        return False
    boto3.client(
        "s3",
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID") or None,
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY") or None,
        region_name=os.environ.get("AWS_REGION") or None,
    ).put_object(Bucket=bucket, Key=key, Body=jpeg, ContentType="image/jpeg")
    return True


async def _upload_evidence(interview_id: str, jpeg: bytes) -> str | None:
    """Upload a violation frame; returns the S3 key or None (no bucket/failure)."""
    if not os.environ.get("S3_BUCKET_NAME", ""):
        return None
    key = (
        f"proctor/{interview_id}/"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}.jpg"
    )
    try:
        ok = await asyncio.to_thread(_s3_upload_sync, jpeg, key)
        return key if ok else None
    except Exception as e:
        logger.warning(f"[proctor] evidence upload failed (non-fatal): {e}")
        return None


# ── Vision call ─────────────────────────────────────────────────────────────────

def _parse_vision(raw: str) -> dict | None:
    try:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        data = json.loads(m.group(0))
        if not isinstance(data.get("suspicious"), bool):
            return None
        return {
            "suspicious": data["suspicious"],
            "apps":       [str(a) for a in (data.get("apps") or [])][:8],
            "reason":     str(data.get("reason") or "")[:300],
        }
    except Exception:
        return None


async def _vision_analyze(jpeg: bytes) -> dict | None:
    """Claude Haiku vision verdict on one frame. None = couldn't verify."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or not jpeg:
        return None
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
        resp = await asyncio.wait_for(
            client.messages.create(
                model=VISION_MODEL,
                max_tokens=VISION_MAX_TOKENS,
                system=_VISION_SYSTEM,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "media_type_base64" if False else "base64",
                            "media_type": "image/jpeg",
                            "data": base64.b64encode(jpeg).decode(),
                        }},
                        {"type": "text", "text": "Audit this screen."},
                    ],
                }],
            ),
            timeout=VISION_TIMEOUT_S,
        )
        raw = "".join(getattr(b, "text", "") for b in (resp.content or []))
        return _parse_vision(raw)
    except Exception as e:
        logger.warning(f"[proctor] vision call failed (no-op): {e}")
        return None


# ── Integrity score (used by the report) ────────────────────────────────────────

# Deduction per event type, with a per-type cap so one spammy signal can't
# zero the score alone.
_SCORE_RULES: dict[str, tuple[int, int]] = {
    # event_type:        (deduct_each, cap_total)
    "vision_violation":   (20, 60),
    "share_stopped":      (10, 30),
    "tab_hidden":         (3,  18),
    "window_blur":        (2,  12),
    "paste":              (5,  15),
    "multi_monitor":      (5,  5),
    "share_never_started": (30, 30),
}


def compute_integrity(events: list[dict]) -> dict:
    """
    events: [{event_type, severity, payload, frame_s3_key, created_at}, ...]
    Returns {"score": 0-100, "level": clean|minor|flagged, "counts": {...},
             "violations": [...]}  — pure function, unit-testable.
    """
    counts: dict[str, int] = {}
    deductions: dict[str, int] = {}
    violations: list[dict] = []
    for ev in events:
        et = ev.get("event_type", "")
        counts[et] = counts.get(et, 0) + 1
        rule = _SCORE_RULES.get(et)
        if rule:
            each, cap = rule
            deductions[et] = min(cap, deductions.get(et, 0) + each)
        if et == "vision_violation":
            p = ev.get("payload") or {}
            violations.append({
                "at":      str(ev.get("created_at") or ""),
                "trigger": p.get("trigger", ""),
                "apps":    p.get("apps", []),
                "reason":  p.get("reason", ""),
                "frame_s3_key": ev.get("frame_s3_key"),
            })
    score = max(0, 100 - sum(deductions.values()))
    level = "clean" if score >= 85 else ("minor" if score >= 60 else "flagged")
    return {"score": score, "level": level, "counts": counts, "violations": violations}


# ── ProctorSession — one per interview ──────────────────────────────────────────

class ProctorSession:
    """
    Lifecycle:
      attach_screen_track(track) — start the frame pump + sparse baseline
      handle_beacon(payload)     — Layer-1 signal from the browser
      screen_gone()              — server-side detection of the track vanishing
      stop()                     — cancel tasks, write the summary event

    Test seams: vision_fn / record_fn / upload_fn / frame_fn are injectable;
    production defaults hit Claude, Postgres and S3.
    """

    def __init__(
        self,
        interview_id: str | None,
        tenant_id: str | None,
        on_warning: Callable[[str], Awaitable[None]] | None = None,
        *,
        vision_fn: Callable[[bytes], Awaitable[dict | None]] | None = None,
        record_fn=None,
        upload_fn=None,
        frame_fn: Callable[[], bytes | None] | None = None,
        loop_time: Callable[[], float] | None = None,
    ) -> None:
        self.interview_id = interview_id
        self.tenant_id    = tenant_id
        self._on_warning  = on_warning
        self._vision_fn   = vision_fn or _vision_analyze
        self._record_fn   = record_fn or _db_record_event
        self._upload_fn   = upload_fn or _upload_evidence
        self._frame_fn    = frame_fn            # None → use the live frame buffer
        self._time        = loop_time or (lambda: asyncio.get_event_loop().time())

        self._latest_frame = None               # rtc.VideoFrame (rolling buffer)
        self._pump_task: asyncio.Task | None = None
        self._baseline_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()  # strong refs (GC safety)

        self._vision_calls   = 0
        self._violations     = 0
        self._warnings_given = 0
        self._last_burst_at: dict[str, float] = {}   # trigger type → loop time
        self._share_seen     = False
        self._stopped        = False

    # ── plumbing ────────────────────────────────────────────────────────────────

    def _spawn(self, coro, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def record_event(self, event_type: str, severity: str,
                     payload: dict | None = None, frame_key: str | None = None) -> None:
        if not self.interview_id or not self.tenant_id:
            return
        self._spawn(
            self._record_fn(self.interview_id, self.tenant_id, event_type,
                            severity, payload or {}, frame_key),
            name=f"proctor-ev-{event_type}",
        )

    # ── screen track / frame buffer ────────────────────────────────────────────

    def attach_screen_track(self, track) -> None:
        """Start pumping the screen-share track into the 1-frame buffer."""
        if self._stopped:
            return
        self._share_seen = True
        if self._pump_task and not self._pump_task.done():
            self._pump_task.cancel()
        self._pump_task = asyncio.create_task(self._pump(track), name="proctor-frame-pump")
        if (PROCTOR_VISION_ENABLED and BASELINE_INTERVAL_S > 0
                and (self._baseline_task is None or self._baseline_task.done())):
            self._baseline_task = asyncio.create_task(self._baseline(), name="proctor-baseline")
        logger.info("[proctor] screen track attached — frame buffer live",
                    extra={"interview_id": self.interview_id})

    async def _pump(self, track) -> None:
        """Keep only the LATEST decoded frame — near-zero memory, no backlog."""
        try:
            from livekit import rtc
            stream = rtc.VideoStream(track)
            async for ev in stream:
                self._latest_frame = ev.frame
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[proctor] frame pump ended: {e}")

    def screen_gone(self) -> None:
        """Server-side: the screen track vanished (unsubscribed/unpublished)."""
        if self._pump_task and not self._pump_task.done():
            self._pump_task.cancel()
        self._latest_frame = None

    def _grab_jpeg(self) -> bytes | None:
        """Latest frame → downscaled JPEG bytes. None if no frame available."""
        if self._frame_fn is not None:          # test seam
            return self._frame_fn()
        frame = self._latest_frame
        if frame is None:
            return None
        try:
            from livekit import rtc
            from PIL import Image
            rgba = frame.convert(rtc.VideoBufferType.RGBA)
            img = Image.frombytes("RGBA", (rgba.width, rgba.height), bytes(rgba.data))
            img = img.convert("RGB")
            img.thumbnail((FRAME_MAX_DIM, FRAME_MAX_DIM))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=JPEG_QUALITY)
            return buf.getvalue()
        except Exception as e:
            logger.warning(f"[proctor] frame->jpeg failed: {e}")
            return None

    # ── Layer 1: beacons ────────────────────────────────────────────────────────

    def handle_beacon(self, payload: dict) -> None:
        """Route one browser beacon: record it; suspicious ones trigger vision."""
        if self._stopped:
            return
        btype = str(payload.get("type", "")).strip()
        policy = BEACON_POLICY.get(btype)
        if policy is None:
            logger.debug(f"[proctor] unknown beacon ignored: {btype!r}")
            return
        severity, triggers_vision = policy

        extra = {k: v for k, v in payload.items() if k != "type"}
        if btype == "share_started":
            self._share_seen = True
        # A non-monitor surface (window/tab share) is itself a medium signal.
        if btype == "display_surface" and extra.get("surface") not in (None, "monitor"):
            severity = "medium"
        self.record_event(btype, severity, extra)
        logger.info(f"[proctor] beacon: {btype} ({severity})",
                    extra={"interview_id": self.interview_id})

        if triggers_vision:
            self._maybe_burst(trigger=btype)

    # ── Layer 2: event-triggered vision ─────────────────────────────────────────

    def _maybe_burst(self, trigger: str) -> None:
        """Cooldown-gated: capture now + follow-ups. Focus-returns get 1 frame."""
        if not PROCTOR_VISION_ENABLED or self._stopped:
            return
        if self._vision_calls >= MAX_VISION_CALLS:
            return
        now = self._time()
        # Group blur/hidden and focus/visible under shared cooldown families so
        # blur+hidden firing together (they do) costs one burst, not two.
        family = ("away" if trigger in ("tab_hidden", "window_blur")
                  else "back" if trigger in ("tab_visible", "window_focus")
                  else trigger)
        last = self._last_burst_at.get(family, -1e9)
        if now - last < BURST_COOLDOWN_S:
            return
        self._last_burst_at[family] = now
        followups = BURST_FOLLOWUPS if family == "away" else 0
        self._spawn(self._burst(trigger, followups), name=f"proctor-burst-{trigger}")

    async def _burst(self, trigger: str, followups: int) -> None:
        await self._analyze_one(trigger)
        for _ in range(followups):
            await asyncio.sleep(BURST_FOLLOWUP_GAP_S)
            if self._stopped or self._vision_calls >= MAX_VISION_CALLS:
                return
            await self._analyze_one(trigger + "+followup")

    async def _baseline(self) -> None:
        """Sparse safety net for passive split-screen cheating (no L1 signal)."""
        try:
            while not self._stopped:
                await asyncio.sleep(BASELINE_INTERVAL_S)
                if self._stopped or self._vision_calls >= MAX_VISION_CALLS:
                    return
                await self._analyze_one("baseline")
        except asyncio.CancelledError:
            raise

    async def _analyze_one(self, trigger: str) -> None:
        jpeg = self._grab_jpeg()
        if jpeg is None:
            return
        self._vision_calls += 1
        verdict = await self._vision_fn(jpeg)
        if not verdict:
            return
        if not verdict["suspicious"]:
            logger.debug(f"[proctor] frame clean (trigger={trigger})")
            return

        self._violations += 1
        frame_key = await self._upload_fn(self.interview_id, jpeg) if self.interview_id else None
        self.record_event(
            "vision_violation", "high",
            {"trigger": trigger, "apps": verdict["apps"], "reason": verdict["reason"],
             "violation_n": self._violations},
            frame_key,
        )
        logger.warning(
            f"[proctor] VIOLATION #{self._violations} (trigger={trigger}): "
            f"{verdict['reason']} apps={verdict['apps']}",
            extra={"interview_id": self.interview_id},
        )
        # Escalation ladder: warn from the 2nd confirmed violation, max twice.
        if (PROCTOR_VERBAL_WARNINGS and self._on_warning
                and self._violations >= WARN_AT_VIOLATIONS
                and self._warnings_given < MAX_WARNINGS):
            self._warnings_given += 1
            self._spawn(self._warn(), name="proctor-warning")

    async def _warn(self) -> None:
        try:
            await self._on_warning(
                "Without breaking the interview flow, give the candidate one brief, "
                "polite reminder to please stay on the interview screen and close "
                "other windows, as the session is monitored. Then continue with the "
                "current question."
            )
        except Exception as e:
            logger.warning(f"[proctor] verbal warning failed (non-fatal): {e}")

    # ── shutdown ────────────────────────────────────────────────────────────────

    def stop(self) -> None:
        """Cancel tasks and write the summary event (fire-and-forget)."""
        if self._stopped:
            return
        self._stopped = True
        for t in (self._pump_task, self._baseline_task):
            if t and not t.done():
                t.cancel()
        if self.interview_id and self.tenant_id:
            if not self._share_seen and PROCTOR_ENABLED:
                self.record_event("share_never_started", "high", {})
            self.record_event(
                "proctor_summary", "info",
                {"vision_calls": self._vision_calls,
                 "violations": self._violations,
                 "warnings_given": self._warnings_given,
                 "share_seen": self._share_seen},
            )
