"""
Phase 7 — Recruiter Report Generator
======================================
Assembles a complete recruiter report from evaluation results already in the DB.
No extra Claude API call — reuses the raw_extraction JSON saved by evaluation_engine.py.

Outputs:
  • report_data  — structured JSON (used by dashboard/API)
  • report_html  — self-contained HTML (browser-renderable, print-to-PDF ready)
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import create_report_token
from app.db.session import AsyncSessionLocal
from app.models.interview import (
    Interview, InterviewContext, InterviewTranscript,
    InterviewExtractedData, InterviewScore,
)
from app.models.candidate import Candidate
from app.models.job import Job
from app.models.ats_score import AtsScore
from app.models.report import InterviewReport

logger = logging.getLogger(__name__)


# ── Transcript highlight picker ────────────────────────────────────────────────

def _pick_highlights(turns: list, max_pairs: int = 5) -> list[dict]:
    """
    Return up to max_pairs Q-A exchange dicts from the transcript.
    `turns` is a list of dicts: {"speaker": "ai"|"candidate", "message": "..."}
    (the JSONB array from the redesigned interview_transcripts table).
    """
    highlights = []
    i = 0
    while i < len(turns) and len(highlights) < max_pairs:
        turn = turns[i]
        if turn.get("speaker") == "ai":
            ai_msg = turn.get("message", "")
            # Find next candidate reply
            j = i + 1
            while j < len(turns) and turns[j].get("speaker") == "ai":
                j += 1
            if j < len(turns):
                highlights.append({
                    "question": ai_msg,
                    "answer":   turns[j].get("message", ""),
                })
                i = j + 1
            else:
                i += 1
        else:
            i += 1
    return highlights


# ── Colour helpers ─────────────────────────────────────────────────────────────

def _score_colour(score: int | None) -> str:
    if score is None:
        return "#94A3B8"
    if score >= 8:
        return "#2563EB"
    if score >= 6:
        return "#3B82F6"
    if score >= 4:
        return "#EAB308"
    return "#EF4444"


def _rec_badge(rec: str | None) -> tuple[str, str]:
    """Returns (label, colour) for the recommendation badge."""
    mapping = {
        "proceed_to_l2": ("PROCEED TO L2", "#2563EB"),
        "hold":           ("HOLD",          "#EAB308"),
        "reject":         ("REJECT",        "#EF4444"),
    }
    return mapping.get(rec or "", ("UNKNOWN", "#94A3B8"))


def _overall_colour(score: int | None) -> str:
    if score is None:
        return "#94A3B8"
    if score >= 70:
        return "#2563EB"
    if score >= 50:
        return "#EAB308"
    return "#EF4444"


def _fmt_inr(value: int | None) -> str:
    if value is None:
        return "—"
    lakh = value / 100_000
    return f"₹{lakh:.1f}L / yr"


def _fmt_bool(val: bool | None) -> str:
    if val is None:
        return "—"
    return "Yes" if val else "No"


# ── HTML renderer ──────────────────────────────────────────────────────────────

def _render_html(d: dict) -> str:
    scores     = d.get("scores", {})
    extracted  = d.get("extracted_data", {}) or {}
    strengths  = d.get("strengths", [])
    weaknesses = d.get("weaknesses", [])
    red_flags  = d.get("red_flags", [])
    highlights = d.get("transcript_highlights", [])
    rec_label, rec_color = _rec_badge(d.get("recommendation"))
    overall    = d.get("overall_score")

    # ── Role-tuned scoring weights (optional — null for pre-feature interviews) ──
    weights_doc = d.get("evaluation_weights") or {}
    w           = weights_doc.get("weights") or {}
    _wlabels    = [("JD Fit", "jd_fit"), ("Communication", "communication"),
                   ("Behavioral", "behavioral"), ("Confidence", "confidence"), ("ATS", "ats")]
    weight_chips = "".join(
        f'<span class="wchip">{lbl} <b>{w.get(key)}%</b></span>'
        for lbl, key in _wlabels if w.get(key) is not None
    )
    role_cat  = (weights_doc.get("role_category") or "").replace("_", " ").title()
    w_rationale = weights_doc.get("rationale") or ""
    tuned     = weights_doc.get("source") == "llm"
    weights_section = ""
    if weight_chips:
        weights_section = f"""
    <div class="section">
      <div class="section-title">Scoring Weights {'&mdash; role-tuned' if tuned else '(standard)'}</div>
      <div class="wchips">{weight_chips}</div>
      {f'<p class="wrole">Role profile: <b>{role_cat}</b></p>' if role_cat else ''}
      {f'<p class="summary-text" style="margin-top:6px">{w_rationale}</p>' if w_rationale else ''}
    </div>"""

    def score_bar(label: str, val: int | None, key: str) -> str:
        v   = val or 0
        col = _score_colour(val)
        pct = v * 10
        return f"""
        <div class="score-row">
          <span class="score-label">{label}</span>
          <div class="bar-wrap">
            <div class="bar-fill" style="width:{pct}%;background:{col};"></div>
          </div>
          <span class="score-val" style="color:{col}">{v}/10</span>
        </div>"""

    def bullet_list(items: list, colour: str) -> str:
        if not items:
            return '<p class="muted">None noted.</p>'
        lis = "".join(f'<li><span style="color:{colour}">▸</span> {i}</li>' for i in items)
        return f"<ul class='blist'>{lis}</ul>"

    highlight_rows = ""
    for h in highlights:
        q = h.get("question", "").replace("<", "&lt;").replace(">", "&gt;")
        a = h.get("answer",   "").replace("<", "&lt;").replace(">", "&gt;")
        highlight_rows += f"""
        <div class="hl-row">
          <div class="hl-q"><span class="hl-tag ai-tag">AI</span>{q}</div>
          <div class="hl-a"><span class="hl-tag ca-tag">Candidate</span>{a}</div>
        </div>"""

    # ── Voice & Delivery Analysis (optional — only for voice-weighted roles) ──
    voice = d.get("voice_analysis") or {}
    voice_section = ""
    if voice and (voice.get("narrative") or voice.get("features")):
        vf    = voice.get("features", {}) or {}
        blend = voice.get("blend", {}) or {}

        def _vcell(label: str, val: str) -> str:
            return f'<div class="data-cell"><label>{label}</label><span>{val}</span></div>'

        _delivery = voice.get("delivery_score")
        _hes = vf.get("within_pause_ratio")
        _metrics = "".join([
            _vcell("Delivery Score", f"{_delivery}/10" if _delivery is not None else "—"),
            _vcell("Speaking Pace", f"{vf.get('pace_wpm')} wpm" if vf.get("pace_wpm") is not None else "—"),
            _vcell("Expressiveness", f"{vf.get('pitch_std_hz')} Hz var" if vf.get("pitch_std_hz") is not None else "—"),
            _vcell("Voice Clarity", f"{vf.get('hnr_db')} dB HNR" if vf.get("hnr_db") is not None else "—"),
            _vcell("Hesitation", f"{round(_hes * 100)}%" if _hes is not None else "—"),
            _vcell("Filler Rate", f"{vf.get('filler_per_100w')} /100w" if vf.get("filler_per_100w") is not None else "—"),
        ])

        _blend_note = ""
        if blend:
            _vw = int(round(blend.get("voice_weight", 0) * 100))
            _blend_note = (
                f'<p class="muted" style="margin-top:10px">Vocal delivery blended into scores '
                f'(voice weight {_vw}%): Communication {blend.get("text_communication")}&rarr;'
                f'<b>{blend.get("blended_communication")}</b>, Confidence '
                f'{blend.get("text_confidence")}&rarr;<b>{blend.get("blended_confidence")}</b>.</p>'
            )

        voice_section = f"""
    <div class="section">
      <div class="section-title">Voice &amp; Delivery Analysis</div>
      <p class="summary-text">{voice.get("narrative") or "Acoustic analysis of the candidate's vocal delivery."}</p>
      <div class="data-grid" style="margin-top:14px">{_metrics}</div>
      <div class="grid2" style="margin-top:16px">
        <div>
          <div style="font-size:11.5px;font-weight:600;color:#2563EB;margin-bottom:8px;">Delivery Strengths</div>
          {bullet_list(voice.get("strengths", []) or [], '#2563EB')}
        </div>
        <div>
          <div style="font-size:11.5px;font-weight:600;color:#EAB308;margin-bottom:8px;">Delivery Concerns</div>
          {bullet_list(voice.get("concerns", []) or [], '#EAB308')}
        </div>
      </div>
      {_blend_note}
      <p class="muted" style="margin-top:8px;font-size:11px">Objective acoustic measurements, advisory only — interpret alongside the transcript; accents and audio quality can affect readings.</p>
    </div>"""

    gen_at  = d.get("generated_at", "")
    dur_sec = d.get("duration_seconds")
    dur_str = f"{dur_sec // 60}m {dur_sec % 60}s" if dur_sec else "—"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Interview Report — {d.get('candidate_name','Candidate')}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{font-family:'Inter',sans-serif;background:#F1F5F9;color:#0F172A;font-size:13.5px;}}
  .page{{max-width:860px;margin:32px auto;background:#fff;border-radius:16px;
         box-shadow:0 4px 24px rgba(37,99,235,.10);overflow:hidden;}}

  /* Header */
  .hdr{{background:linear-gradient(135deg,#1E3A8A 0%,#2563EB 60%,#1D4ED8 100%);
        padding:28px 36px;color:#fff;display:flex;justify-content:space-between;align-items:flex-start;}}
  .hdr-logo{{font-size:18px;font-weight:700;letter-spacing:.3px;}}
  .hdr-logo span{{color:#FDE047;}}
  .hdr-meta{{font-size:11px;opacity:.75;text-align:right;line-height:1.8;}}

  /* Candidate strip */
  .cstrip{{background:#EFF6FF;border-bottom:2px solid #DBEAFE;padding:18px 36px;
           display:flex;gap:40px;flex-wrap:wrap;}}
  .cfield{{display:flex;flex-direction:column;gap:2px;}}
  .cfield label{{font-size:10px;font-weight:600;color:#64748B;text-transform:uppercase;letter-spacing:.6px;}}
  .cfield span{{font-size:13.5px;font-weight:600;color:#0F172A;}}

  /* Recommendation banner */
  .rec-banner{{padding:14px 36px;display:flex;align-items:center;gap:18px;
               border-bottom:1px solid #E2E8F0;}}
  .rec-badge{{padding:6px 18px;border-radius:6px;font-size:12px;font-weight:700;
              letter-spacing:.8px;color:#fff;}}
  .rec-score{{font-size:13px;color:#475569;}}
  .rec-score strong{{font-size:22px;font-weight:700;}}

  /* Body sections */
  .body{{padding:0 36px 36px;}}
  .section{{margin-top:28px;}}
  .section-title{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;
                  color:#2563EB;border-bottom:1.5px solid #DBEAFE;padding-bottom:6px;margin-bottom:14px;}}

  /* Score bars */
  .score-row{{display:flex;align-items:center;gap:12px;margin-bottom:9px;}}
  .score-label{{width:130px;font-size:12.5px;font-weight:500;color:#334155;flex-shrink:0;}}
  .bar-wrap{{flex:1;height:8px;background:#EFF6FF;border-radius:4px;overflow:hidden;}}
  .bar-fill{{height:100%;border-radius:4px;transition:width .4s;}}
  .score-val{{width:38px;text-align:right;font-size:12px;font-weight:700;}}

  /* Two-column grid */
  .grid2{{display:grid;grid-template-columns:1fr 1fr;gap:20px;}}

  /* Bullet lists */
  .blist{{list-style:none;display:flex;flex-direction:column;gap:5px;}}
  .blist li{{font-size:12.5px;color:#334155;line-height:1.5;}}
  .blist li span{{margin-right:6px;}}

  /* Key data table */
  .data-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;}}
  .data-cell{{background:#F8FAFC;border:1px solid #E2E8F0;border-radius:9px;padding:10px 13px;}}
  .data-cell label{{font-size:10px;font-weight:600;color:#64748B;text-transform:uppercase;letter-spacing:.5px;display:block;margin-bottom:3px;}}
  .data-cell span{{font-size:13px;font-weight:600;color:#0F172A;}}

  /* Red flags */
  .flag-item{{display:flex;align-items:flex-start;gap:8px;background:#FFF7ED;
              border:1px solid #FDE68A;border-radius:8px;padding:9px 13px;margin-bottom:7px;
              font-size:12.5px;color:#92400E;}}
  .flag-ic{{font-size:14px;margin-top:1px;}}
  .no-flags{{font-size:12.5px;color:#22C55E;font-weight:500;}}

  /* Transcript highlights */
  .hl-row{{background:#F8FAFC;border:1px solid #E2E8F0;border-radius:10px;
           padding:13px 15px;margin-bottom:10px;}}
  .hl-q{{font-size:12.5px;color:#334155;margin-bottom:7px;line-height:1.55;}}
  .hl-a{{font-size:12.5px;color:#0F172A;line-height:1.55;font-weight:500;}}
  .hl-tag{{display:inline-block;font-size:9.5px;font-weight:700;border-radius:4px;
           padding:2px 7px;margin-right:7px;letter-spacing:.4px;}}
  .ai-tag{{background:#EFF6FF;color:#2563EB;}}
  .ca-tag{{background:#FEFCE8;color:#CA8A04;}}

  .summary-text{{font-size:13px;color:#334155;line-height:1.75;}}
  .muted{{font-size:12.5px;color:#94A3B8;}}

  /* Role-tuned scoring weights */
  .wchips{{display:flex;flex-wrap:wrap;gap:8px;}}
  .wchip{{background:#EFF6FF;border:1px solid #DBEAFE;border-radius:7px;
          padding:6px 12px;font-size:12px;color:#334155;}}
  .wchip b{{color:#2563EB;font-weight:700;}}
  .wrole{{font-size:12px;color:#64748B;margin-top:8px;}}
  .wrole b{{color:#0F172A;}}

  /* Footer */
  .footer{{background:#F8FAFC;border-top:1px solid #E2E8F0;padding:14px 36px;
           display:flex;justify-content:space-between;font-size:10.5px;color:#94A3B8;}}

  /* Print button */
  .print-bar{{background:#fff;padding:12px 36px;border-bottom:1px solid #E2E8F0;
              display:flex;justify-content:flex-end;gap:10px;}}
  .btn-pdf{{display:inline-flex;align-items:center;gap:7px;background:#2563EB;color:#fff;
            border:none;border-radius:8px;padding:8px 18px;font-family:'Inter',sans-serif;
            font-size:12.5px;font-weight:600;cursor:pointer;transition:background .15s;}}
  .btn-pdf:hover{{background:#1D4ED8;}}
  .btn-pdf svg{{width:14px;height:14px;fill:none;stroke:currentColor;stroke-width:2.5;}}

  @media print{{
    body{{background:#fff;}}
    .page{{margin:0;border-radius:0;box-shadow:none;}}
    .print-bar{{display:none !important;}}
  }}
</style>
</head>
<body>
<div class="page">

  <!-- Print / PDF bar -->
  <div class="print-bar">
    <button class="btn-pdf" onclick="window.print()">
      <svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 01-2-2v-5a2 2 0 012-2h16a2 2 0 012 2v5a2 2 0 01-2 2h-2"/>
        <rect x="6" y="14" width="12" height="8"/>
      </svg>
      Save as PDF
    </button>
  </div>

  <!-- Header -->
  <div class="hdr">
    <div>
      <div class="hdr-logo">AI HR <span>Assistant</span></div>
      <div style="font-size:12px;opacity:.7;margin-top:4px;">L1 Screening Evaluation Report</div>
    </div>
    <div class="hdr-meta">
      Generated: {gen_at}<br/>
      Interview Duration: {dur_str}
    </div>
  </div>

  <!-- Candidate strip -->
  <div class="cstrip">
    <div class="cfield">
      <label>Candidate</label>
      <span>{d.get('candidate_name','—')}</span>
    </div>
    <div class="cfield">
      <label>Position</label>
      <span>{d.get('position_title','—')}</span>
    </div>
    <div class="cfield">
      <label>ATS Pre-Score</label>
      <span>{d.get('ats_score','—')}/100</span>
    </div>
    <div class="cfield">
      <label>Interview Date</label>
      <span>{d.get('interview_date','—')}</span>
    </div>
  </div>

  <!-- Recommendation banner -->
  <div class="rec-banner">
    <div class="rec-badge" style="background:{rec_color}">{rec_label}</div>
    <div class="rec-score">
      Overall Score &nbsp;
      <strong style="color:{_overall_colour(overall)}">{overall or '—'}</strong>
      <span style="font-size:13px;color:#94A3B8">/100</span>
    </div>
  </div>

  <div class="body">

    <!-- Score breakdown -->
    <div class="section">
      <div class="section-title">Score Breakdown</div>
      {score_bar("Communication",  scores.get("communication"), "communication")}
      {score_bar("Confidence",     scores.get("confidence"),    "confidence")}
      {score_bar("JD Fit",         scores.get("jd_fit"),        "jd_fit")}
      {score_bar("Behavioral",     scores.get("behavioral"),    "behavioral")}
    </div>
{weights_section}
{voice_section}

    <!-- Executive summary -->
    <div class="section">
      <div class="section-title">Executive Summary</div>
      <p class="summary-text">{d.get('summary','—')}</p>
    </div>

    <!-- Strengths & Weaknesses -->
    <div class="section">
      <div class="section-title">Strengths &amp; Weaknesses</div>
      <div class="grid2">
        <div>
          <div style="font-size:11.5px;font-weight:600;color:#2563EB;margin-bottom:8px;">Strengths</div>
          {bullet_list(strengths, '#2563EB')}
        </div>
        <div>
          <div style="font-size:11.5px;font-weight:600;color:#EAB308;margin-bottom:8px;">Weaknesses</div>
          {bullet_list(weaknesses, '#EAB308')}
        </div>
      </div>
    </div>

    <!-- Key interview data -->
    <div class="section">
      <div class="section-title">Key Interview Data</div>
      <div class="data-grid">
        <div class="data-cell"><label>Current CTC</label><span>{_fmt_inr(extracted.get('current_ctc'))}</span></div>
        <div class="data-cell"><label>Expected CTC</label><span>{_fmt_inr(extracted.get('expected_ctc'))}</span></div>
        <div class="data-cell"><label>Salary Fit</label><span>{_fmt_bool(scores.get('salary_fit'))}</span></div>
        <div class="data-cell"><label>Notice Period</label><span>{ str(extracted.get('notice_period_days') or '—') + (' days' if extracted.get('notice_period_days') else '')}</span></div>
        <div class="data-cell"><label>Negotiable</label><span>{_fmt_bool(extracted.get('notice_negotiable'))}</span></div>
        <div class="data-cell"><label>Relocation</label><span>{_fmt_bool(extracted.get('relocation_willing'))}</span></div>
        <div class="data-cell"><label>Earliest Joining</label><span>{extracted.get('earliest_joining') or '—'}</span></div>
        <div class="data-cell"><label>Experience (Stated)</label><span>{str(extracted.get('total_experience_years') or '—') + (' yrs' if extracted.get('total_experience_years') else '')}</span></div>
        <div class="data-cell"><label>Exp. Validated</label><span>{_fmt_bool(scores.get('experience_validated'))}</span></div>
      </div>
    </div>

    <!-- Red flags -->
    <div class="section">
      <div class="section-title">Red Flags</div>
      {"".join(f'<div class="flag-item"><div class="flag-ic">⚠</div><div>{f}</div></div>' for f in red_flags)
        if red_flags else '<p class="no-flags">✓ No red flags detected</p>'}
    </div>

    <!-- Transcript highlights -->
    <div class="section">
      <div class="section-title">Transcript Highlights</div>
      {highlight_rows if highlight_rows else '<p class="muted">No transcript available.</p>'}
    </div>

  </div><!-- /body -->

  <!-- Footer -->
  <div class="footer">
    <span>AI HR Assistant — Confidential</span>
    <span>Interview ID: {d.get('interview_id','')}</span>
  </div>

</div>
</body>
</html>"""


# ── Data assembler ─────────────────────────────────────────────────────────────

async def _assemble(db: AsyncSession, interview_id: str) -> dict | None:
    # Interview
    interview = (await db.execute(
        select(Interview).where(Interview.id == interview_id)
    )).scalar_one_or_none()
    if not interview:
        return None

    # Candidate (profile is embedded as JSONB in Candidate.profile)
    candidate = (await db.execute(
        select(Candidate).where(Candidate.id == interview.candidate_id)
    )).scalar_one_or_none()
    candidate_name = (
        f"{candidate.first_name} {candidate.last_name or ''}".strip()
        if candidate else "Candidate"
    )
    profile: dict = (candidate.profile or {}) if candidate else {}

    # Job
    job = (await db.execute(
        select(Job).where(Job.id == interview.job_id)
    )).scalar_one_or_none()

    # ATS
    ats = (await db.execute(
        select(AtsScore).where(and_(
            AtsScore.candidate_id == interview.candidate_id,
            AtsScore.job_id       == interview.job_id,
        ))
    )).scalar_one_or_none()

    # Interview context — holds the role-tuned evaluation weights
    context = (await db.execute(
        select(InterviewContext).where(InterviewContext.interview_id == interview_id)
    )).scalar_one_or_none()

    # Scores (must exist)
    score_row = (await db.execute(
        select(InterviewScore).where(InterviewScore.interview_id == interview_id)
    )).scalar_one_or_none()
    if not score_row:
        logger.warning(f"[report] evaluation not found for {interview_id}")
        return None

    # Extracted data
    ext_row = (await db.execute(
        select(InterviewExtractedData).where(
            InterviewExtractedData.interview_id == interview_id
        )
    )).scalar_one_or_none()

    # Transcript — single row per interview, ordered JSONB array of turns
    transcript_row = (await db.execute(
        select(InterviewTranscript)
        .where(InterviewTranscript.interview_id == interview_id)
    )).scalar_one_or_none()
    turns = transcript_row.turns if transcript_row else []

    # Pull narrative content from raw_extraction (saved by evaluation engine)
    raw = (ext_row.raw_extraction or {}) if ext_row else {}
    summary    = raw.get("summary", score_row.ai_reasoning or "")
    strengths  = raw.get("strengths",  [])
    weaknesses = raw.get("weaknesses", [])
    red_flags  = raw.get("red_flags",  [])

    # ext_data: read from the 'extracted' JSONB document (new path)
    # Falls back to empty dict gracefully when the column is null (pre-migration rows).
    ext_data: dict = (ext_row.extracted or {}) if ext_row else {}

    now = datetime.now(timezone.utc)

    report_data = {
        "interview_id":     interview_id,
        "candidate_name":   candidate_name,
        "position_title":   job.position_title if job else "—",
        "department":       job.department     if job else None,
        "ats_score":        ats.total_score    if ats else None,
        "interview_date":   interview.started_at.strftime("%d %b %Y") if interview.started_at else "—",
        "duration_seconds": interview.duration_seconds,
        "generated_at":     now.strftime("%d %b %Y, %H:%M UTC"),

        "scores": {
            "communication":        score_row.communication_score,
            "confidence":           score_row.confidence_score,
            "jd_fit":               score_row.jd_fit_score,
            "behavioral":           score_row.behavioral_score,
            "overall":              score_row.overall_score,
            "salary_fit":           score_row.salary_fit,
            "experience_validated": score_row.experience_validated,
        },

        "overall_score":  score_row.overall_score,
        "recommendation": score_row.recommendation,
        "evaluation_weights": (context.evaluation_weights if context else None),
        # Voice & delivery analysis (only present for voice-weighted roles with audio)
        "voice_analysis": raw.get("voice_analysis"),
        "summary":        summary,
        "strengths":      strengths,
        "weaknesses":     weaknesses,
        "red_flags":      red_flags,
        "extracted_data": ext_data,

        "candidate_profile": {
            "skills":           profile.get("skills")           or [],
            "certifications":   profile.get("certifications")   or [],
            "experience_years": float(profile["total_experience_years"])
                                if profile.get("total_experience_years") else None,
        },

        "transcript_highlights": _pick_highlights(turns, max_pairs=5),
    }

    return report_data


# ── Public entry point ─────────────────────────────────────────────────────────

async def run_report(interview_id: str) -> bool:
    """
    Generate and save a recruiter report for the given interview.
    Safe to call as a fire-and-forget asyncio task.
    Retries up to 3 times with exponential backoff on exception.
    Returns True on success.
    """
    logger.info(f"[report] generating report for {interview_id}")

    _max_attempts = 3
    _backoff_seconds = [2, 4, 8]

    for attempt in range(1, _max_attempts + 1):
        try:
            async with AsyncSessionLocal() as db:
                result = await _generate(db, interview_id)
            return result
        except Exception as exc:
            if attempt < _max_attempts:
                wait = _backoff_seconds[attempt - 1]
                logger.warning(
                    f"[report] attempt {attempt}/{_max_attempts} failed for "
                    f"{interview_id} -- retrying in {wait}s: {exc}"
                )
                await asyncio.sleep(wait)
            else:
                logger.error(
                    f"[report] fatal error after {_max_attempts} attempts "
                    f"for {interview_id}: {exc}",
                    exc_info=True,
                )
                return False

    return False  # unreachable but satisfies type checker


async def _generate(db: AsyncSession, interview_id: str) -> bool:
    report_data = await _assemble(db, interview_id)
    if not report_data:
        return False

    # Render HTML
    html = _render_html(report_data)

    # Load interview for tenant_id
    interview = (await db.execute(
        select(Interview).where(Interview.id == interview_id)
    )).scalar_one_or_none()

    # Upsert report row
    existing = (await db.execute(
        select(InterviewReport).where(InterviewReport.interview_id == interview_id)
    )).scalar_one_or_none()

    if existing:
        report_row = existing
    else:
        report_row = InterviewReport(
            interview_id=interview_id,
            tenant_id=str(interview.tenant_id),
        )
        db.add(report_row)

    # Generate a signed token so the HTML report URL is access-controlled.
    # Only holders of this signed link can view the recruiter report.
    report_token = create_report_token(interview_id)
    report_url = (
        f"{settings.app_base_url}/api/v1/interviews/{interview_id}/report/html"
        f"?token={report_token}"
    )

    report_row.candidate_name = report_data["candidate_name"]
    report_row.position_title = report_data["position_title"]
    report_row.ats_score      = report_data["ats_score"]
    report_row.overall_score  = report_data["overall_score"]
    report_row.recommendation = report_data["recommendation"]
    report_row.report_url     = report_url
    report_row.report_data    = report_data
    report_row.report_html    = html
    report_row.generated_at   = datetime.now(timezone.utc)

    await db.commit()
    logger.info(f"[report] ✓ saved report for {interview_id}")

    # Phase 2 — flatten everything into the read-only ATS results export table.
    # Self-contained + non-fatal; the report already committed above.
    try:
        from app.services.ats_results_export import export_interview_results
        await export_interview_results(interview_id)
    except Exception as e:
        logger.warning(f"[ats-export] could not export results for {interview_id}: {e}")

    return True
