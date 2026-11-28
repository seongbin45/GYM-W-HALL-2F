"""작업 스케줄러 등록 확인·자동 등록.

녹화(시간표·감지·지킴이)는 작업 스케줄러가 로그온 때 띄워야 돕니다. 그런데 등록은
scripts\\register_tasks.ps1 을 사람이 따로 실행해야 해서, 실제로 한 강의실 PC 는 작업이
하나도 없어 녹화가 한 번도 돌지 않았습니다. 그래서:

  - setup.bat · doctor.bat · 시간표 편집기(사람이 실행): 확인하고, 빠졌으면 등록합니다.
    권한이 없어 거부되면 UAC 로 다시 시도합니다(elevate=True).
  - 그 밖의 모든 syncnote 명령(작업 스케줄러가 띄운 것 포함): 시작할 때 뒤에서 확인하고,
    빠졌으면 등록합니다(창·UAC 없이, 너무 자주 하지 않게 간격을 둠 — spawn_background).

'빠짐' 의 뜻:
  - 기본 작업(EXPECTED) 중 없는 것
  - 다른 폴더(옛 레포 — 예: Downloads 에서 Program Files 로 옮김)를 가리키는 것
  - 사용 안 함(Disabled)으로 꺼진 것
선택 작업(TerminalAudio·StreamSchedule)은 사람이 -AudioAt/-Stream 으로 고른 것이라 보지
않고, 다시 등록해도 건드리지 않습니다(register_tasks.ps1 은 이름이 같은 것만 바꿈).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from .logging_setup import get as get_logger

log = get_logger("tasks")

FOLDER = "\\Sync Note\\"
EXPECTED = ("EclassSync", "RecordSchedule", "RecordWatch", "RecordGuard",
            "TranscribeQueue", "HomeBackup", "HomePrune", "AutoUpdate", "RemoteOps")
OPTIONAL = ("TerminalAudio", "StreamSchedule")
ACCESS_DENIED = 5          # register_tasks.ps1 이 권한 부족일 때 돌려주는 코드
UAC_CANCELLED = 1223       # 사용자가 UAC 창에서 '아니요'
# 뒤에서 하는 자동 확인 간격: 정상이면 6시간, 고치지 못했으면 1시간 뒤 다시.
RECHECK_OK_SEC = 6 * 3600
RECHECK_BAD_SEC = 3600

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def repo_root() -> Path:
    """src/syncnote/tasks.py → 레포 폴더."""
    return Path(__file__).resolve().parents[2]


def script_path(repo: Path | None = None) -> Path:
    return (repo or repo_root()) / "scripts" / "register_tasks.ps1"


def supported(repo: Path | None = None) -> bool:
    """Windows 이고 레포에서 실행 중일 때만(pip 로 설치한 패키지에는 스크립트가 없음).
    SYNCNOTE_NO_TASK_CHECK=1 이면 끕니다(테스트 — 실제 작업 스케줄러를 건드리지 않게)."""
    if os.environ.get("SYNCNOTE_NO_TASK_CHECK"):
        return False
    return sys.platform == "win32" and script_path(repo).is_file()


@dataclass
class Task:
    name: str
    arguments: str = ""
    user: str = ""
    state: str = ""


@dataclass
class Status:
    checked: bool                       # False = 확인할 수 없었음(PowerShell 실패 등)
    present: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)       # 다른 폴더를 가리킴
    disabled: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.checked and not (self.missing or self.stale or self.disabled)

    def describe(self) -> str:
        if not self.checked:
            return f"확인할 수 없습니다({self.error or '알 수 없는 이유'})"
        if self.ok:
            return f"등록됨 ({len(self.present)}개)"
        parts = []
        if self.missing:
            parts.append("없음: " + ", ".join(self.missing))
        if self.stale:
            parts.append("다른 폴더를 가리킴: " + ", ".join(self.stale))
        if self.disabled:
            parts.append("꺼져 있음: " + ", ".join(self.disabled))
        return " / ".join(parts)


# 폴더가 없거나 비었으면 Get-ScheduledTask 가 ObjectNotFound 오류로 1 을 돌려줍니다 — 그건
# '작업 없음'(빈 목록, 0). 그 밖의 오류(모듈 없음 등)는 3 = 확인할 수 없음.
_QUERY = (
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
    "try { $ts = @(Get-ScheduledTask -TaskPath '" + FOLDER + "' -ErrorAction Stop) } "
    "catch { if ($_.CategoryInfo.Category -eq 'ObjectNotFound') { exit 0 } "
    "[Console]::Error.WriteLine($_); exit 3 }; "
    "$ts | ForEach-Object { '{0}|{1}|{2}|{3}' -f $_.TaskName, "
    "(($_.Actions | Select-Object -First 1).Arguments), $_.Principal.UserId, $_.State }; exit 0"
)


def _powershell(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    from .exec_policy import _powershell, ps_env

    # Windows PowerShell 5.1 을 전체 경로로, PSModulePath 는 비워서(PowerShell 7 창에서 시작해도
    # ScheduledTasks 등 5.1 모듈을 불러오게 — exec_policy.ps_env).
    return subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *args],
        capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, env=ps_env(),
        creationflags=_NO_WINDOW,
    )


def query() -> list[Task] | None:
    """등록된 Sync Note 작업. 확인할 수 없으면 None."""
    try:
        cp = _powershell(["-Command", _QUERY], timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("작업 목록 확인 실패: %s", exc)
        return None
    if cp.returncode != 0:
        log.debug("작업 목록 확인 실패(%s): %s", cp.returncode,
                  cp.stderr.decode("utf-8", "replace")[:300])
        return None
    out = []
    for line in cp.stdout.decode("utf-8", "replace").splitlines():
        bits = line.strip().split("|")
        if len(bits) == 4 and bits[0]:
            out.append(Task(*bits))
    return out


def evaluate(tasks: list[Task] | None, repo: Path | None = None) -> Status:
    """작업 목록 → 상태. (query 와 나눠 테스트가 실제 작업 스케줄러 없이 확인)"""
    if tasks is None:
        return Status(checked=False, error="작업 스케줄러를 읽지 못했습니다")
    # 작업의 인자는 늘 Windows 경로라 Windows 규칙으로 맞춰 봅니다(구분자·대소문자).
    scripts = str(PureWindowsPath(repo or repo_root()) / "scripts").lower().rstrip("\\") + "\\"
    by_name = {t.name: t for t in tasks}
    st = Status(checked=True)
    for name in EXPECTED:
        t = by_name.get(name)
        if t is None:
            st.missing.append(name)
            continue
        st.present.append(name)
        if scripts not in t.arguments.lower():
            st.stale.append(name)
        elif t.state.strip().lower() == "disabled":
            st.disabled.append(name)
    return st


def check(repo: Path | None = None) -> Status:
    if not supported(repo):
        return Status(checked=False, error="Windows 레포 실행이 아님")
    return evaluate(query(), repo)


def _is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def register(repo: Path | None = None, *, elevate: bool = False,
             user: str | None = None) -> tuple[int, str]:
    """register_tasks.ps1 실행. (종료 코드, 출력). 권한 부족이면 elevate 일 때 UAC 로 다시."""
    ps1 = str(script_path(repo))
    user = user or os.environ.get("USERNAME", "")
    # 묻는 일은 호출한 쪽이 먼저 함(ensure 의 ask, 뒤에서 하는 자동 등록은 묻지 않음) — 늘 -Yes
    args = ["-File", ps1, "-Yes"] + (["-User", user] if user else [])
    try:
        cp = _powershell(args, timeout=300)
        rc, out = cp.returncode, (cp.stdout + cp.stderr).decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    if rc == ACCESS_DENIED and elevate and not _is_admin():
        log.warning("작업 등록이 권한 부족으로 거부돼 관리자 권한(UAC)으로 다시 시도합니다.")
        print("작업 스케줄러 등록에 관리자 권한이 필요합니다. 뜨는 창에서 '예' 를 누르세요.")
        inner = (f"-NoProfile -ExecutionPolicy Bypass -File \"{ps1}\" -Yes"
                 + (f" -User \"{user}\"" if user else ""))
        cmd = ("try { $p = Start-Process powershell -Verb RunAs -Wait -PassThru "
               "-WindowStyle Hidden -ArgumentList '" + inner.replace("'", "''") + "'; "
               "exit $p.ExitCode } catch { exit " + str(UAC_CANCELLED) + " }")
        try:
            cp = _powershell(["-Command", cmd], timeout=600)
            rc = cp.returncode
            out += "\n(UAC) " + (cp.stdout + cp.stderr).decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, out + f"\n{exc}"
    return rc, out


# --- 자동 확인 기록(너무 자주 하지 않게) -------------------------------------

def _stamp_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "SyncNote" / "tasks_check.json"


def _key(repo: Path) -> str:
    return str(repo).lower()


def _read_stamps() -> dict:
    try:
        return json.loads(_stamp_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_stamp(repo: Path, status: Status, note: str = "") -> None:
    data = _read_stamps()
    data[_key(repo)] = {"at": time.time(), "ok": status.ok, "detail": status.describe(),
                        "note": note}
    try:
        p = _stamp_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        log.debug("자동 확인 기록 실패: %s", exc)


def due(repo: Path | None = None, now: float | None = None) -> bool:
    """뒤에서 다시 확인할 때가 됐는가."""
    rec = _read_stamps().get(_key(repo or repo_root()))
    if not rec:
        return True
    age = (now or time.time()) - float(rec.get("at", 0))
    return age >= (RECHECK_OK_SEC if rec.get("ok") else RECHECK_BAD_SEC)


LOCK_STALE_SEC = 900


def _lock_path() -> Path:
    return _stamp_path().with_name("tasks_register.lock")


def _holder_dead(lock: Path) -> bool:
    """잠금을 쥔 PID 가 확실히 죽었나. 못 읽거나 모르면 False(살아 있다고 보고 기다림)."""
    try:
        holder = lock.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not holder.isdigit():
        return True                     # PID 를 못 읽는 잠금은 치워도 됨(옛 형식·깨짐)
    pid = int(holder)
    if pid == os.getpid():
        return False                    # 내 잠금(있을 리 없지만) 은 치우지 않음
    try:
        from .recorder import lock as _rec_lock
        return not _rec_lock._pid_exists(pid)     # 커널에 물어 '그런 PID 없음' 이면 죽은 것
    except Exception:  # noqa: BLE001
        return False


def _acquire(wait_sec: float) -> bool:
    """등록을 한 번에 하나만(로그온 때 여러 명령이 동시에 뒤에서 띄우고, setup 도 같이 돎).
    register_tasks.ps1 은 같은 이름의 작업을 지웠다 다시 만들어서 겹치면 서로 실패합니다.

    잠금을 쥔 프로세스가 죽어 잠금만 남은 경우(크래시·강제 종료 — finally 가 안 돎)를 **바로**
    알아채 치웁니다. 예전에는 시간(LOCK_STALE_SEC)으로만 봐서, 죽은 PID 가 남긴 잠금 때문에
    setup.bat 의 작업 등록이 최대 300초 멈췄다가 등록도 못 하고 끝났습니다(GYM-W-HALL-2F 에서 실제)."""
    lock = _lock_path()
    deadline = time.time() + wait_sec
    while True:
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            # 쥔 PID 가 죽었거나(바로), 너무 오래됐으면(백스톱) 치우고 다시 시도
            try:
                dead = _holder_dead(lock)
                age = time.time() - lock.stat().st_mtime
            except OSError:
                continue                 # 방금 사라짐 — 다시 시도
            if dead or age > LOCK_STALE_SEC:
                # 왜 치웠는지 남깁니다. 잠금이 남아 있다는 건 쥔 프로세스가 finally 를
                # 거치지 못하고 비정상 종료(강제 종료·전원 차단·세션 종료)했다는 뜻이고,
                # 그 프로세스 자신은 아무 흔적도 못 남기므로, 이 로그가 '직전 등록이 왜
                # 안 끝났는지' 를 알 수 있는 유일한 기록입니다(원인 불명 방지).
                try:
                    holder = lock.read_text(encoding="utf-8").strip() or "?"
                except OSError:
                    holder = "?"
                why = "쥔 프로세스가 죽음" if dead else f"너무 오래됨({age:.0f}초)"
                log.warning("등록 잠금 회수: 직전 등록(pid=%s)이 끝맺지 못했습니다 — %s. "
                            "비정상 종료(강제 종료·전원 차단 등)로 보입니다.", holder, why)
                try:
                    lock.unlink()
                except OSError:
                    pass
                continue
        except OSError:
            return True                 # 잠금 파일을 못 만들면 그냥 진행(등록을 막지 않음)
        if time.time() >= deadline:
            return False
        time.sleep(1.0)


def _release() -> None:
    try:
        _lock_path().unlink()
    except OSError:
        pass


def ensure(repo: Path | None = None, *, elevate: bool = False, force: bool = False,
           ask=None) -> Status:
    """확인하고 빠졌으면 등록합니다. 녹화를 막지 않도록 무슨 일이 있어도 예외를 던지지 않음.

    다른 곳에서 등록 중이면: 사람이 실행한 것(elevate)은 끝날 때까지 기다렸다가 확인하고,
    뒤에서 자동으로 한 것은 그냥 돌아갑니다.

    ask(status) -> bool: 사람이 실행한 곳(setup·doctor·편집기)에서 '등록할까요?' 를 묻는 함수.
    False 면 등록하지 않음(다음 실행 때 다시 물음). None 이면 묻지 않고 등록(뒤에서 하는 자동 등록 —
    물어볼 사람이 없음, 사용자가 정함)."""
    repo = repo or repo_root()
    # 정규 과정: 모든 자동 등록 전에 PowerShell 실행 정책을 확인하고, 부족하면 영구적으로 켭니다
    # (scripts/exec_policy.ps1). 등록 자체는 -ExecutionPolicy Bypass 라 막히지 않지만, 사람이
    # .ps1 을 직접 실행할 때 막혀 창이 바로 닫히지 않게 합니다. 예외를 던지지 않음.
    from . import exec_policy

    exec_policy.ensure()
    if not _acquire(300 if elevate else 0):
        log.info("다른 곳에서 작업 스케줄러를 등록하는 중이라 이번에는 건너뜁니다.")
        return check(repo)
    try:
        st = check(repo)
        if not st.checked:
            if supported(repo):
                log.warning("작업 스케줄러를 확인할 수 없습니다: %s", st.error)
                _write_stamp(repo, st)
            return st
        if st.ok and not force:
            _write_stamp(repo, st)
            return st
        if ask is not None and not st.ok:
            if not ask(st):
                log.warning("작업 스케줄러 등록을 하지 않기로 했습니다(다음 실행 때 다시 묻습니다) — %s",
                            st.describe())
                return st
        elif not st.ok:
            log.warning("작업 스케줄러 등록이 빠져 있어 자동으로 등록합니다 — %s", st.describe())
        rc, out = register(repo, elevate=elevate)
        after = check(repo)
        if after.ok:
            log.info("작업 스케줄러 등록 완료(%s).", after.describe())
        else:
            tail = out.strip().splitlines()[-3:]
            reason = ("관리자 권한이 필요합니다 — setup.bat 이나 doctor.bat 을 실행해 UAC 에서 '예'"
                      if rc in (ACCESS_DENIED, UAC_CANCELLED) else f"종료 코드 {rc}")
            log.error("작업 스케줄러 자동 등록 실패(%s): %s | %s", reason, after.describe(),
                      " / ".join(tail))
        _write_stamp(repo, after, note=f"register rc={rc}")
        return after
    except Exception as exc:                          # noqa: BLE001 — 절대 막지 않음
        log.exception("작업 스케줄러 확인 중 오류")
        return Status(checked=False, error=str(exc))
    finally:
        _release()


def console_ask(st: Status, *, input_fn=input) -> bool:
    """콘솔에서 묻기(setup.bat·doctor.bat·tasks ensure). 물을 수 없으면(입력 없음) 등록하지 않음."""
    print()
    print("작업 스케줄러에 등록이 필요합니다 — " + st.describe())
    print("  (로그온하면 시간표·감지 녹화와 녹화 지킴이가 저절로 돌게 합니다)")
    for _ in range(3):
        try:
            answer = input_fn("등록할까요? (Y/N): ").strip().lower()
        except (EOFError, OSError):
            print("물어볼 수 없어 등록하지 않았습니다.")
            return False
        if answer in ("y", "yes", "예", "ㅛ"):
            return True
        if answer in ("n", "no", "아니요", "아니오", "ㅜ"):
            print("등록하지 않았습니다. 다음에 setup.bat·doctor.bat 을 실행하면 다시 묻습니다.")
            return False
        print("Y 또는 N 으로 답해 주세요.")
    print("등록하지 않았습니다.")
    return False


def spawn_background(repo: Path | None = None) -> bool:
    """뒤에서(창 없이) `syncnote tasks ensure --auto` 를 띄웁니다. 띄웠으면 True.

    모든 syncnote 명령의 시작에서 부릅니다. 명령을 늦추지 않도록 기다리지 않고, 간격(due)이
    안 됐으면 아무것도 하지 않습니다. Windows 가 아닌 곳·끈 경우(SYNCNOTE_NO_TASK_CHECK)는
    건너뜁니다.
    """
    repo = repo or repo_root()
    if not supported(repo) or not due(repo):
        return False
    # 같은 순간에 여러 명령(로그온 때 작업 셋)이 동시에 띄우지 않게 먼저 기록해 둠
    _write_stamp(repo, Status(checked=True, missing=["(확인 중)"]), note="spawned")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo / "src") + os.pathsep + env.get("PYTHONPATH", "")
    flags = (_NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    try:
        subprocess.Popen([sys.executable, "-m", "syncnote", "tasks", "ensure", "--auto"],
                         cwd=str(repo), env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags, close_fds=True)
        return True
    except OSError as exc:
        log.debug("작업 확인을 뒤에서 띄우지 못했습니다: %s", exc)
        return False
