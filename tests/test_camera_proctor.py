"""
Camera proctoring (Phase 16, Layer 1) — regression suite.

Face beacons routed through ProctorSession with all I/O stubbed: presence
prompt scheduling/cancellation, the multiple-faces warning ladder, camera
lifecycle events, integrity scoring, and the guarantee that NO camera beacon
ever triggers a Layer-2 vision call (that slot is reserved for the in-house
face-detection service).

Run:  venv\\Scripts\\python -m pytest tests/test_camera_proctor.py -q
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.realtime import proctor  # noqa: E402
from app.realtime.proctor import ProctorSession, compute_integrity  # noqa: E402


class Harness:
    def __init__(self, presence_delay=0.05):
        self.events: list[tuple] = []
        self.spoken: list[str] = []
        self.vision_calls = 0

        async def record(iv, tid, etype, sev, payload, fkey):
            self.events.append((etype, sev, payload))

        async def upload(iv, jpeg):
            return None

        async def vision(jpeg):
            self.vision_calls += 1
            return {"suspicious": False, "apps": [], "reason": ""}

        async def say(instr):
            self.spoken.append(instr)

        self.session = ProctorSession(
            "iv-cam", "tenant-1", on_warning=say,
            vision_fn=vision, record_fn=record, upload_fn=upload,
            frame_fn=lambda: b"jpg",
        )
        self._old = (proctor.PRESENCE_PROMPT_DELAY_S, proctor.PROCTOR_VERBAL_WARNINGS,
                     proctor.PROCTOR_VISION_ENABLED, proctor.PROCTOR_CAMERA_ENABLED,
                     proctor.PRESENCE_PROMPTS_MAX)
        proctor.PRESENCE_PROMPT_DELAY_S = presence_delay
        proctor.PROCTOR_VERBAL_WARNINGS = True
        proctor.PROCTOR_VISION_ENABLED = True
        proctor.PROCTOR_CAMERA_ENABLED = True
        proctor.PRESENCE_PROMPTS_MAX = 2

    def restore(self):
        (proctor.PRESENCE_PROMPT_DELAY_S, proctor.PROCTOR_VERBAL_WARNINGS,
         proctor.PROCTOR_VISION_ENABLED, proctor.PROCTOR_CAMERA_ENABLED,
         proctor.PRESENCE_PROMPTS_MAX) = self._old

    def types(self):
        return [e[0] for e in self.events]


async def settle(n=6):
    for _ in range(n):
        await asyncio.sleep(0)


class TestFaceBeacons:
    def test_face_beacons_recorded_no_vision(self):
        async def main():
            h = Harness()
            try:
                for b in ["camera_started", "face_lost", "face_returned",
                          "multiple_faces", "single_face_restored", "camera_stopped"]:
                    h.session.handle_beacon({"type": b})
                await settle()
                types = h.types()
                for b in ["camera_started", "face_lost", "multiple_faces", "camera_stopped"]:
                    assert b in types, b
                # Layer 1 ONLY: no camera beacon may trigger vision
                assert h.vision_calls == 0
            finally:
                h.restore()
        asyncio.run(main())

    def test_severities(self):
        async def main():
            h = Harness()
            try:
                h.session.handle_beacon({"type": "multiple_faces", "count": 2})
                h.session.handle_beacon({"type": "face_lost"})
                await settle()
                sev = {e[0]: e[1] for e in h.events}
                assert sev["multiple_faces"] == "high"
                assert sev["face_lost"] == "medium"
            finally:
                h.restore()
        asyncio.run(main())


class TestPresencePrompt:
    def test_face_lost_prompts_after_delay(self):
        async def main():
            h = Harness(presence_delay=0.03)
            try:
                h.session.handle_beacon({"type": "face_lost"})
                await asyncio.sleep(0.1)
                assert len(h.spoken) == 1
                assert "camera" in h.spoken[0].lower()
                assert "presence_prompt" in h.types()
            finally:
                h.restore()
        asyncio.run(main())

    def test_face_returned_cancels_prompt(self):
        async def main():
            h = Harness(presence_delay=0.2)
            try:
                h.session.handle_beacon({"type": "face_lost"})
                await asyncio.sleep(0.05)
                h.session.handle_beacon({"type": "face_returned", "away_s": 4})
                await asyncio.sleep(0.3)
                assert h.spoken == []                       # prompt was cancelled
                assert "presence_prompt" not in h.types()
            finally:
                h.restore()
        asyncio.run(main())

    def test_prompts_bounded_per_interview(self):
        async def main():
            h = Harness(presence_delay=0.02)
            try:
                for _ in range(5):
                    h.session.handle_beacon({"type": "face_lost"})
                    await asyncio.sleep(0.06)
                assert len(h.spoken) == proctor.PRESENCE_PROMPTS_MAX
            finally:
                h.restore()
        asyncio.run(main())

    def test_stop_cancels_pending_prompt(self):
        async def main():
            h = Harness(presence_delay=0.2)
            try:
                h.session.handle_beacon({"type": "face_lost"})
                await asyncio.sleep(0.02)
                h.session.stop()
                await asyncio.sleep(0.3)
                assert h.spoken == []
            finally:
                h.restore()
        asyncio.run(main())


class TestMultiFaceLadder:
    def test_warning_from_second_detection(self):
        async def main():
            h = Harness()
            try:
                h.session.handle_beacon({"type": "multiple_faces", "count": 2})
                await settle()
                assert h.spoken == []                       # 1st: logged only
                h.session.handle_beacon({"type": "multiple_faces", "count": 2})
                await settle(10)
                assert len(h.spoken) == 1                   # 2nd: Sarah warns
                assert "alone" in h.spoken[0].lower()
                h.session.handle_beacon({"type": "multiple_faces", "count": 3})
                h.session.handle_beacon({"type": "multiple_faces", "count": 3})
                await settle(10)
                assert len(h.spoken) == proctor.MAX_WARNINGS  # capped
            finally:
                h.restore()
        asyncio.run(main())


class TestCameraLifecycle:
    def test_camera_never_started_on_stop(self):
        async def main():
            h = Harness()
            try:
                h.session.stop()
                await settle()
                assert "camera_never_started" in h.types()
            finally:
                h.restore()
        asyncio.run(main())

    def test_no_flag_when_camera_seen_and_summary_fields(self):
        async def main():
            h = Harness()
            try:
                h.session.handle_beacon({"type": "camera_started"})
                h.session.handle_beacon({"type": "face_lost"})
                await settle()
                h.session.stop()
                await settle()
                assert "camera_never_started" not in h.types()
                summ = [e for e in h.events if e[0] == "proctor_summary"][0][2]
                assert summ["camera_seen"] is True
                assert summ["face_lost_episodes"] == 1
            finally:
                h.restore()
        asyncio.run(main())


class TestIntegrityCamera:
    def test_camera_rules(self):
        evs = [{"event_type": "multiple_faces", "payload": {"count": 2},
                "created_at": "10:01:00 UTC"},
               {"event_type": "face_lost"}, {"event_type": "face_lost"},
               {"event_type": "camera_stopped"}]
        r = compute_integrity(evs)
        assert r["score"] == 100 - 15 - 8 - 10
        assert len(r["violations"]) == 1
        assert "Second person" in r["violations"][0]["reason"]
        assert r["violations"][0]["trigger"] == "camera"

    def test_face_lost_cap(self):
        r = compute_integrity([{"event_type": "face_lost"}] * 30)
        assert r["score"] == 100 - 20                        # capped

    def test_multiple_faces_cap_flags(self):
        r = compute_integrity([{"event_type": "multiple_faces", "payload": {}}] * 5)
        assert r["score"] == 100 - 45
        assert r["level"] == "flagged"

    def test_camera_never_started_heavy(self):
        r = compute_integrity([{"event_type": "camera_never_started"}])
        assert r["score"] == 70
