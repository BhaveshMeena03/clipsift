import os
import sys
from pathlib import Path

APP_NAME = "ClipSift"
PKG_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PKG_DIR / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
IS_WINDOWS = sys.platform == "win32"
APP_ROOT = PKG_DIR.parent
DATA_DIR_FILE = "data_dir.txt"


def default_home(is_windows: bool = IS_WINDOWS, app_root: Path = APP_ROOT) -> Path:
    """On Windows everything lives next to the app (any drive it was unzipped to), in a data folder.
    Setup's -DataDir writes data_dir.txt so the launcher finds a folder chosen elsewhere."""
    env = os.environ.get("CLIPSIFT_HOME")
    if env:
        return Path(env).expanduser().resolve()
    if is_windows:
        try:
            # utf-8-sig: Windows PowerShell 5.1 writes a BOM
            custom = (Path(app_root) / DATA_DIR_FILE).read_text(encoding="utf-8-sig").strip()
            if custom:
                return Path(custom)
        except OSError:
            pass
        return Path(app_root) / "data"
    return Path.home() / ".clipsift"


class Paths:
    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home else default_home()

    @property
    def db(self) -> Path:
        return self.home / "clipsift.db"

    @property
    def work(self) -> Path:
        return self.home / "work"

    @property
    def previews(self) -> Path:
        return self.home / "previews"

    @property
    def downloads(self) -> Path:
        return self.home / "downloads"

    @property
    def exports(self) -> Path:
        return self.home / "exports"

    @property
    def models(self) -> Path:
        return self.home / "models"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def tools(self) -> Path:
        return self.home / "tools"

    @property
    def references(self) -> Path:
        return self.home / "references"

    @property
    def lock(self) -> Path:
        return self.home / "worker.lock"

    def source_work(self, source_id: int) -> Path:
        return self.work / f"src_{int(source_id)}"

    def ensure(self) -> "Paths":
        for p in (self.home, self.work, self.previews, self.downloads, self.exports,
                  self.models, self.logs, self.tools, self.references):
            p.mkdir(parents=True, exist_ok=True)
        return self


# Settings the user can change. Stored in the DB, these are only the defaults.
DEFAULT_SETTINGS = {
    "creator_name": "fortysevenfrags",
    "ingame_names": ["fortysevenfrags"],
    "export_dir": "",
    "language_mode": "mixed",  # mixed | en | hi | auto
    "asr_model_gpu": "hinglish-apex",
    "asr_model_cpu": "hinglish-swift",
    "asr_device": "auto",  # auto | cuda | cpu
    "asr_batch_size": 0,
    "use_audio_tagger": True,
    "visual_scan": "auto",  # auto | keyframes | 1fps | off
    "max_candidates": 40,
    "min_clip_s": 15,
    "max_clip_s": 60,
    "quiet_samples_per_hour": 6,
    "captions_on": True,
    "censor_on": True,
    "hook_on": True,
    "caption_words_max": 4,
    "caption_upper": False,
    "caption_highlight": False,
    "caption_y": 0.70,
    "hook_y": 0.30,
    "hook_seconds": 0,  # 0 = whole clip
    "vertical_caption_y": 0.72,
    "vertical_hook_y": 0.14,
    "loudnorm": True,
    "encoder": "auto",  # auto | nvenc | amf | qsv | x264
    "export_quality": "max",  # max | fast
    "download_quality": "best",  # best | 1080 | 720
    "hwaccel_decode": False,
    "use_preferences": True,
    "pref_category_weights": {},
    "pref_duration": None,
    "instagram_username": "fortysevenfrags",
    "instagram_graph_token": "",
    "instagram_graph_user_id": "",
    "ffmpeg_path": "",
    "ffprobe_path": "",
    "deno_path": "",
    "gsi_token": "",
    "profanity_extra": [],
    "profanity_allow": [],
}

SECRET_SETTINGS = {"instagram_graph_token", "gsi_token"}
