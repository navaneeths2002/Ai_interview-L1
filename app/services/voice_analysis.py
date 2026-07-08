"""
Voice & Delivery Analysis Engine
=================================
Post-interview acoustic analysis of the CANDIDATE's recorded audio, for roles
where HOW the candidate speaks matters (sales, tele-caller, support, client-
facing). Text alone can't measure pace, pitch, energy or hesitation — this can.

Pipeline
--------
  1. Extract OBJECTIVE, explainable acoustic features from the local WAV:
       • parselmouth (Praat) → pitch (F0) mean/variation, jitter, shimmer, HNR
       • librosa            → loudness (RMS) dynamics, speech/silence ratio, pace
       • transcript         → word count, filler-word rate
  2. Feed those numbers to Claude → a delivery narrative + 1–10 sub-scores.

Design principles:
  • Explainable, not a black box — every score traces back to a real number,
    so it's defensible if a candidate ever challenges the decision.
  • NO emotion/affect model — those are accent-biased and opaque; avoided on
    purpose for fairness in a diverse-accent candidate base.
  • Advisory only — the recruiter can always override.
  • Fully self-contained; every failure returns None and never breaks evaluation.

Heavy CPU work (parselmouth/librosa) runs in a thread so the event loop is free.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

EVAL_MODEL = "claude-haiku-4-5-20251001"

# Rough filler-word set. "like"/"actually" etc. have legitimate uses, so the
# filler rate is an ESTIMATE — surfaced as a signal, never a hard penalty.
_FILLERS = [
    "um", "uh", "erm", "hmm", "like", "you know", "basically",
    "actually", "i mean", "sort of", "kind of", "literally",
]


# ════════════════════════════════════════════════════════════════════════════════
# 1. Acoustic feature extraction (blocking — run via asyncio.to_thread)
# ════════════════════════════════════════════════════════════════════════════════

def _safe(fn, default=None):
    """Run a metric extraction, swallow any failure (short/silent audio, etc.)."""
    try:
        v = fn()
        if v is None:
            return default
        # Praat returns NaN for undefined metrics
        if isinstance(v, float) and (v != v):  # NaN check
            return default
        return v
    except Exception:
        return default


def _round(v, n=2):
    return round(v, n) if isinstance(v, (int, float)) and v == v else None


def _count_fillers(text: str) -> int:
    t = " " + text.lower() + " "
    total = 0
    for f in _FILLERS:
        total += len(re.findall(r"(?<![a-z])" + re.escape(f) + r"(?![a-z])", t))
    return total


def _extract_features(wav_path: str, candidate_text: str) -> dict[str, Any] | None:
    """
    Extract objective acoustic + timing features from the candidate WAV.
    Returns a plain dict of numbers (JSON-safe) or None if the audio is unusable.
    """
    import parselmouth
    from parselmouth.praat import call
    import librosa
    import numpy as np

    try:
        snd = parselmouth.Sound(wav_path)
    except Exception as e:
        logger.warning(f"[voice] could not load WAV {wav_path}: {e}")
        return None

    duration = _safe(lambda: float(snd.get_total_duration()), 0.0) or 0.0
    if duration < 2.0:
        logger.info(f"[voice] recording too short ({duration:.1f}s) — skipping analysis")
        return None

    # ── Pitch (F0): monotone vs expressive ────────────────────────────────────
    pitch = _safe(lambda: snd.to_pitch())
    f0_mean = _safe(lambda: call(pitch, "Get mean", 0, 0, "Hertz")) if pitch else None
    f0_std  = _safe(lambda: call(pitch, "Get standard deviation", 0, 0, "Hertz")) if pitch else None

    # ── Intensity / loudness ──────────────────────────────────────────────────
    intensity = _safe(lambda: snd.to_intensity())
    int_mean = _safe(lambda: call(intensity, "Get mean", 0, 0, "energy")) if intensity else None

    # ── Voice quality: jitter / shimmer / HNR ─────────────────────────────────
    pp = _safe(lambda: call(snd, "To PointProcess (periodic, cc)", 75, 500))
    jitter = _safe(lambda: call(pp, "Get jitter (local)", 0, 0, 0.0001, 0.02, 1.3)) if pp else None
    shimmer = _safe(
        lambda: call([snd, pp], "Get shimmer (local)", 0, 0, 0.0001, 0.02, 1.3, 1.6)
    ) if pp else None
    harmonicity = _safe(lambda: snd.to_harmonicity())
    hnr = _safe(lambda: call(harmonicity, "Get mean", 0, 0)) if harmonicity else None

    # ── Speech-only timing (librosa) ──────────────────────────────────────────
    # CRITICAL: the candidate's mic track is SILENT while the AI (Sarah) talks,
    # so most of the recording is the candidate LISTENING, not hesitating. We
    # therefore split out the candidate's actual speech segments and score only
    # WITHIN-TURN pauses (< 3s) as hesitation — the long gaps (≥ 3s) are
    # turn-taking / listening to the AI and are EXCLUDED, so listening time is
    # never mistaken for hesitation.
    speech_sec = None
    within_pause_ratio = None   # short pauses inside speaking ÷ speaking time
    mean_pause_sec = None       # avg within-turn pause length
    energy_mean = None
    energy_std = None
    pace_wpm = None
    _LISTEN_GAP = 3.0           # gaps ≥ this are turn-taking/listening, not hesitation
    try:
        y, sr = librosa.load(wav_path, sr=None, mono=True)
        if y is not None and len(y) > 0 and sr:
            raw = librosa.effects.split(y, top_db=30)
            # Merge segments separated by < 0.3s — those are normal between-word/
            # syllable gaps, NOT hesitation. Without this, librosa fragments each
            # utterance into dozens of pieces and every micro-gap inflates the
            # pause ratio. After merging, remaining gaps are meaningful pauses.
            _MERGE = 0.3
            intervals = []
            for s, e in raw:
                if intervals and (s - intervals[-1][1]) / float(sr) < _MERGE:
                    intervals[-1] = (intervals[-1][0], e)
                else:
                    intervals.append((int(s), int(e)))
            if len(intervals):
                speech_samples = int(np.sum([e - s for s, e in intervals]))
                speech_sec = speech_samples / float(sr)
                # Energy on speech-only audio (excludes listening silence).
                speech_only = np.concatenate([y[s:e] for s, e in intervals])
                rms = librosa.feature.rms(y=speech_only)
                if rms is not None and rms.size:
                    energy_mean = float(np.mean(rms))
                    energy_std = float(np.std(rms))
                # Gaps BETWEEN speech segments → split hesitation vs. listening.
                gaps = [
                    (intervals[i][0] - intervals[i - 1][1]) / float(sr)
                    for i in range(1, len(intervals))
                ]
                short_gaps = [g for g in gaps if 0 < g < _LISTEN_GAP]
                if short_gaps:
                    mean_pause_sec = float(np.mean(short_gaps))
                    within_pause_ratio = float(
                        sum(short_gaps) / (speech_sec + sum(short_gaps))
                    )
                else:
                    mean_pause_sec = 0.0
                    within_pause_ratio = 0.0
    except Exception as e:
        logger.debug(f"[voice] librosa features failed: {e}")

    # ── Transcript-derived timing ─────────────────────────────────────────────
    words = len(candidate_text.split())
    fillers = _count_fillers(candidate_text)
    if speech_sec and speech_sec > 1 and words > 0:
        pace_wpm = round(words / (speech_sec / 60.0))
    filler_per_100 = round(fillers / words * 100, 1) if words else None

    features = {
        "duration_sec":       _round(duration, 1),        # full call (incl. listening to AI)
        "speech_sec":         _round(speech_sec, 1),       # candidate's ACTUAL talking time
        "within_pause_ratio": _round(within_pause_ratio, 2),  # hesitation pauses ÷ speaking (listening EXCLUDED)
        "mean_pause_sec":     _round(mean_pause_sec, 2),   # avg within-turn pause length
        "pitch_mean_hz":     _round(f0_mean, 1),
        "pitch_std_hz":      _round(f0_std, 1),      # low = monotone, high = expressive
        "intensity_energy":  _round(int_mean, 4),
        "energy_rms_mean":   _round(energy_mean, 4),
        "energy_rms_std":    _round(energy_std, 4),  # loudness dynamics
        "jitter_local":      _round(jitter, 4),      # pitch instability (lower better)
        "shimmer_local":     _round(shimmer, 4),     # loudness instability (lower better)
        "hnr_db":            _round(hnr, 2),         # clarity (higher better)
        "word_count":        words,
        "pace_wpm":          pace_wpm,               # ~120-160 = natural conversational
        "filler_count":      fillers,
        "filler_per_100w":   filler_per_100,
    }
    return features


# ════════════════════════════════════════════════════════════════════════════════
# 2. Claude synthesis — turn numbers into scores + a recruiter narrative
# ════════════════════════════════════════════════════════════════════════════════

_SYNTH_SYSTEM = """\
You are a speech & communication analyst. You are given OBJECTIVE acoustic
measurements of a candidate's voice from a screening interview, plus the role
they are applying for. Interpret the numbers into a delivery assessment.

Return ONE valid JSON object, no markdown, no extra text:
{
  "communication_voice": <integer 1-10>,   // clarity, pace, fluency FROM VOICE
  "confidence_voice":    <integer 1-10>,   // steadiness, hesitation, energy
  "delivery_score":      <integer 1-10>,   // overall vocal delivery
  "narrative":           "<2-4 sentence recruiter-facing summary of how they sound>",
  "strengths":           ["<short point>", ...],
  "concerns":            ["<short point>", ...]
}

IMPORTANT — the metrics already EXCLUDE listening time. This is a two-way voice
interview: the candidate is silent while the AI interviewer speaks. All metrics
below are computed over the candidate's OWN speech only. `speech_sec` is their
actual talking time; `duration_sec` (which includes listening) is context only —
do NOT treat the difference between them as hesitation or silence.

Interpretation guide (guidelines, use judgement — some values may be null):
- pace_wpm ~120-160 is natural; <100 slow, >180 rushed. (Computed over speech only.)
- pitch_std_hz low (<20) = monotone/flat; higher = expressive, engaging.
- within_pause_ratio = fraction of SPEAKING time spent in short within-turn pauses;
  >0.30 suggests genuine hesitation. mean_pause_sec is the average such pause.
  These EXCLUDE turn-taking gaps, so they reflect real hesitation, not listening.
- filler_per_100w high (>4) suggests disfluency.
- hnr_db higher = clearer voice; jitter/shimmer lower = steadier voice.
- energy_rms_std higher = more dynamic/animated delivery.
Weight EXPRESSIVENESS and CLARITY more for client-facing/sales/support roles.
Be fair: a calm, measured speaker is not automatically worse than an animated one,
and short answers are not automatically a negative.
Return ONLY the JSON."""


def _parse_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group())
        raise


async def _synthesize(features: dict, role_category: str | None) -> tuple[dict, dict]:
    """Call Claude to interpret the features. Returns (synthesis, token_usage)."""
    import anthropic

    role = role_category or "general"
    user = (
        f"ROLE CATEGORY: {role}\n\n"
        f"ACOUSTIC MEASUREMENTS (null = not measurable):\n"
        f"{json.dumps(features, indent=2)}\n\n"
        "Assess the candidate's vocal delivery for this role."
    )
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = await client.messages.create(
        model=EVAL_MODEL,
        max_tokens=700,
        system=_SYNTH_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    raw = resp.content[0].text
    usage = {
        "voice_in":  getattr(resp.usage, "input_tokens", 0),
        "voice_out": getattr(resp.usage, "output_tokens", 0),
    }
    return _parse_json(raw), usage


# ════════════════════════════════════════════════════════════════════════════════
# 3. Public entry point
# ════════════════════════════════════════════════════════════════════════════════

async def run_voice_analysis(
    interview_id: str,
    wav_path: str | None,
    candidate_text: str,
    role_category: str | None = None,
    tenant_id: str | None = None,
) -> dict | None:
    """
    Analyze the candidate's recorded voice. Safe to call fire-and-forget style.

    Returns a dict:
      {
        "features": {...raw metrics...},
        "communication_voice": int, "confidence_voice": int, "delivery_score": int,
        "narrative": str, "strengths": [...], "concerns": [...]
      }
    or None if there is no usable audio (analysis simply skipped).
    """
    import asyncio

    if not wav_path or not os.path.exists(wav_path):
        logger.info(f"[voice] no recording for {interview_id} — skipping voice analysis")
        return None

    try:
        features = await asyncio.to_thread(_extract_features, wav_path, candidate_text)
    except Exception as e:
        logger.warning(f"[voice] feature extraction failed for {interview_id}: {e}")
        return None
    if not features:
        return None

    logger.info(
        f"[voice] {interview_id} features: pace={features.get('pace_wpm')}wpm "
        f"pitch_var={features.get('pitch_std_hz')}Hz "
        f"hesitation={features.get('within_pause_ratio')} "
        f"fillers/100w={features.get('filler_per_100w')}"
    )

    try:
        synthesis, usage = await _synthesize(features, role_category)
    except Exception as e:
        logger.warning(f"[voice] Claude synthesis failed for {interview_id}: {e}")
        # Still return the raw features — the report can show numbers without a narrative.
        return {"features": features, "narrative": None,
                "communication_voice": None, "confidence_voice": None,
                "delivery_score": None, "strengths": [], "concerns": []}

    # Record token usage for per-interview cost tracking (non-fatal).
    try:
        from app.services import cost_tracker
        await cost_tracker.patch_usage(interview_id, tenant_id, usage)
    except Exception:
        pass

    result = {"features": features}
    result.update(synthesis)
    logger.info(
        f"[voice] ✓ {interview_id} delivery={synthesis.get('delivery_score')} "
        f"comm_voice={synthesis.get('communication_voice')} "
        f"conf_voice={synthesis.get('confidence_voice')}"
    )
    return result
