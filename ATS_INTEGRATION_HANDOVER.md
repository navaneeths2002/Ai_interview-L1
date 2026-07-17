# ATS ↔ Interview Agent — Integration Handover

**Audience:** ATS development team
**Owner:** Interview Agent team
**Model:** Single call. The ATS sends only IDs; the Interview Agent reads the
candidate's resume/score/job from the ATS database (read-only), starts the
interview, and returns the join link — all in one request.

---

## 1. How it works (one call)

```
ATS  ──►  POST /api/v1/integration/interview      X-API-Key: <key we give you>
          { candidate_id, job_id, tenant_id }
              │
Interview Agent:
   ├─ reads candidate + parsed_resume + ats_score + JD from the ATS DB (read-only)
   ├─ builds strategy, creates the room + signed invite link, emails the candidate
   └─ returns ──►  { interview_id, join_url, status, evaluation_weights }
```

One request in, the join link out. No import step, no second call.

---

## 2. What the ATS team must provide

### 2a. Read-only DB access  ✅ (already working against `testatsbjr`)
We read your MySQL directly, read-only. Needed:
- A **read-only** MySQL user.
- Host / port / database / user / password — we hold it as `ATS_DATABASE_URL`.
- Network access from our server to your MySQL (firewall / security group), port 3306.

### 2b. The one table we read — ✅ verified end-to-end
We read **only** the consolidated table you grant us read-only access to:

**`AiInterviewScheduleDetails`** — one row per (candidate, job):

| Column | Holds |
|---|---|
| `candidate_id` | the id you send |
| `job_id` | the id you send |
| `ResumeParseData` (json) | raw `/parse` output — we read name/email/phone/skills from here |
| `ScoreJsonData` (json) | raw `/ats-score` result |
| `JobDetailsJsonData` (json) | job basics: `JOB_TITLE`, `JOB_DESCRIPTION`, `REQUIREMENTS`, … |

**You populate this table**, then call the endpoint below with the `candidate_id`
+ `job_id`. We never touch any other table.

**Note:** `JobDetailsJsonData` currently carries only job *basics* — no required
skills, salary, or min-experience. The interview still runs; it's just lighter on
JD-alignment. If you want richer JD-aware interviews, add those fields to
`JobDetailsJsonData`.

### 2c. Call the endpoint
When a recruiter selects a candidate for the AI interview, call the endpoint below.
You only send IDs — we read the rest ourselves.

---

## 3. The endpoint

### Request
```
POST  https://<interview-agent-host>/api/v1/integration/interview
Content-Type: application/json
X-API-Key: <the key we give you>

{
  "candidate_id": "<your candidate id>",
  "job_id":       "<your job id>",
  "tenant_id":    "<your tenant id>"
}
```

### Success — `200 OK`
```json
{
  "interview_id": "e57628ef-...",
  "candidate_id": "...",
  "status": "scheduled",
  "join_url": "https://<host>/interview/e57628ef-...?token=...",
  "message": "Interview scheduled for <name>.",
  "evaluation_weights": { ... }
}
```
Store `interview_id` (to fetch results later). `join_url` is already emailed to
the candidate; keep it if you want to show/resend it.

### Example
```bash
curl -X POST https://<host>/api/v1/integration/interview \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your key>" \
  -d '{"candidate_id":"CAND-001","job_id":"JOB-77","tenant_id":"tenant-001"}'
```

### Errors
| Status | Meaning | What to check |
|---|---|---|
| `401` | Missing/invalid `X-API-Key` | the key we gave you |
| `404` | Candidate not found for that `candidate_id` | ID correct? row exists? |
| `422` | Candidate has **no email** | email is mandatory |
| `502` | ATS DB read failed | schema names / DB reachable / read grant |
| `503` | `ATS_DATABASE_URL` not configured on our side | our setup, not yours |
| `500` | Interview creation failed | contact Interview Agent team |

---

## 4. Getting results back (Phase 2 — not built yet)

After the interview, scores + a recruiter report exist. To be decided together:
- **Webhook** — we POST `interview.completed` to a URL you expose, or
- **Polling** — you call our read endpoints with the returned `interview_id`.

---

## 5. Go-live checklist

- [ ] ATS: read-only DB user + connection string shared
- [ ] ATS: network/SSL access to Postgres opened for our server
- [ ] ATS: table/column names provided (Section 2b)
- [ ] ATS: confirm `candidate_email` is present on every candidate
- [ ] Interview Agent: `ATS_DATABASE_URL` set + schema placeholders filled + `CONNECTOR_API_KEY` set
- [ ] ATS: call the endpoint on candidate selection with the `X-API-Key`
- [ ] Both: first real candidate tested end-to-end (validates the field mapping)

---

## 6. API contract (interactive)

While our server runs, the live contract is at:
```
https://<interview-agent-host>/docs        (Swagger UI — "Integration" section)
```
