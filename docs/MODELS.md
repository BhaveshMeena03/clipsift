# Models

All models run locally. Setup downloads the defaults; others are optional (Settings > Models or `python -m clipsift.models download <name>`).

| Name | Used for | Source | License | Download | On disk |
|---|---|---|---|---|---|
| hinglish-apex | Speech to text on GPU (default). Hindi, English and mixed speech written as Roman Hinglish | [Oriserve/Whisper-Hindi2Hinglish-Apex](https://huggingface.co/Oriserve/Whisper-Hindi2Hinglish-Apex) @ `f3214ee`, based on whisper-large-v3-turbo | Apache-2.0 | 1.6 GB | 1.6 GB |
| hinglish-swift | Speech to text on CPU (default fallback) | [Oriserve/Whisper-Hindi2Hinglish-Swift](https://huggingface.co/Oriserve/Whisper-Hindi2Hinglish-Swift) @ `cd0d4d0`, based on whisper-base | Apache-2.0 | 0.3 GB | 0.15 GB |
| ced-mini | Audio events: laughter, screaming, shouting, gunshots, explosions, music | [sherpa-onnx audio-tagging release](https://github.com/k2-fsa/sherpa-onnx/releases/tag/audio-tagging-models), CED model by mispeech | Apache-2.0 | 48 MB | 10 MB (int8) |
| RapidOCR (PP-OCR) | Reading kill feeds, banners, money popups, reference captions | bundled in the `rapidocr-onnxruntime` package | Apache-2.0 | in package | ~15 MB |
| Silero VAD | Finding speech | bundled in `faster-whisper` | MIT | in package | ~2 MB |
| large-v3 (optional) | Stock Whisper, writes Hindi in Devanagari, romanized afterwards | Systran/faster-whisper-large-v3 | MIT | 3.1 GB | 3.1 GB |
| large-v3-turbo, large-v2, medium, small (optional) | Stock Whisper alternatives | Systran / mobiuslabsgmbh on Hugging Face | MIT | 0.5 to 3.1 GB | same |

## Why the Hinglish models

Stock Whisper on mixed Hindi/English often picks one language per 30 seconds and garbles the other (tested here: an English line was transcribed as nonsense "Hindi"). The Oriserve models are fine-tuned to write what was said in Roman Hinglish. On a mixed test line, Apex produced "Bro what are you doing? Oh my god, that was insane, let's go. Bhai kya kar raha hai? Paagal hai kya? Ek bacha hai." in one pass.

With stock models ClipSift still works: Devanagari output is converted to casual Roman Hindi (`bhai kya kar raha hai`, not academic `bhāī kyā`), and low-confidence segments are re-decoded in the language their own audio sounds like, keeping whichever reading the model scores higher.

## Conversion

The Oriserve models are published as transformers checkpoints. `setup\convert_models.ps1` converts them once to CTranslate2 float16, pinned to the commits above, in a temporary environment with PyTorch that is deleted afterwards. Nothing is downloaded from unofficial re-uploads.

## GPU memory

ClipSift reads free VRAM with `nvidia-smi` and asks the speech runtime which precisions the card supports, then picks float16, int8_float16, int8 or float32 in that order (GTX 10-series cards skip float16), and falls back to CPU only when nothing fits. On an out-of-memory error during a chunk it retries with int8, then falls back to CPU, and says so in the job message. Only one heavy model is loaded at a time.
