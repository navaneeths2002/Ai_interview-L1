# Interview Agent — Technical Knowledge Base

Deep implementation reference. Read this before making changes to any core file.
Every technical choice is documented here with the reason it was made.

---

## 1. Overall Architecture Pattern

### Why microservice, not monolith?

The HRMS SaaS platform already has existing services: Resume Parser, ATS Scoring Engine, JD Generator. Each is a separate REST API. This Interview Agent is intentionally built as a standalone microservice so it can:
- Be deployed, scaled, and restarted independently
- Fail without taking down the rest of the platform
- Be called by any service in the platform via HTTP

### Why two processes (FastAPI + Agent Worker)?

The voice agent (LiveKit) needs to run as a **persistent worker** that stays connected to LiveKit Cloud and waits for jobs. FastAPI is a request-response server. They have fundamentally different lifecycles — mixing them in one process causes resource conflicts and shutdown issues. Keeping them separate means:
- FastAPI can restart without disconnecting active interviews
- The agent worker can crash and restart without affecting the API
- In production, multiple agent workers can run in parallel for concurrent interviews

---

## 2. Web Framework — FastAPI

**Why FastAPI and not Django/Flask?**

| Requirement | FastAPI | Django | Flask |
|---|---|---|---|
| Async I/O (DB, HTTP calls) | Native async | Bolt-on | Bolt-on |
| Request validation | Pydantic built-in | Separate library | Separate library |
| Auto API docs | Built-in | Separate | Separate |
| Speed | Very fast | Slow | Medium |

This service makes many async calls simultaneously (DB writes, LiveKit API, Claude API, Deepgram). FastAPI is built on `asyncio` natively — all of these run concurrently without blocking.

---

## 3. Database — PostgreSQL + SQLAlchemy 2.0 async

**Why PostgreSQL?**
- Already used by the existing HRMS platform
- `JSONB` column type: stores flexible structured data (ATS score breakdowns, extracted interview data, full report payload) with full indexing support
- `ARRAY(Text)` column type: stores skill lists, preferred locations natively
- Strong UUID primary key support
- `pgvector` extension available for future semantic search over transcripts

**Why SQLAlchemy 2.0?**
- Proper `async` support with `asyncpg` driver
- Alembic (same ecosystem) handles migrations automatically
- `Mapped[type]` syntax gives full type hints — IDE catches column name errors

**Why `asyncpg` driver?**
Fastest PostgreSQL driver for Python — written in Cython. 3–5× faster than `psycopg2` for async workloads. SQLAlchemy URL: `postgresql+asyncpg://`

**Why `psycopg2-binary` as well?**
Alembic runs synchronously and cannot use the async `asyncpg` driver. `psycopg2-binary` is used only by Alembic during `alembic upgrade head`.

**Why UUID primary keys?**
Globally unique, safe to generate in Python before inserting, safe for distributed systems.

---

## 4. Multi-Tenancy Pattern

**Why `X-Tenant-ID` header?**
The HRMS platform has multiple companies (tenants). Every table has a `tenant_id` column. `TenantMiddleware` reads the header and tags all DB writes.

**Routes excluded from tenant check:**
- `/health`, `/docs`, `/openapi.json`, `/redoc`
- `/interview/*`, `/static/*`
- `/api/v1/interviews/{id}/token` — candidate doesn't know their tenant
- `/api/v1/interviews/{id}/report/html` — browser can't send custom headers on direct URL open

---

## 5. Voice Pipeline — LiveKit

**Why LiveKit?**
- Open source with self-host option
- `livekit-agents` Python framework: handles the entire VAD→STT→LLM→TTS pipeline with clean API
- First-class India region support
- Cost-effective compared to Twilio/Agora

**Why `iceTransportPolicy: "relay"` in the browser?**
Corporate firewalls block WebRTC UDP. Forcing relay mode uses LiveKit's TURN servers over TCP/443 — always open, works everywhere.

**Why `max_participants=3`?**
Three participants join every room: the agent worker, the candidate browser, and the Simli avatar (`simli-avatar` identity). Lowering this would prevent Simli from joining.

---

## 6. Speech-to-Text — Deepgram

**Why Deepgram and not OpenAI Whisper?**
OpenAI Whisper is batch-only — no real-time streaming. A 1–3 second STT delay makes conversation feel broken. Deepgram streams partial transcripts as the candidate speaks.

**Why `nova-2-general` with language `"en"`?**
Originally `nova-3` + `en-IN` was used. During mic-detection debugging it was switched to `nova-2-general` with the broader `"en"` language code — wider detection envelope, fewer dropped utterances. This is the current production setting in `agent.py`.

**Why `endpointing_ms=200`?**
Default 25ms cuts off candidates who pause briefly to think. Originally 300ms; reduced to 200ms for faster turn response while still allowing natural pauses.

**Why `interim_results=True`?**
Deepgram sends partial transcripts while candidate is still speaking. The livekit-agents framework uses these to start preemptive LLM generation — reduces perceived response latency.

---

## 7. LLM — Anthropic Claude

**Model selection:**
| Task | Model | Reason |
|---|---|---|
| Real-time conversation | `claude-haiku-4-5-20251001` | Fastest, lowest latency |
| Strategy generation | `claude-haiku-4-5-20251001` | One-time call, medium complexity |
| Post-interview evaluation | `claude-haiku-4-5-20251001` | Only confirmed-available on this account |

**IMPORTANT:** Only `claude-haiku-4-5-20251001` is confirmed available on this Anthropic account.
`claude-3-5-sonnet-20241022` and `claude-sonnet-4-5-20251001` return 404 on this key.

**Why load_dotenv() explicitly in agent worker?**
The agent worker is a separate process from FastAPI. FastAPI's lifespan does not run. `load_dotenv()` must be called at module top in `agent.py`. Also, `anthropic.LLM(api_key=os.environ["ANTHROPIC_API_KEY"])` must pass the key explicitly — the SDK does not auto-read it in the worker process context.

---

## 8. Text-to-Speech — ElevenLabs

**Why ElevenLabs?**
Best voice quality. Most natural-sounding output. Critical when candidates are supposed to believe they're talking to a near-human interviewer.

**Why `eleven_turbo_v2_5`?**
~300ms latency vs ~600ms for `eleven_multilingual_v2`. Right balance for live conversation.

**VoiceSettings rationale:**
```python
stability=0.45        # Lower = more natural variation. 0.9 sounds robotic.
similarity_boost=0.80 # Keeps Sarah's voice character.
style=0.35            # Professional-level expressiveness. 0 = flat.
speed=0.95            # Slightly slower than default. Natural HR speech pace.
```

**Voice ID not name:**
ElevenLabs API requires the voice ID hex string (e.g., `EXAVITQu4vr4xnSDxMaL`), not display name. Display names can change; IDs are permanent.

---

## 9. Voice Activity Detection — Silero VAD

Neural network-based VAD — understands speech patterns, not just volume. Much lower false-positive rate than WebRTC VAD (energy-based). Fewer accidental interruptions from keyboard clicks or breathing.

---

## 10. Natural Behaviour System (HRInterviewAgent)

**Why subclass `Agent` and override `on_user_turn_completed`?**
Lets us inject behaviour between STT completion and LLM generation without touching the pipeline mechanics.

**Why random pre-response delay (300–600ms)?**
Humans don't respond in 0ms. An instant response feels robotic. Also prevents premature response if Deepgram sends a slightly early final transcript.

**Why 20% filler probability?**
At higher rates, fillers become a predictable pattern — candidates notice. At 20% (~every 5 turns), they feel natural.

**Why `min_interruption_words=4`?**
Single coughs, "yeah", or background noise shouldn't stop the AI mid-sentence. Raised from 2 to 4 after testing — short acknowledgments ("okay", "I see") were falsely interrupting Sarah.

**Why the mishear guard only fires on EMPTY text?**
The original guard rejected anything ≤1 word — which discarded valid short answers like "yes", "no", "okay". Now only truly empty transcripts trigger "Could you say that again?".

**Why AI messages are captured via `session.on("conversation_item_added")` — NOT `turn_ctx`?**
In livekit-agents 1.x, `on_user_turn_completed`'s `turn_ctx` is a **temporary copy** (`chat_ctx.copy()`) made before the LLM call. The assistant reply is written to the agent's REAL chat context, never to the copy — so reading `turn_ctx` after `super()` always returned nothing and AI turns were silently never saved. The fix: listen to the session-level `conversation_item_added` event, filter for `"assistant"` in the item role, and queue the transcript save there. This was one of the hardest-to-find bugs in the project.

**Why VAD `activation_threshold=0.1`?**
Default thresholds missed quiet/degraded mic audio (laptop mics on corporate WiFi with relay-only WebRTC). 0.1 is very sensitive — catches everything; Deepgram filters out non-speech downstream.

---

## 11. LangGraph State Machine

**Why LangGraph and not a simple if/else stage machine?**
LangGraph gives us a proper directed graph with typed state, async node execution, conditional edges, and built-in state persistence. As the interview grows more complex (branching, parallel paths, memory), LangGraph scales; a hand-rolled if/else machine does not.

**State injection into LLM:**
`HRInterviewAgent.instructions` is a Python `@property` that returns `BASE_SYSTEM_PROMPT + stage_instruction`. The `instructions` property on `Agent` is read by livekit-agents on every LLM call — this is the hook point for dynamic context.

**Max 3 turns per stage:**
Prevents the interview from getting stuck if a candidate repeatedly gives vague answers. After 3 attempts the graph force-advances to keep the interview flowing.

---

## 12. Evaluation Engine (Phase 6)

**File:** `app/services/evaluation_engine.py`

**Why its own AsyncSessionLocal?**
The evaluation engine is called as `asyncio.create_task()` from the agent worker process — completely outside the FastAPI request lifecycle. It cannot receive a FastAPI `db` dependency. It creates its own `AsyncSessionLocal` from `DATABASE_URL`.

**ATS score has no interview_id:**
`AtsScore` is keyed by `(candidate_id, job_id)`. Must query with `and_()` clause. There is no foreign key to `interviews`.

**raw_extraction JSONB:**
The Claude response is saved verbatim as JSONB in `InterviewExtractedData.raw_extraction`. This includes `summary`, `strengths`, `weaknesses`, `red_flags`. The report generator reads this to avoid a second Claude API call.

**Why fire-and-forget?**
Evaluation takes 2–5 seconds (Claude API call + DB writes). The interview session is already over at this point. Running it in a background task lets the agent worker clean up the room immediately without waiting.

### Weighted overall score — computed in CODE, not by Claude

Claude returns only the four per-dimension scores (1–10). The overall score (0–100) is recalculated exactly in Python after JSON parsing — because an LLM asked to "apply weights" approximates and drifts by ±2 points:

| Dimension | Weight |
|---|---|
| JD Fit | 35% |
| Communication | 25% |
| Behavioral | 15% |
| Confidence | 15% |
| ATS Boost | 10% |

```python
weighted   = (jd_fit*35 + communication*25 + behavioral*15 + confidence*15) / 10   # → 0–90
ats_boost  = (ats_score / 100) * 10                                                # → 0–10
overall    = max(0, min(100, round(weighted + ats_boost)))
```

JD Fit carries the most weight because L1 screening exists to answer one question: does this candidate match the job? The ATS boost rewards candidates the pre-screen already ranked highly.

**Certifications gotcha:** the resume parser returns certifications as dicts (`{certification_name: ...}`), not strings. The prompt builder extracts `certification_name` — passing raw dicts caused `TypeError: expected str`.

---

## 13. Report Generator (Phase 7)

**File:** `app/services/report_generator.py`

**Why no extra Claude call?**
The evaluation engine already calls Claude and saves a rich `raw_extraction` JSONB with everything needed for the report narrative. Re-calling Claude would cost extra tokens and time for no benefit.

**Why self-contained HTML (inline CSS)?**
The HTML is stored in the database and served directly from a route. No external CDN, no separate CSS file, no JavaScript dependency. It renders correctly even offline or in a restricted browser environment.

**Why browser print-to-PDF instead of server-side PDF generation?**
Server-side PDF generation (WeasyPrint, Puppeteer) adds heavy dependencies and complexity. Browser print-to-PDF is free, produces excellent output, works everywhere, and needs zero extra packages.

**Why `/report/html` is excluded from tenant middleware:**
Browsers cannot add custom headers when navigating to a URL directly. The report URL is intended to be opened in a browser — requiring a tenant header would make it inaccessible without Postman.

**Why a signed report token instead of a fully open URL:**
An open URL means anyone who guesses/leaks an interview UUID can read a candidate's full evaluation. The route now requires `?token=` — a JWT (`type: report`, HS256, 7-day expiry) created by `create_report_token()` when the report is generated and embedded into `report_url`. `verify_report_token()` checks signature, expiry, type, and interview_id match. Same pattern as the candidate invite token — auth via signed URL where headers are impossible.

---

## 14. Simli Avatar (Phase 7.5 → 10.5)

**Current implementation:** official `livekit-plugins-simli==1.5.11` plugin, used directly in `agent.py`.
**Legacy file:** `app/realtime/avatar_session.py` — custom bridge, NO LONGER USED (kept for reference).

### Why Simli and not HeyGen/Tavus/D-ID?

| Tool | Cost/min | LiveKit native | Accept raw audio | Verdict |
|---|---|---|---|---|
| **Simli** | $0.009 | ✅ official plugin | ✅ PCM direct | **Chosen** |
| Tavus | $0.32–0.59 | ✅ plugin | ✅ | Quality but expensive |
| HeyGen | $0.10–0.20 | ⚠️ Beta | ❌ needs own TTS | Architecture mismatch |
| D-ID | $5.90+ | ❌ | ⚠️ Complex | Prohibitive cost |

Simli is the only provider designed as pure audio-in → face-video-out with no opinion about LLM or TTS. ElevenLabs PCM bytes feed directly in. Cost is negligible (~₹0.75 per 30-min interview).

### Why the official plugin replaced the custom bridge — CRITICAL LESSON

The first implementation (`avatar_session.py`) ran Simli **in-process**: a custom `SimliAudioForwarder` tapped TTS audio, and a `_VideoOnlyPublisher` pumped 30fps video frames through the agent's own event loop. The `frame.to_ndarray().tobytes()` conversion at 30fps **starved the asyncio event loop** — candidate microphone audio frames were never processed, VAD never fired, and the agent logged "input speech hasn't started yet" forever. **Symptom: mic worked perfectly without the avatar, died the moment the avatar started.** Thread-pool offloading and `auto_gain_control=False` reduced but did not eliminate the starvation.

The official plugin fixes this architecturally:

```python
avatar = simli.AvatarSession(
    simli_config=simli.SimliConfig(api_key=..., face_id=...),
    avatar_participant_identity="simli-avatar",
    avatar_participant_name="Sarah",
)
await avatar.start(session, room=ctx.room, livekit_url=..., livekit_api_key=..., livekit_api_secret=...)
```

- The avatar runs as a **separate LiveKit worker** — zero work on the agent's event loop, so candidate audio processing is never starved.
- It must be started **BEFORE `session.start()`** (it wires itself into the session's output).
- The `simli-avatar` participant publishes **both** lip-synced video AND the agent's voice audio.
- Because the avatar republishes the voice, the room's own audio output must be disabled to avoid double audio:

```python
await session.start(
    room=ctx.room,
    agent=hr_agent,
    room_options=room_io.RoomOptions(
        audio_input=room_io.AudioInputOptions(auto_gain_control=False),
        audio_output=(not avatar_active),   # avatar publishes voice itself
    ),
)
```

**Browser side (interview.html):** plays ALL audio tracks — including from the `simli-avatar` participant (the old `!isSimliAvatar` filter was removed; with the official plugin, the voice comes FROM the avatar participant). Video from `simli-avatar` is attached over the plasma canvas; plasma remains the fallback if avatar fails.

**Graceful degradation:** if `SIMLI_API_KEY`/`SIMLI_FACE_ID` is missing or `avatar.start()` throws, `avatar_active=False` → room audio output is enabled → voice-only interview. `AVATAR_ENABLED=false` forces voice-only outright. Avatar failure can never kill an interview.

**Reconnect handling:** Simli tears the avatar down on every candidate disconnect, so on reconnect the avatar is **restarted** (`_start_avatar()`), with an optional fallback to room audio (`AVATAR_FALLBACK_TO_ROOM_AUDIO`). See §15.5 Capability 4 for the full mechanism — this is the single most important thing to understand about the avatar.

---

## 15. interview.html Architecture

**Single HTML file — why?**
The candidate opens a URL in their browser. No build step, no npm, no bundler. A single self-contained file is deployed as a static asset and works anywhere.

**LiveKit JS SDK via CDN:**
```html
<script src="https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.min.js"></script>
```
The UMD build exposes `LivekitClient` as a global.

**Candidate name prefetch:**
```javascript
(async function prefetchInterviewInfo() {
  const res = await fetch('/api/v1/interviews/' + interviewId + '/token');
  cachedTokenData = await res.json();
  // update h-uname and h-av (initials avatar) in header
})();
```
Token endpoint is excluded from tenant middleware. The token itself is cached and reused when "Start Interview" is clicked — avoids double API call.

**Avatar state machine:**
`setAvatarMode(mode)` where mode ∈ `{idle, speaking, listening, thinking}`:
- Updates `plasmaMode` → drives plasma canvas animation colours/effects
- Updates `#av-glow` CSS class → drives box-shadow glow animation
- Controls waveform bar visibility

**Plasma canvas fallback:**
If Simli video never arrives (keys missing, service down), the animated plasma orb continues showing. When Simli video arrives, canvas is hidden and video is shown. The glow ring remains visible around both.

**Color tokens (CSS variables):**
```css
--pri: #2563EB      /* primary blue */
--ok:  #CA8A04      /* yellow (dark mode: #EAB308) */
--bg:  #EFF6FF      /* page background */
--bdr: #DBEAFE      /* border blue */
```
All components reference these — changing one token rebrands the whole page.

---

## 15.5 In-Interview Crash Recovery (Phase 11)

**File:** `app/realtime/crash_recovery.py`

### Design constraint: a SEPARATE unit
Explicit requirement: do not touch the agent pipeline internals. All recovery logic lives in one self-contained module with its **own DB engine** (`pool_size=2, max_overflow=3, pool_pre_ping`) so it never competes with the agent's DB pool. `agent.py` only calls thin hooks marked `CRASH RECOVERY HOOK`.

### Capability 1 — 60s disconnect grace period
**Problem:** a page reload fires `participant_disconnected`, which previously ran `ctx.shutdown()` instantly — the interview was finalized and could never continue.
**Fix:** `DisconnectGraceTimer`. `candidate_left()` starts a 60s asyncio countdown; `candidate_returned()` cancels it and returns `True` if a countdown was active (i.e., this is a reconnect → Sarah re-greets). Only if the countdown expires does `ctx.shutdown()` run the normal completion path.
**Tradeoff:** "End Interview" also looks like a disconnect → evaluation starts ~60s after the candidate leaves.

**CRITICAL companion setting — `close_on_disconnect=False`:**
The SDK's RoomIO defaults to `close_on_disconnect=True`, which closes the AgentSession the INSTANT the linked participant disconnects — before the grace timer can do anything. Symptom: grace timer logs "RECONNECTED within grace period" but the re-greet throws `RuntimeError: AgentSession isn't running`. The session must be kept alive across the disconnect:
```python
room_options=room_io.RoomOptions(..., close_on_disconnect=False)
```
Re-linking on reconnect is automatic: the candidate rejoins with the SAME identity (`candidate-{interview_id}`), and RoomIO's audio input stays subscribed to `track_subscribed` events matched by identity — the new mic track re-attaches with no extra code.

**`generate_reply()` is SYNC:** it returns a `SpeechHandle`, it is not a coroutine. Wrapping the call itself in `asyncio.create_task()` is wrong. The re-greet uses an async helper (delay so the avatar/audio re-attaches, then the sync call inside try/except).

**Hold the re-greet task reference:** the resume task does `await asyncio.sleep(...)`; an `asyncio.create_task()` whose result isn't referenced can be **garbage-collected mid-sleep** and silently vanish (symptom: re-greet never fires, no error). Kept alive in a `_pending_tasks` set with a done-callback to discard.

### Capability 2 — stage persistence
After every LangGraph advance (both voice and typed paths), `queue_save_stage()` fire-and-forgets the current state to `interview_contexts.question_flow` (an existing JSONB column — **no migration needed**):
```json
{"resume": {"stage": "notice_period", "turns_in_stage": 1,
            "captured": {"captured_experience": true, ...}, "saved_at": "..."}}
```
Saves are non-fatal by design — a DB hiccup can never break the live interview.

### Capability 3 — resume on a fresh agent job
If the agent process crashes, LiveKit dispatches a new job when the candidate rejoins the room. The entrypoint calls `load_resume_state()`:
- Returns the saved payload **only if** status is still `in_progress` AND the saved stage is meaningful (not `intro`/`complete`) — otherwise the interview starts normally.
- `apply_resume()` restores stage, turn count, and capture flags, and **regenerates `stage_instruction`** via `interview_graph._make_instruction()` so Claude focuses on the right topic immediately.
- The greeting is swapped for `resume_greeting_instructions()`: *"Welcome back, {name}! … we'll continue right where we left off"* + re-asks the current stage's question. A `_STAGE_TOPIC` dict maps each stage to a human-readable topic for the re-ask.

### Capability 4 — avatar restart on reconnect (`_start_avatar` / `_fallback_to_room_audio` in agent.py)
**The hardest bug.** When the avatar is active, `avatar.start()` sets `session.output.audio = DataStreamAudioOutput(destination_identity="simli-avatar")` — all of Sarah's voice streams to the `simli-avatar` participant. Per Simli's own support: **the avatar is torn down on EVERY candidate disconnect** (they bill per connected minute). So after a reload the audio pipe points at a dead participant → `no stream available` / `failed to perform clear buffer rpc`, the session destabilizes, and the candidate re-drops a few seconds later. (Voice-only has no such pipe — the room re-subscribes the audio track natively, which is why voice-only recovery "just works.")

**Fix (Simli's recommendation):** on reconnect, start a **fresh** `AvatarSession`.
- `_start_avatar()` is the single startup path (used at boot AND on reconnect). On reconnect it `aclose()`s the stale avatar first (releases its `conversation_item_added` listener + join task), then starts a new one — which re-runs the `session.output.audio = DataStreamAudioOutput(...)` line, re-linking the pipe.
- A new `AvatarSession` = a new Simli billing session, but concurrency is unaffected because the old one already disconnected (confirmed by Simli).
- The reconnect handler restarts the avatar **before** the re-greet, then waits ~1.5s for it to settle, so "Welcome back" actually has a live avatar to render it.

**`_fallback_to_room_audio()` — toggleable safety net (`AVATAR_FALLBACK_TO_ROOM_AUDIO`, default on).** If the restart fails, it constructs a `_ParticipantAudioOutput` (internal SDK class) and assigns `session.output.audio` to it, so Sarah's voice publishes as a normal room track and the candidate still hears her (face just won't animate). Wrapped in try/except — an SDK change can never crash the interview. This is the "fallback" Simli deferred to the LiveKit team; we built a minimal version.

### Capability 5 — client rejoin (interview.html)
A full browser **F5** destroys the page; on reload it would show the fresh **Start Interview** screen, so the candidate never gets back in (the server is holding the session, but the client doesn't return). Fix: the on-load token prefetch checks `status === "in_progress"` and, if so, relabels the button to **"Rejoin Interview"** + shows a "Welcome back" prompt.
**Why one click, not silent auto-join:** browsers block audio autoplay until a user gesture, and a page reload doesn't count. A silent auto-join would connect with **no voice** until the candidate interacted. The single Rejoin click both reconnects AND unlocks audio. The candidate must click within the 60s grace window.

### Why `_session_holder` / `_avatar_holder` list pattern in agent.py
The `participant_connected` handler (which triggers the re-greet + avatar restart) is registered BEFORE the `AgentSession` and avatar exist. Mutable lists let the closure reach them once created: `_session_holder.append(session)`, `_avatar_holder` holds the live `AvatarSession` so the reconnect handler can `aclose()` + restart it.

### Why start time is only set on FIRST join
`participant_connected` fires again on reconnect. Overwriting `interview_start_time` would corrupt the duration calculation, so it's set only when `interview_start_time[0] is None`.

### The avatar+reconnect root cause was the worker's network
The `publisher connection timeout` that crashed `_audio_forwarding_task` is the **agent worker's** connection to LiveKit Cloud, not the candidate's. Proven by hotspot test: on a stable network the avatar holds; on corporate WiFi it dies. Same root cause as the browser needing `iceTransportPolicy: "relay"`. In production (worker on EC2) this largely disappears.

---

## 16. Alembic Migrations

**Why Alembic must import all models:**
`alembic/env.py` must import every model module before `Base.metadata` is examined. Models register themselves with `Base.metadata` on import. If a model file isn't imported, Alembic can't see its table and won't generate a migration for it.

```python
# alembic/env.py — must include ALL model modules:
from app.models import base, candidate, job, ats_score, interview, report  # noqa: F401
```

**Sync vs async connection in Alembic:**
Alembic runs from the CLI synchronously. The app uses `asyncpg` (async). Alembic uses `psycopg2` (sync). Both connect to the same DB, just with different drivers.

---

## 17. API Route Auth Matrix

| Route | Tenant Required | Reason |
|---|---|---|
| `/api/v1/interviews/trigger` | ✅ | Company-specific operation |
| `/api/v1/interviews/{id}/token` | ❌ | Candidate opens link from email |
| `/api/v1/interviews/{id}/evaluate` | ✅ | Internal/recruiter operation |
| `/api/v1/interviews/{id}/evaluation` | ✅ | Internal/recruiter operation |
| `/api/v1/interviews/{id}/report` | ✅ | Internal/recruiter operation |
| `/api/v1/interviews/{id}/report/html` | ❌ (signed `?token=` instead) | Browser direct URL — can't send headers; report token (7d) required |

---

## 18. Key Numbers

| Setting | Value | Why |
|---|---|---|
| STT model / language | nova-2-general / "en" | Broader detection than nova-3 + en-IN |
| STT `endpointing_ms` | 200ms | Fast turn response, still natural pause |
| VAD `activation_threshold` | 0.1 | Very sensitive — catches quiet/degraded mics |
| VAD `min_speech_duration` | 0.05s | Detect short utterances ("yes") |
| Pre-response delay | 300–600ms random | Human response time simulation |
| Filler probability | 20% | Natural without being predictable |
| `min_interruption_words` | 4 | Prevent short acknowledgments from interrupting |
| `min_endpointing_delay` | 0.4s | Wait before responding |
| `max_endpointing_delay` | 6.0s | Allow long pauses in long answers |
| `aec_warmup_duration` | 0 | Skip echo-canceller warmup (mic debugging) |
| ElevenLabs stability | 0.45 | Expressive, not robotic |
| ElevenLabs style | 0.35 | Professional expressiveness |
| LangGraph max turns/stage | 3 | Force-advance if candidate is vague |
| Disconnect grace period | 60s | Reload-proof; survives network blips (`GRACE_PERIOD_SECONDS`) |
| `AVATAR_ENABLED` | true | false → reliable voice-only (no avatar SPOF) |
| `AVATAR_FALLBACK_TO_ROOM_AUDIO` | true | voice via room track if avatar (re)start fails |
| Avatar re-greet settle delay | ~1.5s | Lets the restarted avatar publish before Sarah speaks |
| Eval weights | 35/25/15/15 + 10 ATS | JD Fit / Comm / Behav / Conf + ATS boost |
| Agent DB pool | 5 + 10 overflow | Per worker job process |
| Crash recovery DB pool | 2 + 3 overflow | Isolated from agent pool |
| FastAPI DB pool | 10 + 20 overflow | Request-scoped sessions |
| LiveKit max_participants | 3 | Agent + candidate + simli-avatar |
| LiveKit empty_timeout | 30s | Close room shortly after last leave |
| Invite token expiry | 24h | Candidate join link |
| Report token expiry | 7 days | Recruiter report link |

---

## 19. Common Errors and Fixes

| Error | Cause | Fix |
|---|---|---|
| `anthropic.AuthenticationError` in agent worker | `load_dotenv()` not called / key not passed explicitly | Absolute-path `load_dotenv()` at top of agent.py; `api_key=os.environ[...]` in LLM constructor |
| `anthropic.NotFoundError: model: claude-*` | Model not on this API key's account | Use `claude-haiku-4-5-20251001` only |
| `UndefinedTableError: relation "..." does not exist` | Migration not run | `venv\Scripts\python -m alembic upgrade head` |
| Transcripts never saved (silent) | Plain `load_dotenv()` failed when worker started from another cwd → empty `DATABASE_URL` | Load `.env` by absolute path from `__file__` |
| AI turns never saved | `turn_ctx` in `on_user_turn_completed` is a temporary copy — assistant reply isn't in it | Capture via `session.on("conversation_item_added")` |
| `PostgresSyntaxError: syntax error at or near ":"` | `:param::jsonb` cast breaks SQLAlchemy named-param parsing with asyncpg | `bindparam("x", type_=PG_JSONB)` + pass Python list/dict |
| `function jsonb_concat(jsonb, text) does not exist` | JSON passed as string, not typed jsonb | Same `bindparam(type_=PG_JSONB)` fix |
| Mic dead ONLY when avatar active | Custom Simli bridge's 30fps in-process rendering starved the event loop — VAD never ran | Official `livekit-plugins-simli` (avatar = separate worker) |
| "input speech hasn't started yet" forever | Same event-loop starvation, or track not subscribed | Official plugin + explicit `publication.set_subscribed(True)` for candidate audio |
| Voice repeating / double audio | Both avatar AND room publishing the agent's voice | `room_options.audio_output=(not avatar_active)` |
| Avatar video but NO voice | Browser filtered out `simli-avatar` audio (old `!isSimliAvatar` check) | interview.html plays ALL audio tracks |
| ElevenLabs 401 `detected_unusual_activity` | Free-tier abuse block | Paid plan (Creator) + restart worker to reload key |
| Interview stuck `in_progress` forever | Candidate leaving never ended the agent job; also `hr_agent` referenced before definition in `on_shutdown` | Grace timer → `ctx.shutdown()`; create `hr_agent` before registering `on_shutdown` |
| `UniqueViolationError` on re-trigger (jobs) | Blind INSERT of candidate/job/ats rows | UPSERT pattern in context_builder (SELECT then update-or-insert) |
| `TypeError: expected str, dict found` in evaluation | Certifications are dicts | Extract `certification_name` |
| Reload kills interview | Instant shutdown on `participant_disconnected` | 60s `DisconnectGraceTimer` (crash_recovery.py) |
| `RuntimeError: AgentSession isn't running` on reconnect | RoomIO default `close_on_disconnect=True` closed the session the moment the candidate disconnected | `room_io.RoomOptions(close_on_disconnect=False)` — grace timer owns finalization |
| Re-greet never fires after reconnect (no log, no error) | Unreferenced `asyncio.create_task` GC'd mid-`sleep` | Hold the task in a `_pending_tasks` set with done-callback discard |
| Avatar silent / frozen after reconnect, candidate re-drops; `no stream available`, `failed to perform clear buffer rpc` | Simli tears the avatar down on disconnect; audio pipe points at a dead participant | Restart a fresh `AvatarSession` on reconnect (`_start_avatar()`); `_fallback_to_room_audio()` if it fails |
| After browser F5, candidate lands on Start screen (recovery "not working") | Client never auto-rejoins; only the Start button triggers a join | `interview.html` prefetch detects `status=in_progress` → "Rejoin Interview" one-click (gesture also unlocks audio autoplay) |
| `Extra inputs are not permitted` (pydantic) when adding an `.env` var | Every `.env` key must be declared in `Settings` (config.py) | Add the field (e.g. `avatar_enabled: bool`, `avatar_fallback_to_room_audio: bool`) |
| `400 X-Tenant-ID header required` on report URL | Route not excluded from middleware | `path.endswith("/report/html")` in `_is_excluded()` + signed `?token=` |
| `classList.add('')` DOMException | Empty string passed to classList | Use class assignment (`className = 'av-glow idle'`) |
| Avatar not showing | `SIMLI_API_KEY` or `SIMLI_FACE_ID` empty, or Simli concurrent-session limit hit | Add keys to `.env`; check Simli plan limits |
| SMTP 535 auth failed | Gmail requires App Password, not account password | Generate App Password in Google account settings |

---

## 20. Build Status & What's Next

### Completed (Phases 1–11)
All core phases are DONE: DB models + migrations, FastAPI app, context building, voice pipeline, real-time transcripts, weighted evaluation engine, recruiter report (signed token URL), official Simli avatar, workflow recovery (APScheduler), hardening (rate limits, structured logging, systemd/EC2 scripts), signed invite tokens + auto email, and in-interview crash recovery (60s grace + stage persistence + "Welcome back" resume).

### Capacity (current single-machine dev setup)
- **With avatar: ~1–3 concurrent interviews** — Simli plan session limit is the wall
- **Voice-only: ~5–8 concurrent** — ElevenLabs Creator allows ~5 concurrent TTS streams
- **Monthly volume: ~40–50 interviews** — ElevenLabs Creator = 131k chars/month
- Scaling out = run more agent workers on more machines (LiveKit load-balances jobs automatically); upgrade ElevenLabs/Simli plans; tune PG `max_connections`

### Pending / planned
- Email verification on join page (verify candidate email against token before start)
- Recruiter dashboard (transcript, scores, approve/reject, forward to L2)
- JWT Bearer auth for recruiter APIs (replace X-Tenant-ID)
- Telephony (FreeSWITCH/Asterisk phone interviews)
- Multi-agent architecture (HR / Technical / Fraud / Observer / Summary agents)
- ngrok / public deployment for team testing (in progress)
- Cosmetic: "playback_finished called more times than captured" warning (harmless, parked)
