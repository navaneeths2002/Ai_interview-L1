"""
Pricing & cost computation
===========================
Single source of truth for per-interview MARGINAL (usage-based) cost.

Tracks the variable spend of one interview across the 5 metered tools:
Claude (tokens) · ElevenLabs (characters) · Deepgram (audio seconds) ·
LiveKit (participant-minutes) · Simli (avatar minutes).

Fixed infra (EC2, managed DB) is flat monthly and intentionally NOT counted
per-interview — amortize it separately (see scripts/cost_estimate.py).

All rates are ESTIMATES (Jan 2026) — verify against current vendor pricing.
"""

from __future__ import annotations

# ── Rates (USD) — edit as vendor pricing changes ────────────────────────────────
CLAUDE_IN_PER_MTOK   = 1.0       # Haiku 4.5 input,  $ per 1M tokens
CLAUDE_OUT_PER_MTOK  = 5.0       # Haiku 4.5 output, $ per 1M tokens
DEEPGRAM_PER_MIN     = 0.0058    # nova-2 streaming, $ per audio-minute
LIVEKIT_PER_PPMIN    = 0.0025    # LiveKit Cloud,    $ per participant-minute
LIVEKIT_PARTICIPANTS = 3         # agent + candidate + simli-avatar
SIMLI_PER_MIN        = 0.009     # avatar rendering, $ per minute
# ElevenLabs marginal: turbo v2.5 = 0.5 credits/char; Scale tier = $330 / 2M credits.
#   0.5 * (330 / 2_000_000) = 0.0000825 $/char
EL_USD_PER_CHAR      = 0.0000825


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def compute_cost(usage: dict | None) -> dict:
    """
    Convert a merged usage dict → per-tool cost breakdown + total (USD).

    Expected usage keys (any missing → treated as 0):
      llm_in, llm_out            — conversation Claude tokens
      eval_in, eval_out          — evaluation Claude tokens
      strategy_in, strategy_out  — strategy Claude tokens
      tts_chars                  — ElevenLabs characters
      stt_seconds                — Deepgram audio seconds
      duration_seconds           — interview length (LiveKit participant-minutes)
      avatar_seconds             — seconds the Simli avatar was active
    """
    u = usage or {}

    claude_in  = _num(u.get("llm_in"))  + _num(u.get("eval_in"))  + _num(u.get("strategy_in"))
    claude_out = _num(u.get("llm_out")) + _num(u.get("eval_out")) + _num(u.get("strategy_out"))

    claude     = claude_in / 1_000_000 * CLAUDE_IN_PER_MTOK + claude_out / 1_000_000 * CLAUDE_OUT_PER_MTOK
    elevenlabs = _num(u.get("tts_chars")) * EL_USD_PER_CHAR
    deepgram   = _num(u.get("stt_seconds")) / 60 * DEEPGRAM_PER_MIN
    livekit    = _num(u.get("duration_seconds")) / 60 * LIVEKIT_PARTICIPANTS * LIVEKIT_PER_PPMIN
    simli      = _num(u.get("avatar_seconds")) / 60 * SIMLI_PER_MIN

    total = claude + elevenlabs + deepgram + livekit + simli
    return {
        "claude":     round(claude,     4),
        "elevenlabs": round(elevenlabs, 4),
        "deepgram":   round(deepgram,   4),
        "livekit":    round(livekit,    4),
        "simli":      round(simli,      4),
        "total_usd":  round(total,      4),
    }
