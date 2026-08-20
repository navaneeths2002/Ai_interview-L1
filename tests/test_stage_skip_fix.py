"""
Stage-skip fix — regression suite (Steps 1–7).

Covers the exact bug reported: the candidate asks a counter-question (or just
says "okay") and the stage machine wrongly advances/skips — leaving stages
incomplete. Pure-function tests over advance_node + classify_utterance, plus
the async verifier's reopen flow with a stubbed LLM. Runs in milliseconds,
no network, no DB.

Run:  venv\\Scripts\\python -m pytest tests/test_stage_skip_fix.py -q
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.realtime.interview_graph import (  # noqa: E402
    MAX_ATTEMPTS_PER_STAGE,
    MAX_DETOURS_PER_STAGE,
    advance_node,
    classify_utterance,
    make_initial_state,
)
from app.realtime import stage_verifier  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────────

def state_at(stage: str, **over):
    """Fresh state positioned at `stage` with its question already asked."""
    s = dict(make_initial_state("Navaneeth"))
    s["stage"] = stage
    s["asked"] = {**s["asked"], stage: True}
    s.update(over)
    return s


def run(state, text):
    """One graph step: returns the merged post-turn state."""
    merged = {**state, "last_candidate_text": text}
    updates = advance_node(merged)
    return {**merged, **updates}


# ── classify_utterance ─────────────────────────────────────────────────────────

class TestClassify:
    def test_counter_questions(self):
        for q in [
            "What is the salary range for this position?",
            "Is this role remote or hybrid?",
            "Can you tell me more about the company?",
            "how many days a week is the office",
            "will I get work from home option",
            "tell me about the team",
            "one quick question — does the company provide relocation support?",
            "okay but what about the notice period on your side?",
        ]:
            assert classify_utterance(q) == "question", q

    def test_acks(self):
        for a in ["okay", "ok thanks", "got it", "hmm okay", "sounds good", "great"]:
            assert classify_utterance(a) == "ack", a

    def test_real_answers_not_questions(self):
        for ans in [
            "My current CTC is 12 LPA",
            "Should be around two months",           # elliptical answer
            "Would be 15 LPA expected",              # elliptical answer
            "I have five years of experience",
            "Yes, I am open to relocating anywhere",
            "Two months notice, buyout is possible",
            "I can join within two weeks",
        ]:
            assert classify_utterance(ans) == "answer", ans

    def test_bare_yes_is_not_ack_swallowed(self):
        # "yes" classifies as ack (short affirmation) — the relocation gate in
        # advance_node decides whether it counts as the answer.
        assert classify_utterance("yes") in ("ack", "answer")


# ── the reported bug: counter-questions must NOT capture/skip ─────────────────

class TestCounterQuestionsDontSkip:
    def test_salary_question_does_not_capture_ctc(self):
        s = run(state_at("current_ctc"), "Is the salary for this role above 10 LPA?")
        assert s["stage"] == "current_ctc"
        assert "current_ctc" not in s["stage_outcomes"]
        assert s["detours_in_stage"] == 1
        assert s["turns_in_stage"] == 0          # no attempt burned

    def test_remote_question_does_not_capture_relocation(self):
        s = run(state_at("relocation"), "Is this role remote or hybrid?")
        assert s["stage"] == "relocation"
        assert "relocation" not in s["stage_outcomes"]

    def test_office_days_question_does_not_capture_notice(self):
        s = run(state_at("notice_period"), "How many days a week is the office?")
        assert s["stage"] == "notice_period"
        assert "notice_period" not in s["stage_outcomes"]

    def test_project_start_question_does_not_capture_joining(self):
        s = run(state_at("joining"), "When would the project start?")
        assert s["stage"] == "joining"
        assert "joining" not in s["stage_outcomes"]

    def test_role_question_does_not_capture_intro(self):
        s = run(state_at("intro"), "Can you tell me more about this role and the company please?")
        assert s["stage"] == "intro"
        assert "intro" not in s["stage_outcomes"]

    def test_detour_instruction_tells_sarah_to_answer_then_reask(self):
        s = run(state_at("current_ctc"), "What does the role involve day to day?")
        instr = s["stage_instruction"].lower()
        assert "re-ask" in instr or "pending question" in instr


class TestAcksDontSkip:
    def test_okay_after_detour_does_not_capture_relocation(self):
        s = state_at("relocation", detoured_since_ask=True)
        s = run(s, "okay")
        assert s["stage"] == "relocation"
        assert "relocation" not in s["stage_outcomes"]

    def test_okay_never_captures_notice(self):
        s = run(state_at("notice_period"), "okay")
        assert s["stage"] == "notice_period"
        assert "notice_period" not in s["stage_outcomes"]


# ── real answers still advance ────────────────────────────────────────────────

class TestRealAnswersAdvance:
    def test_ctc_answer_advances_with_evidence(self):
        s = run(state_at("current_ctc"), "My current CTC is 12 LPA")
        assert s["stage_outcomes"].get("current_ctc") == "captured"
        assert s["stage"] != "current_ctc"
        assert "12 LPA" in s["stage_evidence"]["current_ctc"]

    def test_experience_answer_advances(self):
        s = run(state_at("experience"), "I have 5 years of experience in backend development")
        assert s["stage_outcomes"].get("experience") == "captured"

    def test_notice_answer_advances(self):
        s = run(state_at("notice_period"), "Two months notice, buyout is possible")
        assert s["stage_outcomes"].get("notice_period") == "captured"

    def test_joining_answer_advances(self):
        s = run(state_at("joining"), "I can join within two weeks of the offer")
        assert s["stage_outcomes"].get("joining") == "captured"

    def test_elliptical_notice_answer_advances(self):
        s = run(state_at("notice_period"), "Should be around 2 months, negotiable")
        assert s["stage_outcomes"].get("notice_period") == "captured"

    def test_bare_yes_right_after_relocation_question_is_the_answer(self):
        s = run(state_at("relocation"), "yes")
        assert s["stage_outcomes"].get("relocation") == "captured"

    def test_full_relocation_answer_advances(self):
        s = run(state_at("relocation"), "Yes, I am open to relocating anywhere in India")
        assert s["stage_outcomes"].get("relocation") == "captured"


# ── detour budget: no infinite stall ──────────────────────────────────────────

class TestDetourBudget:
    def test_question_spam_converges_to_not_disclosed(self):
        s = state_at("current_ctc")
        for i in range(MAX_DETOURS_PER_STAGE + MAX_ATTEMPTS_PER_STAGE + 2):
            s = run(s, f"And question number {i}: what about the benefits?")
            if s["stage"] != "current_ctc":
                break
        assert s["stage"] != "current_ctc"
        assert s["stage_outcomes"].get("current_ctc") == "not_disclosed"

    def test_detours_within_budget_burn_no_attempts(self):
        s = state_at("expected_ctc")
        for q in ["What is the team size?", "Is there wfh?", "okay"]:
            s = run(s, q)
        assert s["stage"] == "expected_ctc"
        assert s["turns_in_stage"] == 0
        assert s["detours_in_stage"] == 3
        # A real answer afterwards still captures normally
        s = run(s, "I am expecting around 15 LPA")
        assert s["stage_outcomes"].get("expected_ctc") == "captured"


# ── non-answers still burn attempts → explicit not_disclosed ──────────────────

class TestUnansweredStillConverges:
    def test_evasive_answers_reach_not_disclosed(self):
        s = state_at("current_ctc")
        for _ in range(MAX_ATTEMPTS_PER_STAGE):
            s = run(s, "I would prefer discussing that with HR later in person")
        assert s["stage_outcomes"].get("current_ctc") == "not_disclosed"
        assert s["stage"] != "current_ctc"


# ── wrap-up completeness gate + verifier reopen loop-back ─────────────────────

class TestWrapupGateAndReopen:
    def _all_outcomes(self):
        return {st: "captured" for st in
                ["intro", "experience", "current_ctc", "expected_ctc",
                 "notice_period", "relocation", "joining"]}

    def test_wrapup_loops_back_to_missing_stage(self):
        outcomes = self._all_outcomes()
        outcomes.pop("notice_period")            # simulating a verifier reopen
        s = state_at("wrap_up", stage_outcomes=outcomes)
        s = run(s, "thanks, that's all from my side")
        assert s["stage"] == "notice_period"     # circled back, not complete

    def test_wrapup_completes_when_all_recorded(self):
        s = state_at("wrap_up", stage_outcomes=self._all_outcomes())
        s = run(s, "thank you, bye")
        assert s["stage"] == "complete"

    def test_reopened_stage_is_next_pending_after_capture(self):
        outcomes = self._all_outcomes()
        outcomes.pop("current_ctc")              # reopened earlier stage
        s = state_at("joining", stage_outcomes=outcomes,
                     captured_joining=False)
        s = run(s, "I can join in two weeks")
        assert s["stage"] == "current_ctc"       # loop-back to the hole


# ── step 7: verifier reopen flow (stubbed LLM, no network) ────────────────────

class TestVerifier:
    def _drive(self, verdict, stage="current_ctc"):
        reopened: list[str] = []

        async def fake_verify(st, texts):
            return verdict

        async def main():
            stage_verifier.queue_verify(
                "test-interview", stage, ["what about benefits?"],
                reopened.append, _verify_fn=fake_verify,
            )
            await asyncio.sleep(0.05)            # let the task run
        asyncio.run(main())
        return reopened

    def test_not_answered_triggers_reopen(self):
        assert self._drive(False) == ["current_ctc"]

    def test_answered_no_reopen(self):
        assert self._drive(True) == []

    def test_unverifiable_no_reopen(self):
        assert self._drive(None) == []

    def test_unknown_stage_ignored(self):
        assert self._drive(False, stage="wrap_up") == []

    def test_verdict_parser(self):
        p = stage_verifier._parse_verdict
        assert p('{"answered": true, "value": "12 LPA"}') is True
        assert p('Sure! {"answered": false, "value": null}') is False
        assert p("garbage") is None
        assert p('{"answered": "maybe"}') is None
