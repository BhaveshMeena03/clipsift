"""Cheap whole-recording audio pass. 10 Hz loudness and transients, speech intervals,
and 0.5 Hz audio event tags. One checkpoint per 10 minute block."""
import logging
from pathlib import Path

import numpy as np

from ..media.audio import SR, stream_pcm
from ..util import Cancelled, atomic_write_json, read_json
from .tagger import GROUP_NAMES

log = logging.getLogger(__name__)

BLOCK_S = 600.0
FRAME = SR // 10          # 100 ms
TAG_WIN_S = 2.0


def rms_db(x: np.ndarray) -> np.ndarray:
    n = len(x) // FRAME
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    fr = x[: n * FRAME].reshape(n, FRAME)
    rms = np.sqrt(np.mean(fr * fr, axis=1) + 1e-12)
    return (20 * np.log10(rms + 1e-9)).astype(np.float32)


def spectral_flux(x: np.ndarray, n_fft: int = 1024, hop: int = 160) -> np.ndarray:
    """Onset strength at 10 Hz: max of the 10 ms flux values inside each 100 ms frame."""
    n_out = len(x) // FRAME
    out = np.zeros(n_out, dtype=np.float32)
    n_frames = (len(x) - n_fft) // hop + 1
    if n_frames <= 1:
        return out
    win = np.hanning(n_fft).astype(np.float32)
    flux = np.zeros(n_frames, np.float32)
    prev = None
    step = 3000  # 30 s of frames per piece keeps memory small
    for j0 in range(0, n_frames, step):
        j1 = min(n_frames, j0 + step)
        piece = x[j0 * hop: (j1 - 1) * hop + n_fft]
        frames = np.lib.stride_tricks.sliding_window_view(piece, n_fft)[::hop]
        mag = np.log1p(10.0 * np.abs(np.fft.rfft(frames * win, axis=1))).astype(np.float32)
        before = np.vstack([(mag[:1] if prev is None else prev[None, :]), mag[:-1]])
        flux[j0:j1] = np.maximum(0.0, mag - before).sum(axis=1)
        prev = mag[-1]
    per = FRAME // hop
    m = min(n_out, n_frames // per)
    if m:
        out[:m] = flux[: m * per].reshape(m, per).max(axis=1)
    return out


def speech_intervals(x: np.ndarray, t0: float) -> list[list[float]]:
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    if len(x) < SR // 2:
        return []
    ts = get_speech_timestamps(x, VadOptions(threshold=0.5, min_silence_duration_ms=300, speech_pad_ms=100,
                                             min_speech_duration_ms=200))
    return [[round(t0 + d["start"] / SR, 2), round(t0 + d["end"] / SR, 2)] for d in ts]


def merge_intervals(iv: list[list[float]], gap: float = 0.3) -> list[list[float]]:
    out: list[list[float]] = []
    for a, b in sorted(iv):
        if out and a - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def scan_block(path: Path, t0: float, dur: float, all_tracks, voice_tracks, tagger, cancel=None) -> dict:
    full = np.concatenate([a for _t, a in stream_pcm(path, all_tracks, SR, t0, dur, block_s=60, cancel=cancel)] or
                          [np.zeros(0, np.float32)])
    if voice_tracks and sorted(voice_tracks) != sorted(all_tracks or [0]):
        voice = np.concatenate([a for _t, a in stream_pcm(path, voice_tracks, SR, t0, dur, block_s=60, cancel=cancel)]
                               or [np.zeros(0, np.float32)])
    else:
        voice = full
    if cancel and cancel():
        raise Cancelled()
    out = {"t0": t0, "n_frames": len(full) // FRAME, "rms_all": rms_db(full).tolist(),
           "flux": spectral_flux(full).tolist(), "rms_voice": rms_db(voice).tolist(),
           "speech": speech_intervals(voice, t0), "tags": {g: [] for g in GROUP_NAMES}}
    if tagger is not None:
        w = int(TAG_WIN_S * SR)
        for i in range(0, max(0, len(full) - w // 4), w):
            if cancel and i % (w * 30) == 0 and cancel():
                raise Cancelled()
            res = tagger.tag(full[i:i + w])
            for g in GROUP_NAMES:
                out["tags"][g].append(round(res[g], 3))
    return out


def run_audio_scan(path: Path, duration: float, out_dir: Path, *, all_tracks=None, voice_tracks=None,
                   tagger=None, cancel=None, on_progress=None) -> dict:
    """Returns merged arrays. Finished blocks are read from disk on resume."""
    out_dir.mkdir(parents=True, exist_ok=True)
    n_blocks = max(1, int(np.ceil(duration / BLOCK_S)))
    blocks = []
    for b in range(n_blocks):
        t0 = b * BLOCK_S
        dur = min(BLOCK_S, duration - t0)
        if dur <= 0.05:
            break
        f = out_dir / f"block_{b:04d}.json"
        data = read_json(f)
        if not data or (tagger is not None and not any(data["tags"].values()) and dur > 3):
            data = scan_block(path, t0, dur, all_tracks, voice_tracks, tagger, cancel)
            atomic_write_json(f, data)
        blocks.append(data)
        if on_progress:
            on_progress((b + 1) / n_blocks)
    return merge_blocks(blocks, duration)


def merge_blocks(blocks: list[dict], duration: float) -> dict:
    n = int(np.ceil(duration * 10))
    rms_all = np.full(n, -90.0, np.float32)
    rms_voice = np.full(n, -90.0, np.float32)
    flux = np.zeros(n, np.float32)
    n_tag = int(np.ceil(duration / TAG_WIN_S))
    tags = {g: np.zeros(n_tag, np.float32) for g in GROUP_NAMES}
    speech = []
    has_tags = False
    for bl in blocks:
        i0 = int(round(bl["t0"] * 10))
        for arr, key in ((rms_all, "rms_all"), (rms_voice, "rms_voice"), (flux, "flux")):
            v = np.asarray(bl[key], np.float32)[: max(0, n - i0)]
            arr[i0:i0 + len(v)] = v
        j0 = int(round(bl["t0"] / TAG_WIN_S))
        for g in GROUP_NAMES:
            v = np.asarray(bl["tags"].get(g, []), np.float32)[: max(0, n_tag - j0)]
            if len(v):
                has_tags = True
            tags[g][j0:j0 + len(v)] = v
        speech.extend(bl["speech"])
    return {"rms_all": rms_all, "rms_voice": rms_voice, "flux": flux, "tags": tags, "has_tags": has_tags,
            "speech": merge_intervals(speech)}
