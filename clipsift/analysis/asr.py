"""faster-whisper wrapper: picks GPU or CPU, falls back on load or out-of-memory errors."""
import gc
import logging
import re
from dataclasses import dataclass

import numpy as np

from .. import models
from ..gpu import cuda_device_count, nvidia_smi, setup_cuda_dlls
from ..text.translit import romanize
from ..util import Cancelled

log = logging.getLogger(__name__)

# Things Whisper is known to write over silence, music or noise.
HALLUCINATIONS = [
    r"thanks? (you )?for watching", r"please (like and )?subscribe", r"subscribe to (my|the) channel",
    r"subtitles? (by|created by)", r"amara\.org", r"transcribed by", r"don't forget to (like|subscribe)",
    r"see you (in the )?next (video|time)", r"\bmbc\b", r"copyright", r"www\.", r"字幕",
    r"^(you|bye|okay|nan)\.?$", r"^\W*$", r"सब्सक्राइब", r"धन्यवाद",
]
_HALL_RE = re.compile("|".join(HALLUCINATIONS), re.I)
# Urdu, Arabic, Bengali, Gurmukhi... anything we cannot romanize
_FOREIGN_SCRIPT = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u0980-\u0D7F\u0E00-\u0FFF\u3040-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF]")


@dataclass
class AsrPlan:
    device: str
    compute_type: str
    model: str
    reason: str


def plan_asr(settings: dict, models_dir) -> AsrPlan:
    want = settings.get("asr_device", "auto")
    gpu_model = settings.get("asr_model_gpu", "large-v3")
    cpu_model = settings.get("asr_model_cpu", "small")
    if want in ("auto", "cuda"):
        n = cuda_device_count()
        gpus = nvidia_smi()
        if n > 0:
            free = gpus[0]["free_mb"] if gpus else None
            need16, need8 = models.VRAM_NEED.get(gpu_model, (4300, 2900))
            if free is None or free >= need16:
                return AsrPlan("cuda", "float16", gpu_model,
                               f"GPU {gpus[0]['name'] if gpus else ''} ({free if free is not None else '?'} MB free)")
            if free >= need8:
                return AsrPlan("cuda", "int8_float16", gpu_model, f"GPU with {free} MB free, using int8 to fit")
            reason = f"only {free} MB VRAM free (need about {need8} MB)"
        else:
            reason = "no CUDA GPU available to the speech model"
        if want == "cuda":
            log.warning("GPU requested but not usable: %s", reason)
        return AsrPlan("cpu", "int8", cpu_model, f"CPU fallback: {reason}")
    return AsrPlan("cpu", "int8", cpu_model, "CPU selected in settings")


class Transcriber:
    def __init__(self, plan: AsrPlan, models_dir, cpu_threads: int = 0, cpu_model: str = "small"):
        self.plan = plan
        self.models_dir = models_dir
        self.cpu_threads = cpu_threads
        self.cpu_model = cpu_model
        self.model = None
        self._load()

    def _load(self):
        setup_cuda_dlls()
        from faster_whisper import WhisperModel
        path = models.whisper_dir(self.models_dir, self.plan.model)
        if not models.whisper_ready(self.models_dir, self.plan.model):
            raise RuntimeError(f"Speech model '{self.plan.model}' is not downloaded. Open Settings > Models.")
        try:
            self.model = WhisperModel(str(path), device=self.plan.device, compute_type=self.plan.compute_type,
                                      cpu_threads=self.cpu_threads)
        except Exception as e:
            if self.plan.device != "cuda":
                raise
            log.warning("GPU model load failed (%s), using CPU", e)
            self._to_cpu(f"GPU load failed: {str(e)[:160]}")

    def _to_cpu(self, reason: str):
        self.close()
        cpu_model = self.cpu_model
        if not models.whisper_ready(self.models_dir, cpu_model):
            if not models.whisper_ready(self.models_dir, self.plan.model):
                raise RuntimeError(f"GPU failed ({reason}) and the CPU model '{cpu_model}' is not downloaded.")
            cpu_model = self.plan.model
        self.plan = AsrPlan("cpu", "int8", cpu_model, f"CPU fallback: {reason}")
        from faster_whisper import WhisperModel
        self.model = WhisperModel(str(models.whisper_dir(self.models_dir, cpu_model)), device="cpu",
                                  compute_type="int8", cpu_threads=self.cpu_threads)

    def close(self):
        if self.model is not None:
            del self.model
            self.model = None
            gc.collect()

    def transcribe(self, audio: np.ndarray, *, offset: float, language_mode: str = "mixed", prompt: str = "",
                   hotwords: str = "", beam_size: int = 5, cancel=None, on_progress=None,
                   vad: bool = True, seg_prefix: str = "") -> list[dict]:
        for attempt in range(3):
            try:
                return self._transcribe(audio, offset=offset, language_mode=language_mode, prompt=prompt,
                                        hotwords=hotwords, beam_size=beam_size, cancel=cancel,
                                        on_progress=on_progress, vad=vad, seg_prefix=seg_prefix)
            except Cancelled:
                raise
            except RuntimeError as e:
                msg = str(e).lower()
                if self.plan.device == "cuda" and ("out of memory" in msg or "cuda" in msg or "cublas" in msg):
                    if self.plan.compute_type == "float16" and attempt == 0:
                        log.warning("GPU error (%s), retrying with int8_float16", e)
                        self.close()
                        self.plan = AsrPlan("cuda", "int8_float16", self.plan.model, "retry after GPU memory error")
                        self._load()
                    else:
                        self._to_cpu(f"GPU error during transcription: {str(e)[:120]}")
                    continue
                raise
        raise RuntimeError("transcription failed after retries")

    def _transcribe(self, audio, *, offset, language_mode, prompt, hotwords, beam_size, cancel, on_progress,
                    vad, seg_prefix):
        if models.family(self.plan.model) == "hinglish":
            # these models are trained to be run with language=en and write Roman Hinglish
            return self._decode(audio, offset=offset, language="en", multilingual=False, prompt=prompt,
                                hotwords=hotwords, beam_size=beam_size, cancel=cancel, on_progress=on_progress,
                                vad=vad, seg_prefix=seg_prefix)
        segs = self._decode(audio, offset=offset, language=None if language_mode in ("mixed", "auto") else language_mode,
                            multilingual=language_mode == "mixed", prompt=prompt, hotwords=hotwords,
                            beam_size=beam_size, cancel=cancel, on_progress=on_progress, vad=vad, seg_prefix=seg_prefix)
        if language_mode == "mixed":
            segs = self._repair_code_switch(audio, offset, segs, prompt=prompt, hotwords=hotwords,
                                            beam_size=beam_size, cancel=cancel)
        return segs

    def _decode(self, audio, *, offset, language, multilingual, prompt, hotwords, beam_size, cancel,
                on_progress, vad, seg_prefix):
        dur = len(audio) / 16000
        if dur < 0.3:
            return []
        kwargs = dict(beam_size=beam_size, word_timestamps=True, condition_on_previous_text=False,
                      temperature=[0.0, 0.2, 0.4, 0.6], compression_ratio_threshold=2.4,
                      log_prob_threshold=-1.0, no_speech_threshold=0.6, vad_filter=vad,
                      initial_prompt=prompt or None, hotwords=hotwords or None, language=language,
                      multilingual=multilingual)
        if vad:
            kwargs["vad_parameters"] = {"min_silence_duration_ms": 500, "speech_pad_ms": 200}
        import ctranslate2
        ctranslate2.set_random_seed(1234)  # temperature fallback samples; keep runs repeatable
        segments, info = self.model.transcribe(audio, **kwargs)
        out = []
        for i, seg in enumerate(segments):
            if cancel and cancel():
                raise Cancelled()
            if on_progress:
                on_progress(min(1.0, seg.end / dur))
            words = [{"start": round(float(offset + w.start), 3), "end": round(float(offset + w.end), 3),
                      "text": romanize(w.word), "prob": round(float(w.probability), 3)}
                     for w in (seg.words or [])]
            text = romanize(seg.text).strip()
            flags = []
            if _HALL_RE.search(text):
                flags.append("hallucination_phrase")
            if seg.compression_ratio > 2.2:
                flags.append("repetitive")
            if seg.avg_logprob < -0.8:
                flags.append("low_logprob")
            if seg.no_speech_prob > 0.6:
                flags.append("no_speech")
            if _FOREIGN_SCRIPT.search(text):
                flags.append("foreign_script")
            out.append({"id": f"{seg_prefix}{i}", "start": round(float(offset + seg.start), 3),
                        "end": round(float(offset + seg.end), 3), "text": text, "words": words,
                        "avg_logprob": round(float(seg.avg_logprob), 3),
                        "no_speech_prob": round(float(seg.no_speech_prob), 3),
                        "compression_ratio": round(float(seg.compression_ratio), 3),
                        "lang": getattr(seg, "language", None) or info.language, "flags": flags})
        return out

    def pick_language(self, piece: np.ndarray) -> tuple[str, float]:
        """English or Hindi only. Urdu counts as Hindi: same speech, different script."""
        _lang, _p, probs = self.model.detect_language(piece)
        d = dict(probs)
        en = d.get("en", 0.0)
        hi = d.get("hi", 0.0) + d.get("ur", 0.0)
        total = (en + hi) or 1.0
        return ("hi", hi / total) if hi > en else ("en", en / total)

    def _repair_code_switch(self, audio, offset, segs, *, prompt, hotwords, beam_size, cancel):
        """Re-decode shaky segments in the language their own audio sounds like.
        Keeps whichever reading the model itself is more confident in."""
        out = []
        for seg in segs:
            if cancel and cancel():
                raise Cancelled()
            shaky = (seg["lang"] not in ("en", "hi") or seg["avg_logprob"] < -0.55
                     or {"low_logprob", "foreign_script", "repetitive"} & set(seg["flags"]))
            if not shaky or "hallucination_phrase" in seg["flags"]:
                out.append(seg)
                continue
            a0 = max(0, int((seg["start"] - offset - 0.3) * 16000))
            a1 = min(len(audio), int((seg["end"] - offset + 0.3) * 16000))
            piece = audio[a0:a1]
            if len(piece) < 8000:
                out.append(seg)
                continue
            lang, conf = self.pick_language(piece)
            if lang == seg["lang"] and "foreign_script" not in seg["flags"]:
                out.append(seg)
                continue
            alt = self._decode(piece, offset=offset + a0 / 16000, language=lang, multilingual=False, prompt=prompt,
                               hotwords=hotwords, beam_size=beam_size, cancel=cancel, on_progress=None, vad=False,
                               seg_prefix=seg["id"] + "r")
            alt = [a for a in alt if a["words"]]
            if not alt:
                out.append(seg)
                continue
            alt_lp = _mean_logprob(alt)
            if "foreign_script" in seg["flags"] or alt_lp > seg["avg_logprob"] + 0.15:
                for a in alt:
                    a["flags"] = sorted(set(a["flags"]) | {"relanguaged"})
                    a["prev_text"] = seg["text"]
                    a["lang_conf"] = round(conf, 3)
                out.extend(alt)
            else:
                out.append(seg)
        return out


def _mean_logprob(segs: list[dict]) -> float:
    tot = sum(max(0.01, s["end"] - s["start"]) for s in segs)
    return sum(s["avg_logprob"] * max(0.01, s["end"] - s["start"]) for s in segs) / tot if tot else -9.0


def segment_status(seg: dict, speech_overlap: float | None = None) -> str:
    """ok | flagged | dropped. 'dropped' never reaches captions."""
    flags = set(seg.get("flags") or [])
    if "hallucination_phrase" in flags:
        return "dropped"
    if "no_speech" in flags and seg.get("avg_logprob", 0) < -0.7:
        return "dropped"
    if speech_overlap is not None and speech_overlap < 0.2:
        return "dropped"
    if "repetitive" in flags and seg.get("avg_logprob", 0) < -0.6:
        return "dropped"
    words = seg.get("words") or []
    low = [w for w in words if w.get("prob") is not None and w["prob"] < 0.45]
    if flags or (words and len(low) / len(words) > 0.25):
        return "flagged"
    return "ok"
