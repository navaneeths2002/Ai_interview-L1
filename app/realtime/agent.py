import asyncio
import json
import logging
import random
import uuid
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

from app.core.logging_config import get_logger

# Silence chatty WebRTC / ICE / charset debug spam from aiortc P2P transport
logging.getLogger("aiortc").setLevel(logging.WARNING)
logging.getLogger("aiortc.rtcdtlstransport").setLevel(logging.WARNING)
logging.getLogger("aioice").setLevel(logging.WARNING)
logging.getLogger("aioice.ice").setLevel(logging.WARNING)
logging.getLogger("charset_normalizer").setLevel(logging.WARNING)

logger = get_logger(__name__)

from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, RoomInputOptions, cli
from livekit.agents import llm
from livekit.plugins import deepgram, elevenlabs, anthropic, silero
from livekit.plugins.elevenlabs import VoiceSettings
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import text

from app.realtime.interview_graph import build_interview_graph, make_initial_state, InterviewState
from app.realtime.avatar_session import AvatarSession

load_dotenv()


# ── Base system prompt (behaviour rules — never changes) ───────────────────────

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
- When the candidate spells out a name or word letter-by-letter (e.g. "Navaneeth — N-A-V-A-N-E-E-T-H" or "TCS — T-C-S"), confirm by saying "Got it — [assembled name], thank you" and use that exact spelling from this point forward
- When the candidate corrects a previously mentioned name or company (e.g. "Actually it's Infosys, not Infosis"), acknowledge naturally: "My apologies — Infosys, noted." and use the corrected spelling going forward
- Never ask the candidate to spell something out again once they've already done so
"""

# Short filler lines spoken before the main LLM response (20% of turns)
_FILLERS = ["Hmm.", "Right.", "I see.", "Okay.", "Sure.", "Got it." , "Aha!", "Gotcha."]

# Responses when transcript is too short to be a real answer
_MISHEAR = [
    "Sorry, I didn't quite catch that — could you say that again?",
    "Apologies, I missed that — could you repeat it?",
    "Sorry about that, could you say that once more?",
]

# Single-word answers that are valid despite being short
_VALID_SHORT = {"yes", "no", "yeah", "nope", "okay", "ok", "sure", "nah", "yep"}


# ── Database helpers ────────────────────────────────────────────────────────────

_agent_engine = None
_agent_session_factory = None


def _get_db_factory():
    global _agent_engine, _agent_session_factory
    if _agent_session_factory is None:
        db_url = os.environ.get("DATABASE_URL", "")
        if not db_url:
            return None
        _agent_engine = create_async_engine(db_url, pool_size=5, max_overflow=10)
        _agent_session_factory = async_sessionmaker(
            bind=_agent_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _agent_session_factory


async def _load_interview_context(interview_id: str) -> dict:
    """
    Loads tenant_id, candidate_name, and ATS context (skills/gaps) from DB.
    Returns a dict with those keys; falls back to safe defaults on failure.
    """
    defaults = {
        "tenant_id": None,
        "candidate_name": "the candidate",
        "skills_to_probe": [],
        "gaps_to_probe": [],
    }
    factory = _get_db_factory()
    if not factory:
        return defaults
    try:
        async with factory() as session:
            # Interview + candidate name
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
                logger.warning("No interview row found", extra={"interview_id": interview_id})
                return defaults

            tenant_id = row[0]
            candidate_name = (row[1] or "the candidate").strip()

            # Interview context (skills/gaps from ATS strategy)
            ctx_row = (await session.execute(
                text("""
                    SELECT gaps_to_probe, skills_to_validate
                    FROM interview_contexts
                    WHERE interview_id = :id
                    LIMIT 1
                """),
                {"id": interview_id},
            )).fetchone()

            return {
                "tenant_id": tenant_id,
                "candidate_name": candidate_name,
                "gaps_to_probe": list(ctx_row[0] or []) if ctx_row else [],
                "skills_to_probe": list(ctx_row[1] or []) if ctx_row else [],
            }
    except Exception as e:
        logger.error("Interview context lookup failed", extra={"interview_id": interview_id, "error": str(e)})
        return defaults


async def _save_transcript(
    interview_id: str,
    tenant_id: str,
    speaker: str,
    message: str,
    spoken_at: datetime,          # ← captured at queue time, not task-run time
    node: str | None = None,      # LangGraph stage at time of utterance
) -> None:
    """
    Upsert a turn into the single interview_transcripts row for this interview.

    First call   → INSERT a new row with turns = [{...}], turn_count = 1
    Subsequent   → UPDATE: append to turns array via ||, increment turn_count

    The UNIQUE constraint on interview_id makes ON CONFLICT safe even when
    multiple fire-and-forget tasks run concurrently.
    """
    if not message.strip():
        return
    factory = _get_db_factory()
    if not factory:
        return
    now = datetime.now(timezone.utc)

    # Wrap the turn in a JSON array so PostgreSQL's || operator appends it
    # to the existing turns array atomically.
    turn_json = json.dumps([{
        "speaker":   speaker,
        "message":   message.strip(),
        "spoken_at": spoken_at.isoformat(),
        "node":      node,
    }])

    try:
        async with factory() as session:
            await session.execute(
                text("""
                    INSERT INTO interview_transcripts
                        (id, tenant_id, interview_id, turns, turn_count, created_at, updated_at)
                    VALUES
                        (:id, :tenant_id, :interview_id, :turn_json::jsonb, 1, :now, :now)
                    ON CONFLICT (interview_id) DO UPDATE SET
                        turns      = interview_transcripts.turns || :turn_json::jsonb,
                        turn_count = interview_transcripts.turn_count + 1,
                        updated_at = :now
                """),
                {
                    "id":           str(uuid.uuid4()),
                    "tenant_id":    tenant_id,
                    "interview_id": interview_id,
                    "turn_json":    turn_json,
                    "now":          now,
                },
            )
            await session.commit()
    except Exception as e:
        logger.error("Transcript save failed", extra={"speaker": speaker, "interview_id": interview_id, "error": str(e)})


async def _update_interview_status(
    interview_id: str,
    status: str,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    duration_seconds: int | None = None,
) -> None:
    factory = _get_db_factory()
    if not factory:
        return
    now = datetime.now(timezone.utc)
    try:
        # Fixed SQL — no f-string, all values are parameterized.
        # COALESCE keeps the existing column value when the caller passes None.
        async with factory() as session:   # uses the already-fetched factory (BUG 7 fix)
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
        logger.info("Interview status updated", extra={"interview_id": interview_id, "status": status})
    except Exception as e:
        logger.error("Interview status update failed", extra={"interview_id": interview_id, "status": status, "error": str(e)})


# ── Text extraction ─────────────────────────────────────────────────────────────

def _extract_text(message: llm.ChatMessage) -> str:
    for part in message.content:
        if hasattr(part, "text"):
            return part.text
        if isinstance(part, str):
            return part
    return ""


# ── Agent ───────────────────────────────────────────────────────────────────────

class HRInterviewAgent(Agent):
    """
    Wraps the livekit-agents Agent with:
      - LangGraph state machine (drives which topic to ask about each turn)
      - Phase-specific LLM instructions (injected as system prompt per turn)
      - Transcript saving (fire-and-forget)
      - Natural human behaviour (fillers, mishear, pre-response delay)
    """

    def __init__(
        self,
        graph_state: InterviewState,
        interview_id: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        super().__init__(instructions=BASE_SYSTEM_PROMPT)
        self._graph = build_interview_graph()
        self._graph_state: InterviewState = graph_state
        self.interview_id = interview_id
        self.tenant_id = tenant_id

    # ── Dynamic instructions ──────────────────────────────────────────────────

    @property
    def instructions(self) -> str:
        """
        Overrides the base `instructions` property so that livekit-agents
        reads the CURRENT stage-focused prompt on every LLM call.
        """
        stage_instr = self._graph_state.get("stage_instruction", "")
        if not stage_instr:
            return BASE_SYSTEM_PROMPT

        return (
            BASE_SYSTEM_PROMPT
            + "\n\n## CURRENT FOCUS\n"
            + stage_instr
            + "\n\nFocus your next response on this. If the candidate has already answered it, "
            "acknowledge naturally and pivot to gathering it cleanly."
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _queue_save(self, speaker: str, text: str, node: str | None = None) -> None:
        if self.interview_id and self.tenant_id and text.strip():
            # Capture timestamp NOW — before the async task runs — so fire-and-forget
            # tasks always write the actual spoken time, not the DB-write time.
            spoken_at = datetime.now(timezone.utc)
            asyncio.create_task(
                _save_transcript(self.interview_id, self.tenant_id, speaker, text, spoken_at, node)
            )

    def _log_stage(self) -> None:
        stage = self._graph_state.get("stage", "?")
        turns = self._graph_state.get("turns_in_stage", 0)
        logger.debug("Graph stage advanced", extra={"stage": stage, "turns_in_stage": turns, "interview_id": self.interview_id})

    # ── Main hook ─────────────────────────────────────────────────────────────

    async def on_user_turn_completed(
        self,
        turn_ctx: llm.ChatContext,
        new_message: llm.ChatMessage,
    ) -> None:
        text = _extract_text(new_message).strip()
        logger.debug("STT transcript received", extra={"text_preview": text[:80], "interview_id": self.interview_id})
        words = text.split()

        # Mishear guard
        if len(words) <= 1 and text.lower() not in _VALID_SHORT:
            await self.session.say(random.choice(_MISHEAR), allow_interruptions=False)
            return

        # ── Save candidate message (non-blocking) ────────────────────────────
        self._queue_save("candidate", text, node=self._graph_state.get("stage"))

        # ── Advance LangGraph state with candidate's response ────────────────
        try:
            new_state = await self._graph.ainvoke(
                {**self._graph_state, "last_candidate_text": text}
            )
            self._graph_state = new_state
            self._log_stage()
        except Exception as e:
            logger.error("LangGraph advance error", extra={"interview_id": self.interview_id, "error": str(e)})

        # ── Natural pre-response pause ────────────────────────────────────────
        await asyncio.sleep(random.uniform(0.3, 0.6))

        # ── 20% filler ───────────────────────────────────────────────────────
        if random.random() < 0.20:
            await self.session.say(random.choice(_FILLERS), allow_interruptions=False)
            await asyncio.sleep(0.15)

        # ── Generate AI response (instructions property gives stage focus) ────
        ctx_len_before = len(turn_ctx.messages())

        await super().on_user_turn_completed(turn_ctx, new_message)

        # ── Capture and save AI response ──────────────────────────────────────
        for msg in turn_ctx.messages()[ctx_len_before:]:
            if getattr(msg, "role", None) == "assistant":
                ai_text = _extract_text(msg).strip()
                if ai_text:
                    self._queue_save("ai", ai_text, node=self._graph_state.get("stage"))
                break

    # ── Typed text input ──────────────────────────────────────────────────────

    async def handle_typed_input(self, text: str) -> None:
        """
        Handle a message the candidate typed in the chat box.

        Runs the same pipeline as a voice turn:
          save → graph advance → optional filler → generate_reply (LLM + TTS)

        generate_reply adds the user_input to the chat context, calls the LLM,
        synthesises TTS, and triggers on_user_turn_completed so the AI reply
        is captured and saved to the transcript automatically.
        """
        text = text.strip()
        if not text:
            return

        # NOTE: do NOT call _queue_save here — generate_reply() triggers
        # on_user_turn_completed() which saves the candidate message and the
        # AI response in one place.  Saving here too would double-write (BUG 12).

        # ── Advance LangGraph state ──────────────────────────────────────────
        try:
            new_state = await self._graph.ainvoke(
                {**self._graph_state, "last_candidate_text": text}
            )
            self._graph_state = new_state
            self._log_stage()
        except Exception as e:
            logger.error("LangGraph advance error (typed input)", extra={"interview_id": self.interview_id, "error": str(e)})

        # ── Natural pre-response pause ───────────────────────────────────────
        await asyncio.sleep(random.uniform(0.3, 0.6))

        # ── 20% filler ──────────────────────────────────────────────────────
        if random.random() < 0.20:
            await self.session.say(random.choice(_FILLERS), allow_interruptions=False)
            await asyncio.sleep(0.15)

        # ── Generate AI response (stage-aware via instructions property) ─────
        await self.session.generate_reply(user_input=text)


# ── Entrypoint ──────────────────────────────────────────────────────────────────

async def entrypoint(ctx: JobContext):
    await ctx.connect()

    # Derive interview_id from room name ("interview-<uuid>")
    room_name = ctx.room.name or ""
    interview_id: str | None = None
    if room_name.startswith("interview-"):
        interview_id = room_name[len("interview-"):]
        logger.info("Agent connected to interview room", extra={"room": room_name, "interview_id": interview_id})

    # Load context from DB (tenant, candidate name, skills/gaps)
    ctx_data = {}
    if interview_id:
        ctx_data = await _load_interview_context(interview_id)

    tenant_id = ctx_data.get("tenant_id")
    candidate_name = ctx_data.get("candidate_name", "the candidate")
    gaps_to_probe = ctx_data.get("gaps_to_probe", [])
    skills_to_probe = ctx_data.get("skills_to_probe", [])

    if not tenant_id:
        logger.warning("tenant_id not found — transcripts and status updates disabled", extra={"interview_id": interview_id})

    # Build initial LangGraph state
    graph_state = make_initial_state(
        candidate_name=candidate_name,
        skills_to_probe=skills_to_probe,
        gaps_to_probe=gaps_to_probe,
    )
    logger.info("LangGraph initialised", extra={"stage": "intro", "candidate": candidate_name, "interview_id": interview_id})

    # interview_start_time is set when the CANDIDATE actually joins the room
    # (not when the agent connects).  Use a mutable list so the closure in
    # on_shutdown can read the value set later by on_participant_connected.
    interview_start_time: list[datetime | None] = [None]

    # Mutable holder so on_shutdown can reach the avatar even though it's
    # created *after* on_shutdown is defined.
    _avatar_holder: list[AvatarSession] = []

    # NOTE: Do NOT mark in_progress here.  The agent joining the room does not
    # mean the candidate has joined.  Status is set inside on_participant_connected
    # below, triggered only when the candidate's browser connects.

    session = AgentSession(
        vad=silero.VAD.load(
            min_speech_duration=0.05,       # start detecting after 50 ms of speech
            min_silence_duration=0.5,       # end turn after 500 ms silence
            activation_threshold=0.35,      # lower = more sensitive (default 0.5)
            prefix_padding_duration=0.3,    # include 300 ms before speech onset
        ),
        stt=deepgram.STT(
            api_key=os.environ["DEEPGRAM_API_KEY"],
            language="en-IN",
            model="nova-2-general",         # nova-3 lacks en-IN; nova-2-general is reliable
            endpointing_ms=300,
            interim_results=True,
            smart_format=True,              # fix punctuation & casing automatically
            punctuate=True,
            filler_words=False,             # don't transcribe "um" / "uh" as words
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
        min_interruption_words=4,  # raised from 2 — prevents false barge-in from coughs/noise
        min_endpointing_delay=0.4,
        max_endpointing_delay=6.0,
    )

    # Mark interview as completed on shutdown, then fire evaluation
    async def on_shutdown() -> None:
        if interview_id and tenant_id:
            end_time = datetime.now(timezone.utc)
            start   = interview_start_time[0]  # None if candidate never joined

            # Only compute duration if the candidate actually joined
            duration = int((end_time - start).total_seconds()) if start else None

            await _update_interview_status(
                interview_id,
                "completed",
                ended_at=end_time,
                duration_seconds=duration,
            )

            # Drain all pending transcript save tasks before running evaluation.
            # _queue_save() fires create_task() calls throughout the interview.
            # Those tasks may still be in-flight when on_shutdown fires.
            # Yielding to the event loop here lets them all complete so the
            # transcript is fully written before evaluation reads it.
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if pending:
                logger.info(
                    f"Waiting for {len(pending)} pending task(s) to flush before evaluation",
                    extra={"interview_id": interview_id},
                )
                await asyncio.gather(*pending, return_exceptions=True)

            # Phase 6: trigger evaluation engine directly (await, not create_task)
            # Using create_task inside a shutdown callback is unreliable because
            # the event loop may already be winding down.
            try:
                from app.services.evaluation_engine import run_evaluation
                await run_evaluation(interview_id)
                logger.info("Post-interview evaluation complete", extra={"interview_id": interview_id})
            except Exception as e:
                logger.error("Post-interview evaluation failed", extra={"interview_id": interview_id, "error": str(e)})

        # Stop avatar session if it was started
        if _avatar_holder:
            await _avatar_holder[0].stop()

    ctx.add_shutdown_callback(on_shutdown)

    # ── Mark in_progress only when the CANDIDATE joins ────────────────────────
    # This fires for every remote participant.  We filter by identity prefix
    # to skip the simli-avatar participant that also joins the same room.
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant) -> None:
        if not interview_id or not tenant_id:
            return
        # Only act on the candidate — skip simli-avatar and any other agents
        if participant.identity == "simli-avatar":
            return
        if not participant.identity.startswith("candidate-"):
            return

        # Capture start time at the moment the candidate's browser connects
        interview_start_time[0] = datetime.now(timezone.utc)
        logger.info("Candidate joined room", extra={"identity": participant.identity, "interview_id": interview_id})
        asyncio.create_task(
            _update_interview_status(
                interview_id, "in_progress", started_at=interview_start_time[0]
            )
        )

    hr_agent = HRInterviewAgent(
        graph_state=graph_state,
        interview_id=interview_id,
        tenant_id=tenant_id,
    )

    await session.start(
        room=ctx.room,
        agent=hr_agent,
        room_input_options=RoomInputOptions(),
    )

    # ── Text input via data channel ───────────────────────────────────────────
    # Candidate can type in the chat box; the browser publishes a data packet
    # with topic="text_input". We forward it through the same agent pipeline as
    # a voice turn so the AI responds naturally (including spelling corrections).
    @ctx.room.on("data_received")
    def on_data_received(data_packet) -> None:
        try:
            if getattr(data_packet, "topic", None) == "text_input":
                text = data_packet.data.decode("utf-8").strip()
                if text:
                    logger.debug("Text input received", extra={"text_preview": text[:80], "interview_id": interview_id})
                    asyncio.create_task(hr_agent.handle_typed_input(text))
        except Exception as e:
            logger.error("data_received handler error", extra={"interview_id": interview_id, "error": str(e)})

    # ── Avatar (optional — gracefully disabled if Simli keys not set) ────────
    avatar = AvatarSession()
    _avatar_holder.append(avatar)   # expose to on_shutdown closure

    simli_api_key = os.environ.get("SIMLI_API_KEY", "")
    simli_face_id = os.environ.get("SIMLI_FACE_ID", "")

    simli_forwarder = None
    if simli_api_key and simli_face_id:
        # Hard 8-second cap so a slow/failed Simli connection never delays the interview
        try:
            simli_forwarder = await asyncio.wait_for(
                avatar.start(
                    room_name=room_name,
                    lk_url=os.environ.get("LIVEKIT_URL", ""),
                    lk_api_key=os.environ.get("LIVEKIT_API_KEY", ""),
                    lk_api_secret=os.environ.get("LIVEKIT_API_SECRET", ""),
                    simli_api_key=simli_api_key,
                    simli_face_id=simli_face_id,
                    current_audio_output=session.output.audio,
                ),
                timeout=8.0,
            )
        except asyncio.TimeoutError:
            logger.warning("Simli start timed out (>8s) — falling back to voice-only", extra={"interview_id": interview_id})
            await avatar.stop()
            simli_forwarder = None
    else:
        logger.info("SIMLI_API_KEY or SIMLI_FACE_ID not set — voice-only mode", extra={"interview_id": interview_id})

    if simli_forwarder is not None:
        session.output.audio = simli_forwarder
        logger.info("Simli avatar active — face video published to room", extra={"interview_id": interview_id})
    else:
        logger.info("Running in voice-only mode", extra={"interview_id": interview_id})

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
