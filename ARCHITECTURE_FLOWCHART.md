# Interview Agent — Architecture Flowcharts

> Generated from a full read-through of the codebase (no code was modified).
> Diagrams are in Mermaid — view in VS Code (Markdown Preview + Mermaid extension), GitHub, or https://mermaid.live

---

## 1. High-Level System Architecture

```mermaid
flowchart TB
    subgraph EXT["External HRMS Platform"]
        ATS["ATS Scoring Engine"]
        RP["Resume Parser"]
        JD["JD Generator"]
    end

    subgraph P1["Process 1 — FastAPI Server (app/main.py)"]
        MW["TenantMiddleware<br/>X-Tenant-ID + Rate Limiter"]
        TRIG["POST /api/v1/interviews/trigger"]
        TOK["GET /interviews/{id}/token<br/>(signed invite JWT)"]
        EVALAPI["GET/POST /interviews/{id}/evaluate"]
        REPAPI["GET/POST /interviews/{id}/report[/html]"]
        RECAPI["GET/POST /admin/recovery/*"]
        SCHED["APScheduler<br/>4 recovery jobs (10–60 min)"]
    end

    subgraph P2["Process 2 — LiveKit Agent Worker (app/realtime/agent.py)"]
        HRA["HRInterviewAgent"]
        LG["LangGraph state machine<br/>(interview_graph.py — 9 stages)"]
        CR["Crash Recovery<br/>(60s grace timer + stage persistence)"]
    end

    subgraph VOICE["Voice / Video Pipeline"]
        LK["LiveKit Cloud<br/>WebRTC room (max 3 participants)"]
        VAD["Silero VAD"]
        STT["Deepgram STT<br/>nova-2-general"]
        LLM["Claude Haiku 4.5<br/>(realtime conversation)"]
        TTS["Deepgram TTS<br/>aura-2-vesta-en ('Sarah')"]
        SIMLI["Simli Avatar<br/>(official plugin, separate worker)"]
    end

    subgraph SVC["Services (app/services)"]
        CB["context_builder<br/>(Phase-3 orchestrator)"]
        RE["resume_extractor"]
        AE["ats_extractor"]
        SB["strategy_builder<br/>(Claude Haiku)"]
        EM["email_service (SMTP)"]
        EE["evaluation_engine<br/>(Claude Haiku)"]
        RG["report_generator<br/>(no LLM call)"]
        CT["cost_tracker + pricing"]
        RECV["recovery service"]
    end

    DB[("PostgreSQL<br/>10 tables, JSONB-heavy")]
    CAND["Candidate Browser<br/>(interview.html)"]

    ATS -- "trigger payload<br/>(parsed resume + ATS score + job)" --> TRIG
    TRIG --> CB
    CB --> RE & AE & SB & EM
    CB --> DB
    CB -- "create room + invite token" --> LK
    EM -- "join link email" --> CAND
    CAND -- "?token=JWT" --> TOK
    TOK -- "LiveKit token" --> CAND
    CAND <--> LK
    LK -- "dispatch job" --> HRA
    HRA --> LG
    HRA --> CR
    HRA <--> VAD & STT & LLM & TTS
    TTS --> SIMLI --> LK
    HRA -- "transcript turns" --> DB
    HRA -- "on_shutdown" --> EE
    EE --> DB
    EE -- "chains into" --> RG
    RG --> DB
    SCHED --> RECV
    RECV -- "retry eval / report,<br/>expire abandoned" --> EE & RG & DB
    EVALAPI & REPAPI & RECAPI --> DB
    HRA & EE & CB --> CT --> DB
```

---

## 2. End-to-End Interview Lifecycle

```mermaid
flowchart TD
    A["ATS shortlists candidate"] --> B["POST /api/v1/interviews/trigger<br/>X-Tenant-ID + resume + ATS score + job"]
    B --> C["context_builder.build_interview_context()"]
    C --> C1["extract_resume_data()"]
    C --> C2["extract_ats_data()<br/>missing skills + risk flags"]
    C1 & C2 --> D["strategy_builder (Claude Haiku)<br/>skills_to_validate, gaps_to_probe,<br/>role-tuned evaluation weights"]
    D --> E["Upsert Candidate / Job / AtsScore<br/>Insert Interview (scheduled) + InterviewContext"]
    E --> F["Create LiveKit room<br/>interview-{id}, max 3, empty_timeout 30s"]
    F --> G["create_invite_token() — 24h JWT<br/>join_url = /interview/{id}?token=…"]
    G --> H["Email invite to candidate<br/>(skipped silently if SMTP unset)"]
    H --> I["Candidate clicks link →<br/>GET /interviews/{id}/token?token=…"]
    I -->|"valid signature + not expired"| J["LiveKit JWT issued →<br/>browser joins room (relay/TCP 443)"]
    I -->|"invalid / expired"| X1["401 Unauthorized"]
    J --> K["LiveKit dispatches agent job →<br/>entrypoint() loads context from DB"]
    K --> K1{"in_progress with<br/>saved stage?"}
    K1 -->|yes| K2["apply_resume() —<br/>'Welcome back…' re-greet"]
    K1 -->|no| K3["Fresh greeting (intro stage)"]
    K2 & K3 --> L["Start Simli avatar (optional) +<br/>AgentSession (VAD/STT/LLM/TTS)"]
    L --> M["CONVERSATION LOOP<br/>(see diagram 3)"]
    M --> N["Candidate leaves / wrap_up done"]
    N --> O["DisconnectGraceTimer — 60s"]
    O -->|"rejoins in time"| M
    O -->|"grace expires"| P["on_shutdown:<br/>status=completed, duration saved,<br/>transcript tasks drained"]
    P --> Q["run_evaluation() — Claude Haiku<br/>scores 1–10: communication, confidence,<br/>jd_fit, behavioral + extraction + recommendation"]
    Q --> R["overall (0–100) computed IN CODE<br/>with role-tuned weights<br/>(default JD 35 / Comm 25 / Behav 15 / Conf 15 / ATS 10)"]
    R --> S["Save InterviewScore +<br/>InterviewExtractedData"]
    S --> T["run_report() — no LLM call<br/>HTML + JSON report, signed 7-day report_url"]
    T --> U["Recruiter reviews:<br/>GET /evaluation · GET /report ·<br/>GET /report/html?token=…"]
    P --> V["cost_tracker.finalize_and_log()<br/>Claude + Deepgram + LiveKit + Simli + TTS → total_usd"]
```

---

## 3. Realtime Voice Turn Loop

```mermaid
flowchart TD
    A["Candidate speaks<br/>(or types in chat box)"] --> B["Silero VAD detects speech"]
    B --> C["Deepgram STT nova-2-general<br/>streaming, endpointing 200ms"]
    C --> D["on_user_turn_completed()"]
    D --> D1{"empty text?"}
    D1 -->|yes| D2["Mishear guard:<br/>'Could you say that again?'"]
    D1 -->|no| E["Save candidate turn → DB<br/>(fire-and-forget JSONB append)"]
    E --> F["LangGraph.ainvoke()<br/>advance_node()"]
    F --> F1{"stage answered?<br/>(heuristic regex detector)<br/>OR 3 turns in stage?"}
    F1 -->|yes| F2["Advance to next stage,<br/>new stage_instruction"]
    F1 -->|no| F3["Stay in stage,<br/>retry instruction"]
    F2 & F3 --> G["queue_save_stage() →<br/>persist stage to<br/>interview_contexts.question_flow"]
    G --> H["Natural pause 300–600ms<br/>+ 20% filler ('Hmm.', 'Right.')"]
    H --> I["Claude Haiku 4.5 generates reply<br/>(base prompt + role context + stage_instruction)"]
    I --> J["conversation_item_added event →<br/>save AI turn to DB"]
    I --> K["Deepgram TTS aura-2-vesta-en<br/>('Sarah', 24kHz)"]
    K --> L{"avatar active?"}
    L -->|yes| M["Simli plugin worker:<br/>lip-synced video + voice republished<br/>as 'simli-avatar' participant<br/>(room audio output disabled)"]
    L -->|no| N["Voice via room audio track"]
    M & N --> O["Candidate hears/sees reply"]
    O --> A
```

---

## 4. LangGraph Interview State Machine

```mermaid
stateDiagram-v2
    [*] --> intro
    intro --> experience : ≥8 words spoken OR 3 turns
    experience --> current_ctc : years pattern detected OR 3 turns
    current_ctc --> expected_ctc : salary figure detected OR 3 turns
    expected_ctc --> notice_period : salary figure detected OR 3 turns
    notice_period --> relocation : notice keywords OR 3 turns
    relocation --> joining : yes/no / relocation words OR 3 turns
    joining --> wrap_up : joining date words OR 3 turns
    wrap_up --> complete : always (1 turn)
    complete --> [*]

    note right of intro
        Each stage emits a stage_instruction
        injected into Claude's system prompt.
        Captured flags persist to DB after
        every turn (crash recovery).
    end note
```

---

## 5. Crash Recovery & Durability

```mermaid
flowchart TD
    subgraph IN["In-Interview Recovery (Phase 11)"]
        A["Candidate disconnects<br/>(reload / network blip / End)"] --> B["DisconnectGraceTimer<br/>60s countdown"]
        B -->|"reconnects ≤60s"| C["candidate_returned() →<br/>restart Simli avatar →<br/>'Welcome back…' re-greet,<br/>same stage resumes"]
        B -->|"no return"| D["ctx.shutdown() →<br/>normal completion path"]
        E["Agent worker crashes"] --> F["LiveKit dispatches fresh job"]
        F --> G["load_resume_state() from<br/>interview_contexts.question_flow"]
        G --> C
    end

    subgraph BG["Background Recovery (Phase 8 — APScheduler)"]
        H["every 15 min:<br/>in_progress > 2h → complete + evaluate"]
        I["every 10 min:<br/>completed, no score → re-evaluate"]
        J["every 10 min:<br/>scored, no report → re-generate"]
        K["every 60 min:<br/>scheduled > 24h, 0 turns → expired"]
        L["also all run once at FastAPI startup"]
    end
```

---

## 6. Database Schema (ER Overview)

```mermaid
erDiagram
    CANDIDATES ||--o{ INTERVIEWS : "candidate_id"
    JOBS ||--o{ INTERVIEWS : "job_id"
    CANDIDATES ||--o{ ATS_SCORES : ""
    JOBS ||--o{ ATS_SCORES : ""
    INTERVIEWS ||--|| INTERVIEW_CONTEXTS : "strategy + weights + saved stage"
    INTERVIEWS ||--|| INTERVIEW_TRANSCRIPTS : "turns JSONB array"
    INTERVIEWS ||--|| INTERVIEW_SCORES : "scores + recommendation"
    INTERVIEWS ||--|| INTERVIEW_EXTRACTED_DATA : "CTC, notice, relocation…"
    INTERVIEWS ||--|| INTERVIEW_REPORTS : "HTML + JSON + signed URL"
    INTERVIEWS ||--|| INTERVIEW_COSTS : "usage + cost JSONB"

    CANDIDATES {
        uuid id PK
        string tenant_id
        string ats_candidate_id "unique per tenant"
        jsonb profile "skills, experience, projects"
    }
    INTERVIEWS {
        uuid id PK
        string status "scheduled/in_progress/completed/failed/expired"
        string join_url
        datetime started_at
        int duration_seconds
    }
    INTERVIEW_CONTEXTS {
        array skills_to_validate
        array gaps_to_probe
        jsonb question_flow "LangGraph stage persistence"
        jsonb evaluation_weights "role-tuned"
    }
    INTERVIEW_SCORES {
        int communication_score "1-10"
        int jd_fit_score "1-10"
        int overall_score "0-100, computed in code"
        string recommendation "proceed_to_l2/hold/reject"
    }
```

---

## Key Facts (verified in code)

| Aspect | Value |
|---|---|
| Framework | FastAPI + livekit-agents (two separate processes) |
| Conversation LLM | `claude-haiku-4-5-20251001` (also used for strategy + evaluation) |
| STT | Deepgram `nova-2-general` (streaming, 200ms endpointing) |
| TTS | **Deepgram `aura-2-vesta-en`** ("Sarah") — code moved off ElevenLabs; some docs/pricing labels still say ElevenLabs |
| Avatar | Simli via official `livekit-plugins-simli` (separate worker); optional, graceful fallback to voice-only |
| Orchestration | LangGraph — single `advance_node`, 9 stages, heuristic detectors, max 3 turns/stage |
| Auth | X-Tenant-ID (HRMS APIs) + signed JWTs (invite 24h, report 7-day) |
| DB | PostgreSQL + SQLAlchemy 2.0 async (asyncpg), JSONB-heavy, one row per interview per table |
| Durability | 60s disconnect grace, per-turn stage persistence, 4 APScheduler recovery jobs, eval→report auto-chain |
| Cost tracking | Per-interview marginal USD across Claude / Deepgram / LiveKit / Simli / TTS in `interview_costs` |
