# Interview Agent — AI L1 HR Screening Microservice

**Author:** Navaneeth S (aimlteam@interbiz.in)
**Type:** SaaS HRMS microservice
**Stack:** Python 3.12 · FastAPI · PostgreSQL · LiveKit · Deepgram · ElevenLabs · Anthropic Claude · Simli
**Status:** Phases 1–11 complete — invite-token auth, official Simli avatar plugin, weighted evaluation, in-interview crash recovery all live

---

## What This Service Does

This is the **AI-powered L1 (first-round) HR screening** microservice for an existing HRMS SaaS platform. When the ATS (Applicant Tracking System) shortlists a candidate, this service:

1. Receives the candidate's parsed resume + ATS score + job description
2. Builds a personalised interview strategy using Claude
3. Creates a LiveKit voice room
4. Generates a **signed invite token** and embeds it in the join link
5. Emails the secure join link to the candidate automatically
6. Conducts a full voice interview via the browser — a real-time animated human avatar acts as the HR interviewer
7. Stores the full transcript to PostgreSQL in real time
8. Evaluates the candidate post-interview using Claude (scores communication, confidence, JD fit, behavioral)
9. Generates a recruiter-ready HTML report (print-to-PDF) — auto-triggered after evaluation

The service is **fully autonomous** — zero human involvement needed until the recruiter reviews the report.

---

## Project Structure

```
interview-agent/
├── app/
│   ├── main.py                        # FastAPI entry point — includes all routers
│   ├── core/
│   │   ├── config.py                  # Pydantic settings (reads .env)
│   │   ├── middleware.py              # TenantMiddleware — X-Tenant-ID enforcement
│   │   ├── security.py                # create_invite_token() + verify_invite_token()
│   │   ├── rate_limiter.py            # slowapi rate limiting (per tenant + per IP)
│   │   └── logging_config.py          # Structured JSON logging (prod) / readable (dev)
│   ├── api/v1/routes/
│   │   ├── health.py                  # GET /health
│   │   ├── interviews.py              # POST /api/v1/interviews/trigger
│   │   ├── session.py                 # GET /api/v1/interviews/{id}/token  ← requires signed token
│   │   ├── evaluation.py              # GET|POST /api/v1/interviews/{id}/evaluate
│   │   ├── reports.py                 # GET|POST /api/v1/interviews/{id}/report[/html]
│   │   └── recovery.py                # GET /admin/recovery/status  POST /admin/recovery/run
│   ├── models/
│   │   ├── base.py                    # BaseModel: id (UUID), tenant_id, timestamps
│   │   ├── candidate.py               # Candidate + CandidateProfile
│   │   ├── job.py                     # Job
│   │   ├── ats_score.py               # AtsScore
│   │   ├── interview.py               # Interview, InterviewContext, InterviewTranscript,
│   │   │                              #   InterviewExtractedData, InterviewScore
│   │   └── report.py                  # InterviewReport
│   ├── schemas/
│   │   └── interview.py               # Pydantic request/response schemas
│   ├── services/
│   │   ├── resume_extractor.py        # Parses ATS resume JSON → structured dict
│   │   ├── ats_extractor.py           # Parses ATS score JSON → missing skills, flags
│   │   ├── strategy_builder.py        # Claude → personalised interview strategy
│   │   ├── context_builder.py         # Orchestrates extraction + DB saves + email invite
│   │   ├── email_service.py           # Sends interview invite email via SMTP
│   │   ├── evaluation_engine.py       # Post-interview Claude scoring (Phase 6)
│   │   ├── report_generator.py        # HTML + JSON report builder (Phase 7)
│   │   └── recovery.py                # Crash recovery functions (Phase 8)
│   ├── workers/
│   │   └── scheduler.py               # APScheduler — 4 periodic recovery jobs
│   ├── realtime/
│   │   ├── room_manager.py            # LiveKit room creation + token generation
│   │   ├── agent.py                   # Voice agent worker (separate process)
│   │   ├── interview_graph.py         # LangGraph state machine (9 stages)
│   │   ├── crash_recovery.py          # Phase 11 — grace timer, stage persistence, resume
│   │   └── avatar_session.py          # LEGACY custom Simli bridge (unused — official plugin now)
│   ├── db/
│   │   └── session.py                 # Async SQLAlchemy engine + get_db dependency
│   └── static/
│       └── interview.html             # Candidate browser page (LiveKit JS + Simli video)
├── alembic/                           # Database migrations
│   └── versions/                      # Auto-generated migration files
├── systemd/
│   ├── interview-api.service          # Systemd unit — FastAPI server
│   └── interview-worker.service       # Systemd unit — LiveKit agent worker
├── scripts/
│   ├── setup_ubuntu.sh                # One-time EC2 Ubuntu setup
│   ├── deploy.sh                      # Pull → migrate → restart + health check
│   └── backup_db.sh                   # PostgreSQL backup (14-day retention)
├── requirements.txt
└── .env                               # API keys (never commit)
```

---

## Two Processes

Both must be running for an interview to work.

### Process 1 — FastAPI Server
```bash
venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Process 2 — LiveKit Agent Worker
```bash
venv\Scripts\python -m app.realtime.agent dev
```
The `dev` flag enables hot-reload.

---

## How to Trigger an Interview (Postman)

```
POST http://localhost:8000/api/v1/interviews/trigger
Headers:
  X-Tenant-ID: tenant-001
  Content-Type: application/json

Body: {
  "ats_candidate_id": "CAND-001",
  "candidate_name": "Navaneeth S",
  "candidate_email": "candidate@example.com",
  "candidate_phone": "+91XXXXXXXXXX",
  "resume_filename": "Navaneeth_S.pdf",
  "parsed_resume": { ...ATS resume parser response... },
  "ats_score_data": { ...ATS scorer response... },
  "job": { ...job details from ATS... }
}
```

Response includes `join_url` — this is a **signed secure link** automatically emailed to the candidate.

```json
{
  "interview_id": "...",
  "candidate_id": "...",
  "status": "scheduled",
  "join_url": "http://localhost:8000/interview/{id}?token=eyJhbGci...",
  "message": "Interview scheduled for Navaneeth S. Missing skills: FastAPI, Docker..."
}
```

---

## Signed Invite Token (Candidate Auth)

When an interview is triggered, the system generates a **signed JWT invite token** embedded in the join link.

**Files:**
- `app/core/security.py` — `create_invite_token()` + `verify_invite_token()`
- `app/services/email_service.py` — sends invite email via SMTP

**Token payload:**
```json
{
  "type": "invite",
  "interview_id": "...",
  "candidate_email": "...",
  "exp": "<24 hours from creation>"
}
```

**How it works:**
```
Trigger interview → signed token generated → join_url = /interview/{id}?token=...
        ↓
Email sent to candidate automatically (fire-and-forget)
        ↓
Candidate clicks link → browser reads ?token from URL
        ↓
Page calls GET /interviews/{id}/token?token=...
        ↓
Server verifies: genuine signature? ✅  not expired? ✅  matches interview? ✅
        ↓
LiveKit room token issued → interview starts
```

If no token → 401. Expired or tampered → 401.

**Config:**
```env
INVITE_TOKEN_EXPIRE_HOURS=24   # default: 24 hours
```

**Email is skipped silently** if `SMTP_USER` / `SMTP_PASSWORD` are not set in `.env`.

---

## Full Pipeline (end-to-end)

```
Candidate clicks secure join link (email)
    → Token verified by server
    → Simli avatar appears (real-time animated face)
    → Agent greets candidate
    ↓
Candidate speaks
    → Silero VAD detects speech (activation_threshold=0.1, very sensitive)
    → Deepgram STT (nova-2-general, language "en", endpointing 200ms) transcribes
    → HRInterviewAgent.on_user_turn_completed() fires
        → Mishear guard (empty text only → "Could you say that again?")
        → Save candidate message to DB (fire-and-forget)
        → LangGraph.ainvoke() — advance stage machine
        → crash_recovery.queue_save_stage() — persist stage to DB (Phase 11)
        → Pre-response delay 300–600ms
        → 20% chance filler word ("Hmm.", "Right.", etc.)
    → Claude Haiku generates response (stage-aware system prompt)
    → AI response captured via session "conversation_item_added" event → saved to DB
    → ElevenLabs TTS (Sarah voice, turbo) synthesises audio
    → Official Simli plugin (livekit-plugins-simli) runs the avatar as a
      SEPARATE LiveKit worker — it republishes the agent's voice + lip-synced
      face video as the "simli-avatar" participant (room's own audio output is
      disabled when avatar is active → no double audio)
    → Candidate sees animated face + hears voice
    → Repeat until wrap_up stage completes
    ↓
Candidate disconnects (reload / network blip / leaves)
    → DisconnectGraceTimer starts 60s countdown (Phase 11)
    → Reconnects in time → Sarah re-greets "Welcome back…" and continues the stage
    → Doesn't return → ctx.shutdown() fires the normal completion path
    ↓
Interview ends
    → on_shutdown: status → "completed", duration saved, transcript tasks drained
    → run_evaluation(interview_id)                           ← Phase 6
    → run_evaluation saves scores to InterviewScore table
    → chains into run_report(interview_id)                   ← Phase 7
    → run_report saves HTML + JSON to InterviewReport table
```

Barge-in: candidate can interrupt AI with 4+ words (`min_interruption_words=4`) and it stops immediately.

---

## LangGraph State Machine (interview_graph.py)

```
intro → experience → current_ctc → expected_ctc → notice_period
      → relocation → joining → wrap_up → complete
```

Each stage: heuristic detector checks if candidate answered, then advances or stays.
Max 3 turns per stage before force-advancing.
`stage_instruction` is injected into the LLM system prompt each turn via the `instructions` property override on `HRInterviewAgent`.

---

## Avatar (Simli — Official Plugin)

**Package:** `livekit-plugins-simli==1.5.11` (used in `agent.py`)

The avatar uses the **official LiveKit Simli plugin**, started BEFORE `session.start()`:

```python
avatar = simli.AvatarSession(
    simli_config=simli.SimliConfig(api_key=..., face_id=...),
    avatar_participant_identity="simli-avatar",
    avatar_participant_name="Sarah",
)
await avatar.start(session, room=ctx.room, livekit_url=..., livekit_api_key=..., livekit_api_secret=...)
```

How it works:
- The plugin dispatches the avatar as a **separate LiveKit worker** — rendering does NOT run on the agent's event loop, so it can never starve candidate-audio processing (the root failure of the old custom approach).
- The avatar participant (`simli-avatar`) publishes BOTH the lip-synced face video AND the agent's voice audio. The room's own audio output is therefore disabled when the avatar is active: `room_io.RoomOptions(audio_output=(not avatar_active))` — this prevents double audio.
- The browser (`interview.html`) plays ALL audio tracks (including from `simli-avatar`).

**History:** `app/realtime/avatar_session.py` is the old custom implementation (SimliAudioForwarder + _VideoOnlyPublisher). Its in-process 30fps video rendering starved the event loop and broke mic input. The file is kept for reference but is **no longer imported by agent.py**.

**Required env vars:**
```env
SIMLI_API_KEY=...
SIMLI_FACE_ID=...   # Upload HR persona photo at simli.com → get face_id
```

If keys are missing or `avatar.start()` fails, avatar is disabled silently — interview continues voice-only (room audio output re-enabled).

---

## Evaluation Engine (Phase 6)

**File:** `app/services/evaluation_engine.py`

- Model: `claude-haiku-4-5-20251001`
- Triggered automatically in `on_shutdown` (after transcript tasks drained)
- Loads full transcript + candidate profile + job + ATS from DB
- Claude returns per-dimension scores (1–10): `communication`, `confidence`, `jd_fit`, `behavioral` + `recommendation` (proceed_to_l2 / hold / reject)
- **Overall score (0–100) is computed exactly in code** (not by Claude) with these weights:

| Dimension | Weight |
|---|---|
| JD Fit | 35% |
| Communication | 25% |
| Behavioral | 15% |
| Confidence | 15% |
| ATS Boost | 10% |

```python
overall = (jd_fit*35 + communication*25 + behavioral*15 + confidence*15) / 10
        + (ats_score / 100) * 10        # ATS boost, capped at 10 points
overall = max(0, min(100, round(overall)))
```

- Saves to `InterviewScore` and `InterviewExtractedData` tables
- After saving, chains into `run_report(interview_id)`

**Manual trigger:**
```
POST /api/v1/interviews/{id}/evaluate
Headers: X-Tenant-ID: tenant-001
```

**View results:**
```
GET /api/v1/interviews/{id}/evaluation
Headers: X-Tenant-ID: tenant-001
```

---

## Report Generator (Phase 7)

**File:** `app/services/report_generator.py`

- No extra Claude call — reuses `raw_extraction` JSONB from `InterviewExtractedData`
- Generates self-contained HTML report (all CSS inline, no external dependencies)
- Saves to `InterviewReport` table: `report_html`, `report_data` (JSONB), `report_url`, `generated_at`
- `report_url` = `{APP_BASE_URL}/api/v1/interviews/{id}/report/html?token=<signed report token>`

**Report access token:** the HTML report URL requires a signed JWT (`type: report`, 7-day expiry — `create_report_token()` / `verify_report_token()` in `app/core/security.py`). The token is embedded in `report_url` when the report is generated. Opening the URL without/with an invalid token → 401/403.

**View HTML report (browser — use the full `report_url` from the JSON report):**
```
GET http://localhost:8000/api/v1/interviews/{id}/report/html?token=eyJhbGci...
```
Click **Save as PDF** button → browser print dialog → Save as PDF.

**View JSON report (Postman):**
```
GET /api/v1/interviews/{id}/report
Headers: X-Tenant-ID: tenant-001
```

---

## Workflow Recovery (Phase 8)

**Files:** `app/services/recovery.py` + `app/workers/scheduler.py`

Four recovery functions run on startup and periodically via APScheduler:

| Function | Interval | What it does |
|---|---|---|
| `recover_stuck_interviews` | Every 15 min | in_progress > 2h → mark completed + run evaluation |
| `retry_missing_evaluations` | Every 10 min | completed but no score → re-run evaluation |
| `retry_missing_reports` | Every 10 min | scored but no report → re-run report |
| `expire_abandoned_interviews` | Every 60 min | scheduled > 24h + 0 transcript turns → mark expired |

**Admin endpoints:**
```
GET  /api/v1/admin/recovery/status   Headers: X-Tenant-ID: tenant-001
POST /api/v1/admin/recovery/run      Headers: X-Tenant-ID: tenant-001
```

---

## In-Interview Crash Recovery (Phase 11)

**Files:** `app/realtime/crash_recovery.py` (self-contained unit, own DB engine pool_size=2) + reconnect/avatar-restart hooks in `app/realtime/agent.py` + client rejoin flow in `app/static/interview.html`. The recovery *logic* is isolated in crash_recovery.py; the agent only calls thin marked hooks.

Five parts work together:

**1. Disconnect grace period (60s)** — `DisconnectGraceTimer`
A page reload or network blip looks like a disconnect. Instead of finalizing instantly, a 60-second countdown starts. If the candidate reconnects in time, the countdown is cancelled and the interview continues. If not, `ctx.shutdown()` runs the normal completion path (status → completed, evaluation, report). Grace = `GRACE_PERIOD_SECONDS = 60.0`.
**Requires `close_on_disconnect=False` in `room_io.RoomOptions`** — the SDK default (True) closes the AgentSession the instant the candidate disconnects, which breaks the grace timer (`RuntimeError: AgentSession isn't running` on reconnect).

**2. Stage persistence** — `queue_save_stage()` / `load_resume_state()`
After every LangGraph advance (voice and typed), the current stage + turn count + capture flags are saved fire-and-forget to `interview_contexts.question_flow` (existing JSONB column — no migration). If the agent process crashes, the stage survives in the DB.

**3. Resume on a fresh agent job** — `apply_resume()` + `resume_greeting_instructions()`
When a new agent job starts for an interview that is still `in_progress` with a saved stage (not intro/complete), the LangGraph state is restored to that stage and Sarah re-greets: *"Welcome back, {name}! … we'll continue right where we left off"* — then re-asks the current stage's question instead of restarting from the introduction.

**4. Avatar restart on reconnect** — `_start_avatar()` / `_fallback_to_room_audio()` (in `agent.py`)
Per Simli, the avatar is **torn down on every candidate disconnect** (per-minute billing). Its audio pipe (`DataStreamAudioOutput` → `simli-avatar` participant) then points at a dead participant, so on reconnect the agent re-greets to a silent/frozen avatar. Fix: on reconnect (avatar mode), `aclose()` the old avatar and start a **fresh** `AvatarSession` (re-points `session.output.audio`) **before** the re-greet. Concurrency is unaffected because the old session already disconnected. If the restart fails and `AVATAR_FALLBACK_TO_ROOM_AUDIO=true`, voice falls back to a room audio track so the candidate still hears Sarah (face won't animate). The re-greet waits ~1.5s for the new avatar to settle before speaking.

**5. Client rejoin (in `interview.html`)**
A full browser refresh destroys the page → it would otherwise show the fresh **Start Interview** screen. The on-load token prefetch now checks `status === "in_progress"` and, if so, relabels the button to **"Rejoin Interview"** and shows a "Welcome back" prompt. **One click** rejoins — deliberately not auto-join, because browsers block audio autoplay until a user gesture (a silent auto-join would connect with no voice). The candidate must click Rejoin within the 60s grace window.

```
Candidate reloads page (full F5)
    → server: participant_disconnected → grace_timer.candidate_left() (60s countdown)
    → new page loads → prefetch sees status=in_progress → button = "Rejoin Interview"
    → candidate clicks Rejoin (one gesture → unlocks audio + reconnects)
    → server: participant_connected → grace_timer.candidate_returned() == True
        → _start_avatar() restarts Simli (or _fallback_to_room_audio())
        → Sarah: "Welcome back…" + re-asks current stage question
    → interview continues, start time / duration NOT corrupted

Agent worker crashes mid-interview
    → LiveKit dispatches a fresh job when candidate (re)joins
    → load_resume_state() finds saved stage in question_flow
    → apply_resume() restores stage + captured flags + stage_instruction
    → greeting = "Welcome back…" instead of fresh intro
```

**Known tradeoffs:**
- Clicking "End Interview" also looks like a disconnect, so evaluation starts ~60s after the candidate leaves (grace period must expire first).
- The candidate must click **Rejoin within 60s** of refreshing, or the interview finalizes.
- Each avatar restart spins a **new Simli billing session** (concurrency unaffected — old one already gone).
- `_fallback_to_room_audio()` uses an internal SDK class (`_ParticipantAudioOutput`); it's wrapped in try/except so a future SDK change can't crash the interview.

---

## Environment Variables (.env)

```env
# App
APP_ENV=development
APP_BASE_URL=http://localhost:8000
SECRET_KEY=dev-secret-key

# Invite Token
INVITE_TOKEN_EXPIRE_HOURS=24

# Database
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/interview_agent

# Redis
REDIS_URL=redis://localhost:6379

# AI
ANTHROPIC_API_KEY=sk-ant-...
DEEPGRAM_API_KEY=...
ELEVENLABS_API_KEY=...

# LiveKit
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...

# Simli Avatar
SIMLI_API_KEY=...
SIMLI_FACE_ID=...
AVATAR_ENABLED=true                   # set false → reliable voice-only mode (no avatar single-point-of-failure)
AVATAR_FALLBACK_TO_ROOM_AUDIO=true    # if avatar can't (re)start, route Sarah's voice via room audio so she's still heard

# AWS (S3)
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=ap-south-1
S3_BUCKET_NAME=...

# ATS Service
ATS_BASE_URL=...
ATS_SERVICE_TOKEN=...

# Email (optional — invite email skipped if not set)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=...
SMTP_PASSWORD=...
EMAIL_FROM=noreply@yourapp.com
```

---

## Database (PostgreSQL)

```bash
# Apply all migrations
venv\Scripts\python -m alembic upgrade head

# Create new migration after model change
venv\Scripts\python -m alembic revision --autogenerate -m "description"
```

**Tables:**
| Table | Purpose |
|---|---|
| `candidates` | Candidate master record |
| `candidate_profiles` | Skills, experience, certifications |
| `jobs` | Job/position details |
| `ats_scores` | ATS pre-score per candidate+job |
| `interviews` | Interview lifecycle (status, timing, join_url, join_expires_at) |
| `interview_contexts` | Strategy, gaps, skills to probe |
| `interview_transcripts` | Full turn-by-turn transcript (JSONB) |
| `interview_extracted_data` | CTC, notice, relocation, raw_extraction JSONB |
| `interview_scores` | Evaluation scores (all dimensions) |
| `interview_reports` | Final recruiter report (HTML + JSON + URL) |

---

## Multi-Tenancy

Every table has `tenant_id`. Every API request must include `X-Tenant-ID` header **except**:
- `/health`, `/docs`, `/openapi.json`, `/redoc`
- `/interview/*`, `/static/*`
- `/api/v1/interviews/{id}/token`  ← uses signed invite token instead
- `/api/v1/interviews/{id}/report/html`  ← uses signed report token (?token=) instead of headers

---

## API Reference

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/v1/interviews/trigger` | X-Tenant-ID | Trigger new interview + send invite email |
| GET | `/api/v1/interviews/{id}/token` | Signed invite token (?token=) | Get LiveKit token (candidate) |
| GET | `/api/v1/interviews/{id}/evaluation` | X-Tenant-ID | Get evaluation scores |
| POST | `/api/v1/interviews/{id}/evaluate` | X-Tenant-ID | Manually trigger evaluation |
| GET | `/api/v1/interviews/{id}/report` | X-Tenant-ID | Get JSON report |
| GET | `/api/v1/interviews/{id}/report/html` | Signed report token (?token=) | View HTML report in browser |
| POST | `/api/v1/interviews/{id}/report` | X-Tenant-ID | Trigger/regenerate report |
| GET | `/api/v1/admin/recovery/status` | X-Tenant-ID | Recovery health check |
| POST | `/api/v1/admin/recovery/run` | X-Tenant-ID | Manually trigger recovery |
| GET | `/health` | None | Health check |

---

## Build Phases

| Phase | Status | Description |
|---|---|---|
| 1 | ✅ Done | Database models + Alembic migrations |
| 2 | ✅ Done | FastAPI app + middleware + routing |
| 3 | ✅ Done | Context building — resume/ATS extraction + Claude strategy |
| 4 | ✅ Done | Voice pipeline — LiveKit + Deepgram + Claude + ElevenLabs |
| 5 | ✅ Done | Real-time transcript saving during interview |
| 6 | ✅ Done | Evaluation engine — Claude Haiku post-interview scoring |
| 7 | ✅ Done | Recruiter report — HTML + JSON, auto-triggered, print-to-PDF |
| 7.5 | ✅ Done | Simli real-time human avatar — lip-sync face video in browser |
| 8 | ✅ Done | Workflow durability + crash recovery (APScheduler + 4 recovery jobs) |
| 9 | ✅ Done | Hardening — rate limiting, structured logging, systemd + EC2 scripts |
| 10 | ✅ Done | Signed invite token — secure candidate join link + auto email invite |
| 10.5 | ✅ Done | Official Simli plugin (livekit-plugins-simli) — avatar as separate worker, mic + avatar coexist |
| 10.6 | ✅ Done | Weighted evaluation — JD Fit 35 / Comm 25 / Behav 15 / Conf 15 / ATS 10, exact in-code calculation |
| 10.7 | ✅ Done | Signed report token — HTML report URL requires ?token= (7-day expiry) |
| 11 | ✅ Done | In-interview crash recovery — 60s grace timer, stage persistence, "Welcome back" resume |

---

## Next Features (Planned)

| Feature | Description |
|---|---|
| Email verification on join page | Candidate must enter email before interview starts — verified against token |
| Telephony (phone interviews) | FreeSWITCH/Asterisk — call candidate directly, no browser needed |
| Recruiter dashboard | UI for transcript, scores, approve/reject, forward to L2 |
| JWT for recruiter APIs | Replace X-Tenant-ID with proper Bearer token auth for HRMS integration |
| Multi-agent architecture | Separate HR, Technical, Fraud Detection, Observer, Summary agents |

---

## Known Constraints

- **Corporate WiFi:** WebRTC UDP is blocked on most office networks. The interview page forces `iceTransportPolicy: "relay"` to use LiveKit's TURN servers over TCP/443. Whitelist `*.livekit.cloud` (TCP 443) with IT if needed.
- **Claude overload:** `claude-haiku-4-5-20251001` occasionally returns 529. The agent framework retries automatically.
- **Two processes required:** FastAPI server + agent worker must both be running.
- **Simli is optional:** If `SIMLI_API_KEY` or `SIMLI_FACE_ID` is missing (or `avatar.start()` fails), avatar is silently disabled — voice-only mode with room audio output re-enabled.
- **Avatar must use the official plugin:** `livekit-plugins-simli`. The custom in-process bridge (`avatar_session.py`) starved the event loop and broke mic input — do not revert to it.
- **Avatar is a voice single-point-of-failure:** when active, ALL of Sarah's audio is routed through the Simli worker (`audio_output=(not avatar_active)`). If a network blip kills the agent→avatar audio pipe (`_audio_forwarding_task` crashes with `publisher connection timeout`), the avatar freezes and the candidate gets text but NO voice. Mitigations now in place: (a) on reconnect the avatar is **restarted** (`_start_avatar()`), (b) `AVATAR_FALLBACK_TO_ROOM_AUDIO=true` re-routes voice to a room track if the restart fails, (c) `AVATAR_ENABLED=false` disables the avatar entirely for guaranteed-reliable voice-only operation on bad networks.
- **Avatar + corporate WiFi:** the `publisher connection timeout` failures originate on the **agent worker's** connection to LiveKit Cloud, not the candidate's. Running the worker on a stable network (mobile hotspot / EC2) makes the avatar reliable; it was the corporate WiFi all along (same root cause as the browser needing `iceTransportPolicy: "relay"`).
- **Model on account:** Only `claude-haiku-4-5-20251001` confirmed available. `claude-3-5-sonnet` and `claude-sonnet-4-5-20251001` are NOT on this account.
- **Room max_participants = 3:** Agent + candidate + simli-avatar. Do not lower this. `empty_timeout=30s`.
- **Email is optional:** If SMTP not configured, invite email is skipped — join_url is still returned in the trigger response and can be shared manually.
- **End Interview waits 60s:** the disconnect grace timer (Phase 11) cannot distinguish "End Interview" from a reload, so completion/evaluation starts ~60s after the candidate leaves.
- **JSONB writes from the agent:** always bind with `bindparam("x", type_=PG_JSONB)` and pass Python lists/dicts. Never use `::jsonb` casts in `text()` SQL — asyncpg + SQLAlchemy named params break on it.
- **AI transcript turns:** must be captured via `session.on("conversation_item_added")` — `turn_ctx` in `on_user_turn_completed` is a temporary copy and never contains the assistant reply.
