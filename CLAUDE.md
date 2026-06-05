# Interview Agent — AI L1 HR Screening Microservice

**Author:** Navaneeth S (aimlteam@interbiz.in)
**Type:** SaaS HRMS microservice
**Stack:** Python 3.12 · FastAPI · PostgreSQL · LiveKit · Deepgram · ElevenLabs · Anthropic Claude · Simli
**Status:** Phases 1–9 complete + Signed Invite Token auth live

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
│   │   └── avatar_session.py          # Simli avatar — audio forwarder + video publisher
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
    → Silero VAD detects speech
    → Deepgram STT (nova-3, en-IN) transcribes
    → HRInterviewAgent.on_user_turn_completed() fires
        → Mishear guard (1 unclear word → "Could you say that again?")
        → Save candidate message to DB (fire-and-forget)
        → LangGraph.ainvoke() — advance stage machine
        → Pre-response delay 300–600ms
        → 20% chance filler word ("Hmm.", "Right.", etc.)
    → Claude Haiku generates response (stage-aware system prompt)
    → Save AI response to DB
    → ElevenLabs TTS (Sarah voice, turbo) synthesises audio
        → SimliAudioForwarder intercepts PCM frames:
            ├── Resamples to 16 kHz mono → sends to Simli (lip sync)
            └── Forwards to room audio output → candidate hears voice
    → Simli renders face animation → publishes video to room
    → Candidate sees animated face + hears voice (no double audio)
    → Repeat until wrap_up stage completes
    ↓
Interview ends (candidate leaves / wrap_up complete)
    → on_shutdown: status → "completed", duration saved
    → asyncio.create_task(run_evaluation(interview_id))      ← Phase 6
    → run_evaluation saves scores to InterviewScore table
    → asyncio.create_task(run_report(interview_id))          ← Phase 7
    → run_report saves HTML + JSON to InterviewReport table
```

Barge-in: candidate can interrupt AI with 2+ words and it stops immediately.

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

## Avatar (Simli)

**File:** `app/realtime/avatar_session.py`

Architecture:
- `SimliAudioForwarder` — custom `livekit.agents.io.AudioOutput` inserted into the audio chain after `session.start()`. Intercepts every PCM frame: resamples to 16 kHz mono, sends 6 kB chunks to Simli, and also calls `next_in_chain` so room audio continues normally.
- `_VideoOnlyPublisher` — connects to the interview LiveKit room as `simli-avatar` participant, publishes only a video track (no audio = no double audio). Pumps `yuva420p` frames from SimliClient directly into `rtc.VideoSource`.
- `AvatarSession` — lifecycle manager. Returns a `SimliAudioForwarder` that replaces `session.output.audio`. Stops cleanly in `on_shutdown`.

**Required env vars:**
```env
SIMLI_API_KEY=...
SIMLI_FACE_ID=...   # Upload HR persona photo at simli.com → get face_id
```

If keys are missing, avatar is disabled silently — interview continues voice-only.

---

## Evaluation Engine (Phase 6)

**File:** `app/services/evaluation_engine.py`

- Model: `claude-haiku-4-5-20251001`
- Triggered automatically in `on_shutdown` via `asyncio.create_task`
- Loads full transcript + candidate profile + job + ATS from DB
- Returns structured JSON scores: `communication`, `confidence`, `jd_fit`, `behavioral`, `overall` (0–10), `recommendation` (proceed_to_l2 / hold / reject)
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
- `report_url` = `{APP_BASE_URL}/api/v1/interviews/{id}/report/html`

**View HTML report (browser — no auth header needed):**
```
GET http://localhost:8000/api/v1/interviews/{id}/report/html
```
Click **Save as PDF** button → browser print dialog → Save as PDF.

**View JSON report (Postman):**
```
GET /api/v1/interviews/{id}/report
Headers: X-Tenant-ID: tenant-001
```

---

## Crash Recovery (Phase 8)

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
- `/api/v1/interviews/{id}/report/html`  ← browser-opened URL, no headers possible

---

## API Reference

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/v1/interviews/trigger` | X-Tenant-ID | Trigger new interview + send invite email |
| GET | `/api/v1/interviews/{id}/token` | Signed invite token (?token=) | Get LiveKit token (candidate) |
| GET | `/api/v1/interviews/{id}/evaluation` | X-Tenant-ID | Get evaluation scores |
| POST | `/api/v1/interviews/{id}/evaluate` | X-Tenant-ID | Manually trigger evaluation |
| GET | `/api/v1/interviews/{id}/report` | X-Tenant-ID | Get JSON report |
| GET | `/api/v1/interviews/{id}/report/html` | None | View HTML report in browser |
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

- **Corporate WiFi:** WebRTC UDP is blocked on most office networks. The interview page forces `iceTransportPolicy: "relay"` to use LiveKit's TURN servers over TCP/443.
- **Claude overload:** `claude-haiku-4-5-20251001` occasionally returns 529. The agent framework retries automatically.
- **Two processes required:** FastAPI server + agent worker must both be running.
- **Simli is optional:** If `SIMLI_API_KEY` or `SIMLI_FACE_ID` is missing, avatar is silently disabled — voice-only mode.
- **Audio model on account:** Only `claude-haiku-4-5-20251001` confirmed available. `claude-3-5-sonnet` and `claude-sonnet-4-5-20251001` are NOT on this account.
- **Room max_participants = 3:** Agent + candidate + simli-avatar. Do not lower this.
- **Email is optional:** If SMTP not configured, invite email is skipped — join_url is still returned in the trigger response and can be shared manually.
