"""작업 스케줄러 등록 확인·자동 등록(src/syncnote/tasks.py).

실제 강의실 PC 하나는 register_tasks.ps1 을 아무도 실행하지 않아 작업이 하나도 없었고, 그래서
녹화가 한 번도 돌지 않았습니다. 사용자 요청: setup.bat·doctor.bat 에서 감지하고, 모든 스크립트·
프로그램이 실행될 때 자동 등록.

PowerShell 은 가짜로 바꿉니다 — 실제 작업 스케줄러는 건드리지 않습니다.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from syncnote import cli
from syncnote import tasks as T

REPO = Path(r"C:\Program Files\GYM-W-HALL-2F")
SCRIPTS = SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _task(name, repo=REPO, state="Ready"):
    args = f'"{repo}\\scripts\\hidden.vbs" "{repo}\\scripts\\x.bat"'
    return T.Task(name, args, "1121", state)


# --- 판정 -------------------------------------------------------------------

def test_all_expected_tasks_present_is_ok():
    st = T.evaluate([_task(n) for n in T.EXPECTED], REPO)
    assert st.ok and st.missing == [] and len(st.present) == len(T.EXPECTED)


def test_no_tasks_at_all_is_reported_as_missing():
    """강의실 PC 에서 실제로 있었던 상태."""
    st = T.evaluate([], REPO)
    assert not st.ok and st.missing == list(T.EXPECTED)
    assert "RecordSchedule" in st.describe()


def test_tasks_pointing_at_an_old_clone_are_stale():
    """Downloads 에서 Program Files 로 옮기면 작업은 옛 폴더의 스크립트를 부릅니다."""
    old = Path(r"C:\Users\1121\Downloads\GYM-W-HALL-2F")
    st = T.evaluate([_task(n, repo=old) for n in T.EXPECTED], REPO)
    assert not st.ok and st.stale == list(T.EXPECTED)


def test_a_disabled_task_is_reported():
    tasks = [_task(n, state="Disabled" if n == "RecordWatch" else "Ready") for n in T.EXPECTED]
    st = T.evaluate(tasks, REPO)
    assert not st.ok and st.disabled == ["RecordWatch"]


def test_optional_tasks_are_not_required():
    st = T.evaluate([_task(n) for n in T.EXPECTED + T.OPTIONAL], REPO)
    assert st.ok


def test_could_not_read_is_not_ok_but_not_missing():
    st = T.evaluate(None, REPO)
    assert not st.checked and not st.ok and st.missing == []


# --- PowerShell 결과 읽기 ------------------------------------------------------

class _CP:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out.encode("utf-8")
        self.stderr = err.encode("utf-8")


def test_query_parses_the_powershell_lines(monkeypatch):
    out = "RecordGuard|\"C:\\r\\scripts\\hidden.vbs\" \"C:\\r\\scripts\\record_guard.bat\"|1121|Ready\n"
    monkeypatch.setattr(T, "_powershell", lambda args, timeout: _CP(0, out))
    got = T.query()
    assert got == [T.Task("RecordGuard", '"C:\\r\\scripts\\hidden.vbs" "C:\\r\\scripts\\record_guard.bat"',
                          "1121", "Ready")]


def test_query_failure_is_none(monkeypatch):
    monkeypatch.setattr(T, "_powershell", lambda args, timeout: _CP(3, "", "모듈 없음"))
    assert T.query() is None


def test_empty_folder_query_is_an_empty_list_not_a_failure():
    """폴더가 없으면 Get-ScheduledTask 가 ObjectNotFound 로 실패합니다 — '작업 없음' 이어야 함
    (아니면 '확인할 수 없음' 으로 보여 등록을 하지 않음). 이 PC 에서 실제로 걸렸습니다."""
    assert "ObjectNotFound" in T._QUERY and "exit 0" in T._QUERY


# --- 등록 --------------------------------------------------------------------

def test_access_denied_retries_with_uac_for_the_original_user(monkeypatch):
    calls = []

    def fake(args, timeout):
        calls.append(args)
        return _CP(T.ACCESS_DENIED if len(calls) == 1 else 0)

    monkeypatch.setattr(T, "_powershell", fake)
    monkeypatch.setattr(T, "_is_admin", lambda: False)
    rc, _ = T.register(REPO, elevate=True, user="1121")
    assert rc == 0 and len(calls) == 2
    assert calls[0][-2:] == ["-User", "1121"]
    assert "RunAs" in calls[1][-1] and '-User "1121"' in calls[1][-1]


def test_background_registration_never_asks_for_uac(monkeypatch):
    calls = []
    monkeypatch.setattr(T, "_powershell", lambda a, timeout: calls.append(a) or _CP(T.ACCESS_DENIED))
    monkeypatch.setattr(T, "_is_admin", lambda: False)
    rc, _ = T.register(REPO, elevate=False, user="1121")
    assert rc == T.ACCESS_DENIED and len(calls) == 1


@pytest.fixture
def stamps(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return tmp_path


def test_ensure_registers_only_when_something_is_missing(stamps, monkeypatch):
    seq = [T.Status(checked=True, missing=["RecordSchedule"]),
           T.Status(checked=True, present=list(T.EXPECTED))]
    monkeypatch.setattr(T, "check", lambda repo=None: seq.pop(0))
    reg = []
    monkeypatch.setattr(T, "register", lambda repo=None, **kw: reg.append(kw) or (0, ""))
    st = T.ensure(REPO, elevate=True)
    assert st.ok and reg == [{"elevate": True}]

    monkeypatch.setattr(T, "check", lambda repo=None: T.Status(checked=True,
                                                               present=list(T.EXPECTED)))
    reg.clear()
    assert T.ensure(REPO).ok and reg == []


def test_ensure_never_raises(stamps, monkeypatch):
    def boom(repo=None):
        raise RuntimeError("작업 스케줄러 서비스가 꺼짐")

    monkeypatch.setattr(T, "check", boom)
    st = T.ensure(REPO)
    assert not st.ok and "꺼짐" in st.error


def test_only_one_registration_at_a_time(stamps):
    assert T._acquire(0)
    try:
        assert not T._acquire(0), "등록이 동시에 두 번 돌면 서로 지우고 만들다 실패합니다"
    finally:
        T._release()
    assert T._acquire(0)
    T._release()


def test_a_dead_holders_lock_is_stolen_immediately(stamps, monkeypatch):
    """크래시·강제 종료로 잠금만 남은 경우(쥔 PID 가 죽음): 시간을 기다리지 않고 바로 뺏음.
    GYM-W-HALL-2F 에서 죽은 PID 가 남긴 잠금 때문에 setup.bat 작업 등록이 멈췄던 회귀."""
    from syncnote.recorder import lock as rec_lock

    lock = T._lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("4294967000", encoding="utf-8")        # 존재할 리 없는 PID
    monkeypatch.setattr(rec_lock, "_pid_exists", lambda pid: pid != 4294967000)
    t0 = time.time()
    assert T._acquire(300) is True                          # 바로 뺏어야 함(300초 기다리지 않음)
    assert time.time() - t0 < 5
    T._release()


def test_a_live_holders_lock_is_not_stolen(stamps, monkeypatch):
    from syncnote.recorder import lock as rec_lock

    lock = T._lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("12345", encoding="utf-8")
    monkeypatch.setattr(rec_lock, "_pid_exists", lambda pid: True)   # 살아 있음
    assert T._acquire(0) is False                           # 뺏지 않고 포기
    assert lock.exists()


@pytest.mark.parametrize("content,pid_alive,dead", [
    ("99999", False, True),            # 죽은 PID → 치움
    ("99999", True, False),            # 산 PID → 둠
    ("garbage", True, True),           # PID 아님 → 치움(옛 형식)
    ("", True, True),                  # 빈 잠금 → 치움
])
def test_holder_dead_logic(stamps, monkeypatch, content, pid_alive, dead):
    from syncnote.recorder import lock as rec_lock

    monkeypatch.setattr(rec_lock, "_pid_exists", lambda pid: pid_alive)
    lock = T._lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(content, encoding="utf-8")
    assert T._holder_dead(lock) is dead


# --- 모든 명령의 시작에서 -----------------------------------------------------

def test_background_check_is_throttled(stamps, monkeypatch):
    monkeypatch.delenv("SYNCNOTE_NO_TASK_CHECK", raising=False)
    monkeypatch.setattr(T, "supported", lambda repo=None: True)
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd))
    assert T.spawn_background(REPO)
    assert spawned and spawned[0][-3:] == ["tasks", "ensure", "--auto"]
    assert not T.spawn_background(REPO), "명령마다 PowerShell 을 띄우면 안 됩니다"

    # 고치지 못한 상태면 1시간 뒤, 정상이면 6시간 뒤 다시
    T._write_stamp(REPO, T.Status(checked=True, missing=["RecordWatch"]))
    assert not T.due(REPO) and T.due(REPO, now=time.time() + T.RECHECK_BAD_SEC + 1)
    T._write_stamp(REPO, T.Status(checked=True, present=list(T.EXPECTED)))
    assert not T.due(REPO, now=time.time() + T.RECHECK_BAD_SEC + 1)
    assert T.due(REPO, now=time.time() + T.RECHECK_OK_SEC + 1)


def test_off_switch_and_non_windows_skip(monkeypatch):
    monkeypatch.setenv("SYNCNOTE_NO_TASK_CHECK", "1")
    assert not T.supported()


def test_every_command_starts_the_background_check(monkeypatch):
    seen = []
    monkeypatch.setattr(T, "spawn_background", lambda repo=None: seen.append(1) or True)
    monkeypatch.setattr(cli, "cmd_record_recover", lambda args: 0)
    cli.main(["record", "recover"])
    assert seen == [1]


@pytest.mark.parametrize("argv", [["tasks", "status"], ["doctor"]])
def test_tasks_and_doctor_do_not_spawn_another_check(monkeypatch, argv):
    seen = []
    monkeypatch.setattr(T, "spawn_background", lambda repo=None: seen.append(1) or True)
    monkeypatch.setattr(cli, "cmd_tasks", lambda args: 0)
    monkeypatch.setattr(cli, "cmd_doctor", lambda args: 0)
    cli.main(argv)
    assert seen == []


# --- 스크립트 ------------------------------------------------------------------

def test_setup_and_doctor_bat_register_missing_tasks():
    setup = (SCRIPTS / "setup.bat").read_text(encoding="ascii")
    doctor = (SCRIPTS / "doctor.bat").read_text(encoding="ascii")
    assert '"%PY%" -m syncnote tasks ensure' in setup
    assert '"%PY%" -m syncnote tasks ensure' in doctor
    # 시간표 편집기는 doctor.bat --json 의 출력을 JSON 으로 읽음 — 그때는 섞지 않음
    assert 'if /i not "%~1"=="--json"' in doctor


def test_register_script_works_without_admin_and_takes_the_user():
    ps = (SCRIPTS / "register_tasks.ps1").read_text(encoding="utf-8-sig")
    assert "Assert-Admin" not in ps, "관리자가 아니면 아예 시도하지 않던 구조"
    assert "[string]$User = $env:USERNAME" in ps
    assert "-UserId $User" in ps and "$env:USERNAME `" not in ps
    assert "exit 5" in ps


# --- register_tasks.ps1: 이미 맞는 작업은 그대로, 오류는 진짜로 멈춤 -------------------
# 강의실 PC: 작업이 관리자 창(SSH)에서 만들어져 일반 창에서 지울 수 없었는데, 바꿀 것이 없는데도
# 지웠다 다시 만들려다 '액세스 거부' + '이미 있음' 이 났고, 그런데도 '등록:' 을 찍고 '완료' 로 끝남.
# (프롬프트에서 .\register_tasks.ps1 로 실행하면 ScheduledTasks 모듈 함수가 스크립트의
#  $ErrorActionPreference = "Stop" 을 보지 못함 — 이 PC 에서 재현.)

import re  # noqa: E402

_PS = (SCRIPTS / "register_tasks.ps1").read_text(encoding="utf-8-sig")


@pytest.mark.parametrize("verb", ["Get", "Register", "Unregister", "Enable"])
def test_every_task_store_call_passes_error_action(verb):
    code = "\n".join(ln for ln in _PS.splitlines() if not ln.lstrip().startswith("#"))
    # 줄 끝 백틱(`)으로 이어지는 여러 줄 호출까지 한 덩어리로
    calls = [m.group(0) for m in re.finditer(
        rf"{verb}-ScheduledTask\b(?:[^\n]*`\n)*[^\n]*", code)]
    assert calls, verb
    for call in calls:
        assert "-ErrorAction" in call, f"-ErrorAction 이 없음: {call[:80]}"


def test_an_already_correct_task_is_left_alone():
    body = _PS[_PS.index("function Register-One"):_PS.index("function Test-SameTask")]
    same = body.index("Test-SameTask")
    assert same < body.index("Unregister-ScheduledTask"), "확인 전에 지우면 안 됨"
    assert "return" in body[same:body.index("Unregister-ScheduledTask")]
    # 비교 항목: 실행할 것·계정·로그온 방식·설명(주기·시각)·트리거
    test = _PS[_PS.index("function Test-SameTask"):]
    for key in ("Arguments", "WorkingDirectory", "UserId", "LogonType", "RunLevel",
                "Description", "Triggers"):
        assert key in test, key


def test_access_denied_is_recognised_in_korean_and_by_hresult():
    trap = _PS[_PS.index("trap {"):_PS.index("function Remove-All")]
    assert "액세스가 거부" in trap and "0x80070005" in trap and "exit 5" in trap


# --- 등록 전에 묻기(사용자 요청: 작업을 새로 등록할지 사용자에게 물어볼 것) -----------------
# 사람이 실행한 곳(setup·doctor·편집기·register_tasks.ps1)은 묻고, 뒤에서 하는 자동 등록은 묻지
# 않음(사용자가 정함). 아니요면 다음 실행 때 다시 물음.

def test_a_no_answer_registers_nothing(stamps, monkeypatch):
    monkeypatch.setattr(T, "check", lambda repo=None: T.Status(checked=True, missing=["RecordWatch"]))
    reg = []
    monkeypatch.setattr(T, "register", lambda repo=None, **kw: reg.append(kw) or (0, ""))
    asked = []
    st = T.ensure(REPO, elevate=True, ask=lambda s: asked.append(s.describe()) or False)
    assert reg == [] and not st.ok and "RecordWatch" in asked[0]


def test_a_yes_answer_registers(stamps, monkeypatch):
    seq = [T.Status(checked=True, missing=["RecordWatch"]),
           T.Status(checked=True, present=list(T.EXPECTED))]
    monkeypatch.setattr(T, "check", lambda repo=None: seq.pop(0))
    reg = []
    monkeypatch.setattr(T, "register", lambda repo=None, **kw: reg.append(kw) or (0, ""))
    assert T.ensure(REPO, ask=lambda s: True).ok and len(reg) == 1


def test_nothing_is_asked_when_all_is_registered(stamps, monkeypatch):
    monkeypatch.setattr(T, "check", lambda repo=None: T.Status(checked=True, present=list(T.EXPECTED)))
    assert T.ensure(REPO, ask=lambda s: pytest.fail("물을 것이 없는데 물음")).ok


@pytest.mark.parametrize("answers,want", [(["y"], True), (["예"], True), (["n"], False),
                                          (["?", "Y"], True), (["?", "?", "?"], False)])
def test_console_ask(answers, want, capsys):
    it = iter(answers)
    st = T.Status(checked=True, missing=["RecordSchedule"])
    assert T.console_ask(st, input_fn=lambda _p: next(it)) is want
    assert "RecordSchedule" in capsys.readouterr().out


def test_console_ask_without_input_registers_nothing():
    def eof(_p):
        raise EOFError
    assert T.console_ask(T.Status(checked=True, missing=["RecordWatch"]), input_fn=eof) is False


def test_the_script_is_always_called_with_yes_after_asking(monkeypatch):
    calls = []
    monkeypatch.setattr(T, "_powershell", lambda a, timeout: calls.append(a) or _CP(0))
    T.register(REPO, user="1121")
    assert "-Yes" in calls[0]


def test_background_and_people_run_commands(monkeypatch):
    seen = {}
    monkeypatch.setattr(T, "supported", lambda repo=None: True)
    monkeypatch.setattr(T, "ensure", lambda **kw: seen.update(kw) or T.Status(checked=True))
    monkeypatch.setattr(cli, "_load", lambda a: None)
    cli.main(["tasks", "ensure"])
    assert seen["ask"] is T.console_ask                   # setup.bat·doctor.bat — 물음
    cli.main(["tasks", "ensure", "--auto"])
    assert seen["ask"] is None                             # 뒤에서 하는 자동 등록 — 묻지 않음
    cli.main(["tasks", "ensure", "--yes"])
    assert seen["ask"] is None


def test_register_script_asks_before_changing_anything():
    plan = _PS[_PS.index("$script:Planning = $true"):]
    assert plan.index("Read-Host") < plan.index("& $RegisterAll", plan.index("Read-Host"))
    assert "if (-not $Yes)" in plan and "exit 2" in plan
    # 계획 단계에서는 아무것도 바꾸지 않음
    one = _PS[_PS.index("function Register-One"):_PS.index("function Test-SameTask")]
    planning = one[one.index("if ($script:Planning)"):one.index("if ($same)")]
    assert "Register-ScheduledTask" not in planning and "Unregister" not in planning
