"""
Post-interview speaker check (Voice Guard v1 — Option 3A)
=========================================================
Detects whether a SECOND distinct voice spoke during the candidate-side
recording of a finished interview — the classic proxy-interview fraud where an
"expert friend" takes over for the hard questions. Runs AFTER the interview on
the saved WAV; it never touches the live voice pipeline. On detection the
result is stored with the evaluation and surfaced as a RED FLAG in the
recruiter report. Sarah never says anything (product decision: silent v1).

Algorithm (validated on real + synthetic two-voice fixtures)
-----------------------------------------------------------
    recording → energy-VAD voiced windows (~5s each)
             → speaker embedding per window (CAM++ ONNX via sherpa-onnx, CPU)
             → ROBUST primary-voice centroid (iterative trim — tolerates a
               minority impostor contaminating the mean)
             → similarity timeline vs centroid, smoothed (moving avg of 3)
             → an impostor appears as a SUSTAINED CONTIGUOUS RUN of low
               similarity (temporal structure is the discriminator — single
               noisy windows are absorbed by the smoothing + run-length gate)
    verdict "2 voices" ⇔ some low run's net speech ≥ RUN_MIN_SECONDS

Why not clustering: pairwise 2–5s window similarities of the SAME speaker vary
hugely (p10 ≈ 0.07!) — distance-threshold clustering over-splits and k-means
absorbs a minority speaker into dominant intra-speaker variance. Both were
tried and failed on fixtures; the run detector separated cleanly
(genuine max-run 11.9s vs impostor 22.1s at T=0.60/smooth 3).

All failures are non-fatal: missing model/audio/deps → returns None and the
post-interview chain continues.

Config (.env, all optional)
---------------------------
  SPEAKER_CHECK_ENABLED=true
  SPEAKER_CHECK_MODEL=app/models_data/speaker_embedding_en_cam++.onnx
  SPEAKER_CHECK_RUN_THRESHOLD=0.60   # smoothed similarity below this = "low"
  SPEAKER_CHECK_RUN_MIN_SECONDS=17   # net low-run speech to declare 2nd voice
"""

from __future__ import annotations

import logging
import os
import pathlib

import numpy as np

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
_ENABLED    = os.getenv("SPEAKER_CHECK_ENABLED", "true").lower() != "false"
_MODEL_PATH = os.getenv(
    "SPEAKER_CHECK_MODEL",
    str(pathlib.Path(__file__).resolve().parent.parent / "models_data"
        / "speaker_embedding_en_cam++.onnx"),
)
_RUN_THRESHOLD   = float(os.getenv("SPEAKER_CHECK_RUN_THRESHOLD", "0.60"))
_RUN_MIN_SECONDS = float(os.getenv("SPEAKER_CHECK_RUN_MIN_SECONDS", "17"))

_TARGET_SR   = 16000
_SEG_SECONDS = 5.0     # embedding window (5s = stable embeddings; 2s was too noisy)
_MIN_SEG     = 2.5     # discard voiced chunks shorter than this
_FRAME_MS    = 30      # energy-VAD frame
_SMOOTH      = 3       # moving-average width over the similarity timeline

_extractor = None      # lazy singleton — model loads once per process


def _get_extractor():
    global _extractor
    if _extractor is not None:
        return _extractor
    try:
        import sherpa_onnx
        if not os.path.exists(_MODEL_PATH):
            logger.warning(f"[speaker-check] model file missing: {_MODEL_PATH} — check skipped")
            return None
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=_MODEL_PATH, num_threads=2)
        _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        logger.info(f"[speaker-check] embedding model loaded (dim={_extractor.dim})")
        return _extractor
    except Exception as e:
        logger.warning(f"[speaker-check] extractor unavailable — check skipped: {e}")
        return None


# ── Audio helpers ──────────────────────────────────────────────────────────────

def _load_mono_16k(wav_path: str) -> np.ndarray | None:
    """Load a WAV → float32 mono @16k. None on any failure."""
    try:
        import soundfile as sf
        samples, sr = sf.read(wav_path, dtype="float32", always_2d=True)
        mono = samples.mean(axis=1)
        if sr != _TARGET_SR:
            import librosa
            mono = librosa.resample(mono, orig_sr=sr, target_sr=_TARGET_SR)
        return mono.astype(np.float32)
    except Exception as e:
        logger.warning(f"[speaker-check] could not load audio ({wav_path}): {e}")
        return None


def _voiced_segments(audio: np.ndarray) -> list[tuple[int, int]]:
    """
    Energy-based voiced regions → ~5s windows for embedding.
    Threshold adapts to the recording's own noise floor, so quiet mics work.
    """
    frame = int(_TARGET_SR * _FRAME_MS / 1000)
    n = len(audio) // frame
    if n == 0:
        return []
    rms = np.sqrt(np.mean(audio[: n * frame].reshape(n, frame) ** 2, axis=1))
    floor  = np.percentile(rms, 10)          # noise floor estimate
    thresh = max(floor * 3.0, np.percentile(rms, 30))
    voiced = rms > thresh

    # merge voiced frames into regions (bridging gaps ≤ 300ms)
    regions: list[tuple[int, int]] = []
    start, gap = None, 0
    max_gap = int(300 / _FRAME_MS)
    for i, v in enumerate(voiced):
        if v:
            if start is None:
                start = i
            gap = 0
        elif start is not None:
            gap += 1
            if gap > max_gap:
                regions.append((start, i - gap))
                start, gap = None, 0
    if start is not None:
        regions.append((start, n - 1))

    # slice regions into windows, drop < MIN_SEG
    win = int(_SEG_SECONDS * 1000 / _FRAME_MS)
    min_frames = int(_MIN_SEG * 1000 / _FRAME_MS)
    out: list[tuple[int, int]] = []
    for a, b in regions:
        i = a
        while i <= b:
            j = min(i + win, b + 1)
            if j - i >= min_frames:
                out.append((i * frame, j * frame))
            i = j
    return out


def _robust_centroid(X: np.ndarray, trim: float = 0.2, iters: int = 3) -> np.ndarray:
    """
    Primary-voice centroid that shakes off a minority impostor: start from the
    plain mean, then repeatedly drop the least-similar `trim` fraction and
    recompute — the centroid converges onto the dominant voice.
    """
    c = X.mean(axis=0)
    c /= np.linalg.norm(c)
    for _ in range(iters):
        sims = X @ c
        keep = sims >= np.quantile(sims, trim)
        c = X[keep].mean(axis=0)
        c /= np.linalg.norm(c)
    return c


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


# ── Public entry point ─────────────────────────────────────────────────────────

def run_speaker_check(wav_path: str) -> dict | None:
    """
    Analyze a finished interview's recording for a second distinct voice.
    Returns a result dict, or None if the check can't run (disabled / no model /
    too little audio) — callers treat None as "no signal", never as an error.
    """
    if not _ENABLED:
        return None
    ex = _get_extractor()
    if ex is None:
        return None
    audio = _load_mono_16k(wav_path)
    if audio is None or len(audio) < _TARGET_SR * 30:     # <30s → nothing to say
        return None

    segs = _voiced_segments(audio)
    if len(segs) < 6:
        logger.info(f"[speaker-check] only {len(segs)} usable segments — skipping")
        return None

    # ── embeddings per window ─────────────────────────────────────────────────
    embs, spans, durs = [], [], []
    for a, b in segs:
        try:
            stream = ex.create_stream()
            stream.accept_waveform(_TARGET_SR, audio[a:b])
            stream.input_finished()
            if ex.is_ready(stream):
                v = np.array(ex.compute(stream), dtype=np.float32)
                nrm = np.linalg.norm(v)
                if nrm > 0:
                    embs.append(v / nrm)
                    spans.append((a / _TARGET_SR, b / _TARGET_SR))
                    durs.append((b - a) / _TARGET_SR)
        except Exception:
            continue
    if len(embs) < 6:
        return None
    X, durs_arr = np.stack(embs), np.array(durs)

    # ── similarity timeline vs robust primary centroid, smoothed ─────────────
    sims = X @ _robust_centroid(X)
    sm = np.convolve(sims, np.ones(_SMOOTH) / _SMOOTH, mode="same")

    # ── sustained low runs = candidate second voice ───────────────────────────
    low = sm < _RUN_THRESHOLD
    runs: list[dict] = []      # {seconds, start, end, mean_similarity}
    i = 0
    while i < len(low):
        if low[i]:
            j = i
            while j < len(low) and low[j]:
                j += 1
            runs.append({
                "seconds":         round(float(durs_arr[i:j].sum()), 1),
                "start":           _fmt_ts(spans[i][0]),
                "end":             _fmt_ts(spans[j - 1][1]),
                "mean_similarity": round(float(sims[i:j].mean()), 3),
            })
            i = j
        else:
            i += 1
    runs.sort(key=lambda r: -r["seconds"])
    suspicious = [r for r in runs if r["seconds"] >= _RUN_MIN_SECONDS]

    result = {
        "speakers_detected":     2 if suspicious else 1,
        "suspicious_spans":      suspicious[:5],
        "longest_low_run_s":     runs[0]["seconds"] if runs else 0.0,
        "windows_analyzed":      int(len(embs)),
        "total_speech_seconds":  round(float(durs_arr.sum()), 1),
        "primary_mean_similarity": round(float(np.percentile(sims, 75)), 3),
        "run_threshold":         _RUN_THRESHOLD,
        "run_min_seconds":       _RUN_MIN_SECONDS,
        "method":                "campp_run_detector_v1",
    }
    logger.info(
        f"[speaker-check] voices={result['speakers_detected']} "
        f"windows={result['windows_analyzed']} speech={result['total_speech_seconds']}s "
        f"longest_low_run={result['longest_low_run_s']}s suspicious={len(suspicious)}"
    )
    return result
