"""
Simli Avatar Session
=====================
Bridges ElevenLabs TTS audio → Simli face-rendering servers → LiveKit room video track.

Architecture (audio-clean design):
  1. SimliAudioForwarder sits in the livekit-agents audio output chain.
     capture_frame() forwards audio to BOTH:
       a) Simli (for lip-sync rendering)
       b) next_in_chain (the normal room audio output) so the candidate still hears the voice

  2. AvatarSession starts the SimliClient, then a VIDEO-ONLY renderer joins our
     interview room as "simli-avatar" and publishes ONLY the face video track.
     No Simli audio is published — the candidate hears the agent's direct audio
     (via next_in_chain above), avoiding any echo/double-audio.

  3. In the browser, interview.html subscribes to the "simli-avatar" participant's
     video track and renders it in the avatar circle.  The plasma canvas is hidden.
"""

import asyncio
import audioop
import logging

import numpy as np
from livekit import rtc
from livekit.agents import io
from livekit.api import AccessToken, VideoGrants

from simli import SimliClient, SimliConfig
from simli.simli import TransportMode

logger = logging.getLogger(__name__)

_SIMLI_TARGET_RATE = 16_000   # Hz — Simli expects PCM16 16 kHz mono
_SIMLI_CHUNK_BYTES = 6_000    # ~187 ms per chunk
_FPS               = 30


# ── PCM resampler ──────────────────────────────────────────────────────────────

def _resample(data: bytes, src_rate: int) -> bytes:
    """Resample PCM-16 mono to 16 000 Hz using stdlib audioop (Python 3.12)."""
    if src_rate == _SIMLI_TARGET_RATE:
        return data
    resampled, _ = audioop.ratecv(data, 2, 1, src_rate, _SIMLI_TARGET_RATE, None)
    return resampled


# ── Custom AudioOutput — taps TTS stream and forwards to Simli ─────────────────

class SimliAudioForwarder(io.AudioOutput):
    """
    Sits in the livekit-agents audio chain BEFORE the room output.
    For every PCM frame:
      • Resamples → 16 kHz mono → sends to Simli in 6 kB chunks (lip sync).
      • Calls next_in_chain so the candidate hears the voice normally.
    """

    def __init__(
        self,
        simli_client: SimliClient,
        next_in_chain: io.AudioOutput,
    ) -> None:
        super().__init__(
            label="SimliForwarder",
            capabilities=io.AudioOutputCapabilities(pause=False),
            next_in_chain=next_in_chain,
        )
        self._simli = simli_client
        self._buf   = bytearray()

    # ── AudioOutput implementation ────────────────────────────────────────────

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)   # bookkeeping only

        # ── Room audio FIRST — candidate must always hear the AI voice ────────
        # This runs before Simli processing so a Simli error can never silence
        # the candidate's audio (BUG 3 / BUG 4 fix).
        if self.next_in_chain:
            try:
                await self.next_in_chain.capture_frame(frame)
            except Exception as e:
                logger.warning(f"[audio] next_in_chain error (candidate may miss audio): {e}")

        # ── Simli lip-sync path — best-effort, errors are non-fatal ──────────
        try:
            raw = bytes(frame.data)
            if frame.num_channels == 2:
                raw = audioop.tomono(raw, 2, 0.5, 0.5)

            pcm16 = _resample(raw, frame.sample_rate)

            self._buf.extend(pcm16)
            while len(self._buf) >= _SIMLI_CHUNK_BYTES:
                chunk = bytes(self._buf[:_SIMLI_CHUNK_BYTES])
                self._buf = self._buf[_SIMLI_CHUNK_BYTES:]
                asyncio.create_task(self._safe_send(chunk))
        except Exception as e:
            logger.debug(f"[simli] capture_frame error: {e}")

    def flush(self) -> None:
        if self._buf:
            asyncio.create_task(self._safe_send(bytes(self._buf)))
            self._buf = bytearray()
        if self.next_in_chain:
            self.next_in_chain.flush()

    def clear_buffer(self) -> None:
        self._buf = bytearray()
        asyncio.create_task(self._safe_clear())
        if self.next_in_chain:
            self.next_in_chain.clear_buffer()

    async def _safe_send(self, data: bytes) -> None:
        try:
            await self._simli.send(data)
        except Exception as e:
            logger.debug(f"[simli] send error: {e}")

    async def _safe_clear(self) -> None:
        try:
            await self._simli.clearBuffer()
        except Exception as e:
            logger.debug(f"[simli] clearBuffer error: {e}")


# ── Video-only LiveKit publisher ────────────────────────────────────────────────

class _VideoOnlyPublisher:
    """
    Connects to the interview LiveKit room as "simli-avatar" and publishes
    ONLY a video track.  No audio track is published, so there is no risk
    of double-audio in the browser.

    Pumps video frames from SimliClient directly into the VideoSource using
    capture_frame (no AVSynchronizer needed since we have no audio to sync).
    """

    WIDTH  = 512
    HEIGHT = 512

    def __init__(
        self,
        simli_client: SimliClient,
        room_url: str,
        room_token: str,
    ) -> None:
        self._client     = simli_client
        self._room_url   = room_url
        self._room_token = room_token
        self._room       = rtc.Room()
        self._video_src  = rtc.VideoSource(self.WIDTH, self.HEIGHT)
        self._video_track = rtc.LocalVideoTrack.create_video_track(
            "SimliVideo", self._video_src
        )

    async def connect(self) -> None:
        await self._room.connect(url=self._room_url, token=self._room_token)
        pub_opts = rtc.TrackPublishOptions(
            source=rtc.TrackSource.SOURCE_CAMERA,
            simulcast=False,
            video_encoding=rtc.VideoEncoding(
                max_framerate=_FPS,
                max_bitrate=1_500_000,
            ),
        )
        await self._room.local_participant.publish_track(self._video_track, pub_opts)
        logger.info("[avatar] video track published to room")

    async def render_loop(self) -> None:
        """
        Pump video frames from Simli → LiveKit room. Runs until Simli stops.

        CRITICAL FIX: the numpy→bytes conversion (frame.to_ndarray().tobytes())
        is CPU-heavy. Done on the event loop at 30fps it STARVES the agent's
        audio-input processing, so the candidate's microphone audio never gets
        processed (VAD never fires, "input speech hasn't started" forever).

        We offload the heavy conversion to a thread pool and yield to the loop
        after every frame, so audio frames are always processed in between.
        """
        loop = asyncio.get_running_loop()

        def _convert(f) -> tuple[int, int, bytes]:
            # Runs in a worker thread — does NOT block the event loop.
            return f.width, f.height, f.to_ndarray().tobytes()

        async for frame in self._client.getVideoStreamIterator("yuva420p"):
            if frame is None:
                break
            try:
                # Heavy CPU work offloaded to a thread → event loop stays free
                width, height, data = await loop.run_in_executor(None, _convert, frame)
                lk_frame = rtc.VideoFrame(
                    width,
                    height,
                    rtc.VideoBufferType.I420A,
                    data,
                )
                self._video_src.capture_frame(lk_frame)
                # Yield control so queued audio-input tasks run between frames
                await asyncio.sleep(0)
            except Exception as e:
                logger.debug(f"[avatar] frame error: {e}")

    async def disconnect(self) -> None:
        try:
            await self._room.disconnect()
        except Exception:
            pass


# ── Token helper ────────────────────────────────────────────────────────────────

def _make_avatar_token(room_name: str, api_key: str, api_secret: str) -> str:
    return (
        AccessToken(api_key=api_key, api_secret=api_secret)
        .with_identity("simli-avatar")
        .with_name("AI Interviewer")
        .with_grants(
            VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=False,
            )
        )
        .to_jwt()
    )


# ── Public AvatarSession ────────────────────────────────────────────────────────

class AvatarSession:
    """
    Manages the full Simli avatar lifecycle for one interview room.

    Call start() after session.start().
    The returned SimliAudioForwarder should replace session.output.audio.
    """

    def __init__(self) -> None:
        self._client:       SimliClient | None          = None
        self._publisher:    _VideoOnlyPublisher | None  = None
        self._render_task:  asyncio.Task | None         = None

    async def start(
        self,
        room_name: str,
        lk_url: str,
        lk_api_key: str,
        lk_api_secret: str,
        simli_api_key: str,
        simli_face_id: str,
        current_audio_output: io.AudioOutput | None,
    ) -> "SimliAudioForwarder | None":
        """
        Start the Simli session.
        Returns a SimliAudioForwarder to insert into the audio chain,
        or None if avatar is disabled / start fails.
        """
        if not simli_api_key or not simli_face_id:
            logger.warning("[avatar] SIMLI_API_KEY or SIMLI_FACE_ID not set — avatar disabled")
            return None

        try:
            config = SimliConfig(
                faceId=simli_face_id,
                handleSilence=True,
                maxSessionLength=3600,
                maxIdleTime=600,
            )
            self._client = SimliClient(
                api_key=simli_api_key,
                config=config,
                transport_mode=TransportMode.P2P,
                retry_count=1,   # fail-fast: P2P only, don't retry with LIVEKIT transport
            )

            logger.info("[avatar] starting SimliClient…")
            await self._client.start()
            await self._client.sendSilence(0.5)   # bootstrap
            logger.info("[avatar] SimliClient started")

            # Build the video-only publisher
            avatar_token = _make_avatar_token(room_name, lk_api_key, lk_api_secret)
            self._publisher = _VideoOnlyPublisher(
                simli_client=self._client,
                room_url=lk_url,
                room_token=avatar_token,
            )
            await self._publisher.connect()

            # Start the video render loop as background task
            self._render_task = asyncio.create_task(
                self._publisher.render_loop(),
                name="simli-render",
            )

            # Detect silent render loop exit so we can log it.
            # The browser falls back to the plasma canvas when video stops.
            def _on_render_done(task: asyncio.Task) -> None:
                if task.cancelled():
                    return  # normal stop() shutdown — expected
                exc = task.exception() if not task.cancelled() else None
                if exc:
                    logger.warning(f"[avatar] render loop crashed: {exc} — video gone black")
                else:
                    logger.warning(
                        "[avatar] render loop exited normally — Simli stream ended "
                        "(video will be black; browser plasma canvas fallback active)"
                    )

            self._render_task.add_done_callback(_on_render_done)

            # Build the audio forwarder (Simli side + room side via next_in_chain)
            forwarder = SimliAudioForwarder(
                simli_client=self._client,
                next_in_chain=current_audio_output,  # type: ignore[arg-type]
            )
            logger.info("[avatar] ready — video-only track live, audio forwarder ready")
            return forwarder

        except Exception as e:
            logger.error(f"[avatar] failed to start: {e}", exc_info=True)
            await self._cleanup()
            return None

    async def stop(self) -> None:
        logger.info("[avatar] stopping…")
        await self._cleanup()

    async def _cleanup(self) -> None:
        if self._render_task and not self._render_task.done():
            self._render_task.cancel()
            try:
                await self._render_task
            except asyncio.CancelledError:
                pass

        if self._publisher:
            await self._publisher.disconnect()
            self._publisher = None

        if self._client:
            try:
                await self._client.stop()
            except Exception:
                pass
            self._client = None
