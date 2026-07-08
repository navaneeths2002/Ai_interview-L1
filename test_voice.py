"""
Throwaway manual test for the voice-analysis engine.
Runs the full feature extraction + Claude synthesis on a saved recording,
independent of the interview gate. Delete this file when done.

Run from the project root:
    venv\Scripts\python test_voice.py
"""
import asyncio
import json
import pathlib

from dotenv import load_dotenv

# Load .env so ANTHROPIC_API_KEY (and DATABASE_URL) are available.
_ROOT = pathlib.Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env")

from app.services.voice_analysis import run_voice_analysis

# The recording you just captured. If it's missing, fall back to the newest .wav.
WAV = _ROOT / "recordings" / "c2ee3306-5fc3-4115-8f8d-8c8ab2f42481.wav"
if not WAV.exists():
    wavs = sorted((_ROOT / "recordings").glob("*.wav"), key=lambda p: p.stat().st_mtime)
    WAV = wavs[-1] if wavs else WAV
    print(f"[test] using newest recording: {WAV.name}")

TEXT = (
    "Yeah, it's totally okay for me. I'm working as a software developer in IT "
    "in Hyderabad. I'm mainly working for a product that is mainly HRMS, so "
    "building AI and ML features inside that project. And the product."
)


async def main():
    result = await run_voice_analysis(
        interview_id="voice-test",
        wav_path=str(WAV),
        candidate_text=TEXT,
        role_category="technical_ic",
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
