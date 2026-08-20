"""
Async Stage Verifier (Step 7 of the stage-skip fix)
====================================================
A background "auditor" that double-checks each stage AFTER the conversation
has already moved on — LLM-grade judgment with ZERO latency in the voice loop.

How it fits the two-layer design:
  • The heuristic gate in interview_graph.py decides FAST (per turn, local).
  • This verifier corrects any heuristic mistake a couple of seconds BEHIND:
    when a stage is marked "captured", a tiny Haiku call checks whether the
    candidate actually provided the information. If not, the stage is
    REOPENED (outcome removed + capture flag cleared) via a callback, and the
    graph's existing loop-back machinery (_next_pending_stage + the wrap-up
    completeness gate) makes Sarah circle back: "one earlier detail is still
    missing…" — no new re-ask mechanism needed.

Guarantees:
  • Never in the request path — fire-and-forget task per closed stage.
  • Each stage is verified at most ONCE (no reopen loops).
  • Only "captured" outcomes are audited — "not_disclosed" was already an
    explicit, acknowledged refusal.
  • Any failure (timeout, bad JSON, API error) is a silent no-op: the system
    degrades to exactly the heuristic behaviour.
  • Verdicts landing after the interview completes are discarded.

Cost: one small Haiku call per stage (~7 per interview) ≈ tenths of a cent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

VERIFIER_MODEL      = "claude-haiku-4-5-20251001"
VERIFIER_TIMEOUT_S  = 12.0
VERIFIER_MAX_TOKENS = 120

# What each stage was supposed to collect — the auditor's yardstick.
STAGE_GOALS: dict[str, str] = {
    "intro":         "a brief self-introduction: current role, company, or area of work",
    "experience":    "their total years of professional experience (a number, or 'fresher')",
    "current_ctc":   "their current CTC / annual salary (a figure or range)",
    "expected_ctc":  "their expected salary for the new role (a figure or range)",
    "notice_period": "their notice period duration (and ideally whether it is negotiable)",
    "relocation":    "whether they are open to relocating (a yes/no or a preference)",
    "joining":       "their earliest possible joining date or timeframe",
}

_SYSTEM_PROMPT = (
    "You audit an HR screening interview. Given what information a stage was "
    "supposed to collect and the candidate's actual replies during that stage, "
    "judge whether the candidate genuinely provided the information. "
    "A counter-question, an acknowledgment, or talk about a different topic "
    "does NOT count as providing it. Reply with ONLY a JSON object: "
    '{"answered": true|false, "value": "<the provided value, or null>"}'
)

# Strong refs to in-flight tasks — an unreferenced asyncio task can be GC'd.
_PENDING: set[asyncio.Task] = set()


def _parse_verdict(raw: str) -> bool | None:
    """Extract {"answered": bool} from the model reply. None = unparseable."""
    try:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        data = json.loads(m.group(0))
        answered = data.get("answered")
        return bool(answered) if isinstance(answered, bool) else None
    except Exception:
        return None


async def _verify(stage: str, texts: list[str]) -> bool | None:
    """
    Ask Haiku whether the stage was genuinely answered.
    Returns True / False, or None when verification wasn't possible
    (missing key, timeout, API error, bad JSON) — callers treat None as
    "leave everything as it is".
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    goal = STAGE_GOALS.get(stage)
    if not api_key or not goal or not texts:
        return None

    replies = "\n".join(f"- {t}" for t in texts[-6:])  # last 6 turns is plenty
    user_msg = (
        f"Stage goal — the interviewer needed: {goal}\n"
        f"Candidate's replies during this stage:\n{replies}\n\n"
        "Did the candidate genuinely provide the requested information?"
    )

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
        resp = await asyncio.wait_for(
            client.messages.create(
                model=VERIFIER_MODEL,
                max_tokens=VERIFIER_MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            ),
            timeout=VERIFIER_TIMEOUT_S,
        )
        raw = "".join(
            getattr(block, "text", "") for block in (resp.content or [])
        )
        return _parse_verdict(raw)
    except Exception as e:
        logger.warning(f"[verifier] {stage}: LLM check failed (no-op): {e}")
        return None


def queue_verify(
    interview_id: str | None,
    stage: str,
    texts: list[str],
    on_not_answered: Callable[[str], None],
    *,
    _verify_fn: Callable[[str, list[str]], Awaitable[bool | None]] | None = None,
) -> None:
    """
    Fire-and-forget audit of a just-closed stage.

    on_not_answered(stage) is called ONLY when the LLM confidently says the
    stage was not answered — the agent uses it to request a reopen, which the
    graph applies at the start of the next turn (race-free by design: state
    is never mutated from this background task's await context).

    _verify_fn is a test seam — production always uses the Haiku call.
    """
    if stage not in STAGE_GOALS:
        return

    verify = _verify_fn or _verify

    async def _run() -> None:
        verdict = await verify(stage, texts)
        if verdict is False:
            logger.warning(
                f"[verifier] stage '{stage}' marked captured but NOT actually "
                "answered — requesting reopen (Sarah will circle back)",
                extra={"interview_id": interview_id},
            )
            try:
                on_not_answered(stage)
            except Exception as e:
                logger.error(f"[verifier] reopen callback failed: {e}")
        elif verdict is True:
            logger.info(
                f"[verifier] stage '{stage}' confirmed answered ✓",
                extra={"interview_id": interview_id},
            )
        # None → couldn't verify → leave everything as it is

    task = asyncio.create_task(_run(), name=f"stage-verify-{stage}")
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
