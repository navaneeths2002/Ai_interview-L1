import asyncio
import logging
import pathlib
import random
import uuid
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

from app.core.logging_config import get_logger

# Silence chatty WebRTC / ICE / charset debug spam
logging.getLogger("aiortc").setLevel(logging.WARNING)
logging.getLogger("aiortc.rtcdtlstransport").setLevel(logging.WARNING)
logging.getLogger("aioice").setLevel(logging.WARNING)
logging.getLogger("aioice.ice").setLevel(logging.WARNING)
logging.getLogger("charset_normalizer").setLevel(logging.WARNING)
logging.getLogger("root").setLevel(logging.WARNING)  # suppress lk.agent.session noise

logger = get_logger(__name__)

# ── FIX 1: Load .env using absolute path so it always works regardless of   ──
# ── working directory. Plain load_dotenv() fails when agent is started from ──
# ── a different directory, leaving DATABASE_URL empty and silently          ──
# ── disabling all transcript saving.                                        ──
_env_path = pathlib.Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=_env_path, override=False)

from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli
from livekit.agents import llm
from livekit.agents.voice import room_io
from livekit.plugins import deepgram, elevenlabs, anthropic, silero, simli
from livekit.plugins.elevenlabs import VoiceSettings
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.realtime.interview_graph import build_interview_graph, make_initial_state, InterviewState


# ── Base system prompt ─────────────────────────────────────────────────────────

BASE_SYSTEM_PROMPT = """You are a professional HR interviewer conducting an L1 screening call.

Rules:
- Be friendly, natural, engaging, warm, professional, and conversational
- Ask ONE question at a time — never stack two questions in one turn
- Keep your responses short — 1 to 2 sentences maximum
- Listen carefully and ask natural follow-up questions when needed
- Do not reveal scoring or internal evaluation
- When all information is gathered, thank the candidate and close professionally

Natural behavior (follow these exactly):
- Occasionally begin responses with natural acknowledgments: "Right,", "I see,", "Got it,", "Sure,", "Okay,", "Absolutely,"
- When noting important info (salary, notice period) say "Noted, thank you" or "Let me note that down"
- Use natural transitions: "Great, moving on —", "Perfect, and —", "Wonderful, so —"
- If an answer is vague or incomplete, ask naturally: "Could you tell me a bit more about that?"
- Vary your phrasing — never ask the same question the same way twice
- Occasionally use a brief pause phrase like "Hmm, interesting" before a follow-up

Handling typed text and spelling corrections:
- The candidate may type their response in a chat box instead of speaking — treat typed messages exactly like spoken ones
- When the candidate spells out a name or word letter-by-letter (e.g. "Navaneeth — N-A-V-A-N-E-E-T-H"), confirm by saying "Got it — [assembled name], thank you"
- When the candidate corrects a name, acknowledge naturally and use the corrected spelling going forward
"""

_FILLERS   = ["Hmm.", "Right.", "I see.", "Okay.", "Sure.", "Got it.", "Aha!", "Gotcha."]
_MISHEAR   = [
    "Sorry, I didn't quite catch that — could you say that again?",
    "Apologies, I missed that — could you repeat it?",
    "Sorry about that, could you say that once more?",
]
_VALID_SHORT = {"yes", "no", "yeah", "nope", "okay", "ok", "sure", "nah", "yep"}


# ── Database helpers ───────────────────────────────────────────────────────────

_agent_engine          = None
_agent_session_factory = None


def _get_db_factory():
    global _agent_engine, _agent_session_factory
    if _agent_session_factory is None:
        db_url = os.environ.get("DATABASE_URL", "")
        if not db_url:
            logger.error(
                "[db] DATABASE_URL is empty — all DB operations disabled. "
                "Ensure .env exists at project root and contains DATABASE_URL."
            )
            return None
        _agent_engine = create_async_engine(
            db_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,     # validates connections — prevents stale silent failures
        )
        _agent_session_factory = async_sessionmaker(
            bind=_agent_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        logger.info("[db] agent DB engine created")
    return _agent_session_factory


async def _load_interview_context(interview_id: str, max_retries: int = 4) -> dict:
    """
    Load tenant_id, candidate_name, skills/gaps from DB with retry backoff.
    Returns safe defaults only after all retries fail.
    """
    defaults = {
        "tenant_id":      None,
        "candidate_name": "the candidate",
        "skills_to_probe": [],
        "gaps_to_probe":  [],
    }
    factory = _get_db_factory()
    if not factory:
        return defaults

    backoff = [0.5, 1.0, 2.0, 4.0]
    for attempt in range(1, max_retries + 1):
        try:
            async with factory() as session:
                row = (await session.execute(
                    text("""
                        SELECT i.tenant_id,
                               c.first_name || ' ' || COALESCE(c.last_name, '') AS candidate_name
                        FROM interviews i
                        JOIN candidates c ON c.id = i.candidate_id
                        WHERE i.id = :id
                    """),
                    {"id": interview_id},
                )).fetchone()

                if not row:
                    logger.warning(
                        "[context] interview row not found — check room name matches interview_id",
                        extra={"interview_id": interview_id},
                    )
                    return defaults

                tenant_id      = row[0]
                candidate_name = (row[1] or "the candidate").strip()

                ctx_row = (await session.execute(
                    text("""
                        SELECT gaps_to_probe, skills_to_validate
                        FROM interview_contexts
                        WHERE interview_id = :id
                        LIMIT 1
                    """),
                    {"id": interview_id},
                )).fetchone()

                result = {
                    "tenant_id":       tenant_id,
                    "candidate_name":  candidate_name,
                    "gaps_to_probe":   list(ctx_row[0] or []) if ctx_row else [],
                    "skills_to_probe": list(ctx_row[1] or []) if ctx_row else [],
                }
                logger.info(
                    f"[context] loaded — tenant={tenant_id} candidate={candidate_name}",
                    extra={"interview_id": interview_id},
                )
                return result

        except Exception as e:
            wait = backoff[min(attempt - 1, len(backoff) - 1)]
            if attempt < max_retries:
                logger.warning(
                    f"[context] DB lookup failed (attempt {attempt}/{max_retries}), "
                    f"retrying in {wait}s: {e}",
                    extra={"interview_id": interview_id},
                )
                await asyncio.sleep(wait)
            else:
                logger.error(
                    f"[context] DB lookup FAILED after {max_retries} attempts — "
                    f"transcript saving disabled. Error: {e}",
                    extra={"interview_id": interview_id},
                    exc_info=True,
                )

    return defaults


async def _save_transcript(
    interview_id: str,
    tenant_id:    str,
    speaker:      str,
    message:      str,
    spoken_at:    datetime,
    node:         str | None = None,
) -> None:
    """
    Upsert one turn into interview_transcripts.

    FIX 2: Pass turn data as a Python list (not JSON string) with explicit
    PG_JSONB type binding so asyncpg sends it as jsonb — not text — avoiding
    the 'function jsonb_concat(jsonb, text) does not exist' error.
    """
    if not message.strip():
        return
    factory = _get_db_factory()
    if not factory:
        return

    now       = datetime.now(timezone.utc)
    turn_data = [{
        "speaker":   speaker,
        "message":   message.strip(),
        "spoken_at": spoken_at.isoformat(),
        "node":      node,
    }]

    try:
        async with factory() as session:
            await session.execute(
                text("""
                    INSERT INTO interview_transcripts
                        (id, tenant_id, interview_id, turns, turn_count, created_at, updated_at)
                    VALUES
                        (:id, :tenant_id, :interview_id, :turn_json, 1, :now, :now)
                    ON CONFLICT (interview_id) DO UPDATE SET
                        turns      = interview_transcripts.turns || :turn_json,
                        turn_count = interview_transcripts.turn_count + 1,
                        updated_at = :now
                """).bindparams(
                    bindparam("turn_json", type_=PG_JSONB)
                ),
                {
                    "id":           str(uuid.uuid4()),
                    "tenant_id":    tenant_id,
                    "interview_id": interview_id,
                    "turn_json":    turn_data,
                    "now":          now,
                },
            )
            await session.commit()
            logger.info(
                f"[transcript] ✓ saved {speaker} turn",
                extra={"interview_id": interview_id},
            )
    except Exception as e:
        logger.error(
            f"[transcript] ✗ FAILED saving {speaker} turn: {e}",
            extra={"interview_id": interview_id},
            exc_info=True,
        )


async def _update_interview_status(
    interview_id:     str,
    status:           str,
    started_at:       datetime | None = None,
    ended_at:         datetime | None = None,
    duration_seconds: int | None      = None,
) -> None:
    factory = _get_db_factory()
    if not factory:
        return
    now = datetime.now(timezone.utc)
    try:
        async with factory() as session:
            await session.execute(
                text("""
                    UPDATE interviews
                    SET    status           = :status,
                           updated_at       = :now,
                           started_at       = COALESCE(:started_at,       started_at),
                           ended_at         = COALESCE(:ended_at,         ended_at),
                           duration_seconds = COALESCE(:duration_seconds, duration_seconds)
                    WHERE  id = :interview_id
                """),
                {
                    "status":           status,
                    "interview_id":     interview_id,
                    "now":              now,
                    "started_at":       started_at,
                    "ended_at":         ended_at,
                    "duration_seconds": duration_seconds,
                },
            )
            await session.commit()
        logger.info(
            "Interview status updated",
            extra={"interview_id": interview_id, "status": status},
        )
    except Exception as e:
        logger.error(
            "Interview status update failed",
            extra={"interview_id": interview_id, "status": status, "error": str(e)},
        )


# ── Text extraction ────────────────────────────────────────────────────────────

def _extract_text(message) -> str:
    """
    Extract plain text from a ChatMessage across all livekit-agents SDK versions.
    Handles str, objects with .text, objects with .content.
    """
    if not message:
        return ""

    content = getattr(message, "content", None)
    if content is None:
        return ""

    parts = content if isinstance(content, list) else [content]

    for part in parts:
        if isinstance(part, str):
            return part
        if hasattr(part, "text") and isinstance(getattr(part, "text", None), str):
            return part.text
        if hasattr(part, "content") and isinstance(getattr(part, "content", None), str):
            return part.content

    return ""


# ── Agent ──────────────────────────────────────────────────────────────────────

class HRInterviewAgent(Agent):
    """
    LiveKit Agent for AI HR interviews.
    - LangGraph state machine drives interview stages
    - Transcript saved via conversation_item_added event (FIX 3)
    - Natural human behaviour (fillers, mishear guard, pre-response delay)
    """

    def __init__(
        self,
        graph_state:  InterviewState,
        interview_id: str | None = None,
        tenant_id:    str | None = None,
    ) -> None:
        super().__init__(instructions=BASE_SYSTEM_PROMPT)
        self._graph                          = build_interview_graph()
        self._graph_state: InterviewState    = graph_state
        self.interview_id                    = interview_id
        self.tenant_id                       = tenant_id
        self._transcript_tasks: set[asyncio.Task] = set()

    @property
    def instructions(self) -> str:
        stage_instr = self._graph_state.get("stage_instruction", "")
        if not stage_instr:
            return BASE_SYSTEM_PROMPT
        return (
            BASE_SYSTEM_PROMPT
            + "\n\n## CURRENT FOCUS\n"
            + stage_instr
            + "\n\nFocus your next response on this. "
            "If the candidate has already answered, acknowledge and pivot cleanly."
        )

    def _queue_save(self, speaker: str, text: str, node: str | None = None) -> None:
        if not self.interview_id:
            logger.warning("[transcript] skipped — interview_id is None")
            return
        if not self.tenant_id:
            logger.warning(
                "[transcript] skipped — tenant_id is None",
                extra={"interview_id": self.interview_id},
            )
            return
        if not text.strip():
            return

        spoken_at = datetime.now(timezone.utc)
        logger.info(
            f"[transcript] queuing {speaker} turn ({len(text)} chars)",
            extra={"interview_id": self.interview_id, "stage": node},
        )
        task = asyncio.create_task(
            _save_transcript(
                self.interview_id, self.tenant_id,
                speaker, text, spoken_at, node,
            )
        )
        self._transcript_tasks.add(task)
        task.add_done_callback(self._transcript_tasks.discard)

    def _log_stage(self) -> None:
        logger.debug(
            "Graph stage",
            extra={
                "stage":          self._graph_state.get("stage", "?"),
                "turns_in_stage": self._graph_state.get("turns_in_stage", 0),
                "interview_id":   self.interview_id,
            },
        )

    async def on_user_turn_completed(
        self,
        turn_ctx:    llm.ChatContext,
        new_message: llm.ChatMessage,
    ) -> None:
        """
        Fires when candidate finishes speaking.
        Saves candidate turn, advances LangGraph, generates AI response.
        AI response is captured via conversation_item_added event (not here).
        """
        text = _extract_text(new_message).strip()
        logger.debug(
            "STT received",
            extra={"preview": text[:80], "interview_id": self.interview_id},
        )
        words = text.split()

        # ── FIX 6: Relaxed mishear guard ─────────────────────────────────────
        # Old: len(words) <= 1 — discarded everything including short valid answers
        # New: only reject empty strings, let short words through
        if not text:
            await self.session.say(random.choice(_MISHEAR), allow_interruptions=False)
            return

        # Save candidate turn
        self._queue_save("candidate", text, node=self._graph_state.get("stage"))

        # Advance LangGraph
        try:
            new_state = await self._graph.ainvoke(
                {**self._graph_state, "last_candidate_text": text}
            )
            self._graph_state = new_state
            self._log_stage()
        except Exception as e:
            logger.error(
                "LangGraph advance error",
                extra={"interview_id": self.interview_id, "error": str(e)},
            )

        # Natural pause
        await asyncio.sleep(random.uniform(0.3, 0.6))

        # 20% filler word
        if random.random() < 0.20:
            try:
                await self.session.say(random.choice(_FILLERS), allow_interruptions=False)
                await asyncio.sleep(0.15)
            except Exception:
                pass  # filler failure should never break the interview

        # Generate AI response — AI text is saved via conversation_item_added event
        await super().on_user_turn_completed(turn_ctx, new_message)

    async def handle_typed_input(self, text: str) -> None:
        """Handle candidate typed message — same pipeline as voice turn."""
        text = text.strip()
        if not text:
            return

        try:
            new_state = await self._graph.ainvoke(
                {**self._graph_state, "last_candidate_text": text}
            )
            self._graph_state = new_state
            self._log_stage()
        except Exception as e:
            logger.error(
                "LangGraph advance error (typed)",
                extra={"interview_id": self.interview_id, "error": str(e)},
            )

        await asyncio.sleep(random.uniform(0.3, 0.6))

        if random.random() < 0.20:
            try:
                await self.session.say(random.choice(_FILLERS), allow_interruptions=False)
                await asyncio.sleep(0.15)
            except Exception:
                pass

        await self.session.generate_reply(user_input=text)


# ── Entrypoint ─────────────────────────────────────────────────────────────────

async def entrypoint(ctx: JobContext):
    await ctx.connect()

    # Derive interview_id from room name ("interview-<uuid>")
    room_name    = ctx.room.name or ""
    interview_id: str | None = None
    if room_name.startswith("interview-"):
        interview_id = room_name[len("interview-"):]
        logger.info(
            "Agent connected to room",
            extra={"room": room_name, "interview_id": interview_id},
        )

    # Load context from DB
    ctx_data = {}
    if interview_id:
        ctx_data = await _load_interview_context(interview_id)

    tenant_id      = ctx_data.get("tenant_id")
    candidate_name = ctx_data.get("candidate_name", "the candidate")
    gaps_to_probe  = ctx_data.get("gaps_to_probe",  [])
    skills_to_probe = ctx_data.get("skills_to_probe", [])

    if not tenant_id:
        logger.error(
            "⚠️  tenant_id is None — DB writes disabled. "
            "Check: 1) DATABASE_URL in .env  "
            "2) interview row exists  "
            "3) agent started from project root directory",
            extra={"interview_id": interview_id},
        )

    graph_state = make_initial_state(
        candidate_name=candidate_name,
        skills_to_probe=skills_to_probe,
        gaps_to_probe=gaps_to_probe,
    )
    logger.info(
        "LangGraph initialised",
        extra={"stage": "intro", "candidate": candidate_name, "interview_id": interview_id},
    )

    interview_start_time: list[datetime | None] = [None]

    # ── FIX 4: Create hr_agent BEFORE on_shutdown ─────────────────────────────
    # Previously hr_agent was created AFTER on_shutdown was defined and
    # registered. If shutdown fired before hr_agent was assigned, the closure
    # raised UnboundLocalError and the status was never written to DB.
    # Now hr_agent is created first — the closure reference is always valid.
    hr_agent = HRInterviewAgent(
        graph_state=graph_state,
        interview_id=interview_id,
        tenant_id=tenant_id,
    )

    # ── Shutdown callback ──────────────────────────────────────────────────────
    async def on_shutdown() -> None:
        if interview_id and tenant_id:
            end_time = datetime.now(timezone.utc)
            start    = interview_start_time[0]
            duration = int((end_time - start).total_seconds()) if start else None

            await _update_interview_status(
                interview_id,
                "completed",
                ended_at=end_time,
                duration_seconds=duration,
            )

            # Drain only transcript save tasks — not ALL event loop tasks
            pending_saves = list(hr_agent._transcript_tasks)
            if pending_saves:
                logger.info(
                    f"Draining {len(pending_saves)} transcript task(s) before evaluation",
                    extra={"interview_id": interview_id},
                )
                await asyncio.gather(*pending_saves, return_exceptions=True)

            try:
                from app.services.evaluation_engine import run_evaluation
                await run_evaluation(interview_id)
                logger.info(
                    "Post-interview evaluation complete",
                    extra={"interview_id": interview_id},
                )
            except Exception as e:
                logger.error(
                    "Post-interview evaluation failed",
                    extra={"interview_id": interview_id, "error": str(e)},
                )

    ctx.add_shutdown_callback(on_shutdown)

    # ── Log and subscribe to audio tracks from participants ───────────────────
    @ctx.room.on("track_subscribed")
    def on_track_subscribed(track, publication, participant) -> None:
        logger.info(
            f"[audio] ✓ Track SUBSCRIBED — kind={track.kind} "
            f"participant={participant.identity}",
            extra={"interview_id": interview_id},
        )

    @ctx.room.on("track_published")
    def on_track_published(publication, participant) -> None:
        logger.info(
            f"[audio] Track published — kind={publication.kind} "
            f"participant={participant.identity}",
            extra={"interview_id": interview_id},
        )
        # Explicitly set subscription to True for candidate audio tracks
        if (publication.kind == "audio"
                and hasattr(participant, "identity")
                and str(participant.identity).startswith("candidate-")):
            try:
                publication.set_subscribed(True)
                logger.info(
                    f"[audio] Explicitly subscribed to candidate audio track",
                    extra={"interview_id": interview_id},
                )
            except Exception as e:
                logger.warning(f"[audio] set_subscribed failed: {e}")

    # ── Mark in_progress when candidate joins ─────────────────────────────────
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant) -> None:
        if not interview_id or not tenant_id:
            return
        if participant.identity == "simli-avatar":
            return
        if not participant.identity.startswith("candidate-"):
            return

        interview_start_time[0] = datetime.now(timezone.utc)
        logger.info(
            "Candidate joined room",
            extra={"identity": participant.identity, "interview_id": interview_id},
        )
        asyncio.create_task(
            _update_interview_status(
                interview_id, "in_progress",
                started_at=interview_start_time[0],
            )
        )

    # ── Shut down agent when candidate leaves ──────────────────────────────────
    # When the candidate clicks "End Interview" or closes the browser tab,
    # room.disconnect() fires on their side. Without this handler the agent
    # stays in the room indefinitely and on_shutdown never fires — the
    # interview stays stuck in "in_progress" forever.
    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant) -> None:
        # Only react to the candidate leaving — ignore avatar/other agents
        if participant.identity == "simli-avatar":
            return
        if not participant.identity.startswith("candidate-"):
            return

        logger.info(
            "Candidate left room — shutting down agent job",
            extra={"identity": participant.identity, "interview_id": interview_id},
        )
        # ctx.shutdown() triggers on_shutdown callback which:
        # 1. Marks interview as completed
        # 2. Drains transcript saves
        # 3. Runs evaluation + report generation
        asyncio.create_task(ctx.shutdown())

    # ── AgentSession ──────────────────────────────────────────────────────────
    session = AgentSession(
        vad=silero.VAD.load(
            min_speech_duration=0.05,       # 50ms — detect short utterances
            min_silence_duration=0.5,
            activation_threshold=0.1,       # very sensitive — catches quiet/degraded audio
            prefix_padding_duration=0.3,
        ),
        stt=deepgram.STT(
            api_key=os.environ["DEEPGRAM_API_KEY"],
            language="en",          # changed from en-IN — broader detection
            model="nova-2-general",
            endpointing_ms=200,     # reduced from 300ms — faster response
            interim_results=True,
            smart_format=True,
            punctuate=True,
            filler_words=False,
        ),
        llm=anthropic.LLM(
            model="claude-haiku-4-5-20251001",
            api_key=os.environ["ANTHROPIC_API_KEY"],
        ),
        tts=elevenlabs.TTS(
            api_key=os.environ["ELEVENLABS_API_KEY"],
            voice_id="EXAVITQu4vr4xnSDxMaL",  # Sarah
            model="eleven_turbo_v2_5",
            voice_settings=VoiceSettings(
                stability=0.45,
                similarity_boost=0.80,
                style=0.35,
                speed=0.95,
            ),
        ),
        allow_interruptions=True,
        min_interruption_words=4,
        min_endpointing_delay=0.4,
        max_endpointing_delay=6.0,
        aec_warmup_duration=0,
    )

    # ── Avatar via OFFICIAL livekit-plugins-simli (must start BEFORE session) ──
    # The official plugin dispatches the avatar as a SEPARATE LiveKit worker that
    # renders video + republishes audio. It does NOT run on this event loop, so
    # it cannot starve the candidate-audio pipeline (the root cause of the
    # custom-forwarder approach failing). When active, the avatar publishes the
    # agent's voice itself — so we disable the room's own audio output to avoid
    # double audio.
    simli_api_key = os.environ.get("SIMLI_API_KEY", "")
    simli_face_id = os.environ.get("SIMLI_FACE_ID", "")
    avatar_active = False

    if simli_api_key and simli_face_id:
        try:
            avatar = simli.AvatarSession(
                simli_config=simli.SimliConfig(
                    api_key=simli_api_key,
                    face_id=simli_face_id,
                ),
                avatar_participant_identity="simli-avatar",
                avatar_participant_name="Sarah",
            )
            await avatar.start(
                session,
                room=ctx.room,
                livekit_url=os.environ.get("LIVEKIT_URL", ""),
                livekit_api_key=os.environ.get("LIVEKIT_API_KEY", ""),
                livekit_api_secret=os.environ.get("LIVEKIT_API_SECRET", ""),
            )
            avatar_active = True
            logger.info("Simli avatar active (official plugin)", extra={"interview_id": interview_id})
        except Exception as e:
            logger.warning(
                f"Simli avatar failed to start — falling back to voice-only: {e}",
                extra={"interview_id": interview_id},
            )
            avatar_active = False
    else:
        logger.info("Simli keys not set — voice-only mode", extra={"interview_id": interview_id})

    await session.start(
        room=ctx.room,
        agent=hr_agent,
        room_options=room_io.RoomOptions(
            # auto_gain_control=False disables the per-frame APM native call.
            audio_input=room_io.AudioInputOptions(auto_gain_control=False),
            # When avatar is active it republishes the audio itself → disable
            # the room's direct audio output to prevent double audio.
            audio_output=(not avatar_active),
        ),
    )

    # ── FIX 3: Capture AI responses via conversation_item_added event ─────────
    # The old approach read from turn_ctx AFTER super().on_user_turn_completed().
    # But turn_ctx is a TEMPORARY COPY made before the LLM call — the AI response
    # is written to self._agent.chat_ctx (the real context), not the copy.
    # So turn_ctx[ctx_len_before:] was ALWAYS empty — AI turns were never saved.
    #
    # The correct approach: listen to the session's conversation_item_added event,
    # which fires for every message added to the real chat context.
    @session.on("conversation_item_added")
    def on_conversation_item_added(event) -> None:
        try:
            item     = event.item
            role     = getattr(item, "role", None)
            role_str = str(role).lower() if role is not None else ""

            if "assistant" in role_str:
                ai_text = _extract_text(item).strip()
                if ai_text:
                    hr_agent._queue_save(
                        "ai", ai_text,
                        node=hr_agent._graph_state.get("stage"),
                    )
                else:
                    logger.debug(
                        "[transcript] assistant item had no extractable text",
                        extra={"interview_id": interview_id},
                    )
        except Exception as e:
            logger.error(
                f"[transcript] conversation_item_added handler error: {e}",
                extra={"interview_id": interview_id},
            )

    # ── Text input via data channel ───────────────────────────────────────────
    @ctx.room.on("data_received")
    def on_data_received(data_packet) -> None:
        try:
            if getattr(data_packet, "topic", None) == "text_input":
                text = data_packet.data.decode("utf-8").strip()
                if text:
                    logger.debug(
                        "Text input received",
                        extra={"preview": text[:80], "interview_id": interview_id},
                    )
                    asyncio.create_task(hr_agent.handle_typed_input(text))
        except Exception as e:
            logger.error(
                "data_received error",
                extra={"interview_id": interview_id, "error": str(e)},
            )

    await session.generate_reply(
        instructions=(
            "Greet the candidate warmly. Welcome them to their screening interview. "
            f"Their name is {candidate_name}. "
            "Mention that the session will be recorded and ask for their consent to proceed. "
            "Be natural and friendly — like a real HR person starting a call."
        )
    )


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            api_key=os.environ["LIVEKIT_API_KEY"],
            api_secret=os.environ["LIVEKIT_API_SECRET"],
            ws_url=os.environ["LIVEKIT_URL"],
        )
    )
