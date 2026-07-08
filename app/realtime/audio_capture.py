"""
Candidate Audio Capture (local WAV)
====================================
Self-contained unit — records the CANDIDATE's audio track to a local WAV file
so the post-interview voice-analysis engine (parselmouth/librosa) can score
delivery (pace, pitch, energy, hesitation) for voice-heavy roles.

Design mirrors crash_recovery.py: fully isolated, the agent only calls thin
hooks (start / stop), and EVERY failure is swallowed — audio capture must never
disturb the live interview pipeline.

Phase: "Option B" — store locally now (no LiveKit Egress, no S3). The saved path
is written to interviews.recording_s3_key. When we move to S3 later, only the
final upload step changes; nothing else in the pipeline does.

Frames arrive from LiveKit as int16 PCM. We open the WAV lazily on the first
frame (so we adopt the track's real sample_rate / channels) and append raw PCM.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import wave

logger = logging.getLogger(__name__)

# Where recordings are written. Local dir now; swap for S3 upload later.
# Absolute path derived from the project root so it works from any cwd.
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_RECORDINGS_DIR = pathlib.Path(
    os.environ.get("RECORDINGS_DIR", str(_PROJECT_ROOT / "recordings"))
)

# Master toggle — set VOICE_CAPTURE_ENABLED=false to disable recording entirely.
CAPTURE_ENABLED = os.environ.get("VOICE_CAPTURE_ENABLED", "true").strip().lower() not in (
    "false", "0", "no",
)

# Recordings older than this are auto-deleted by the scheduler's retention job.
# Local WAVs are large (~100 MB / 20 min), so keep the window short until S3.
RECORDINGS_RETENTION_DAYS = int(os.environ.get("RECORDINGS_RETENTION_DAYS", "7"))

# File extensions the retention sweep is allowed to delete (never touches others).
_AUDIO_EXTS = (".wav", ".opus", ".mp3", ".ogg", ".m4a", ".flac")


def _recording_path(interview_id: str) -> pathlib.Path:
    _RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    return _RECORDINGS_DIR / f"{interview_id}.wav"


def cleanup_old_recordings(max_age_days: int | None = None) -> int:
    """
    Delete recordings older than `max_age_days` (default RECORDINGS_RETENTION_DAYS).
    Returns the number of files deleted. Filesystem-only, fully self-contained,
    and safe to run from the scheduler — every failure is swallowed.
    Set RECORDINGS_RETENTION_DAYS=0 to disable deletion entirely.
    """
    import time

    days = RECORDINGS_RETENTION_DAYS if max_age_days is None else max_age_days
    if days <= 0 or not _RECORDINGS_DIR.exists():
        return 0

    cutoff = time.time() - days * 86_400
    deleted = 0
    try:
        for f in _RECORDINGS_DIR.iterdir():
            try:
                if (f.is_file()
                        and f.suffix.lower() in _AUDIO_EXTS
                        and f.stat().st_mtime < cutoff):
                    f.unlink()
                    deleted += 1
            except Exception:
                pass  # a single stubborn file must not stop the sweep
    except Exception as e:
        logger.warning(f"[audio-capture] retention sweep failed (non-fatal): {e}")

    if deleted:
        logger.info(
            f"[audio-capture] retention: deleted {deleted} recording(s) older than {days}d"
        )
    return deleted


class CandidateAudioRecorder:
    """
    Reads frames off a LiveKit audio track and appends them to a WAV file.

    Usage (from the agent):
        rec = CandidateAudioRecorder(interview_id)
        rec.start(track)          # on candidate audio track_subscribed
        path = await rec.stop()   # in on_shutdown, before evaluation
    """

    def __init__(self, interview_id: str | None) -> None:
        self.interview_id = interview_id
        self._task: asyncio.Task | None = None
        self._wave: wave.Wave_write | None = None
        self._path: pathlib.Path | None = None
        self._frames_written = 0
        self._started = False

    # ── Public hooks ──────────────────────────────────────────────────────────
    def start(self, track) -> None:
        """Begin recording the given audio track. Idempotent + safe no-op on error."""
        if not CAPTURE_ENABLED:
            logger.info("[audio-capture] start skipped — VOICE_CAPTURE_ENABLED is off")
            return
        if not self.interview_id:
            logger.warning("[audio-capture] start skipped — interview_id is None")
            return
        if self._started:
            # Already recording (e.g. a reconnect re-subscribes the track) — keep
            # the first stream; a second file would truncate the first.
            logger.info("[audio-capture] start skipped — already recording")
            return
        try:
            # Imported lazily so a livekit import hiccup can't break module load.
            from livekit import rtc
            self._started = True
            self._path = _recording_path(self.interview_id)
            stream = rtc.AudioStream(track)
            logger.info(
                "[audio-capture] AudioStream created — spawning pump",
                extra={"interview_id": self.interview_id},
            )
            self._task = asyncio.create_task(
                self._pump(stream), name=f"audio-capture-{self.interview_id}"
            )
            logger.info(
                f"[audio-capture] recording candidate → {self._path}",
                extra={"interview_id": self.interview_id},
            )
        except Exception as e:
            self._started = False
            logger.warning(f"[audio-capture] start failed (non-fatal): {e}")

    async def stop(self) -> str | None:
        """Stop recording, close the file, return the local WAV path (or None)."""
        if not self._started:
            return None
        try:
            if self._task and not self._task.done():
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
        except Exception as e:
            logger.debug(f"[audio-capture] task cancel ignored: {e}")

        self._close_wave()

        if self._path and self._frames_written > 0:
            logger.info(
                f"[audio-capture] saved {self._frames_written} frames → {self._path}",
                extra={"interview_id": self.interview_id},
            )
            return str(self._path)

        logger.info(
            "[audio-capture] no candidate audio captured — nothing saved",
            extra={"interview_id": self.interview_id},
        )
        return None

    # ── Internal ──────────────────────────────────────────────────────────────
    async def _pump(self, stream) -> None:
        """Read frames off the stream and append raw PCM to the WAV file."""
        logger.info(
            f"[audio-capture] pump started — reading candidate frames",
            extra={"interview_id": self.interview_id},
        )
        first = True
        try:
            async for event in stream:
                # Different livekit versions yield an event-with-.frame or the frame.
                frame = getattr(event, "frame", event)
                if frame is None:
                    continue
                if first:
                    first = False
                    logger.info(
                        "[audio-capture] first frame received — "
                        f"sr={getattr(frame, 'sample_rate', None)} "
                        f"ch={getattr(frame, 'num_channels', None)} "
                        f"samples={getattr(frame, 'samples_per_channel', None)}",
                        extra={"interview_id": self.interview_id},
                    )
                self._ensure_wave(frame)
                if self._wave is not None:
                    try:
                        data = frame.data
                        pcm = data.tobytes() if hasattr(data, "tobytes") else bytes(data)
                        self._wave.writeframes(pcm)
                        self._frames_written += 1
                    except Exception as we:
                        if self._frames_written == 0:
                            logger.warning(
                                f"[audio-capture] frame write error (non-fatal): {we}",
                                extra={"interview_id": self.interview_id},
                            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[audio-capture] pump stopped (non-fatal): {e}")
        finally:
            logger.info(
                f"[audio-capture] pump ended — {self._frames_written} frames total",
                extra={"interview_id": self.interview_id},
            )
            try:
                await stream.aclose()
            except Exception:
                pass

    def _ensure_wave(self, frame) -> None:
        """Open the WAV lazily on the first frame using the track's real format."""
        if self._wave is not None or self._path is None:
            return
        try:
            sample_rate = int(getattr(frame, "sample_rate", 48000) or 48000)
            channels = int(getattr(frame, "num_channels", 1) or 1)
            wf = wave.open(str(self._path), "wb")
            wf.setnchannels(channels)
            wf.setsampwidth(2)  # int16 PCM
            wf.setframerate(sample_rate)
            self._wave = wf
            logger.debug(
                f"[audio-capture] WAV opened @ {sample_rate}Hz x{channels}",
                extra={"interview_id": self.interview_id},
            )
        except Exception as e:
            logger.warning(f"[audio-capture] could not open WAV (non-fatal): {e}")

    def _close_wave(self) -> None:
        if self._wave is not None:
            try:
                self._wave.close()
            except Exception:
                pass
            self._wave = None
