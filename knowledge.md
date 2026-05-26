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

**Why `nova-3` with `en-IN`?**
Latest Deepgram model, best accuracy. `en-IN` language code provides Indian English accent optimisation.

**Why `endpointing_ms=300`?**
Default 25ms cuts off candidates who pause briefly to think. 300ms is a comfortable natural pause.

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

**Why `min_interruption_words=2`?**
Single coughs, "yeah", or background noise shouldn't stop the AI mid-sentence.

**Why capture AI messages via `turn_ctx.messages[ctx_len_before:]`?**
In livekit-agents 1.x, `on_user_turn_completed`'s `turn_ctx` is mutated in-place during the super() call. The assistant message is appended after LLM generation. Capturing `messages[ctx_len_before:]` gets all new messages added during the super() call, from which we extract the assistant turn for transcript saving.

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

---

## 14. Simli Avatar (Phase 7.5)

**File:** `app/realtime/avatar_session.py`

### Why Simli and not HeyGen/Tavus/D-ID?

| Tool | Cost/min | LiveKit native | Accept raw audio | Verdict |
|---|---|---|---|---|
| **Simli** | $0.009 | ✅ (TransportMode.LIVEKIT) | ✅ PCM direct | **Chosen** |
| Tavus | $0.32–0.59 | ✅ plugin | ✅ | Quality but expensive |
| HeyGen | $0.10–0.20 | ⚠️ Beta | ❌ needs own TTS | Architecture mismatch |
| D-ID | $5.90+ | ❌ | ⚠️ Complex | Prohibitive cost |

Simli is the only provider designed as pure audio-in → face-video-out with no opinion about LLM or TTS. ElevenLabs PCM bytes feed directly in. Cost is negligible (~₹0.75 per 30-min interview).

### Audio pipeline (no double audio)

**Why `_VideoOnlyPublisher` instead of `LivekitRenderer`?**

The built-in `LivekitRenderer` publishes BOTH video AND audio to the room via `AVSynchronizer`. The audio it publishes is the same ElevenLabs audio re-processed by Simli — candidates heard it twice.

`_VideoOnlyPublisher`:
- Connects to the room as `simli-avatar`
- Publishes ONLY the video track (no audio track registered)
- Pumps `yuva420p` frames directly via `VideoSource.capture_frame()` — no `AVSynchronizer`
- Zero risk of double audio

**Why `VideoSource.capture_frame()` directly (not AVSynchronizer)?**
`AVSynchronizer` requires both audio AND video pushes to maintain sync. Since we have video-only, direct `capture_frame()` is correct. Without audio to wait for, there's nothing to synchronise against.

### SimliAudioForwarder — how it sits in the pipeline

`livekit.agents.io.AudioOutput` has 3 abstract methods:
- `capture_frame(frame)` — called per PCM frame from TTS output
- `flush()` — called when a speech segment ends
- `clear_buffer()` — called on interruption (barge-in)

`SimliAudioForwarder` implements all three. For each frame:
1. Converts stereo → mono if needed (`audioop.tomono`)
2. Resamples from ElevenLabs rate → 16 kHz (`audioop.ratecv`)
3. Buffers until 6000 bytes → sends chunk to Simli (`_safe_send`)
4. Calls `next_in_chain.capture_frame(frame)` → room audio → candidate hears normally

**Why insert into chain after `session.start()`?**
`session.output.audio` is read by `agent_activity.py` dynamically on each speech turn (not captured at startup). Setting it after `session.start()` works correctly. Setting it before risks it being overwritten by RoomIO setup.

**Why `_avatar_holder` list pattern?**
`on_shutdown` is defined before the avatar is created. A mutable list lets the closure reference an object that doesn't exist yet:
```python
_avatar_holder: list[AvatarSession] = []   # defined before on_shutdown
# in on_shutdown: if _avatar_holder: await _avatar_holder[0].stop()
# after session.start(): _avatar_holder.append(avatar)
```
Only ONE shutdown callback is registered. Do not add a second one.

**PCM format Simli requires:**
- Sample rate: 16 000 Hz
- Bit depth: 16-bit signed (PCM16)
- Channels: mono
- Chunk size: 6000 bytes (~187 ms)
- `audioop.ratecv(data, 2, 1, src_rate, 16000, None)` — stdlib, no extra dependency (Python 3.12)

**Browser side (interview.html):**
- `<video id="avatar-video">` positioned absolutely over plasma canvas, `border-radius:50%`, `z-index:2`, `display:none` initially
- On `TrackSubscribed` from `simli-avatar` participant: `track.attach(videoEl)`, show video, hide canvas
- No Simli audio track is ever published — no muting needed in browser

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
| `/api/v1/interviews/{id}/report/html` | ❌ | Browser direct URL — can't send headers |

---

## 18. Key Numbers

| Setting | Value | Why |
|---|---|---|
| STT `endpointing_ms` | 300ms | Natural pause, not sentence break |
| Pre-response delay | 300–600ms random | Human response time simulation |
| Filler probability | 20% | Natural without being predictable |
| `min_interruption_words` | 2 | Prevent cough/noise from interrupting |
| `min_endpointing_delay` | 0.4s | Wait before responding |
| `max_endpointing_delay` | 6.0s | Allow long pauses in long answers |
| ElevenLabs stability | 0.45 | Expressive, not robotic |
| ElevenLabs style | 0.35 | Professional expressiveness |
| LangGraph max turns/stage | 3 | Force-advance if candidate is vague |
| Simli chunk size | 6000 bytes | ~187ms at 16 kHz PCM16 mono |
| Simli sample rate | 16 000 Hz | Simli's required input format |
| LiveKit max_participants | 3 | Agent + candidate + simli-avatar |
| Report HTML route | No tenant auth | Browser-openable URL |

---

## 19. Common Errors and Fixes

| Error | Cause | Fix |
|---|---|---|
| `anthropic.AuthenticationError` in agent worker | `load_dotenv()` not called / key not passed explicitly | `load_dotenv()` at top of agent.py; `api_key=os.environ[...]` in LLM constructor |
| `anthropic.NotFoundError: model: claude-*` | Model not on this API key's account | Use `claude-haiku-4-5-20251001` only |
| `UndefinedTableError: relation "..." does not exist` | Migration not run | `venv\Scripts\python -m alembic upgrade head` |
| Voice repeating 2–3 times | `LivekitRenderer` publishing audio AND agent publishing audio | Use `_VideoOnlyPublisher` (video-only) |
| `classList.add('')` DOMException | Empty string passed to classList | Use class assignment (`className = 'av-glow idle'`) |
| `400 X-Tenant-ID header required` on report URL | Route not excluded from middleware | Add `path.endswith("/report/html")` to `_is_excluded()` |
| Avatar not showing | `SIMLI_API_KEY` or `SIMLI_FACE_ID` empty | Add both to `.env`. Check simli.com dashboard for face_id. |

---

## 20. Pending Phases

### Phase 8 — Workflow Durability
On server restart: scan `interviews` for `status = 'completed'` with no `InterviewScore` row → re-trigger `run_evaluation`. On evaluation crash: DB-backed retry counter, exponential backoff.

### Phase 9 — Hardening + Deployment
- `slowapi` rate limiting on trigger endpoint (per tenant)
- Structured JSON logging with request IDs (`python-json-logger`)
- ELK Stack or Grafana+Loki for log aggregation
- systemd unit files for both processes on EC2
- nginx reverse proxy + SSL via certbot
- AWS Secrets Manager instead of `.env` file
- IAM role for S3 (remove static access keys from env)
- `gunicorn -k uvicorn.workers.UvicornWorker` for multi-process FastAPI in production
