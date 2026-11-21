"""ffmpeg 를 릴리스 목록에서 받기 — 실패하면 이전 릴리스로 롤백.

네트워크·실제 ffmpeg 없이: 릴리스 JSON·zip·ffmpeg 출력을 가짜로 줍니다.
"""

import errno
import hashlib
import io
import json
import types
import urllib.error
import zipfile
from pathlib import Path

import pytest

from syncnote import ffmpeg_fetch as F

GOOD_DEVICES = " D  dshow\n D  gdigrab         GDI API Windows frame grabber\n"
GOOD_ENCODERS = " V....D libx264  H.264\n A....D aac      AAC (Advanced Audio Coding)\n"


def _zip(top="ffmpeg-x", marker=b"ok") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{top}/bin/ffmpeg.exe", marker)
        z.writestr(f"{top}/bin/ffprobe.exe", b"p")
    return buf.getvalue()


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _asset(tag, data=b"", digest=None, kind="essentials_build.zip"):
    return {"name": f"ffmpeg-{tag}-{kind}", "size": len(data),
            "browser_download_url": f"https://dl/{tag}/{kind}",
            "digest": f"sha256:{_sha(data) if digest is None else digest}"}


def _release(tag, *assets, draft=False, pre=False, when="2026-11-01T00:00:00Z"):
    return {"tag_name": tag, "draft": draft, "prerelease": pre, "published_at": when,
            "assets": list(assets)}


class _Resp(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


def opener_for(routes: dict):
    """url -> bytes | Exception | list(차례로)."""
    calls = []

    def _open(url, timeout=30):
        calls.append(url)
        v = routes.get(url)
        if isinstance(v, list):
            v = v.pop(0)
        if v is None:
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)
        if isinstance(v, BaseException):
            raise v
        return _Resp(v)
    _open.calls = calls
    return _open


def runner(bad: dict | None = None):
    """ffmpeg 출력 흉내. 파일 내용(marker)에 따라 기능을 뺍니다."""
    bad = bad or {}

    def run(cmd, **_kw):
        marker = Path(cmd[0]).read_bytes().decode()
        devices, encoders = GOOD_DEVICES, GOOD_ENCODERS
        for feature in bad.get(marker, []):
            devices = devices.replace(feature, "x")
            encoders = encoders.replace(feature, "x")
        return types.SimpleNamespace(stdout=devices if cmd[2] == "-devices" else encoders,
                                     stderr="", returncode=0)
    return run


# -- 후보 순서 ---------------------------------------------------------------
def test_candidates_stable_newest_first_then_nightly_and_skip_others():
    rels = [_release("2026-11-19-git-a", _asset("2026-11-19-git-a"), when="2026-11-19T00:00:00Z"),
            _release("9.1", _asset("9.1"), _asset("9.1", kind="full_build.zip"),
                     _asset("9.1", kind="essentials_build.7z")),
            _release("9.0.2", _asset("9.0.2")),
            _release("10.0", _asset("10.0"), draft=True),                   # 초안은 제외
            _release("9.10", _asset("9.10")),                               # 9.10 > 9.1
            _release("2026-11-20-git-b", _asset("2026-11-20-git-b"), when="2026-11-20T00:00:00Z")]
    order = [c.version for c in F.candidates(rels)]
    assert order == ["9.10", "9.1", "9.0.2", "2026-11-20-git-b", "2026-11-19-git-a"]
    c = F.candidates(rels)[1]
    assert c.name == "ffmpeg-9.1-essentials_build.zip" and len(c.sha256) == 64


# -- 내려받기 ------------------------------------------------------------------
def test_download_retries_transient_errors_then_checks_hash(tmp_path):
    data = _zip()
    c = F.Candidate("a.zip", "https://dl/a", "9.1", sha256=_sha(data))
    waits = []
    op = opener_for({"https://dl/a": [urllib.error.URLError("reset"),
                                      urllib.error.HTTPError("u", 503, "x", {}, None), data]})
    F.download(c, tmp_path / "a.zip", opener=op, sleep=waits.append, say=lambda _m: None)
    assert waits == [2, 4] and (tmp_path / "a.zip").read_bytes() == data


def test_download_gives_up_without_retry_on_404_and_on_bad_hash(tmp_path):
    waits = []
    with pytest.raises(F.CandidateFailed, match="내려받기 실패"):
        F.download(F.Candidate("a.zip", "https://dl/none", "9.1"), tmp_path / "a.zip",
                   opener=opener_for({}), sleep=waits.append, say=lambda _m: None)
    assert waits == []
    with pytest.raises(F.CandidateFailed, match="SHA-256"):
        F.download(F.Candidate("b.zip", "https://dl/b", "9.1", sha256="0" * 64),
                   tmp_path / "b.zip", opener=opener_for({"https://dl/b": b"tampered"}),
                   sleep=waits.append, say=lambda _m: None)
    assert not list(tmp_path.iterdir())                                  # 반쪽 파일 안 남김


def test_extract_rejects_paths_that_escape(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../../evil.exe", b"x")
    (tmp_path / "e.zip").write_bytes(buf.getvalue())
    with pytest.raises(F.CandidateFailed, match="바깥"):
        F.extract(tmp_path / "e.zip", tmp_path / "x")
    assert not (tmp_path.parent / "evil.exe").exists()


def test_missing_features_reads_ffmpeg_output(tmp_path):
    exe = tmp_path / "ffmpeg.exe"
    exe.write_bytes(b"nogdi")
    got = F.missing_features(exe, run=runner({"nogdi": ["gdigrab", "libx264"]}))
    # 화면 캡처(gdigrab)는 Windows 에서만 필요
    assert got == (["gdigrab", "libx264"] if F.os.name == "nt" else ["libx264"])
    assert F.missing_features(tmp_path / "gone.exe") == ["실행 안 됨"]


# -- 전체: 롤백 --------------------------------------------------------------
def _routes(*specs):
    """(tag, zip bytes, digest 덮어쓰기) -> (releases, routes)."""
    rels, routes = [], {}
    for tag, data, digest in specs:
        a = _asset(tag, data, digest)
        rels.append(_release(tag, a))
        routes[a["browser_download_url"]] = data
    routes[F.RELEASES_API] = json.dumps(rels).encode()
    routes[F.GYAN_URL + ".sha256"] = None
    return routes


def test_rolls_back_to_older_release_until_one_works(tmp_path):
    routes = _routes(("9.1", _zip(marker=b"v91"), "0" * 64),              # 해시 틀림
                     ("9.0.2", _zip(marker=b"v902"), None),               # 기능 없음
                     ("9.0", _zip(marker=b"v90"), None))                  # 정상
    logs = []
    r = F.install(tmp_path, opener=opener_for(routes), run=runner({"v902": ["libx264"]}),
                  sleep=lambda _s: None, say=logs.append)
    assert r.ok and r.used.version == "9.0"
    assert [v for v, _ in r.tried] == ["9.1", "9.0.2"]
    assert "SHA-256" in r.tried[0][1] and "libx264" in r.tried[1][1]
    assert (tmp_path / "ffmpeg" / "bin" / "ffmpeg.exe").read_bytes() == b"v90"
    assert any("롤백: 9.0.2" in m for m in logs) and any("롤백: 9.0" in m for m in logs)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ffmpeg"]      # 임시 폴더 정리


def test_existing_good_ffmpeg_is_kept_without_downloading(tmp_path):
    (tmp_path / "ffmpeg" / "bin").mkdir(parents=True)
    (tmp_path / "ffmpeg" / "bin" / "ffmpeg.exe").write_bytes(b"have")
    op = opener_for({})
    r = F.install(tmp_path, opener=op, run=runner(), say=lambda _m: None)
    assert r.ok and op.calls == []


def test_broken_existing_ffmpeg_is_replaced_and_old_one_removed(tmp_path):
    (tmp_path / "ffmpeg" / "bin").mkdir(parents=True)
    (tmp_path / "ffmpeg" / "bin" / "ffmpeg.exe").write_bytes(b"old")
    routes = _routes(("9.1", _zip(marker=b"new"), None))
    r = F.install(tmp_path, opener=opener_for(routes), run=runner({"old": ["aac"]}),
                  say=lambda _m: None)
    assert r.ok and (tmp_path / "ffmpeg" / "bin" / "ffmpeg.exe").read_bytes() == b"new"
    assert not (tmp_path / "ffmpeg.old").exists()


def test_swap_failure_puts_the_old_ffmpeg_back(tmp_path, monkeypatch):
    (tmp_path / "ffmpeg" / "bin").mkdir(parents=True)
    (tmp_path / "ffmpeg" / "bin" / "ffmpeg.exe").write_bytes(b"old")
    new = tmp_path / "new"
    (new / "bin").mkdir(parents=True)

    def boom(*_a, **_k):
        raise PermissionError("locked")
    monkeypatch.setattr(F.shutil, "move", boom)
    with pytest.raises(F.CandidateFailed, match="옮기지 못함"):
        F.swap_in(new, tmp_path)
    assert (tmp_path / "ffmpeg" / "bin" / "ffmpeg.exe").read_bytes() == b"old"


def test_list_unreachable_falls_back_to_gyan_dev(tmp_path):
    data = _zip(marker=b"gyan")
    routes = {F.RELEASES_API: urllib.error.URLError("blocked"),
              F.GYAN_URL + ".sha256": f"{_sha(data)} *ffmpeg-release-essentials.zip".encode(),
              F.GYAN_URL: data}
    r = F.install(tmp_path, opener=opener_for(routes), run=runner(), say=lambda _m: None)
    assert r.ok and r.used.version == "gyan.dev" and r.used.sha256 == _sha(data)


def test_everything_fails_reports_each_reason(tmp_path):
    routes = _routes(("9.1", _zip(marker=b"a"), "0" * 64), ("9.0", _zip(marker=b"b"), "0" * 64))
    r = F.install(tmp_path, opener=opener_for(routes), run=runner(), say=lambda _m: None)
    assert not r.ok and [v for v, _ in r.tried] == ["9.1", "9.0", "gyan.dev"]
    assert not (tmp_path / "ffmpeg").exists()


def test_disk_full_stops_instead_of_trying_older_releases(tmp_path, monkeypatch):
    routes = _routes(("9.1", _zip(), None), ("9.0", _zip(), None))
    real_open = Path.open

    def full(self, mode="r", *a, **k):
        if "w" in mode and self.name.endswith(".part"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(self, mode, *a, **k)
    monkeypatch.setattr(Path, "open", full)
    r = F.install(tmp_path, opener=opener_for(routes), run=runner(), say=lambda _m: None)
    assert not r.ok and len(r.tried) == 1 and "디스크" in r.tried[0][1]


# -- pip(imageio-ffmpeg) 경로 --------------------------------------------------
def _fake_imageio(monkeypatch, exe):
    import sys
    import types as _t

    mod = _t.ModuleType("imageio_ffmpeg")
    mod.get_ffmpeg_exe = lambda: str(exe)
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", mod)


def test_pip_ffmpeg_used_only_when_it_has_needed_features(tmp_path, monkeypatch):
    from syncnote.recorder import ffmpeg_util as U

    exe = tmp_path / "ffmpeg-win-x86_64-v9.0.2.exe"
    exe.write_bytes(b"x")
    _fake_imageio(monkeypatch, exe)
    U.reset_cache()
    monkeypatch.setattr(F, "missing_features", lambda _p, **_k: [])
    assert U.pip_ffmpeg() == str(exe)
    monkeypatch.setattr(F, "missing_features", lambda _p, **_k: ["libx264"])
    assert U.pip_ffmpeg() == str(exe)                                   # 프로세스당 한 번(캐시)
    U.reset_cache()
    assert U.pip_ffmpeg() is None                                        # 기능 없음 -> 안 씀
    U.reset_cache()


def test_pip_ffmpeg_absent_module_is_skipped(monkeypatch):
    import sys

    from syncnote.recorder import ffmpeg_util as U

    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)            # import 실패
    U.reset_cache()
    assert U.pip_ffmpeg() is None
    U.reset_cache()


def test_pip_install_reports_failure_and_rechecks_after_success(monkeypatch):
    from syncnote.recorder import ffmpeg_util as U

    calls = []
    fail = lambda cmd, **k: calls.append(cmd) or types.SimpleNamespace(returncode=1)  # noqa: E731
    assert F.pip_install(run=fail, say=lambda _m: None) == ""
    assert calls[0][1:4] == ["-m", "pip", "install"] and "imageio-ffmpeg>=0.5" in calls[0]
    resets = []
    monkeypatch.setattr(U, "reset_cache", lambda: resets.append(1))
    monkeypatch.setattr(U, "pip_ffmpeg", lambda: "C:/site/ffmpeg.exe")
    ok = lambda cmd, **k: types.SimpleNamespace(returncode=0)  # noqa: E731
    assert F.pip_install(run=ok, say=lambda _m: None) == "C:/site/ffmpeg.exe" and resets


def test_find_usable_rejects_a_found_ffmpeg_without_features(monkeypatch):
    from syncnote.recorder import ffmpeg_util as U

    monkeypatch.setattr(U, "find_ffmpeg", lambda: "C:/old/ffmpeg.exe")
    monkeypatch.setattr(F, "missing_features", lambda _p, **_k: ["libx264"])
    logs = []
    assert F.find_usable(say=logs.append) == "" and "FFMPEG" in logs[0]
    monkeypatch.setattr(F, "missing_features", lambda _p, **_k: [])
    assert F.find_usable(say=logs.append) == "C:/old/ffmpeg.exe"
