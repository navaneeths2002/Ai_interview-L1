"""
Screen proctoring (Phase 15) — regression suite.

Pure-logic tests over ProctorSession with injected seams (no network, no DB,
no S3, no LiveKit): beacon routing, event-driven bursts with cooldowns/caps,
sparse-baseline gating, escalation ladder, and the integrity score.

Run:  venv\\Scripts\\python -m pytest tests/test_proctor.py -q
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.realtime import proctor  # noqa: E402
from app.realtime.proctor import ProctorSession, compute_integrity, _parse_vision  # noqa: E402


# ── harness ────────────────────────────────────────────────────────────────────

class Harness:
    """ProctorSession with every external dependency stubbed + a fake clock."""

    def __init__(self, verdicts=None, vision_enabled=True):
        self.events: list[tuple] = []          # (event_type, severity, payload, frame_key)
        self.warnings: list[str] = []
        self.vision_calls = 0
        self._verdicts = list(verdicts or [])
        self.now = 1000.0

        async def record(iv, tid, etype, sev, payload, fkey):
            self.events.append((etype, sev, payload, fkey))

        async def upload(iv, jpeg):
            return f"s3://fake/{len(self.events)}.jpg"

        async def vision(jpeg):
            self.vision_calls += 1
            return self._verdicts.pop(0) if self._verdicts else {"suspicious": False, "apps": [], "reason": ""}

        async def warn(instr):
            self.warnings.append(instr)

        self.session = ProctorSession(
            "iv-1", "tenant-1", on_warning=warn,
            vision_fn=vision, record_fn=record, upload_fn=upload,
            frame_fn=lambda: b"fake-jpeg-bytes",
            loop_time=lambda: self.now,
        )
        # force flags for deterministic tests regardless of local .env
        self._old = (proctor.PROCTOR_VISION_ENABLED, proctor.PROCTOR_VERBAL_WARNINGS)
        proctor.PROCTOR_VISION_ENABLED = vision_enabled
        proctor.PROCTOR_VERBAL_WARNINGS = True

    def restore(self):
        proctor.PROCTOR_VISION_ENABLED, proctor.PROCTOR_VERBAL_WARNINGS = self._old

    def event_types(self):
        return [e[0] for e in self.events]


async def settle(n=6):
    for _ in range(n):
        await asyncio.sleep(0)


def drive(coro):
    return asyncio.run(coro)


SUS = {"suspicious": True, "apps": ["ChatGPT"], "reason": "AI assistant visible"}


# ── beacons → events ───────────────────────────────────────────────────────────

class TestBeacons:
    def test_beacons_are_recorded_with_policy_severity(self):
        async def main():
            h = Harness()
            try:
                for b in ["share_started", "tab_hidden", "window_blur", "paste", "multi_monitor"]:
                    h.session.handle_beacon({"type": b})
                await settle()
                types = h.event_types()
                assert "share_started" in types and "tab_hidden" in types
                sev = {e[0]: e[1] for e in h.events}
                assert sev["share_started"] == "info"
                assert sev["tab_hidden"] == "medium"
                assert sev["share_started"] == "info"
            finally:
                h.restore()
        drive(main())

    def test_unknown_beacon_ignored(self):
        async def main():
            h = Harness()
            try:
                h.session.handle_beacon({"type": "evil_injection"})
                await settle()
                assert h.events == []
            finally:
                h.restore()
        drive(main())

    def test_non_monitor_surface_flagged_medium(self):
        async def main():
            h = Harness()
            try:
                h.session.handle_beacon({"type": "display_surface", "surface": "window"})
                await settle()
                assert h.events[0][1] == "medium"
            finally:
                h.restore()
        drive(main())


# ── event-driven bursts ────────────────────────────────────────────────────────

class TestBursts:
    def test_tab_hidden_triggers_vision(self):
        async def main():
            h = Harness(verdicts=[SUS])
            try:
                h.session.handle_beacon({"type": "tab_hidden"})
                await settle()
                assert h.vision_calls >= 1
                assert "vision_violation" in h.event_types()
                vio = [e for e in h.events if e[0] == "vision_violation"][0]
                assert vio[2]["apps"] == ["ChatGPT"]
                assert vio[3] and vio[3].startswith("s3://")   # evidence uploaded
            finally:
                h.restore()
        drive(main())

    def test_share_stopped_never_calls_vision(self):
        async def main():
            h = Harness()
            try:
                h.session.handle_beacon({"type": "share_stopped"})
                await settle()
                assert h.vision_calls == 0
                assert h.events[0][1] == "high"
            finally:
                h.restore()
        drive(main())

    def test_cooldown_collapses_blur_and_hidden(self):
        async def main():
            h = Harness()
            try:
                # blur + hidden fire together in browsers — same 'away' family
                h.session.handle_beacon({"type": "window_blur"})
                h.session.handle_beacon({"type": "tab_hidden"})
                await settle()
                first_calls = h.vision_calls
                assert first_calls == 1          # one burst frame (followups sleep)
                # after the cooldown, a new trigger bursts again
                h.now += proctor.BURST_COOLDOWN_S + 1
                h.session.handle_beacon({"type": "tab_hidden"})
                await settle()
                assert h.vision_calls == first_calls + 1
            finally:
                h.restore()
        drive(main())

    def test_hard_cap_stops_vision(self):
        async def main():
            h = Harness()
            try:
                h.session._vision_calls = proctor.MAX_VISION_CALLS
                h.session.handle_beacon({"type": "tab_hidden"})
                await settle()
                assert h.vision_calls == 0
            finally:
                h.restore()
        drive(main())

    def test_vision_disabled_no_calls(self):
        async def main():
            h = Harness(vision_enabled=False)
            try:
                h.session.handle_beacon({"type": "tab_hidden"})
                await settle()
                assert h.vision_calls == 0
                assert "tab_hidden" in h.event_types()   # event still recorded
            finally:
                h.restore()
        drive(main())


# ── escalation ladder ──────────────────────────────────────────────────────────

class TestEscalation:
    def test_warning_from_second_violation_max_twice(self):
        async def main():
            h = Harness(verdicts=[SUS, SUS, SUS, SUS])
            try:
                for i in range(4):
                    h.session.handle_beacon({"type": "tab_hidden"})
                    await settle()
                    h.now += proctor.BURST_COOLDOWN_S + 1
                await settle()
                assert h.session._violations == 4
                assert len(h.warnings) == proctor.MAX_WARNINGS   # warned exactly twice
            finally:
                h.restore()
        drive(main())

    def test_clean_frames_no_warning(self):
        async def main():
            h = Harness()   # default verdict: not suspicious
            try:
                h.session.handle_beacon({"type": "tab_hidden"})
                await settle()
                assert h.warnings == []
                assert "vision_violation" not in h.event_types()
            finally:
                h.restore()
        drive(main())


# ── shutdown summary ───────────────────────────────────────────────────────────

class TestStop:
    def test_stop_writes_summary_and_share_never_started(self):
        async def main():
            h = Harness()
            try:
                h.session.stop()
                await settle()
                types = h.event_types()
                assert "proctor_summary" in types
                assert "share_never_started" in types   # no share ever seen
                # stop is idempotent + blocks further beacons
                h.session.stop()
                h.session.handle_beacon({"type": "tab_hidden"})
                await settle()
                assert types.count("proctor_summary") == 1
            finally:
                h.restore()
        drive(main())

    def test_no_share_never_started_when_share_seen(self):
        async def main():
            h = Harness()
            try:
                h.session.handle_beacon({"type": "share_started"})
                await settle()
                h.session.stop()
                await settle()
                assert "share_never_started" not in h.event_types()
            finally:
                h.restore()
        drive(main())


# ── integrity score (report side) ─────────────────────────────────────────────

class TestIntegrity:
    def test_clean(self):
        r = compute_integrity([])
        assert r["score"] == 100 and r["level"] == "clean"

    def test_minor_signals(self):
        evs = [{"event_type": "tab_hidden"}] * 2 + [{"event_type": "window_blur"}]
        r = compute_integrity(evs)
        assert r["score"] == 100 - 6 - 2
        assert r["level"] == "clean"

    def test_violations_flag(self):
        evs = [
            {"event_type": "vision_violation",
             "payload": {"trigger": "tab_hidden", "apps": ["ChatGPT"], "reason": "AI visible"},
             "frame_s3_key": "k1", "created_at": "10:00:00 UTC"},
            {"event_type": "vision_violation", "payload": {}, "created_at": ""},
            {"event_type": "share_stopped"},
        ]
        r = compute_integrity(evs)
        assert r["score"] == 100 - 40 - 10
        assert r["level"] == "flagged"    # two confirmed violations + share stop
        assert len(r["violations"]) == 2
        assert r["violations"][0]["apps"] == ["ChatGPT"]

    def test_per_type_caps(self):
        evs = [{"event_type": "tab_hidden"}] * 50      # cap 18, not 150
        r = compute_integrity(evs)
        assert r["score"] == 100 - 18

    def test_share_never_started_heavy(self):
        r = compute_integrity([{"event_type": "share_never_started"}])
        assert r["score"] == 70 and r["level"] == "minor"


# ── vision verdict parsing ─────────────────────────────────────────────────────

class TestVisionParse:
    def test_parse(self):
        ok = _parse_vision('{"suspicious": true, "apps": ["ChatGPT","Google"], "reason": "AI open"}')
        assert ok["suspicious"] is True and ok["apps"] == ["ChatGPT", "Google"]
        assert _parse_vision('prefix {"suspicious": false, "apps": [], "reason": ""} suffix')["suspicious"] is False
        assert _parse_vision("garbage") is None
        assert _parse_vision('{"suspicious": "maybe"}') is None
