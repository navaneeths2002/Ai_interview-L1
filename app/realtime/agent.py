import asyncio
import logging
import pathlib
import random
import time
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

from livekit.agents import Agent, AgentSession, JobContext, JobProcess, WorkerOptions, cli
from livekit.agents import llm, metrics
from livekit.agents.voice import room_io
from livekit.plugins import deepgram, anthropic, silero, simli
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.realtime.interview_graph import build_interview_graph, make_initial_state, InterviewState
from app.realtime import crash_recovery
from app.realtime import audio_capture
from app.services import cost_tracker


# ── Deepgram TTS speech-speed control ───────────────────────────────────────────
# Deepgram Aura supports a `speed` query param (0.7–1.5, default 1.0), but the
# livekit plugin doesn't expose it. Both the plugin's WebSocket and HTTP request
# paths build their URL via deepgram.tts._to_deepgram_url(opts, base_url), so we
# wrap that ONE function to inject `speed`. If Deepgram ever rejects the param,
# set the speed back to 1.0 — nothing else is affected.
#
# ┌─────────────────────────────────────────────────────────────────────────┐
# │  EDIT THIS to change how fast Sarah speaks:                              │
# │    1.0  = normal        1.15 = slightly brisk                           │
# │    1.25 = noticeably faster   1.5 = maximum   (allowed range: 0.7–1.5)  │
# └─────────────────────────────────────────────────────────────────────────┘
DEEPGRAM_TTS_SPEED = 1.15

_DG_TTS_SPEED = max(0.7, min(1.5, float(DEEPGRAM_TTS_SPEED)))
if _DG_TTS_SPEED != 1.0:
    try:
        from livekit.plugins.deepgram import tts as _dg_tts_mod
        _dg_orig_to_url = _dg_tts_mod._to_deepgram_url

        def _dg_to_url_with_speed(opts, base_url, *, websocket):
            opts = {**opts, "speed": _DG_TTS_SPEED}
            return _dg_orig_to_url(opts, base_url, websocket=websocket)

        _dg_tts_mod._to_deepgram_url = _dg_to_url_with_speed
        logger.info(f"[tts] Deepgram speech speed override active: speed={_DG_TTS_SPEED}")
    except Exception as _e:
        logger.warning(f"[tts] could not enable Deepgram speed override (non-fatal): {_e}")


# ── Candidate-join wait ────────────────────────────────────────────────────────
# The interview room is created at TRIGGER time (services/context_builder.py) and
# this worker uses AUTOMATIC dispatch (no agent_name in WorkerOptions), so LiveKit
# hands us a job the moment the room exists — long before the candidate opens the
# link that was emailed to them. The entrypoint therefore waits for the candidate
# instead of interviewing an empty room. If nobody arrives within this window the
# job exits quietly WITHOUT finalizing (see the guard in on_shutdown), and the
# interview stays "scheduled" so the join link keeps working. When the candidate
# does open the link later, LiveKit re-creates the room and dispatches a fresh job.
#
# Keep this comfortably above room_manager's empty_timeout (30s) so room closure,
# not this timeout, is the usual way an unattended job ends.
CANDIDATE_JOIN_TIMEOUT = float(os.getenv("CANDIDATE_JOIN_TIMEOUT_SECONDS", "60"))


# ── Base system prompt ─────────────────────────────────────────────────────────

BASE_SYSTEM_PROMPT = """You are a professional HR interviewer conducting an L1 screening call.

Rules:
- Be friendly, natural, engaging, warm, professional, and conversational
- Ask ONE question at a time — never stack two questions in one turn
- Keep your responses short — 1 to 2 sentences maximum
- Listen carefully and ask natural follow-up questions when needed
- Do not reveal scoring or internal evaluation
- When all information is gathered, thank the candidate and close professionally

About the role:
- You are interviewing for a specific role (see ROLE CONTEXT below, if provided)
- If the candidate asks about the job, responsibilities, or required skills, answer briefly and naturally using the ROLE CONTEXT — a sentence or two, never read the description out like a document
- If the candidate asks about salary/compensation/budget, do NOT state any figure — politely deflect: "That's typically discussed in the later rounds." Your job here is to learn THEIR expectation, not share the budget
- Never invent role details that aren't in the ROLE CONTEXT — if you don't know, say it'll be covered in a later round

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
        "job": {},
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
                               c.first_name || ' ' || COALESCE(c.last_name, '') AS candidate_name,
                               j.position_title, j.department, j.location,
                               j.min_experience_years, j.critical_skills, j.jd_text
                        FROM interviews i
                        JOIN candidates c ON c.id = i.candidate_id
                        LEFT JOIN jobs j ON j.id = i.job_id
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
                    "job": {
                        "position_title":       row[2],
                        "department":           row[3],
                        "location":             row[4],
                        "min_experience_years": row[5],
                        "critical_skills":      list(row[6] or []),
                        "jd_text":              row[7],
                    },
                }
                logger.info(
                    f"[context] loaded — tenant={tenant_id} candidate={candidate_name} "
                    f"role={row[2] or 'N/A'}",
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


def _build_job_context(job: dict) -> str:
    """
    Build a CONCISE role summary injected into Sarah's prompt so she can answer
    candidate questions about the job naturally — a few lines, never the full JD.
    Returns "" when no job is available (Sarah just runs role-agnostic).
    """
    if not job or not job.get("position_title"):
        return ""

    parts: list[str] = []
    title = job.get("position_title")
    dept  = job.get("department")
    loc   = job.get("location")
    headline = title
    if dept and loc:
        headline += f" ({dept}, {loc})"
    elif dept:
        headline += f" ({dept})"
    elif loc:
        headline += f" ({loc})"
    parts.append(f"Position: {headline}")

    if job.get("min_experience_years"):
        parts.append(f"Experience required: {job['min_experience_years']}+ years")

    skills = job.get("critical_skills") or []
    if skills:
        parts.append(f"Key skills: {', '.join(skills[:6])}")

    jd = (job.get("jd_text") or "").strip()
    if jd:
        # Trim to a couple of sentences so Sarah summarises, not recites.
        summary = jd[:300].rsplit(".", 1)[0].strip()
        if summary:
            parts.append(f"About the role: {summary}.")

    return "\n".join(parts)


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
        job_context:  str = "",
    ) -> None:
        super().__init__(instructions=BASE_SYSTEM_PROMPT)
        self._graph                          = build_interview_graph()
        self._graph_state: InterviewState    = graph_state
        self.interview_id                    = interview_id
        self.tenant_id                       = tenant_id
        # Role context (title, key skills, short JD summary) injected into every
        # LLM call so Sarah knows the job and can answer the candidate's questions.
        self._job_context                    = job_context or ""
        self._transcript_tasks: set[asyncio.Task] = set()

    @property
    def instructions(self) -> str:
        prompt = BASE_SYSTEM_PROMPT
        if self._job_context:
            prompt += "\n\n## ROLE CONTEXT\n" + self._job_context
        stage_instr = self._graph_state.get("stage_instruction", "")
        if stage_instr:
            prompt += (
                "\n\n## CURRENT FOCUS\n"
                + stage_instr
                + "\n\nFocus your next response on this. "
                "If the candidate has already answered, acknowledge and pivot cleanly."
            )
        return prompt

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
            # CRASH RECOVERY HOOK: persist stage so a crash can resume here
            crash_recovery.queue_save_stage(self.interview_id, self._graph_state)
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
            # CRASH RECOVERY HOOK: persist stage so a crash can resume here
            crash_recovery.queue_save_stage(self.interview_id, self._graph_state)
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

# ── VAD prewarm (startup latency) ──────────────────────────────────────────────
# Silero's model was previously loaded INSIDE the entrypoint, i.e. re-loaded from
# disk for EVERY interview — seconds of dead time before the candidate hears
# anything. prewarm_fnc runs once when the worker boots, so every job reuses the
# already-loaded model. Config lives in _build_vad() so prewarm and the fallback
# can never drift apart.

def _build_vad():
    """Silero VAD config — single source of truth (prewarm + fallback)."""
    return silero.VAD.load(
        # Lever A — barge-in rate control. The old 50ms / 0.1 settings were so
        # sensitive that breaths, clicks, and (worst) Sarah's own echo from the
        # avatar registered as speech, each firing a false barge-in. Raised to
        # filter noise/echo while still catching a genuinely quiet candidate.
        min_speech_duration=0.20,       # 200ms — ignore clicks/breaths/short echo blips
        min_silence_duration=0.5,
        activation_threshold=0.40,      # less sensitive — noise/echo no longer trips it
        prefix_padding_duration=0.3,
    )


def prewarm(proc: JobProcess) -> None:
    """Load the VAD once at worker boot (registered as WorkerOptions.prewarm_fnc)."""
    t0 = time.monotonic()
    proc.userdata["vad"] = _build_vad()
    logger.info(f"[prewarm] Silero VAD loaded in {time.monotonic() - t0:.2f}s")


async def entrypoint(ctx: JobContext):
    # Startup timing — tells us exactly where the join latency goes.
    _t0 = time.monotonic()

    def _lap(label: str) -> None:
        logger.info(f"[startup] {label:<22} +{time.monotonic() - _t0:5.2f}s")

    await ctx.connect()
    _lap("ctx.connect")

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
    _lap("db: interview ctx")

    tenant_id      = ctx_data.get("tenant_id")
    candidate_name = ctx_data.get("candidate_name", "the candidate")
    gaps_to_probe  = ctx_data.get("gaps_to_probe",  [])
    skills_to_probe = ctx_data.get("skills_to_probe", [])
    job_context    = _build_job_context(ctx_data.get("job") or {})
    if job_context:
        logger.info(
            f"[context] role loaded: {(ctx_data.get('job') or {}).get('position_title')}",
            extra={"interview_id": interview_id},
        )

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

    # ── CRASH RECOVERY HOOK: resume from saved stage if agent crashed mid-interview
    resume_state = None
    if interview_id:
        resume_state = await crash_recovery.load_resume_state(interview_id)
        if resume_state:
            graph_state = crash_recovery.apply_resume(graph_state, resume_state)
    _lap("db: resume state")

    logger.info(
        "LangGraph initialised",
        extra={
            "stage": graph_state.get("stage", "intro"),
            "resumed": bool(resume_state),
            "candidate": candidate_name,
            "interview_id": interview_id,
        },
    )

    interview_start_time: list[datetime | None] = [None]
    # Holder so reconnect handler (registered before session exists) can reach it
    _session_holder: list = []
    # Strong refs to fire-and-forget tasks — an unreferenced asyncio task can be
    # garbage-collected before it runs (e.g. the re-greet vanishing mid-sleep)
    _pending_tasks: set[asyncio.Task] = set()

    # Aggregates real LLM/STT/TTS usage across the whole session for cost tracking.
    # Fed by the "metrics_collected" event; read in on_shutdown to compute cost.
    usage_collector = metrics.UsageCollector()

    # ── Avatar lifecycle (Simli) ──────────────────────────────────────────────
    # Held in a list so the reconnect handler (registered before the avatar
    # exists) can reach and RESTART it. Per Simli: the avatar is torn down on
    # EVERY candidate disconnect (per-minute billing), so the audio pipe dies on
    # reload. The fix they recommend is to start a FRESH AvatarSession on
    # reconnect — the old one already disconnected, so concurrency is unaffected.
    _avatar_holder: list = []

    # AVATAR_ENABLED=false → voice-only (room publishes audio directly). Reliable
    # on unstable networks: with the avatar active ALL voice routes through the
    # Simli worker, so a broken agent→avatar pipe leaves the candidate with text
    # but no voice. Voice-only has no such single point of failure.
    avatar_enabled = os.environ.get("AVATAR_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    # AVATAR_FALLBACK_TO_ROOM_AUDIO=true → if the avatar cannot be (re)started,
    # publish Sarah's voice via a normal room audio track so the candidate still
    # HEARS her (the face just won't animate). Safety net — toggle for testing
    # and future use. Default on.
    avatar_fallback_enabled = os.environ.get("AVATAR_FALLBACK_TO_ROOM_AUDIO", "true").strip().lower() not in ("false", "0", "no")
    # AVATAR_WATCHDOG_ENABLED=true → probe avatar health mid-interview and
    # self-heal (restart once, then room-audio fallback) instead of waiting
    # for a candidate reconnect to fix a dead avatar.
    watchdog_enabled = os.environ.get("AVATAR_WATCHDOG_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    simli_api_key = os.environ.get("SIMLI_API_KEY", "")
    simli_face_id = os.environ.get("SIMLI_FACE_ID", "")

    # Mutable avatar state — the watchdog / heal path flips "active" mid-stream.
    # "restarts" budgets mid-stream restarts (reconnect restarts are unbudgeted).
    avatar_state = {"active": False, "restarts": 0}

    async def _start_avatar() -> bool:
        """Create + start a FRESH Simli AvatarSession bound to the live session.
        Used at startup AND on every reconnect. Returns True if active.
        avatar.start() re-points session.output.audio to the new avatar's data
        stream, which is exactly what re-links the audio pipe after a reload."""
        if not avatar_enabled:
            return False
        if not (simli_api_key and simli_face_id):
            logger.info("Simli keys not set — voice-only mode", extra={"interview_id": interview_id})
            return False
        if not _session_holder:
            return False
        session_ref = _session_holder[0]
        # Detach any previous (now-disconnected) avatar first so its stale
        # conversation_item_added listener and join task are released.
        if _avatar_holder:
            old = _avatar_holder.pop()
            try:
                await old.aclose()
            except Exception as e:
                logger.debug(f"[avatar] old avatar aclose ignored: {e}")
        try:
            av = simli.AvatarSession(
                simli_config=simli.SimliConfig(api_key=simli_api_key, face_id=simli_face_id),
                avatar_participant_identity="simli-avatar",
                avatar_participant_name="Sarah",
            )
            await av.start(
                session_ref,
                room=ctx.room,
                livekit_url=os.environ.get("LIVEKIT_URL", ""),
                livekit_api_key=os.environ.get("LIVEKIT_API_KEY", ""),
                livekit_api_secret=os.environ.get("LIVEKIT_API_SECRET", ""),
            )
            _avatar_holder.append(av)
            logger.info("Simli avatar active (official plugin)", extra={"interview_id": interview_id})
            return True
        except Exception as e:
            logger.warning(
                f"Simli avatar failed to start — {e}",
                extra={"interview_id": interview_id},
            )
            return False

    async def _fallback_to_room_audio() -> None:
        """Safety net: route Sarah's voice through a room audio track when the
        avatar cannot be (re)started, so the candidate still hears her even if
        the face is gone. Toggle via AVATAR_FALLBACK_TO_ROOM_AUDIO."""
        if not avatar_fallback_enabled or not _session_holder:
            return
        session_ref = _session_holder[0]
        try:
            from livekit import rtc
            from livekit.agents.voice.room_io._output import _ParticipantAudioOutput
            room_audio = _ParticipantAudioOutput(
                room=ctx.room,
                sample_rate=24000,
                num_channels=1,
                track_publish_options=rtc.TrackPublishOptions(
                    source=rtc.TrackSource.SOURCE_MICROPHONE
                ),
            )
            await room_audio.start()
            session_ref.output.audio = room_audio
            logger.info(
                "[avatar] fallback ACTIVE — voice now via room audio (face disabled)",
                extra={"interview_id": interview_id},
            )
        except Exception as e:
            logger.error(
                f"[avatar] room-audio fallback failed: {e}",
                extra={"interview_id": interview_id},
            )

    # ── AVATAR WATCHDOG: mid-stream self-heal (not just on reconnect) ─────────
    # Failure covered: the agent→Simli pipe dies DURING the interview (Simli
    # worker crash / publisher connection timeout). All voice routes through
    # the avatar participant, so the candidate silently stops hearing Sarah.
    # The watchdog probes health every few seconds while the candidate is
    # connected; on failure it restarts the avatar once, then falls back to
    # room audio permanently. Logic lives in crash_recovery.AvatarWatchdog —
    # these are the thin hooks it needs.
    _avatar_heal_lock = asyncio.Lock()

    def _candidate_in_room() -> bool:
        try:
            return any(
                str(getattr(p, "identity", "")).startswith("candidate-")
                for p in ctx.room.remote_participants.values()
            )
        except Exception:
            return False

    def _avatar_alive() -> bool:
        """Cheap sync health probe — no awaits, safe to call every few seconds."""
        if not avatar_state["active"] or not _avatar_holder:
            return False
        # 1) The simli-avatar participant must still be in the room
        try:
            if not any(
                str(getattr(p, "identity", "")) == "simli-avatar"
                for p in ctx.room.remote_participants.values()
            ):
                return False
        except Exception:
            pass  # can't read participants — don't false-alarm
        # 2) No internal task of the avatar session / audio pipe died with an
        #    error (catches the frozen-pipe case where the participant stays).
        try:
            if crash_recovery.has_dead_task(_avatar_holder[0]):
                return False
            if _session_holder:
                out = getattr(_session_holder[0].output, "audio", None)
                if out is not None and crash_recovery.has_dead_task(out):
                    return False
        except Exception:
            pass
        return True

    async def _heal_avatar() -> None:
        """Restart the avatar once mid-stream; afterwards (or if the restart
        fails) fall back to room audio so the candidate keeps hearing Sarah.
        Idempotent — concurrent triggers (probe + fast path) collapse to one."""
        if _avatar_heal_lock.locked():
            return
        async with _avatar_heal_lock:
            if not avatar_state["active"]:
                return  # already healed / voice-only
            logger.warning(
                "[avatar-watchdog] avatar unhealthy mid-interview — self-healing",
                extra={"interview_id": interview_id},
            )
            restarted = False
            if avatar_state["restarts"] < 1:  # one live restart, then permanent fallback
                avatar_state["restarts"] += 1
                restarted = await _start_avatar()
            if restarted:
                avatar_watchdog.avatar_started()
                logger.info(
                    "[avatar-watchdog] avatar restarted mid-stream ✓",
                    extra={"interview_id": interview_id},
                )
            else:
                avatar_state["active"] = False
                avatar_watchdog.stop()
                await _fallback_to_room_audio()
                logger.warning(
                    "[avatar-watchdog] avatar could not be restarted — voice "
                    "continues via room audio (face disabled)",
                    extra={"interview_id": interview_id},
                )

    avatar_watchdog = crash_recovery.AvatarWatchdog(
        is_avatar_alive=_avatar_alive,
        is_candidate_present=_candidate_in_room,
        on_failure=_heal_avatar,
        interview_id=interview_id,
    )

    # ── FIX 4: Create hr_agent BEFORE on_shutdown ─────────────────────────────
    # Previously hr_agent was created AFTER on_shutdown was defined and
    # registered. If shutdown fired before hr_agent was assigned, the closure
    # raised UnboundLocalError and the status was never written to DB.
    # Now hr_agent is created first — the closure reference is always valid.
    hr_agent = HRInterviewAgent(
        graph_state=graph_state,
        interview_id=interview_id,
        tenant_id=tenant_id,
        job_context=job_context,
    )

    # ── VOICE CAPTURE HOOK: record the candidate's audio to a local WAV so the
    # post-interview voice-analysis engine can score delivery for voice-heavy
    # roles. Fully isolated (audio_capture.py); every failure is swallowed.
    recorder = audio_capture.CandidateAudioRecorder(interview_id)

    # ── Shutdown callback ──────────────────────────────────────────────────────
    async def on_shutdown() -> None:
        avatar_watchdog.stop()

        # ── GUARD: never finalize an interview the candidate did not attend ────
        # This job can end without anyone joining (dispatched at trigger time,
        # room emptied, worker restart). Previously this callback unconditionally
        # wrote status="completed", so the candidate's join link reported
        # "interview already completed" before they had attended — and an
        # evaluation + report were generated from an empty transcript.
        #
        # interview_start_time[0] is set only when the candidate actually joins
        # (on_participant_connected), so it is the authoritative attendance
        # signal. When absent, leave the status untouched: "scheduled" keeps the
        # link working, and a genuinely abandoned "in_progress" row is finalized
        # later by recover_stuck_interviews / expire_abandoned_interviews.
        if interview_start_time[0] is None:
            logger.info(
                "[shutdown] no candidate attendance — status left unchanged "
                "(no completion, no evaluation, no report)",
                extra={"interview_id": interview_id, "room": room_name},
            )
            try:
                await recorder.stop()
            except Exception:
                pass
            return

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

            # ── VOICE CAPTURE HOOK: stop recording + persist the local WAV path
            # to interviews.recording_s3_key BEFORE evaluation, so voice_analysis
            # (invoked inside run_evaluation) can read it. Non-fatal on any error.
            try:
                rec_path = await recorder.stop()
                if rec_path:
                    factory = _get_db_factory()
                    if factory:
                        async with factory() as session:
                            await session.execute(
                                text(
                                    "UPDATE interviews SET recording_s3_key = :p, "
                                    "updated_at = :now WHERE id = :id"
                                ),
                                {"p": rec_path, "now": datetime.now(timezone.utc), "id": interview_id},
                            )
                            await session.commit()
                        logger.info(
                            f"[audio-capture] recording path saved: {rec_path}",
                            extra={"interview_id": interview_id},
                        )
            except Exception as e:
                logger.warning(
                    f"[audio-capture] stop/persist failed (non-fatal): {e}",
                    extra={"interview_id": interview_id},
                )

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

            # ── Per-interview cost: record live usage → finalize → log to terminal ──
            # Runs LAST so conversation + strategy + eval usage are all present.
            try:
                s = usage_collector.get_summary()
                await cost_tracker.patch_usage(interview_id, tenant_id, {
                    "llm_in":           getattr(s, "llm_input_tokens", 0) or 0,
                    "llm_out":          getattr(s, "llm_output_tokens", 0) or 0,
                    "tts_chars":        getattr(s, "tts_characters_count", 0) or 0,
                    "stt_seconds":      round(getattr(s, "stt_audio_duration", 0) or 0, 1),
                    "duration_seconds": duration or 0,
                    "avatar_seconds":   (duration or 0) if avatar_active else 0,
                })
                await cost_tracker.finalize_and_log(interview_id)
            except Exception as e:
                logger.warning(
                    f"[cost] finalize in shutdown failed (non-fatal): {e}",
                    extra={"interview_id": interview_id},
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
        # VOICE CAPTURE HOOK: start recording the candidate's audio track.
        # In this SDK, track.kind is an INTEGER enum (1 = audio, 2 = video) that
        # stringifies to a number — the logs show "kind=1" for candidate audio.
        # Match the enum value (with an int fallback), NOT a "audio" string.
        try:
            from livekit import rtc as _rtc
            _audio_kind = _rtc.TrackKind.KIND_AUDIO
        except Exception:
            _audio_kind = 1
        _tk = getattr(track, "kind", None)
        _is_audio = (_tk == _audio_kind) or (_tk == 1)
        if _is_audio and str(getattr(participant, "identity", "")).startswith("candidate-"):
            logger.info(
                "[audio-capture] candidate audio track detected — starting recorder",
                extra={"interview_id": interview_id},
            )
            recorder.start(track)

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

    # ── CRASH RECOVERY: 60s grace period instead of instant shutdown ──────────
    # A page reload looks like a disconnect. Previously the agent shut down
    # immediately, killing the interview. Now finalization is delayed 60s —
    # if the candidate reconnects in time, the interview simply continues.
    async def _finalize_after_grace() -> None:
        result = ctx.shutdown()
        if asyncio.iscoroutine(result):
            await result

    grace_timer = crash_recovery.DisconnectGraceTimer(
        on_expired=_finalize_after_grace,
        grace_seconds=60.0,
        interview_id=interview_id,
    )

    # ── Mark in_progress when candidate joins (+ re-greet on reconnect) ───────
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant) -> None:
        if not interview_id or not tenant_id:
            return
        if participant.identity == "simli-avatar":
            return
        if not participant.identity.startswith("candidate-"):
            return

        # CRASH RECOVERY HOOK: was this a reconnect within the grace period?
        is_reconnect = grace_timer.candidate_returned()

        # Only set the start time on the FIRST join — a reconnect must not
        # overwrite it (would corrupt the duration calculation).
        if interview_start_time[0] is None:
            interview_start_time[0] = datetime.now(timezone.utc)
            status_task = asyncio.create_task(
                _update_interview_status(
                    interview_id, "in_progress",
                    started_at=interview_start_time[0],
                )
            )
            _pending_tasks.add(status_task)
            status_task.add_done_callback(_pending_tasks.discard)

        logger.info(
            "Candidate joined room" + (" (RECONNECT)" if is_reconnect else ""),
            extra={"identity": participant.identity, "interview_id": interview_id},
        )

        # Sarah re-greets: "Welcome back…" and re-asks the current question.
        # IMPORTANT: with the avatar active, Simli tore it down on the disconnect,
        # so we must RESTART it (or fall back to room audio) BEFORE re-greeting —
        # otherwise the speech streams to a dead avatar participant and is silent.
        # generate_reply() is SYNC (returns a SpeechHandle) — do not wrap the call
        # itself in create_task. The task reference MUST be held (_pending_tasks)
        # — an unreferenced asyncio task can be GC'd mid-await and silently vanish.
        stage = hr_agent._graph_state.get("stage", "intro")
        if is_reconnect and _session_holder and stage not in ("complete", "wrap_up"):

            async def _resume() -> None:
                # Re-link the audio pipe first.
                if avatar_enabled:
                    restarted = await _start_avatar()
                    avatar_state["active"] = restarted
                    if restarted:
                        if watchdog_enabled:
                            avatar_watchdog.avatar_started()
                            avatar_watchdog.start()
                    else:
                        # Avatar couldn't come back — keep the candidate in audio.
                        avatar_watchdog.stop()
                        await _fallback_to_room_audio()
                    # Let the fresh avatar (or room track) settle before speaking.
                    await asyncio.sleep(1.5)
                else:
                    # Voice-only: brief settle so the browser re-attaches audio.
                    await asyncio.sleep(1.0)

                logger.info(
                    f"[crash-recovery] re-greeting candidate (stage={stage})",
                    extra={"interview_id": interview_id},
                )
                try:
                    handle = _session_holder[0].generate_reply(
                        instructions=crash_recovery.resume_greeting_instructions(
                            candidate_name, stage
                        )
                    )
                    logger.info(
                        f"[crash-recovery] re-greet speech scheduled (handle={handle.id})",
                        extra={"interview_id": interview_id},
                    )
                except Exception as e:
                    logger.warning(
                        f"[crash-recovery] re-greet failed (interview continues): {e}",
                        extra={"interview_id": interview_id},
                    )

            task = asyncio.create_task(_resume(), name="crash-recovery-resume")
            _pending_tasks.add(task)
            task.add_done_callback(_pending_tasks.discard)

    # ── Candidate leaves → start grace countdown (NOT instant shutdown) ───────
    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant) -> None:
        # Only react to the candidate leaving — ignore avatar/other agents
        if participant.identity == "simli-avatar":
            # AVATAR WATCHDOG fast path: the avatar dropping while the candidate
            # is still here is a mid-stream failure — heal now, don't wait for
            # the next probe. (No-op if the candidate left first: that teardown
            # is expected, and the reconnect path owns the restart.)
            avatar_watchdog.notify_avatar_left()
            return
        if not participant.identity.startswith("candidate-"):
            return

        # CRASH RECOVERY HOOK: don't finalize yet — give them 60s to return.
        # If they don't come back, _finalize_after_grace() runs ctx.shutdown(),
        # which marks completed, drains transcripts, and runs evaluation.
        grace_timer.candidate_left()

    # ── Wait for the candidate before ANY expensive setup ─────────────────────
    # See CANDIDATE_JOIN_TIMEOUT: this job may have been dispatched at trigger
    # time, with the candidate still hours away from clicking their link. Without
    # this wait the agent greeted an empty room, burned a Simli/TTS session, and
    # then shut down — which stamped the interview "completed" so the join link
    # reported "interview already completed" before the candidate ever attended.
    #
    # Placement matters: AFTER the participant handlers above (so a candidate who
    # arrives during the wait is picked up by on_participant_connected) and BEFORE
    # the VAD / AgentSession / avatar setup below (so nothing is spun up for a
    # room nobody joins).
    if interview_id:
        _candidate_identity = f"candidate-{interview_id}"
        _participant = None
        try:
            _participant = await asyncio.wait_for(
                ctx.wait_for_participant(identity=_candidate_identity),
                timeout=CANDIDATE_JOIN_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.info(
                f"[join-wait] no candidate within {CANDIDATE_JOIN_TIMEOUT:.0f}s — "
                "ending job WITHOUT finalizing; interview stays joinable",
                extra={"interview_id": interview_id, "room": room_name},
            )
            return
        except Exception as e:
            # Never let a wait failure block a real interview — fall through and
            # let the normal participant handlers drive the session.
            logger.warning(
                f"[join-wait] wait_for_participant failed ({e}) — continuing anyway",
                extra={"interview_id": interview_id},
            )

        # wait_for_participant returns IMMEDIATELY when the candidate is already
        # in the room — which is the normal case once the room is (re)created by
        # the candidate joining. rtc.Room only fires participant_connected for
        # participants arriving AFTER we connect, so that path would otherwise
        # skip the in_progress write and leave interview_start_time unset (which
        # the on_shutdown guard reads as "never attended"). Run it explicitly.
        if _participant is not None and interview_start_time[0] is None:
            logger.info(
                "[join-wait] candidate already present at job start — "
                "running join handler explicitly",
                extra={"interview_id": interview_id},
            )
            on_participant_connected(_participant)

    _lap("candidate present")

    # ── VAD: reuse the model prewarmed at worker boot ─────────────────────────
    # Fallback to loading it here if prewarm didn't run (e.g. an execution mode
    # that skips prewarm_fnc) — correctness never depends on the optimisation.
    _vad = None
    try:
        _vad = ctx.proc.userdata.get("vad")
    except Exception:
        _vad = None
    if _vad is None:
        logger.warning("[startup] VAD not prewarmed — loading now (slower join)")
        _vad = _build_vad()
    _lap("vad ready")

    # ── AgentSession ──────────────────────────────────────────────────────────
    session = AgentSession(
        vad=_vad,
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
        tts=deepgram.TTS(
            api_key=os.environ["DEEPGRAM_API_KEY"],
            model="aura-2-vesta-en",   # Sarah — warm female Aura-2 voice
            sample_rate=24000,          # matches the room-audio fallback path
        ),
        allow_interruptions=True,
        # Lever A — require more real words before a barge-in stops Sarah mid-sentence.
        # 4 words let a stray "uh, okay yeah right" (or an echo fragment) cut her off;
        # 6 means it takes a deliberate utterance to interrupt.
        min_interruption_words=6,
        min_endpointing_delay=0.4,
        max_endpointing_delay=6.0,
        aec_warmup_duration=0,
    )
    # CRASH RECOVERY HOOK: let the reconnect handler reach the session for re-greeting
    _session_holder.append(session)

    # ── COST TRACKING: aggregate real LLM/STT/TTS usage across the session ──────
    # The SDK emits one metrics event per model step; UsageCollector sums tokens,
    # TTS characters, and STT audio duration. Read in on_shutdown to compute cost.
    @session.on("metrics_collected")
    def _on_metrics(ev) -> None:
        try:
            usage_collector.collect(ev.metrics)
        except Exception:
            pass  # usage aggregation must never disturb the live pipeline

    # ── Avatar via OFFICIAL livekit-plugins-simli (must start BEFORE session) ──
    # The official plugin dispatches the avatar as a SEPARATE LiveKit worker that
    # renders video + republishes audio. It does NOT run on this event loop, so
    # it cannot starve the candidate-audio pipeline. When active, the avatar
    # publishes the agent's voice itself — so we disable the room's own audio
    # output to avoid double audio. Startup goes through _start_avatar() so the
    # exact same code path is reused on reconnect.
    if not avatar_enabled:
        logger.info("AVATAR_ENABLED=false — voice-only mode (reliable)", extra={"interview_id": interview_id})
    avatar_active = await _start_avatar()
    _lap("avatar start")
    avatar_state["active"] = avatar_active
    if avatar_active and watchdog_enabled:
        # AVATAR WATCHDOG: begin mid-stream health probing (self-heals a dead
        # avatar live instead of waiting for a candidate reconnect).
        avatar_watchdog.avatar_started()
        avatar_watchdog.start()
    if avatar_enabled and not avatar_active:
        logger.info(
            "[avatar] not active at startup — voice-only via room audio",
            extra={"interview_id": interview_id},
        )

    await session.start(
        room=ctx.room,
        agent=hr_agent,
        room_options=room_io.RoomOptions(
            # auto_gain_control=False disables the per-frame APM native call.
            audio_input=room_io.AudioInputOptions(auto_gain_control=False),
            # When avatar is active it republishes the audio itself → disable
            # the room's direct audio output to prevent double audio.
            audio_output=(not avatar_active),
            # CRASH RECOVERY: the SDK default (True) closes the AgentSession the
            # instant the candidate disconnects — which kills the session before
            # the 60s grace timer can do its job ("AgentSession isn't running"
            # on reconnect). The grace timer owns finalization via ctx.shutdown().
            close_on_disconnect=False,
        ),
    )
    _lap("session.start")

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

    # CRASH RECOVERY HOOK: if this agent job is resuming a crashed interview,
    # Sarah says "Welcome back…" and re-asks the current stage's question
    # instead of restarting the interview from the introduction.
    if resume_state:
        greeting = crash_recovery.resume_greeting_instructions(
            candidate_name, graph_state.get("stage", "intro")
        )
    else:
        _role = (ctx_data.get("job") or {}).get("position_title")
        _role_line = (
            f"Briefly mention this screening is for the {_role} position. "
            if _role else ""
        )
        greeting = (
            "Greet the candidate warmly. Welcome them to their screening interview. "
            f"Their name is {candidate_name}. "
            + _role_line +
            "Mention that the session will be recorded and ask for their consent to proceed. "
            "Be natural and friendly — like a real HR person starting a call."
        )

    _lap("greeting: LLM start")
    await session.generate_reply(instructions=greeting)
    _lap("greeting: SPOKEN")   # ← total time from job start to Sarah speaking


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            # Loads the Silero VAD once at worker boot instead of per interview.
            prewarm_fnc=prewarm,
            api_key=os.environ["LIVEKIT_API_KEY"],
            api_secret=os.environ["LIVEKIT_API_SECRET"],
            ws_url=os.environ["LIVEKIT_URL"],
        )
    )
