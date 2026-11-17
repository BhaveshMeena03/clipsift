"""AudioSet event tagger (CED via sherpa-onnx). Its outputs are model guesses with probabilities."""
import csv
import logging
from pathlib import Path

import numpy as np

from .. import models

log = logging.getLogger(__name__)

GROUPS = {
    "laugh": ["Laughter", "Baby laughter", "Giggle", "Snicker", "Belly laugh", "Chuckle, chortle"],
    "scream": ["Screaming"],
    "shout": ["Shout", "Yell", "Whoop", "Battle cry", "Children shouting"],
    "gasp": ["Gasp"],
    "gunshot": ["Gunshot, gunfire", "Machine gun", "Fusillade", "Artillery fire", "Cap gun"],
    "explosion": ["Explosion", "Bang", "Burst, pop"],
    "music": ["Music"],
    "speech": ["Speech"],
    "cheer": ["Cheering"],
}
GROUP_NAMES = list(GROUPS)


class Tagger:
    def __init__(self, models_dir: Path, threads: int = 2):
        import sherpa_onnx
        d = models.tagger_dir(models_dir)
        if not models.tagger_ready(models_dir):
            raise RuntimeError("Audio event model is not downloaded.")
        labels = d / "class_labels_indices.csv"
        cfg = sherpa_onnx.AudioTaggingConfig(
            model=sherpa_onnx.AudioTaggingModelConfig(ced=str(d / "model.int8.onnx"), num_threads=threads,
                                                      provider="cpu"),
            labels=str(labels), top_k=60)
        self._tagger = sherpa_onnx.AudioTagging(cfg)
        with open(labels, encoding="utf-8") as f:
            names = {row[2] for row in csv.reader(f) if len(row) >= 3}
        self._group_of = {}
        for g, labs in GROUPS.items():
            for lab in labs:
                if lab in names:
                    self._group_of[lab] = g

    def tag(self, samples: np.ndarray, sr: int = 16000) -> dict[str, float]:
        out = dict.fromkeys(GROUP_NAMES, 0.0)
        if len(samples) < sr // 4:
            return out
        stream = self._tagger.create_stream()
        stream.accept_waveform(sr, samples.astype(np.float32, copy=False))
        for ev in self._tagger.compute(stream):
            g = self._group_of.get(ev.name)
            if g:
                out[g] = max(out[g], float(ev.prob))
        return out
