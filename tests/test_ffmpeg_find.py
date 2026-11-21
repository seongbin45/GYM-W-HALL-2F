"""ffmpeg 찾기 — winget 으로 깐 뒤 PATH 가 아직 반영되지 않은 창에서도(이슈 #59)."""

from pathlib import Path

import pytest

from syncnote.recorder import ffmpeg_util as F


@pytest.fixture
def no_path_ffmpeg(monkeypatch, tmp_path):
    """PATH·FFMPEG·레포 동봉 어디에도 없는 상태."""
    monkeypatch.delenv("FFMPEG", raising=False)
    monkeypatch.setattr(F.shutil, "which", lambda *_a, **_k: None)
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self: False if "tools" in self.parts
                        and "ffmpeg" in str(self) else real_exists(self))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(F, "pip_ffmpeg", lambda: None)              # pip 쪽은 따로 시험
    return tmp_path / "Microsoft" / "WinGet"


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def test_missing_everywhere_raises_with_install_hint(no_path_ffmpeg):
    with pytest.raises(F.FFmpegMissing) as exc:
        F.find_ffmpeg()
    msg = str(exc.value)
    assert "install_ffmpeg.bat" in msg and "pip" in msg and "winget" in msg


def test_finds_winget_link_without_path(no_path_ffmpeg):
    link = _touch(no_path_ffmpeg / "Links" / "ffmpeg.exe")
    assert F.find_ffmpeg() == str(link)


def test_finds_winget_package_when_links_are_off(no_path_ffmpeg):
    pkgs = no_path_ffmpeg / "Packages"
    _touch(pkgs / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe" / "ffmpeg-8.1.2-full_build"
           / "bin" / "ffmpeg.exe")
    newer = _touch(pkgs / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
                   / "ffmpeg-9.1-full_build" / "bin" / "ffmpeg.exe")
    _touch(pkgs / "Other.Tool" / "ffmpeg-1" / "bin" / "ffmpeg.exe")      # 다른 패키지는 안 봄
    assert F.find_ffmpeg() == str(newer)


def test_path_still_wins_over_winget(no_path_ffmpeg, monkeypatch):
    _touch(no_path_ffmpeg / "Links" / "ffmpeg.exe")
    monkeypatch.setattr(F.shutil, "which", lambda c, *a, **k: "/usr/bin/ffmpeg" if c == "ffmpeg" else None)
    assert F.find_ffmpeg() == "/usr/bin/ffmpeg"
