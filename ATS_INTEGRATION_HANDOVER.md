# ATS ↔ Interview Agent — Integration Handover

**Audience:** ATS development team
**Owner:** Interview Agent team
**Model:** PUSH import. The ATS exports its data as JSON and **POSTs it to us**;
we store it. We then build the trigger payload and start interviews ourselves.
The Interview Agent never connects to the ATS database or API.

---

## 1. How it works (one picture)

```
ATS side:
  export candidate data as JSON  ──►  POST /api/v1/integration/import
                                          │
Interview Agent:
  stores every record verbatim in our DB (table: ats_imports)

Then the Interview Agent team (not the ATS) does:
  POST /api/v1/integration/build-payload  { candidate_id, job_id, tenant_id }
        → returns the trigger JSON (built from the stored record)
  POST /api/v1/interviews/trigger  (that JSON)  → interview scheduled + emailed
```

**What the ATS team needs to do: only Step 1 — push the JSON.** Everything after
is on the Interview Agent side.

> No API key on the endpoint right now (testing). Protect it at the network level
> (private networking / security group). A key can be added later if exposed publicly.

---

## 2. The endpoint the ATS calls

### Request
```
POST  http://<interview-agent-host>/api/v1/integration/import
Content-Type: application/json

{
  "tenant_id": "tenant-001",
  "records": [
    {
      "candidate_id": "CAND-001",
      "job_id": "JOB-77",
      "candidate_name": "Rajiv Chaudhary",
      "candidate_email": "rajiv@example.com",      // REQUIRED for the interview invite
      "candidate_phone": "+91XXXXXXXXXX",
      "resume_filename": "Rajiv_Chaudhary.pdf",
      "parsed_resume": { ...raw /parse output... },
      "ats_score":     { ...raw /ats-score output... },
      "jd":            { ...raw JD / job-description output... }
    }
    // ... one object per candidate+job, batch — send everything
  ]
}
```

- Send **all records in one POST** (batch). Re-sending refreshes existing rows
  (matched on `tenant_id` + `candidate_id` + `job_id`).
- `parsed_resume`, `ats_score`, `jd` are your **raw** exports — passed straight
  through; you don't need to reshape their inner contents.
- **`candidate_email` is required** — the interview join link is emailed to it.

### Success response — `200 OK`
```json
{ "imported": 12, "tenant_id": "tenant-001", "message": "Imported/refreshed 12 record(s)." }
```

### Example
```bash
curl -X POST http://<host>/api/v1/integration/import \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "tenant-001",
    "records": [
      { "candidate_id": "CAND-001", "job_id": "JOB-77",
        "candidate_email": "rajiv@example.com",
        "parsed_resume": {}, "ats_score": {}, "jd": {} }
    ]
  }'
```

---

## 3. What the Interview Agent does after (FYI — no ATS action)

**Build the trigger JSON for a chosen candidate:**
```
POST /api/v1/integration/build-payload
{ "candidate_id": "CAND-001", "job_id": "JOB-77", "tenant_id": "tenant-001" }
```
Returns the full body for the trigger endpoint. **404** if that record wasn't
imported yet.

**Trigger the interview** with that returned JSON:
```
POST /api/v1/interviews/trigger
Header: X-Tenant-ID: tenant-001
Body:   <the JSON returned by build-payload>
```
→ interview scheduled, join link emailed to the candidate.

---

## 4. The one open item — the export shape

We defined the `records[]` shape above. If your export looks different, either:
- map your export into this shape before POSTing (recommended), **or**
- send us one **sample** of your raw export and we'll adapt the importer to it.

The inner contents of `parsed_resume` / `ats_score` / `jd` should be your raw
`/parse`, `/ats-score`, and JD outputs — we consume those formats directly.

---

## 5. Getting results back (Phase 2 — not built yet)

After the interview, scores + a recruiter report exist. To be decided together:
- **Webhook** — we POST `interview.completed` to a URL you expose, or
- **Polling** — you call our read endpoints with the returned `interview_id`.

---

## 6. Go-live checklist

- [ ] ATS: export candidate data into the `records[]` shape (Section 2)
- [ ] ATS: POST it to `/integration/import`
- [ ] ATS: confirm `candidate_email` is present on every record
- [ ] Interview Agent: run the `ats_imports` migration (`alembic upgrade head`)
- [ ] Interview Agent: adjust JD field-name mapping if the export shape differs
- [ ] Both: one real candidate imported → build-payload → trigger → end-to-end test

---

## 7. API contract (interactive)

While our server runs, the live contract is at:
```
http://<interview-agent-host>/docs        (Swagger UI — "Integration" section)
```
