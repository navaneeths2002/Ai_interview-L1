"""
LangGraph state machine for the L1 HR screening interview.

The graph tracks WHAT stage the interview is in and generates a focused
instruction for the LLM at every turn.  It does NOT call the LLM itself —
livekit-agents handles the full STT → LLM → TTS pipeline.  The graph is
purely a state controller.

Flow
----
intro → experience → current_ctc → expected_ctc → notice_period
      → relocation → joining → wrap_up → complete (END)

Each stage:
  1. Checks the candidate's last response for expected data (heuristic detectors)
  2. Either stays in the stage (still trying to extract info) or advances
  3. Emits a `stage_instruction` — injected into the LLM's system prompt so
     it knows what to focus on this specific turn
"""

from __future__ import annotations

import logging
import re
from typing import TypedDict

from langgraph.graph import StateGraph, END

logger = logging.getLogger(__name__)


# ── Stage ordering ──────────────────────────────────────────────────────────────

STAGE_ORDER = [
    "intro",
    "experience",
    "current_ctc",
    "expected_ctc",
    "notice_period",
    "relocation",
    "joining",
    "wrap_up",
]

NEXT_STAGE: dict[str, str] = {
    s: STAGE_ORDER[i + 1] for i, s in enumerate(STAGE_ORDER[:-1])
}
NEXT_STAGE["wrap_up"] = "complete"

# ── Attempt policy (FIX B/RC3 — no silent skipping) ─────────────────────────────
# A stage NEVER force-advances with nothing captured. Instead:
#   attempt 1  — normal question
#   attempt 2  — ESCALATE: Sarah re-asks more directly (standard-process framing)
#   attempt 3+ — mark the stage outcome "not_disclosed" (explicit, recorded) and
#                move on with a polite acknowledgment. Never a silent skip.
MAX_ATTEMPTS_PER_STAGE = 3
ESCALATE_AT            = 2

# Backwards-compat alias (older code/tests referenced this name)
MAX_TURNS_PER_STAGE = MAX_ATTEMPTS_PER_STAGE


# ── State ───────────────────────────────────────────────────────────────────────

class InterviewState(TypedDict):
    # Current stage in the state machine
    stage: str
    # How many LLM turns have happened in the current stage
    turns_in_stage: int
    # The instruction injected into the system prompt for this turn
    stage_instruction: str
    # The most recent thing the candidate said (used by each node for extraction)
    last_candidate_text: str

    # Loaded from DB at interview start
    candidate_name: str
    skills_to_probe: list[str]   # skills ATS flagged as strong
    gaps_to_probe: list[str]     # skills ATS flagged as missing

    # Per-stage capture flags — True once we've detected the answer
    captured_intro: bool
    captured_experience: bool
    captured_current_ctc: bool
    captured_expected_ctc: bool
    captured_notice_period: bool
    captured_relocation: bool
    captured_joining: bool

    # FIX B — closed-loop tracking.
    # asked[stage] is set the moment the graph emits that stage's question
    # instruction (Fix A guarantees the LLM actually receives it). A stage's
    # detector only counts an answer if its question was actually asked.
    asked: dict
    # FIX D — explicit outcome per stage: "captured" | "not_disclosed".
    # wrap_up cannot complete while any stage lacks an outcome (loop-back gate).
    stage_outcomes: dict


# ── Heuristic detectors ─────────────────────────────────────────────────────────
# Fast, synchronous — zero latency impact on the voice pipeline.
# These don't need to be perfect; they just decide when to move on.

# FIX B (RC2): all keyword matching is WHOLE-WORD regex. The old substring
# matching caused real misfires — e.g. `"no" in "i know"` → True advanced the
# relocation stage on a completely unrelated sentence. Cross-stage misfires
# (a notice answer advancing `joining`) are additionally prevented by the
# asked-flag contract in advance_node: a detector only counts when ITS stage's
# question was actually asked.

_NUM_WORD = r"(?:\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten)"


def _detect_intro(text: str) -> bool:
    """Candidate said at least a few words about themselves."""
    return len(text.split()) >= 8


def _detect_experience(text: str) -> bool:
    t = text.lower()
    if re.search(rf"\b{_NUM_WORD}\s*\+?\s*(?:years?|yrs?)\b", t):
        return True
    return bool(re.search(
        r"\b(?:fresher|fresh\s+graduate|no\s+experience|years?\s+(?:of\s+)?experience"
        r"|i\s+have\s+been|worked\s+(?:for|at|with))\b", t))


def _detect_salary(text: str) -> bool:
    """A number WITH salary context — applies to both current and expected CTC."""
    t = text.lower()
    has_number = bool(re.search(r"\d", t)) or bool(re.search(rf"\b{_NUM_WORD}\b", t))
    has_salary_word = bool(re.search(
        r"\b(?:lpa|lakhs?|lacs?|ctc|salary|package|per\s+annum|per\s+month"
        r"|annum|thousand|crores?|k)\b", t))
    return has_number and has_salary_word


def _detect_notice(text: str) -> bool:
    t = text.lower()
    return bool(re.search(
        r"\b(?:notice|months?|weeks?|days?|immediate(?:ly)?|serving|relieving"
        r"|buy\s?out|negotiable)\b", t))


def _detect_relocation(text: str) -> bool:
    t = text.lower()
    return bool(re.search(
        r"\b(?:yes|yeah|yep|no|nope|sure|okay|ok|fine|relocate|relocating"
        r"|relocation|move|moving|willing|open|prefer|preferred|comfortable"
        r"|anywhere|remote|hybrid|onsite|on-site)\b", t))


def _detect_joining(text: str) -> bool:
    t = text.lower()
    return bool(re.search(
        r"\b(?:join|joining|start|available|availability|immediate(?:ly)?"
        r"|months?|weeks?|days?|after|notice|date|asap|soon)\b", t))


# Maps each stage to its detector function
STAGE_DETECTORS: dict[str, callable] = {
    "intro": _detect_intro,
    "experience": _detect_experience,
    "current_ctc": _detect_salary,
    "expected_ctc": _detect_salary,
    "notice_period": _detect_notice,
    "relocation": _detect_relocation,
    "joining": _detect_joining,
    "wrap_up": lambda _: True,      # always advance after 1 wrap-up turn
}

# Maps each stage to the InterviewState flag key for it
CAPTURE_FLAG: dict[str, str | None] = {
    "intro": "captured_intro",
    "experience": "captured_experience",
    "current_ctc": "captured_current_ctc",
    "expected_ctc": "captured_expected_ctc",
    "notice_period": "captured_notice_period",
    "relocation": "captured_relocation",
    "joining": "captured_joining",
    "wrap_up": None,
}


# ── Stage instructions ──────────────────────────────────────────────────────────

def _make_instruction(stage: str, state: InterviewState) -> str:
    """
    Returns a focused instruction string for the given stage.
    This is appended to the base system prompt so the LLM knows exactly
    what to gather in this specific turn.
    """
    name = state.get("candidate_name") or "the candidate"
    gaps = state.get("gaps_to_probe") or []
    gap_note = (
        f" The ATS flagged these as skill gaps in their resume: {', '.join(gaps[:3])}."
        if gaps else ""
    )

    instructions: dict[str, str] = {
        "intro": (
            f"Warmly welcome {name} and ask for a brief self-introduction — "
            "their current role, company, and key area of work. "
            "One friendly question only."
        ),
        "experience": (
            "Ask about their total years of professional experience. "
            "If they already mentioned it in their intro, acknowledge it and confirm the number. "
            "Move on once you have it."
        ),
        "current_ctc": (
            "Ask about their current CTC (annual cost to company / salary). "
            "Be professional and matter-of-fact — this is a standard screening question. "
            "One question only."
        ),
        "expected_ctc": (
            "Ask what salary they are expecting for this new role. "
            "Accept a range or a specific number. If very vague, ask for a rough figure. "
            "One question only."
        ),
        "notice_period": (
            "Ask about their notice period at their current company, "
            "and whether it can be negotiated or bought out. "
            "Both pieces of information are important."
        ),
        "relocation": (
            "Ask whether they are open to relocating if the role requires it. "
            "Note any city or work-mode preferences they mention."
            + gap_note
        ),
        "joining": (
            "Ask about the earliest date they could join if selected. "
            "This helps with onboarding planning."
        ),
        "wrap_up": (
            "You have gathered all the information needed. "
            "Thank the candidate warmly for their time. "
            "Let them know the team will review their profile and get back to them soon. "
            "Close the interview professionally — do NOT ask any more questions."
        ),
        "complete": "",
    }

    return instructions.get(stage, "")


# ── Core node ────────────────────────────────────────────────────────────────────

def _pending_stages(outcomes: dict) -> list[str]:
    """Stages (excluding wrap_up) that still have no recorded outcome."""
    return [s for s in STAGE_ORDER[:-1] if s not in outcomes]


def _next_pending_stage(current: str, outcomes: dict) -> str:
    """
    First stage in FULL interview order that still lacks an outcome (excluding
    the current stage, whose outcome was just recorded). Scanning from the start
    — not merely forward — matters: after a loop-back or a resume with holes, an
    EARLIER stage may be the missing one. In normal forward flow every earlier
    stage already has an outcome, so this degrades to "the immediate next stage".
    Falls through to wrap_up when everything has an outcome.
    """
    for s in STAGE_ORDER[:-1]:
        if s != current and s not in outcomes:
            return s
    return "wrap_up"


def advance_node(state: InterviewState) -> dict:
    """
    The single node in the graph (FIX B + D — closed-loop, no silent skips).

    Per candidate turn:
      1. The stage's detector counts ONLY if the stage's question was actually
         asked (asked-flag contract).
      2. Answered → outcome "captured", advance to the next stage WITHOUT an
         outcome (skips already-answered stages after a loop-back).
      3. Unanswered → re-ask; at ESCALATE_AT the instruction escalates; at
         MAX_ATTEMPTS_PER_STAGE the outcome becomes "not_disclosed" (explicit,
         acknowledged out loud) and the interview moves on. Never a silent skip.
      4. wrap_up cannot complete while any stage lacks an outcome — Sarah loops
         back to collect the missing ones first (FIX D gate).
    Every transition is logged: [graph] a -> b (reason=...).
    """
    stage = state.get("stage", "intro")
    text  = (state.get("last_candidate_text") or "").strip()
    turns = state.get("turns_in_stage", 0)
    asked    = dict(state.get("asked") or {})
    outcomes = dict(state.get("stage_outcomes") or {})

    if stage == "complete":
        return {"stage_instruction": ""}

    updates: dict = {}

    def _emit(target: str, reason: str, prefix: str = "") -> dict:
        """Advance to `target`, mark its question as asked, log the move."""
        updates["stage"] = target
        updates["turns_in_stage"] = 0
        asked[target] = True                     # its question goes out this turn
        instr = _make_instruction(target, dict(state))
        updates["stage_instruction"] = (prefix + instr) if instr else prefix.strip()
        updates["asked"] = asked
        updates["stage_outcomes"] = outcomes
        logger.info(f"[graph] {stage} -> {target} (reason={reason})")
        return updates

    def _stay(reason: str, escalate: bool) -> dict:
        updates["turns_in_stage"] = turns + 1
        prefix = (
            "The candidate has not clearly answered this yet. Rephrase and ask "
            "more directly — briefly mention this is standard information needed "
            "to move their application forward. "
        ) if escalate else ""
        updates["stage_instruction"] = prefix + _make_instruction(stage, state)
        updates["asked"] = asked
        updates["stage_outcomes"] = outcomes
        logger.info(f"[graph] {stage} -> {stage} (stay, attempt={turns + 1}, reason={reason})")
        return updates

    # ── wrap_up: completes after one turn, but ONLY if nothing is missing ──────
    if stage == "wrap_up":
        missing = _pending_stages(outcomes)
        if missing:
            target = missing[0]
            return _emit(
                target, "wrapup_loopback",
                prefix=("Before wrapping up, tell the candidate there are still a "
                        "couple of details you need from them. Then: "),
            )
        updates["stage"] = "complete"
        updates["turns_in_stage"] = 0
        updates["stage_instruction"] = ""
        updates["asked"] = asked
        updates["stage_outcomes"] = outcomes
        logger.info("[graph] wrap_up -> complete (reason=all_outcomes_recorded)")
        return updates

    # ── Detect — ONLY counts if this stage's question was actually asked ───────
    detector  = STAGE_DETECTORS.get(stage, lambda _: False)
    was_asked = bool(asked.get(stage))
    captured_this_turn = bool(text) and was_asked and bool(detector(text))

    flag_key = CAPTURE_FLAG.get(stage)
    already_captured = bool(state.get(flag_key)) if flag_key else False
    if flag_key:
        updates[flag_key] = already_captured or captured_this_turn

    if captured_this_turn or already_captured:
        outcomes.setdefault(stage, "captured")
        nxt = _next_pending_stage(stage, outcomes)
        prefix = ""
        if nxt in STAGE_ORDER and stage in STAGE_ORDER \
                and STAGE_ORDER.index(nxt) < STAGE_ORDER.index(stage):
            # Jumping BACK to collect a stage that was missed earlier (loop-back
            # after a resume hole) — have Sarah frame the return naturally.
            prefix = ("One earlier detail is still missing — mention you'd like "
                      "to circle back to it. Then: ")
        return _emit(nxt, "captured", prefix=prefix)

    if was_asked and (turns + 1) >= MAX_ATTEMPTS_PER_STAGE:
        # Explicit, recorded refusal — acknowledged out loud, never silent.
        outcomes[stage] = "not_disclosed"
        nxt = _next_pending_stage(stage, outcomes)
        return _emit(
            nxt, "not_disclosed",
            prefix=("The candidate did not share the previous detail after several "
                    "asks. Politely say you'll note it and move on — do not press "
                    "again. Then: "),
        )

    # Stay and re-ask (escalated wording on the ESCALATE_AT attempt)
    if not was_asked:
        asked[stage] = True                      # question goes out this turn
    return _stay("unanswered" if was_asked else "not_yet_asked",
                 escalate=was_asked and (turns + 1) >= ESCALATE_AT)


# ── Graph factory ────────────────────────────────────────────────────────────────

def build_interview_graph():
    """Compile and return the interview state machine."""
    builder = StateGraph(InterviewState)
    builder.add_node("advance", advance_node)
    builder.set_entry_point("advance")
    builder.add_edge("advance", END)
    return builder.compile()


def make_initial_state(
    candidate_name: str,
    skills_to_probe: list[str] | None = None,
    gaps_to_probe: list[str] | None = None,
) -> InterviewState:
    """Return the starting state for a fresh interview."""
    state = InterviewState(
        stage="intro",
        turns_in_stage=0,
        stage_instruction="",          # will be set on first graph invocation
        last_candidate_text="",
        candidate_name=candidate_name or "the candidate",
        skills_to_probe=skills_to_probe or [],
        gaps_to_probe=gaps_to_probe or [],
        captured_intro=False,
        captured_experience=False,
        captured_current_ctc=False,
        captured_expected_ctc=False,
        captured_notice_period=False,
        captured_relocation=False,
        captured_joining=False,
        # FIX B: the greeting itself asks for the intro, so intro counts as asked
        # from turn zero. All later stages are marked asked when their instruction
        # is emitted by advance_node.
        asked={"intro": True},
        # FIX D: outcome per stage ("captured" | "not_disclosed") — wrap_up cannot
        # complete until every stage has one.
        stage_outcomes={},
    )
    # Seed the first stage instruction
    state["stage_instruction"] = _make_instruction("intro", state)
    return state
