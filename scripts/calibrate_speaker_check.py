#!/usr/bin/env python3
"""
Voice Guard v1 — threshold calibration / audit over existing recordings
=======================================================================
Runs the post-interview speaker check across every WAV in recordings/ and
prints a per-file verdict table. Use it to:

  1. CALIBRATE: all these recordings are believed to be single-speaker — any
     file reported as "2 voices" here is either a real historical fraud or a
     false positive. Investigate by listening to the printed suspicious spans.
     If several genuine files show longest_low_run close to the threshold,
     raise SPEAKER_CHECK_RUN_MIN_SECONDS in .env (default 17).

  2. BACKFILL/AUDIT: see which past interviews would have been flagged.

Run on the server:
    cd /opt/interview-agent
    sudo -u interview aiinterview_env/bin/python3 scripts/calibrate_speaker_check.py

No DB writes, read-only on the WAVs.
"""

import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
logging.basicConfig(level=logging.WARNING)

from app.services.speaker_check import run_speaker_check  # noqa: E402


def main() -> None:
    wavs = sorted(glob.glob(os.path.join("recordings", "*.wav")))
    if not wavs:
        print("No WAV files found in recordings/ — run from the project root.")
        return

    print(f"\nVoice Guard calibration — {len(wavs)} recording(s)\n")
    print(f"{'file':<44} {'voices':>6} {'speech_s':>9} {'longest_low_run':>16}  spans")
    print("-" * 100)
    flagged = 0
    for w in wavs:
        r = run_speaker_check(w)
        name = os.path.basename(w)[:42]
        if not r:
            print(f"{name:<44} {'—':>6} {'—':>9} {'—':>16}  (skipped: too short / unreadable)")
            continue
        spans = "; ".join(f"{s['start']}-{s['end']}" for s in r["suspicious_spans"][:3]) or "-"
        mark = "  << FLAGGED" if r["speakers_detected"] >= 2 else ""
        if r["speakers_detected"] >= 2:
            flagged += 1
        print(f"{name:<44} {r['speakers_detected']:>6} {r['total_speech_seconds']:>9} "
              f"{r['longest_low_run_s']:>16}  {spans}{mark}")

    print("-" * 100)
    print(f"flagged: {flagged}/{len(wavs)}")
    print(
        "\nReading the table:\n"
        "  • Genuine single-speaker files should show voices=1. Their longest_low_run\n"
        "    values tell you the safety margin to the threshold (default 17s) — if any\n"
        "    genuine file is within ~3s of it, raise SPEAKER_CHECK_RUN_MIN_SECONDS.\n"
        "  • A flagged file is worth LISTENING to at the printed spans before concluding\n"
        "    fraud — this check is advisory, not conclusive.\n"
    )


if __name__ == "__main__":
    main()
