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

import re
from typing import TypedDict, Optional

from langgraph.graph import StateGraph, END


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

# After this many turns in a single stage, force-advance regardless of extraction
MAX_TURNS_PER_STAGE = 3


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


# ── Heuristic detectors ─────────────────────────────────────────────────────────
# Fast, synchronous — zero latency impact on the voice pipeline.
# These don't need to be perfect; they just decide when to move on.

def _detect_intro(text: str) -> bool:
    """Candidate said at least a few words about themselves."""
    return len(text.split()) >= 8


def _detect_experience(text: str) -> bool:
    t = text.lower()
    return bool(re.search(r"\d+\s*(year|yr|yrs)", t)) or any(
        w in t for w in ["years of experience", "years experience", "i have been", "worked for"]
    )


def _detect_salary(text: str) -> bool:
    """Detects any salary figure — applies to both current and expected CTC."""
    t = text.lower()
    has_number = bool(re.search(r"\d", t))
    has_salary_word = any(
        w in t for w in [
            "lpa", "lakh", "lac", "ctc", "salary", "package",
            "per annum", "per month", "k ", "thousand", "crore",
        ]
    )
    return has_number and has_salary_word


def _detect_notice(text: str) -> bool:
    t = text.lower()
    return any(
        w in t for w in [
            "notice", "month", "week", "day", "immediately",
            "serving", "relieving", "buyout", "buy out",
        ]
    )


def _detect_relocation(text: str) -> bool:
    t = text.lower()
    # Any clear yes/no or relocation-related word counts
    return any(
        w in t for w in [
            "yes", "no", "yeah", "nope", "sure", "okay", "fine",
            "relocat", "move", "open to", "willing", "prefer",
            "comfortable", "not comfortable", "anywhere",
        ]
    )


def _detect_joining(text: str) -> bool:
    t = text.lower()
    return any(
        w in t for w in [
            "join", "start", "available", "after", "month", "week",
            "immediately", "soon", "notice", "date",
        ]
    )


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

def advance_node(state: InterviewState) -> dict:
    """
    The single node in the graph.

    On every invocation it:
      1. Checks if the candidate's last response answered the current stage
      2. Either advances to the next stage or stays (up to MAX_TURNS_PER_STAGE)
      3. Emits a `stage_instruction` for the LLM to use this turn
    """
    stage = state.get("stage", "intro")
    text = state.get("last_candidate_text", "")
    turns = state.get("turns_in_stage", 0)

    if stage == "complete":
        return {"stage_instruction": ""}

    # --- Detect whether this stage's data was captured ---
    detector = STAGE_DETECTORS.get(stage, lambda _: False)
    captured_this_turn = detector(text) if text else False

    flag_key = CAPTURE_FLAG.get(stage)
    already_captured = bool(state.get(flag_key)) if flag_key else False

    should_advance = captured_this_turn or already_captured or (turns >= MAX_TURNS_PER_STAGE)

    updates: dict = {}

    # Update the capture flag
    if flag_key:
        updates[flag_key] = already_captured or captured_this_turn

    if should_advance:
        # Move to the next stage
        next_stage = NEXT_STAGE.get(stage, "complete")
        updates["stage"] = next_stage
        updates["turns_in_stage"] = 0
        # Generate the instruction for the NEXT stage
        updates["stage_instruction"] = _make_instruction(next_stage, {**state, **updates})
    else:
        # Stay in current stage, increment turn count
        updates["turns_in_stage"] = turns + 1
        updates["stage_instruction"] = _make_instruction(stage, state)

    return updates


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
    )
    # Seed the first stage instruction
    state["stage_instruction"] = _make_instruction("intro", state)
    return state
