"""저장 뒤 설치·점검 흐름 창 + 저장 분기(config.toml 있음/없음). PySide6 필요."""

import json
import os
import sys
import textwrap
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QDialog, QMessageBox  # noqa: E402

from syncnote.timetable import (
    eclass_fetch,  # noqa: E402
    gui,  # noqa: E402
)
from syncnote.timetable import model as M  # noqa: E402
from syncnote.timetable import setup_flow as SF  # noqa: E402


@pytest.fixture(scope="module")
def app(qt_app):
    return qt_app


def _py(tmp_path, name, body):
    f = tmp_path / f"{name}.py"
    f.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(f)]


DOCTOR = {"ok": False, "info": [["Python", "3.12"], ["설정 파일", "x"]], "config_error": None,
          "sections": [{"title": "파이썬 패키지", "items": [
              {"mark": "O", "name": "requests", "detail": "GitHub 업로드"},
              {"mark": "X", "name": "soundcard", "detail": "시스템 소리 캡처 — pip install soundcard"}]}],
          "verdict": "위의 [X] 항목을 해결하세요."}
DEVICES = {"audio_error": None, "monitors": [{"index": 1, "text": "1: 1920x1080", "primary": True}],
           "groups": [{"kind": "loopback", "title": "시스템 소리 캡처용",
                       "config_key": "recording.audio.system_device",
                       "devices": [{"name": "스피커(Realtek)", "default": True}]},
                      {"kind": "microphone", "title": "마이크",
                       "config_key": "recording.audio.mic_device",
                       "devices": [{"name": "마이크 배열", "default": True}]},
                      {"kind": "speaker", "title": "재생 장치", "config_key": "", "devices": []}]}


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    m = tmp_path / "m"
    m.mkdir()
    cmds = {
        "setup": _py(tmp_path, "setup", f"""
            open(r"{m / 'venv'}", "w").write("ok")          # .venv 가 생긴 것처럼
            open(r"{m / 'setup_ran'}", "a").write("x")
            print("[1/5] 가상환경 만드는 중...")
            print("[2/5] 파이썬 패키지 설치 중...")
            print(" 설치 완료. 이제 다음 순서로 진행하세요.")
        """),
        "state": _py(tmp_path, "state", f"""
            import json, os
            e = lambda n: os.path.exists(os.path.join(r"{m}", n))
            print(json.dumps({{"python": "x", "in_venv": e("venv"),
                              "reqs_bad": [] if e("installed") else ["psutil"],
                              "ffmpeg": None if e("no_ffmpeg") and not e("ffmpeg_fixed")
                                        else "ffmpeg.exe",
                              "chromium": not e("no_chromium") or e("chromium_fixed"),
                              "tailscale": None if e("no_tailscale") and not e("tailscale_fixed")
                                           else "tailscale.exe"}}))
        """),
        "reqcheck": _py(tmp_path, "reqcheck", f"""
            import json, os
            done = os.path.exists(r"{m / 'installed'}")
            items = [{{"line": "requests>=2", "name": "requests", "spec": ">=2", "status": "ok",
                      "installed": "2.3", "detail": "2.3"}},
                     {{"line": "psutil>=5.9", "name": "psutil", "spec": ">=5.9",
                      "status": "ok" if done else "missing",
                      "installed": "6.0" if done else None,
                      "detail": "6.0" if done else "설치 안 됨"}}]
            print(json.dumps({{"python": "x", "items": items}}, ensure_ascii=False))
        """),
        "pip": _py(tmp_path, "pip", f"""
            import sys
            open(r"{m / 'installed'}", "w").write(" ".join(sys.argv[1:]))
            print("Successfully installed psutil-6.0")
        """),
        "status": _py(tmp_path, "status", f"""
            import json, os
            ok = os.path.exists(r"{m / 'pw'}")
            print(json.dumps({{"eclass": "OS 자격증명 저장소" if ok else "없음", "github": "없음"}},
                             ensure_ascii=False))
        """),
        "password": _py(tmp_path, "password", f"""
            open(r"{m / 'pw'}", "w").write("ok")
        """),
        "doctor": _py(tmp_path, "doctor", f"""
            print("echo 줄")
            print({json.dumps(json.dumps(DOCTOR, ensure_ascii=False))})
            print("Press any key to continue . . .")
        """),
        "playwright": _py(tmp_path, "playwright", f"""
            open(r"{m / 'chromium_fixed'}", "w").write("x")
            open(r"{m / 'playwright_ran'}", "a").write("x")
            print("Downloading Chromium ... done")
        """),
        "ffmpeg": _py(tmp_path, "ffmpeg", f"""
            open(r"{m / 'ffmpeg_ran'}", "a").write("x")
            print("ffmpeg 내려받기 실패(가짜)")
        """),
        "tailscale": _py(tmp_path, "tailscale", f"""
            open(r"{m / 'tailscale_fixed'}", "w").write("x")
            print("Successfully installed Tailscale (가짜 winget)")
        """),
        "venv_create": _py(tmp_path, "venv_create", f"""
            open(r"{m / 'venv'}", "w").write("x")
        """),
        "devices": _py(tmp_path, "devices", f"""
            print({json.dumps(json.dumps(DEVICES, ensure_ascii=False))})
        """),
    }
    F = SF.SetupFlow
    monkeypatch.setattr(F, "setup_command", lambda self: cmds["setup"])
    monkeypatch.setattr(F, "state_command", lambda self: cmds["state"])
    monkeypatch.setattr(F, "venv_exists", lambda self: (m / "venv").exists())
    monkeypatch.setattr(F, "reqcheck_command", lambda self: cmds["reqcheck"])
    monkeypatch.setattr(F, "pip_install_command", lambda self, lines: cmds["pip"] + lines)
    monkeypatch.setattr(F, "status_command", lambda self: cmds["status"])
    monkeypatch.setattr(F, "password_command", lambda self: cmds["password"])
    monkeypatch.setattr(F, "doctor_command", lambda self: cmds["doctor"])
    monkeypatch.setattr(F, "devices_command", lambda self: cmds["devices"])
    monkeypatch.setattr(F, "playwright_command", lambda self: cmds["playwright"])
    monkeypatch.setattr(F, "ffmpeg_command", lambda self: cmds["ffmpeg"])
    monkeypatch.setattr(F, "venv_create_command", lambda self: cmds["venv_create"])
    monkeypatch.setattr(F, "tailscale_command", lambda self: cmds["tailscale"])
    monkeypatch.setattr(F, "BACKOFF_BASE", 0.01)
    # 시도 이력은 임시 폴더에(실제 data_dir 에 쓰지 않게)
    monkeypatch.setattr(F, "history_path", lambda self: tmp_path / "setup_history.json")
    monkeypatch.setattr(eclass_fetch, "NEW_CONSOLE", 0)
    return m


def _run(app, dlg, seconds=40):
    dlg.show()
    dlg.start()
    end = time.monotonic() + seconds
    while time.monotonic() < end and dlg.close_btn.text() != "닫기":
        app.processEvents()
        time.sleep(0.02)


def test_flow_installs_verifies_fixes_asks_password_and_shows_results(app, tmp_path, fakes):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[recording.audio]\nsystem_device = "Realtek"\n', encoding="utf-8")
    dlg = SF.SetupFlow(None, cfg)
    _run(app, dlg)
    log = dlg.log.toPlainText()
    assert dlg.close_btn.text() == "닫기", log
    assert dlg.state == {"install": "ok", "verify": "ok", "password": "ok", "check": "warn"}, log
    assert (fakes / "installed").read_text(encoding="utf-8") == "psutil>=5.9"       # 빠진 줄만 설치
    assert (fakes / "pw").exists() and dlg._pw_rounds == 1          # 비밀번호 창이 열렸음
    assert "설치 완료" in log and "Press any key" in log
    assert dlg.stack.currentIndex() == 1                             # 결과 화면
    page = dlg.result_scroll.widget()
    texts = " ".join(w.text() for w in page.findChildren(gui.QLabel))
    assert "soundcard" in texts and "위의 [X] 항목을 해결하세요" in texts
    assert "스피커(Realtek)" in texts and "지금 설정과 일치" in texts
    assert "monitor = 1" in texts
    check = dlg.checklist()
    assert [c[0] for c in check] == ["X", "O", "X", "O"]             # 설정·비번·점검·장치
    assert "github.owner" in check[0][2]
    dlg.close()


def test_flow_keeps_going_when_a_step_fails(app, tmp_path, fakes, monkeypatch):
    monkeypatch.setattr(SF.SetupFlow, "setup_command",
                        lambda self: [sys.executable, "-c", "import sys; sys.exit(3)"])
    monkeypatch.setattr(SF.SetupFlow, "doctor_command",
                        lambda self: [sys.executable, "-c", "print('깨진 출력')"])
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert dlg.state["install"] == "fail" and ".venv" in dlg.notes["install"]
    assert dlg.state["check"] == "fail" and dlg.close_btn.text() == "닫기"
    dlg.close()


def test_password_still_missing_is_a_warning_not_a_loop(app, tmp_path, fakes, monkeypatch):
    monkeypatch.setattr(SF.SetupFlow, "password_command",
                        lambda self: [sys.executable, "-c", "pass"])     # 저장 안 함
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert dlg.state["password"] == "warn" and dlg._pw_rounds == 1
    dlg.close()


def test_last_json_skips_bat_noise():
    assert SF.last_json('echo\n{"a": 1}\nPress any key . . .\n') == {"a": 1}
    assert SF.last_json("no json here") is None


# -- 저장 분기 ---------------------------------------------------------------


@pytest.fixture
def win_factory(app, tmp_path, monkeypatch):
    calls = {"flow": 0, "warn": []}
    monkeypatch.setattr(SF.SetupFlow, "exec",
                        lambda self: calls.__setitem__("flow", calls["flow"] + 1) or 1)
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda _p, title, text, *a, **k: calls["warn"].append(text)))
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Discard)
    example = tmp_path / "config.example.toml"
    example.write_text('[general]\n# 로그 수준\nlog_level = "INFO"\n# 보관 일수\nlog_retention_days = 30\n',
                       encoding="utf-8")
    monkeypatch.setattr(gui.TimetableWindow, "_example_path", lambda self: example)
    monkeypatch.setattr(gui.TimetableWindow, "SETUP_AFTER_SAVE", True)
    made = []

    def make():
        w = gui.TimetableWindow(tmp_path / "config.toml")
        new = w.tt.copy()
        new.courses.append(M.Course("자료구조", ["mon"], "09:00", "10:00"))
        w._apply(new, "t")
        made.append(w)
        return w
    yield make, calls
    for w in made:
        w.undo.setClean()
        w.close()


def test_branch1_missing_settings_warns_opens_settings_fills_then_setup(app, tmp_path, win_factory,
                                                                        monkeypatch):
    from syncnote.timetable.config_wizard import SettingsDialog

    make, calls = win_factory
    (tmp_path / "config.toml").write_text('[general]\nlog_level = "DEBUG"\n', encoding="utf-8")
    seen = {}

    def accept(self):
        seen["fill"], seen["missing"] = self.fill_missing, list(self.missing)
        seen["first"] = [f.dotted for f in self._first_fields()]
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(SettingsDialog, "exec", accept)
    w = make()
    assert w.missing_settings() == ["general.log_retention_days"]
    assert w.save(show_summary=False)
    assert calls["warn"] and "1개" in calls["warn"][0]
    assert seen == {"fill": True, "missing": ["general.log_retention_days"],
                    "first": ["general.log_retention_days"]}
    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert 'log_level = "DEBUG"' in text and "log_retention_days = 30" in text    # 예시 값으로 채움
    assert M.BEGIN in text and calls["flow"] == 1                                 # 시간표 → 설치·점검
    assert w.missing_settings() == []


def test_branch1_nothing_missing_goes_straight_to_setup(app, tmp_path, win_factory, monkeypatch):
    from syncnote.timetable.config_wizard import SettingsDialog

    make, calls = win_factory
    (tmp_path / "config.toml").write_text(
        '[general]\nlog_level = "INFO"\nlog_retention_days = 7\n', encoding="utf-8")
    monkeypatch.setattr(SettingsDialog, "exec", lambda self: pytest.fail("설정 창을 열면 안 됨"))
    w = make()
    assert w.save(show_summary=False)
    assert not calls["warn"] and calls["flow"] == 1


def test_branch2_no_config_settings_then_setup(app, tmp_path, win_factory, monkeypatch):
    from syncnote.timetable.config_wizard import SettingsDialog

    make, calls = win_factory
    monkeypatch.setattr(SettingsDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    w = make()
    assert not w.path.exists()
    assert w.save(show_summary=False)
    assert "log_retention_days = 30" in w.path.read_text(encoding="utf-8")
    assert calls["flow"] == 1 and not calls["warn"]


def test_branch2_cancel_creates_nothing_and_skips_setup(app, tmp_path, win_factory, monkeypatch):
    from syncnote.timetable.config_wizard import SettingsDialog

    make, calls = win_factory
    monkeypatch.setattr(SettingsDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    w = make()
    assert not w.save(show_summary=False)
    assert not w.path.exists() and calls["flow"] == 0


def test_saving_while_closing_skips_setup(app, tmp_path, win_factory, monkeypatch):
    make, calls = win_factory
    (tmp_path / "config.toml").write_text(
        '[general]\nlog_level = "INFO"\nlog_retention_days = 7\n', encoding="utf-8")
    w = make()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Save)
    assert w._confirm_discard()
    assert calls["flow"] == 0 and w.undo.isClean()


def test_file_menu_can_rerun_setup(app, tmp_path, win_factory):
    make, calls = win_factory
    w = make()
    acts = {a.text(): a for m in w.menuBar().actions() for a in m.menu().actions()}
    acts["설치 · 점검 다시 실행…"].trigger()
    assert calls["flow"] == 1


# -- 1. 확인 단계 + 결과 검증 ------------------------------------------------
def test_already_installed_skips_setup_bat(app, tmp_path, fakes):
    (fakes / "venv").write_text("ok", encoding="utf-8")
    (fakes / "installed").write_text("ok", encoding="utf-8")
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert dlg.state["install"] == "ok" and "생략" in dlg.notes["install"]
    assert not (fakes / "setup_ran").exists()
    assert dlg.state["verify"] == "ok"
    dlg.close()


def test_partially_installed_runs_setup_and_reports_what_is_still_missing(app, tmp_path, fakes):
    (fakes / "venv").write_text("ok", encoding="utf-8")
    (fakes / "installed").write_text("ok", encoding="utf-8")
    (fakes / "no_ffmpeg").write_text("ok", encoding="utf-8")       # setup 이 못 고침
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert (fakes / "setup_ran").exists()
    assert (fakes / "ffmpeg_ran").exists()                          # ffmpeg 만 따로 다시 시도
    assert dlg.state["install"] == "warn" and "ffmpeg" in dlg.notes["install"]
    dlg.close()


def test_missing_tailscale_is_installed_with_winget(app, tmp_path, fakes):
    (fakes / "venv").write_text("ok", encoding="utf-8")
    (fakes / "installed").write_text("ok", encoding="utf-8")
    (fakes / "no_tailscale").write_text("ok", encoding="utf-8")    # setup.bat 이 못 깔았음
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert (fakes / "setup_ran").exists() and (fakes / "tailscale_fixed").exists()
    assert dlg.state["install"] == "ok"
    dlg.close()


def test_tailscale_command_is_winget_on_windows():
    cmd = SF.SetupFlow.tailscale_command(None)
    if os.name == "nt":
        assert cmd[:4] == ["winget", "install", "--id", "Tailscale.Tailscale"]
    else:
        assert cmd is None


def test_nonzero_exit_but_actually_installed_counts_as_ok(app, tmp_path, fakes, monkeypatch):
    m = fakes
    monkeypatch.setattr(SF.SetupFlow, "setup_command", lambda self: [
        sys.executable, "-c",
        f"open(r'{m / 'venv'}','w').write('x'); open(r'{m / 'installed'}','w').write('x');"
        "import sys; sys.exit(1)"])
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert dlg.state["install"] == "ok" and "종료 코드 1" in dlg.notes["install"]
    dlg.close()


def test_zero_exit_without_venv_counts_as_failure(app, tmp_path, fakes, monkeypatch):
    monkeypatch.setattr(SF.SetupFlow, "setup_command",
                        lambda self: [sys.executable, "-c", "print('설치 완료')"])
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert dlg.state["install"] == "fail" and "만들어지지 않음" in dlg.notes["install"]
    dlg.close()


# -- 2. 편집기가 쓰는 패키지(A1) · 깨진 설치(B2) -------------------------------
def _reqcheck_fake(tmp_path, m, items_before, items_after, name="rq2"):
    return _py(tmp_path, name, f"""
        import json, os
        after = os.path.exists(r"{m / 'fixed'}")
        items = {items_after!r} if after else {items_before!r}
        print(json.dumps({{"python": "x", "items": items}}, ensure_ascii=False))
    """)


def _item(name, status, line=None):
    return {"line": line or f"{name}>=1", "name": name, "spec": ">=1", "status": status,
            "installed": "1" if status != "missing" else None, "detail": status}


def _pip_fake(tmp_path, m, out="ok", rc=0):
    return _py(tmp_path, "pip2", f"""
        import sys
        open(r"{m / 'pip_args'}", "a", encoding="utf-8").write(" ".join(sys.argv[1:]) + "\\n")
        open(r"{m / 'fixed'}", "w").write("x")
        print({out!r})
        sys.exit({rc})
    """)


def test_broken_package_is_force_reinstalled_once(app, tmp_path, fakes, monkeypatch):
    m = fakes
    (m / "venv").write_text("x", encoding="utf-8")
    (m / "installed").write_text("x", encoding="utf-8")
    rq = _reqcheck_fake(tmp_path, m, [_item("numpy", "broken")], [_item("numpy", "ok")])
    monkeypatch.setattr(SF.SetupFlow, "reqcheck_command", lambda self: rq)
    pip = _pip_fake(tmp_path, m)
    monkeypatch.setattr(SF.SetupFlow, "pip_install_command",
                        lambda self, lines, force=False: pip + (["--force"] if force else []) + lines)
    monkeypatch.setattr(SF.SetupFlow, "editor_holds", lambda self, d: False)
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert dlg.state["verify"] == "ok"
    assert (m / "pip_args").read_text(encoding="utf-8").strip() == "--force numpy>=1"
    dlg.close()


def test_package_held_by_editor_is_deferred_not_installed(app, tmp_path, fakes, monkeypatch):
    m = fakes
    (m / "venv").write_text("x", encoding="utf-8")
    (m / "installed").write_text("x", encoding="utf-8")
    rq = _reqcheck_fake(tmp_path, m, [_item("PySide6", "mismatch", "PySide6>=9"),
                                      _item("psutil", "missing")],
                        [_item("PySide6", "mismatch", "PySide6>=9"), _item("psutil", "ok")])
    monkeypatch.setattr(SF.SetupFlow, "reqcheck_command", lambda self: rq)
    pip = _pip_fake(tmp_path, m)
    monkeypatch.setattr(SF.SetupFlow, "pip_install_command",
                        lambda self, lines, force=False: pip + lines)
    monkeypatch.setattr(SF.SetupFlow, "editor_holds", lambda self, d: d == "PySide6")
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert (m / "pip_args").read_text(encoding="utf-8").split() == ["psutil>=1"]   # PySide6 은 안 건드림
    assert dlg.state["verify"] == "warn" and dlg.deferred == ["PySide6>=9"]
    assert "닫은 뒤 설치" in dlg.notes["verify"]
    # '편집기를 닫고 설치' → 별도 콘솔을 띄우고 편집기 종료
    launched, quit_ = {}, []
    monkeypatch.setattr(SF.subprocess, "Popen", lambda cmd, **k: launched.setdefault("cmd", cmd))
    monkeypatch.setattr(SF.QApplication, "instance",
                        staticmethod(lambda: type("A", (), {"quit": lambda s: quit_.append(1)})()))
    dlg.deferred_btn.click()
    app.processEvents()
    cmd = " ".join(launched["cmd"])
    assert "syncnote.deferred_install" in cmd and "--relaunch" in cmd and cmd.endswith("PySide6>=9")
    assert f"--pid {os.getpid()}" in cmd and quit_


def test_pip_lock_error_moves_package_to_after_close(app, tmp_path, fakes, monkeypatch):
    m = fakes
    (m / "venv").write_text("x", encoding="utf-8")
    (m / "installed").write_text("x", encoding="utf-8")
    rq = _reqcheck_fake(tmp_path, m, [_item("numpy", "mismatch")], [_item("numpy", "mismatch")])
    monkeypatch.setattr(SF.SetupFlow, "reqcheck_command", lambda self: rq)
    pip = _pip_fake(tmp_path, m, out="ERROR: [WinError 32] 다른 프로세스가 파일을 사용 중", rc=1)
    monkeypatch.setattr(SF.SetupFlow, "pip_install_command",
                        lambda self, lines, force=False: pip + lines)
    monkeypatch.setattr(SF.SetupFlow, "editor_holds", lambda self, d: False)
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert dlg.deferred == ["numpy>=1"] and "잠겨" in dlg.log.toPlainText()
    dlg.close()


def test_still_broken_after_reinstall_suggests_vc_redist(app, tmp_path, fakes, monkeypatch):
    m = fakes
    (m / "venv").write_text("x", encoding="utf-8")
    (m / "installed").write_text("x", encoding="utf-8")
    rq = _reqcheck_fake(tmp_path, m, [_item("numpy", "broken")], [_item("numpy", "broken")])
    monkeypatch.setattr(SF.SetupFlow, "reqcheck_command", lambda self: rq)
    pip = _pip_fake(tmp_path, m)
    monkeypatch.setattr(SF.SetupFlow, "pip_install_command",
                        lambda self, lines, force=False: pip + lines)
    monkeypatch.setattr(SF.SetupFlow, "editor_holds", lambda self, d: False)
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert dlg.need_vcredist and "VC++" in dlg.notes["verify"]
    texts = " ".join(w.text() for w in dlg.result_scroll.widget().findChildren(gui.QLabel))
    assert "vc_redist" in texts
    dlg.close()


def test_editor_holds_only_when_running_from_the_same_venv(app, tmp_path, monkeypatch):
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    monkeypatch.setattr(SF.SetupFlow, "editor_in_venv", lambda self: False)
    assert not dlg.editor_holds("PySide6")
    monkeypatch.setattr(SF.SetupFlow, "editor_in_venv", lambda self: True)
    assert dlg.editor_holds("PySide6")             # 이 테스트 프로세스에 PySide6 가 올라와 있음
    assert not dlg.editor_holds("zz-not-loaded")
    dlg.close()


# -- 3. 원인 분류 + 일시적 실패 백오프 + 멈춤 감지 -----------------------------
def _counting_setup(tmp_path, m, fail_times, fail_text, sleep=0.0):
    return _py(tmp_path, "setup_n", f"""
        import os, sys, time
        n_file = r"{m / 'setup_n'}"
        n = int(open(n_file).read()) if os.path.exists(n_file) else 0
        open(n_file, "w").write(str(n + 1))
        if n < {fail_times}:
            time.sleep({sleep})
            print({fail_text!r})
            sys.exit(1)
        open(r"{m / 'venv'}", "w").write("x")
        open(r"{m / 'installed'}", "w").write("x")
        print("설치 완료")
    """)


def test_transient_network_failure_is_retried_with_backoff(app, tmp_path, fakes, monkeypatch):
    s = _counting_setup(tmp_path, fakes, 2, "pip._vendor.urllib3: Read timed out.")
    monkeypatch.setattr(SF.SetupFlow, "setup_command", lambda self: s)
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert (fakes / "setup_n").read_text(encoding="utf-8") == "3"
    assert dlg.state["install"] == "ok" and dlg.attempts["install"] == 3
    assert "네트워크" in dlg.log.toPlainText() and "3/3회째" in dlg.log.toPlainText()
    dlg.close()


def test_transient_failure_gives_up_after_max_tries_with_reason(app, tmp_path, fakes, monkeypatch):
    s = _counting_setup(tmp_path, fakes, 99, "ConnectTimeoutError: connection timed out")
    monkeypatch.setattr(SF.SetupFlow, "setup_command", lambda self: s)
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert (fakes / "setup_n").read_text(encoding="utf-8") == "3"            # 최대 3번
    assert dlg.state["install"] == "fail"
    assert "네트워크" in dlg.notes["install"] and "3번 시도" in dlg.notes["install"]
    dlg.close()


def test_fatal_cause_is_not_retried(app, tmp_path, fakes, monkeypatch):
    s = _counting_setup(tmp_path, fakes, 99, "ERROR: No matching distribution found for x")
    monkeypatch.setattr(SF.SetupFlow, "setup_command", lambda self: s)
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert (fakes / "setup_n").read_text(encoding="utf-8") == "1"
    assert dlg.state["install"] == "fail" and "3.11" in dlg.notes["install"]
    dlg.close()


def test_stalled_process_is_killed_and_retried(app, tmp_path, fakes, monkeypatch):
    s = _counting_setup(tmp_path, fakes, 1, "", sleep=30)          # 첫 번째는 말없이 멈춤
    monkeypatch.setattr(SF.SetupFlow, "setup_command", lambda self: s)
    monkeypatch.setattr(SF.SetupFlow, "TIMEOUTS", {**SF.SetupFlow.TIMEOUTS, "setup": (20, 1.0)})
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert "출력 없이" in dlg.log.toPlainText()
    assert dlg.state["install"] == "ok" and dlg.attempts["install"] == 2
    dlg.close()


def test_python_missing_creates_venv_with_editor_python_then_reruns(app, tmp_path, fakes,
                                                                    monkeypatch):
    m = fakes
    s = _py(tmp_path, "setup_py", f"""
        import os, sys
        if not os.path.exists(r"{m / 'venv'}"):
            print("[X] Python 이 설치돼 있지 않습니다.")
            sys.exit(1)
        open(r"{m / 'installed'}", "w").write("x")
        print("설치 완료")
    """)
    monkeypatch.setattr(SF.SetupFlow, "setup_command", lambda self: s)
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert dlg.state["install"] == "ok", dlg.log.toPlainText()
    assert "venv" in dlg._remedies and "파이썬을 찾지 못함" in dlg.log.toPlainText()
    dlg.close()


def test_only_chromium_missing_reinstalls_just_chromium(app, tmp_path, fakes):
    for n in ("venv", "installed", "no_chromium"):
        (fakes / n).write_text("x", encoding="utf-8")
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert (fakes / "playwright_ran").exists() and not (fakes / "ffmpeg_ran").exists()
    assert dlg.state["install"] == "ok"
    dlg.close()


def test_pip_transient_failure_is_retried(app, tmp_path, fakes, monkeypatch):
    m = fakes
    (m / "venv").write_text("x", encoding="utf-8")
    (m / "installed").write_text("x", encoding="utf-8")
    rq = _reqcheck_fake(tmp_path, m, [_item("psutil", "missing")], [_item("psutil", "ok")])
    monkeypatch.setattr(SF.SetupFlow, "reqcheck_command", lambda self: rq)
    pip = _py(tmp_path, "pip_n", f"""
        import os, sys
        f = r"{m / 'pip_n'}"
        n = int(open(f).read()) if os.path.exists(f) else 0
        open(f, "w").write(str(n + 1))
        if n == 0:
            print("WARNING: Retrying ... ProxyError('Cannot connect to proxy.')")
            sys.exit(1)
        open(r"{m / 'fixed'}", "w").write("x")
    """)
    monkeypatch.setattr(SF.SetupFlow, "pip_install_command",
                        lambda self, lines, force=False: pip + lines)
    monkeypatch.setattr(SF.SetupFlow, "editor_holds", lambda self, d: False)
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert (m / "pip_n").read_text(encoding="utf-8") == "2" and dlg.state["verify"] == "ok"
    dlg.close()


# -- 4. 사용자가 할 일로 이어 주기(D1·C1) ---------------------------------------
def _doctor_fake(tmp_path, m):
    bad = {**DOCTOR, "ok": False, "config_error": "설정 오류 1건:\n  - eclass.username 이 비어 있습니다",
           "sections": [], "verdict": "설정 파일을 읽지 못해 점검을 멈췄습니다."}
    good = {**DOCTOR, "ok": True, "verdict": "모두 정상입니다."}
    return _py(tmp_path, "doctor_cfg", f"""
        import json, os
        fixed = os.path.exists(r"{m / 'cfg_fixed'}")
        print(json.dumps({good!r} if fixed else {bad!r}, ensure_ascii=False))
    """)


def test_config_error_opens_settings_then_reruns_doctor(app, tmp_path, fakes, monkeypatch):
    m = fakes
    for n in ("venv", "installed", "pw"):
        (m / n).write_text("x", encoding="utf-8")
    d = _doctor_fake(tmp_path, m)
    monkeypatch.setattr(SF.SetupFlow, "doctor_command", lambda self: d)
    calls = []

    def fix():
        calls.append(1)
        (m / "cfg_fixed").write_text("x", encoding="utf-8")
        return True
    dlg = SF.SetupFlow(None, tmp_path / "config.toml", fix_config=fix)
    _run(app, dlg)
    assert calls == [1] and dlg.state["check"] == "ok"
    assert "설정 창을 엽니다" in dlg.log.toPlainText() and "eclass.username" in dlg.log.toPlainText()
    dlg.close()


def test_config_error_not_fixed_continues_once_without_loop(app, tmp_path, fakes, monkeypatch):
    m = fakes
    for n in ("venv", "installed", "pw"):
        (m / n).write_text("x", encoding="utf-8")
    d = _doctor_fake(tmp_path, m)
    monkeypatch.setattr(SF.SetupFlow, "doctor_command", lambda self: d)
    calls = []
    dlg = SF.SetupFlow(None, tmp_path / "config.toml", fix_config=lambda: calls.append(1) or False)
    _run(app, dlg)
    assert calls == [1] and dlg.state["check"] == "warn"
    assert isinstance(dlg.devices, dict)                       # 장치 목록은 그래도 받음
    dlg.close()


def test_hidden_password_window_gets_a_reopen_button(app, tmp_path, fakes, monkeypatch):
    m = fakes
    for n in ("venv", "installed"):
        (m / n).write_text("x", encoding="utf-8")
    slow = _py(tmp_path, "pw_slow", f"""
        import os, time
        f = r"{m / 'pw_launch'}"
        n = int(open(f).read()) if os.path.exists(f) else 0
        open(f, "w").write(str(n + 1))
        if n == 0:
            time.sleep(30)               # 첫 창은 '숨어서' 아무도 입력 안 함
        open(r"{m / 'pw'}", "w").write("ok")
    """)
    monkeypatch.setattr(SF.SetupFlow, "password_command", lambda self: slow)
    monkeypatch.setattr(SF.SetupFlow, "PASSWORD_NUDGE", 0.3)
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    dlg.show()
    dlg.start()
    end = time.monotonic() + 20
    while time.monotonic() < end and not dlg.reopen_btn.isVisible():
        app.processEvents()
        time.sleep(0.02)
    assert dlg.reopen_btn.isVisible() and "안 보이나요" in dlg.headline.text()
    dlg.reopen_btn.click()
    end = time.monotonic() + 30
    while time.monotonic() < end and dlg.close_btn.text() != "닫기":
        app.processEvents()
        time.sleep(0.02)
    assert (m / "pw_launch").read_text(encoding="utf-8") == "2"
    assert dlg.state["password"] == "ok" and dlg._pw_rounds == 1     # 다시 열기는 회차로 안 셈
    assert not dlg.reopen_btn.isVisible()
    dlg.close()


def test_window_passes_settings_editor_to_the_flow(app, tmp_path, win_factory, monkeypatch):
    make, _calls = win_factory
    seen = {}
    monkeypatch.setattr(SF.SetupFlow, "exec",
                        lambda self: seen.setdefault("fix", self.fix_config) and 1)
    w = make()
    w.run_setup_flow()
    assert seen["fix"] == w.edit_settings


# -- 5. 시도 이력·반복 차단 + 마이크 실측(D3) --------------------------------
def _seed_history(tmp_path, runs):
    (tmp_path / "setup_history.json").write_text(json.dumps({"runs": runs}), encoding="utf-8")


def test_same_failure_twice_before_skips_auto_retry(app, tmp_path, fakes, monkeypatch):
    bad = {"steps": {"install": {"state": "fail", "cause": "network", "remedies": []}}}
    _seed_history(tmp_path, [bad, bad])
    s = _counting_setup(tmp_path, fakes, 99, "ConnectTimeoutError: connection timed out")
    monkeypatch.setattr(SF.SetupFlow, "setup_command", lambda self: s)
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert (fakes / "setup_n").read_text(encoding="utf-8") == "1"          # 다시 안 함
    assert dlg.state["install"] == "fail" and "지난번에도" in dlg.notes["install"]
    runs = json.loads((tmp_path / "setup_history.json").read_text(encoding="utf-8"))["runs"]
    assert len(runs) == 3 and runs[0]["steps"]["install"]["cause"] == "network"
    dlg.close()


def test_one_previous_failure_still_retries(app, tmp_path, fakes, monkeypatch):
    bad = {"steps": {"install": {"state": "fail", "cause": "network", "remedies": []}}}
    _seed_history(tmp_path, [bad])
    s = _counting_setup(tmp_path, fakes, 1, "Read timed out")
    monkeypatch.setattr(SF.SetupFlow, "setup_command", lambda self: s)
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert (fakes / "setup_n").read_text(encoding="utf-8") == "2" and dlg.state["install"] == "ok"
    dlg.close()


def test_remedy_that_did_not_help_last_time_is_skipped(app, tmp_path, fakes):
    _seed_history(tmp_path, [{"steps": {"install": {"state": "warn", "cause": None,
                                                    "remedies": ["chromium"]}}}])
    for n in ("venv", "installed", "no_chromium"):
        (fakes / n).write_text("x", encoding="utf-8")
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert not (fakes / "playwright_ran").exists()
    assert "지난번에도 해결하지 못해" in dlg.log.toPlainText()
    assert dlg.state["install"] == "warn"
    dlg.close()


def _devices_with_mics(tmp_path, mics, name="dev_mics"):
    d = json.loads(json.dumps(DEVICES))
    d["probed"] = True
    d["groups"][1]["devices"] = mics
    return _py(tmp_path, name, f"""
        print({json.dumps(json.dumps(d, ensure_ascii=False))})
    """)


def _ready(m):
    for n in ("venv", "installed", "pw"):
        (m / n).write_text("x", encoding="utf-8")


def test_silent_default_mic_is_flagged_with_a_working_alternative(app, tmp_path, fakes, monkeypatch):
    _ready(fakes)
    dv = _devices_with_mics(tmp_path, [
        {"name": "마이크 배열(인텔)", "default": False, "peak": 0.0034, "dead": False, "error": None},
        {"name": "마이크 배열(Audio Share)", "default": True, "peak": 0.0, "dead": True, "error": None}])
    monkeypatch.setattr(SF.SetupFlow, "devices_command", lambda self: dv)
    monkeypatch.setattr(SF.SetupFlow, "doctor_command", lambda self: _py(
        tmp_path, "doc_ok", f"print({json.dumps(json.dumps({**DOCTOR, 'ok': True}))})"))
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    mark, text = dlg.mic_status()
    assert mark == "X" and "Audio Share" in text and "인텔" in text
    assert dlg.state["check"] == "warn" and dlg.checklist()[3][0] == "X"
    texts = " ".join(w.text() for w in dlg.result_scroll.widget().findChildren(gui.QLabel))
    assert "무음" in texts and "소리 들어옴" in texts
    dlg.close()


def test_configured_mic_overrides_default_and_all_silent_means_permission(app, tmp_path, fakes,
                                                                         monkeypatch):
    _ready(fakes)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[recording.audio]\nmic_device = "인텔"\n', encoding="utf-8")
    alive = [{"name": "마이크 배열(인텔)", "default": False, "peak": 0.01, "dead": False, "error": None},
             {"name": "마이크 배열(Audio Share)", "default": True, "peak": 0.0, "dead": True,
              "error": None}]
    monkeypatch.setattr(SF.SetupFlow, "devices_command", lambda self: _devices_with_mics(tmp_path, alive))
    dlg = SF.SetupFlow(None, cfg)
    _run(app, dlg)
    assert dlg.mic_status()[0] == "O"                    # 설정한 인텔 마이크는 살아 있음
    dlg.close()
    dead = [{**m, "dead": True, "peak": 0.0} for m in alive]
    monkeypatch.setattr(SF.SetupFlow, "devices_command",
                        lambda self: _devices_with_mics(tmp_path, dead, "dev_dead"))
    dlg = SF.SetupFlow(None, cfg)
    _run(app, dlg)
    mark, text = dlg.mic_status()
    assert mark == "X" and ("권한" in text or "개인 정보" in text)
    dlg.close()


def test_ffmpeg_next_action_hint_reaches_the_install_note(app, tmp_path, fakes, monkeypatch):
    (fakes / "venv").write_text("ok", encoding="utf-8")
    (fakes / "installed").write_text("ok", encoding="utf-8")
    (fakes / "no_ffmpeg").write_text("ok", encoding="utf-8")
    hint = _py(tmp_path, "ffmpeg_hint", """
        print("[X] ffmpeg 를 설치하지 못했습니다. 시도한 순서와 이유:")
        print("다음 할 일: USB 로 zip 을 tools 폴더에 넣고 다시 실행하세요.")
    """)
    monkeypatch.setattr(SF.SetupFlow, "ffmpeg_command", lambda self: hint)
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert dlg.state["install"] == "warn"
    assert "다음 할 일: ffmpeg — USB 로 zip" in dlg.notes["install"]
    dlg.close()
