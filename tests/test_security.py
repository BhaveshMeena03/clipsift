"""Supply chain and lookup safety: pinned models, safe unpacking, no programs from the current folder,
and Instagram links that can only reach Instagram."""
import io
import os
import re
import tarfile
from pathlib import Path

import pytest

from clipsift import models
from clipsift.prefs.instagram import InstagramError, post_url
from clipsift.util import find_on_path


def test_every_downloaded_model_is_pinned():
    for name, info in models.WHISPER.items():
        assert re.fullmatch(r"[0-9a-f]{40}", info.get("revision", "")), name
    for name, info in models.TAGGER.items():
        assert info["sha256"] and all(re.fullmatch(r"[0-9a-f]{64}", h) for h in info["sha256"].values()), name


def _tar(members: dict[str, bytes], symlink: str | None = None) -> Path:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tf:
        for name, data in members.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        if symlink:
            ti = tarfile.TarInfo("pkg/link")
            ti.type, ti.linkname = tarfile.SYMTYPE, symlink
            tf.addfile(ti)
    return buf


@pytest.mark.parametrize("evil", ["C:/clipsift_evil.txt", "/clipsift_evil.txt", "../clipsift_evil.txt"])
def test_unpack_never_writes_outside_its_folder(tmp_path, evil):
    arc = tmp_path / "m.tar.bz2"
    arc.write_bytes(_tar({"pkg/ok.txt": b"ok", evil: b"pwned"}).getvalue())
    dest = tmp_path / "out"
    dest.mkdir()
    try:
        models.unpack(arc, dest)
    except tarfile.TarError:
        pass  # refusing the whole archive is fine too
    assert not (tmp_path / "clipsift_evil.txt").exists()
    assert not Path("C:/clipsift_evil.txt").exists() and not Path("/clipsift_evil.txt").exists()


def test_unpack_skips_links(tmp_path):
    arc = tmp_path / "m.tar.bz2"
    arc.write_bytes(_tar({"pkg/ok.txt": b"ok"}, symlink=str(tmp_path)).getvalue())
    models.unpack(arc, tmp_path / "out")
    assert (tmp_path / "out" / "pkg" / "ok.txt").read_bytes() == b"ok"
    assert not os.path.lexists(tmp_path / "out" / "pkg" / "link")


def test_tagger_with_wrong_files_is_refused(tmp_path, monkeypatch):
    d = models.TAGGER["ced-mini"]["dir"]
    fake = _tar({f"{d}/model.int8.onnx": b"not the model", f"{d}/class_labels_indices.csv": b"x"}).getvalue()

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(models.urllib.request, "urlopen", lambda *a, **k: Resp(fake))
    with pytest.raises(RuntimeError, match="pinned checksum"):
        models.download_tagger(tmp_path / "models")
    assert not models.tagger_ready(tmp_path / "models")


def test_programs_are_never_taken_from_the_current_folder(tmp_path, monkeypatch):
    planted = tmp_path / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    planted.write_bytes(b"")
    planted.chmod(0o755)
    elsewhere = tmp_path / "bin"
    elsewhere.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", str(elsewhere)]))
    assert find_on_path("ffmpeg") is None
    real = elsewhere / planted.name
    real.write_bytes(b"")
    real.chmod(0o755)
    assert find_on_path("ffmpeg") == str(real)


@pytest.mark.parametrize("url,want", [
    ("https://www.instagram.com/reel/C9xAbCdEfGh/?igsh=abc", "https://www.instagram.com/reel/C9xAbCdEfGh/"),
    ("instagram.com/p/C9xAbCdEfGh", "https://www.instagram.com/p/C9xAbCdEfGh/"),
    ("https://instagram.com/someone/reels/C9xAbCdEfGh/", "https://www.instagram.com/reel/C9xAbCdEfGh/"),
])
def test_instagram_post_links_are_cleaned(url, want):
    assert post_url(url) == want


@pytest.mark.parametrize("url", ["http://192.168.1.1/instagram.com/p/C9xAbCdEfGh/",
                                 "https://evil.example/?u=instagram.com/p/C9xAbCdEfGh/",
                                 "https://www.instagram.com.evil.example/p/C9xAbCdEfGh/",
                                 "https://user:pw@www.instagram.com/p/C9xAbCdEfGh/",
                                 "https://www.instagram.com:8443/p/C9xAbCdEfGh/",
                                 "file:///C:/Windows/win.ini", "https://www.instagram.com/accounts/login/"])
def test_instagram_refuses_anything_but_a_post(url):
    with pytest.raises(InstagramError):
        post_url(url)
