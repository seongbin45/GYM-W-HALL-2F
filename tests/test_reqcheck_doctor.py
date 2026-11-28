"""Qt 없이 도는 부분: reqcheck, doctor/devices --json (CI 에서도 돎)."""

import json
import textwrap

from syncnote import cli, reqcheck

# -- reqcheck (Qt 없이) ------------------------------------------------------


def test_reqcheck_statuses():
    text = textwrap.dedent("""
        # 주석
        pytest>=1.0          # 설치돼 있음
        pytest<1             # 버전 안 맞음
        zz-not-installed-pkg>=1
        tomli>=2.0 ; python_version < "2.0"
        --index-url https://example.invalid
    """)
    got = {(i["line"], i["status"]) for i in reqcheck.check(text)}
    assert ("pytest>=1.0", "ok") in got and ("pytest<1", "mismatch") in got
    assert ("zz-not-installed-pkg>=1", "missing") in got
    assert any(s == "skip" for ln, s in got if ln.startswith("tomli"))
    assert len(got) == 4                                   # 옵션 줄은 뺌


def test_reqcheck_reads_the_real_requirements_file():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
    names = {i["name"].lower() for i in reqcheck.check(text)}
    assert {"requests", "keyring", "pyside6"} <= names


# -- doctor / devices --json --------------------------------------------------


def test_doctor_json_is_structured_and_text_unchanged(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[general]\ndata_dir = "' + (tmp_path / "d").as_posix() + '"\n'
                   '[github]\nowner = "o"\nrepo = "r"\n[eclass]\nenabled = false\n'
                   '[recording]\nenabled = false\n[transcribe]\nenabled = false\n',
                   encoding="utf-8")
    cli.main(["-q", "-c", str(cfg), "doctor", "--json"])
    rep = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rep["info"][0][0] == "Python" and any(s["title"] == "자격증명" for s in rep["sections"])
    assert rep["verdict"] and isinstance(rep["ok"], bool)
    cli.main(["-q", "-c", str(cfg), "doctor"])
    text = capsys.readouterr().out
    assert "환경 점검" in text and "파이썬 패키지" in text and not text.lstrip().startswith("{")


def test_doctor_shows_recent_local_problems(tmp_path, capsys, monkeypatch):
    """doctor 가 네트워크·토큰 없이 로컬 로그에서 최근 실패를 보여 줘야 합니다.
    incidents 업로드 경로는 토큰이 죽으면 같이 죽으므로, 접속만 하면 보이는
    로컬 자기기록이 필요합니다."""
    # 토큰 실검증은 네트워크를 타므로 테스트에서는 막습니다(이 테스트의 관심사 아님).
    monkeypatch.setattr("syncnote.release.check_token", lambda *a, **k: ("!", "확인 생략"))
    d = tmp_path / "d"
    (d / "logs").mkdir(parents=True)
    (d / "logs" / "syncnote.log").write_text(
        "2026-11-27 18:00:00 [INFO   ] x: ok\n"
        "2026-11-27 18:38:03 [ERROR  ] syncnote.cli: 업로드 0건 — 한 개도 못 올렸습니다\n",
        encoding="utf-8")
    cfg = tmp_path / "config.toml"
    cfg.write_text('[general]\ndata_dir = "' + d.as_posix() + '"\n'
                   '[github]\nowner = "o"\nrepo = "r"\n[eclass]\nenabled = false\n'
                   '[recording]\nenabled = false\n[transcribe]\nenabled = false\n', encoding="utf-8")
    cli.main(["-q", "-c", str(cfg), "doctor"])
    text = capsys.readouterr().out
    assert "최근 이상(로컬" in text
    assert "업로드 0건" in text


def test_doctor_json_reports_config_errors(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[eclass]\nenabled = true\nusername = ""\n', encoding="utf-8")
    cli.main(["-q", "-c", str(cfg), "doctor", "--json"])
    rep = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rep["ok"] is False and "eclass.username" in rep["config_error"]


def test_devices_json_shape(capsys):
    cli.main(["devices", "--json"])
    d = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert [g["kind"] for g in d["groups"]] == ["loopback", "microphone", "speaker"]
    assert d["groups"][0]["config_key"] == "recording.audio.system_device"
    assert "monitors" in d


# -- setupcheck ---------------------------------------------------------------
def test_setupcheck_missing_reasons():
    from syncnote import setupcheck

    full = {"in_venv": True, "reqs_bad": [], "ffmpeg": "f.exe", "chromium": True,
            "tailscale": "tailscale.exe"}
    assert setupcheck.missing(full) == []
    gaps = setupcheck.missing({"in_venv": False, "reqs_bad": ["psutil"], "ffmpeg": None,
                               "chromium": False, "tailscale": None})
    assert gaps == ["가상환경(.venv) 없음", "패키지: psutil", "ffmpeg 없음",
                    "Playwright Chromium 없음", "Tailscale 없음"]


def test_setupcheck_reports_tailscale_path_or_none(tmp_path, monkeypatch):
    from syncnote import setupcheck
    from syncnote.stream import tailnet

    req = tmp_path / "r.txt"
    req.write_text("", encoding="utf-8")
    monkeypatch.setattr(tailnet, "find_exe", lambda configured="": "C:/ts/tailscale.exe")
    assert setupcheck.report(req)["tailscale"] == "C:/ts/tailscale.exe"

    def gone(configured=""):
        raise tailnet.TailscaleError("없음")
    monkeypatch.setattr(tailnet, "find_exe", gone)
    assert setupcheck.report(req)["tailscale"] is None


def test_setupcheck_finds_chromium_where_playwright_puts_it(tmp_path, monkeypatch):
    from syncnote import setupcheck

    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert not setupcheck.chromium_installed()
    (tmp_path / "chromium-1187").mkdir()
    assert setupcheck.chromium_installed()


def test_setupcheck_report_shape(tmp_path):
    from syncnote import setupcheck

    req = tmp_path / "r.txt"
    req.write_text("pytest>=1\nzz-not-installed-pkg\n", encoding="utf-8")
    rep = setupcheck.report(req)
    assert rep["reqs_bad"] == ["zz-not-installed-pkg"] and isinstance(rep["in_venv"], bool)


# -- reqcheck --import (B2) · deferred_install (A1) ----------------------------
def test_reqcheck_import_failure_is_broken(monkeypatch):
    monkeypatch.setattr(reqcheck, "try_import",
                        lambda m: "OSError: DLL load failed" if m == "pytest" else None)
    item = reqcheck.check_line("pytest>=1", imports=True)
    assert item["status"] == "broken" and "DLL load failed" in item["detail"]
    assert reqcheck.check_line("pytest>=1")["status"] == "ok"       # --import 없으면 버전만


def test_import_names_ignores_path_fragments(monkeypatch):
    monkeypatch.setattr(reqcheck.metadata, "packages_distributions",
                        lambda: {"PySide6/QtCore": ["PySide6"], "PySide6": ["PySide6"],
                                 "_priv": ["PySide6"], "faster_whisper": ["faster-whisper"]})
    assert reqcheck.import_names("PySide6") == ["PySide6"]
    assert reqcheck.import_names("faster-whisper") == ["faster_whisper"]
    assert reqcheck.import_names("unknown-pkg") == ["unknown_pkg"]


def test_deferred_install_parses_and_installs_then_relaunches(monkeypatch):
    from syncnote import deferred_install as D

    assert D.parse(["--pid", "42", "--force", "--relaunch", "--", "a>=1", "b"]) == (
        42, True, True, ["a>=1", "b"])
    assert D.pip_args(["a"], force=True)[-3:] == ["--force-reinstall", "--no-deps", "a"]
    calls = {}
    monkeypatch.setattr(D, "wait_pid", lambda pid, timeout=120.0: calls.setdefault("pid", pid) or True)
    monkeypatch.setattr(D.subprocess, "call", lambda args: calls.setdefault("pip", args) and 0)
    monkeypatch.setattr(D.subprocess, "Popen", lambda args, **k: calls.setdefault("relaunch", args))
    monkeypatch.setattr(D.time, "sleep", lambda s: None)
    assert D.main(["--pid", "7", "--relaunch", "--", "psutil>=5.9"]) == 0
    assert calls["pid"] == 7 and calls["pip"][-1] == "psutil>=5.9"
    assert "timetable" in " ".join(calls["relaunch"]) or "timetable_edit.bat" in " ".join(calls["relaunch"])


def test_deferred_install_waits_for_a_real_process():
    import subprocess as sp
    import sys as _sys

    from syncnote import deferred_install as D

    p = sp.Popen([_sys.executable, "-c", "import time; time.sleep(0.5)"])
    assert D.wait_pid(p.pid, timeout=10)
    assert D.wait_pid(0)


# -- 실패 원인 표 ----------------------------------------------------------------
def test_failure_table_classifies_known_outputs():
    from syncnote import setup_failures as SF_

    cases = {
        "[X] Python 이 설치돼 있지 않습니다.": ("python_missing", "auto"),
        "OSError: [WinError 32] 다른 프로세스가 파일을 사용 중": ("file_lock", "transient"),
        "pip._vendor.urllib3.exceptions.ReadTimeoutError: Read timed out.": ("network", "transient"),
        "ProxyError('Cannot connect to proxy.')": ("proxy", "transient"),
        "SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED]": ("ssl", "user"),
        "ERROR: No matching distribution found for ctranslate2": ("no_wheel", "fatal"),
        "OSError: [Errno 28] No space left on device": ("disk_full", "user"),
        "[WinError 206] The filename or extension is too long": ("long_path", "user"),
    }
    for text, (cid, kind) in cases.items():
        c = SF_.classify(text)
        assert (c.id, c.kind) == (cid, kind), text
    assert SF_.classify("모두 잘 됨", rc=0) is None
    assert SF_.classify("알 수 없는 오류", rc=1).id == "unknown"


def test_backoff_grows_and_caps():
    from syncnote.setup_failures import backoff

    assert [backoff(n) for n in (1, 2, 3, 4)] == [2, 4, 8, 16]
    assert backoff(10, cap=30) == 30


def test_empty_output_failure_is_unknown_not_stall():
    """'안 맞는' 패턴을 \\A\\Z 로 쓰면 빈 출력에 맞아, 말없이 끝난 실패를 '멈춤' 으로
    오인해 세 번 다시 돌렸습니다(실제로 그랬음)."""
    from syncnote import setup_failures as SF_

    assert SF_.classify("", rc=3).id == "unknown"
    assert SF_.classify("", rc=0) is None


# -- setup_history ----------------------------------------------------------------
def test_history_repeated_and_remedy_failed(tmp_path):
    from syncnote import setup_history as H

    p = tmp_path / "state" / "h.json"
    assert H.load(p) == []
    for cause in ("network", "network", "ssl"):
        H.append(p, {"install": {"state": "fail", "cause": cause, "remedies": ["chromium"]}})
    runs = H.load(p)
    assert [r["steps"]["install"]["cause"] for r in runs] == ["ssl", "network", "network"]
    assert H.repeated(runs, "install", "ssl") == 1                  # 가장 최근부터 연속만
    assert H.repeated(runs, "install", "network") == 0
    assert H.repeated(runs[1:], "install", "network") == 2
    assert H.remedy_failed(runs, "install", "chromium")
    assert not H.remedy_failed(runs, "install", "ffmpeg")
    for _ in range(30):
        H.append(p, {"install": {"state": "ok"}}, keep=5)
    assert len(H.load(p)) == 5


def test_history_survives_a_broken_file(tmp_path):
    from syncnote import setup_history as H

    p = tmp_path / "h.json"
    p.write_text("{깨짐", encoding="utf-8")
    assert H.load(p) == []
    assert len(H.append(p, {"verify": {"state": "ok"}})) == 1


def test_devices_json_probe_flag(capsys):
    cli.main(["devices", "--json", "--probe"])
    d = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert d["probed"] is True
    for g in d["groups"]:
        for dev in g["devices"]:
            if g["kind"] != "microphone":
                assert "dead" not in dev                    # 루프백·스피커는 재지 않음
