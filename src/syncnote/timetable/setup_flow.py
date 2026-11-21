"""저장 뒤 '설치 → 라이브러리 확인 → 비밀번호 → 환경 점검' 을 차례로 돌리는 창.

시간표 편집기에서 저장하면(설정 확인·저장이 끝난 뒤) 이 창이 이어서 엽니다.

  1. 설치        scripts/setup.bat 을 실행(가상환경·라이브러리·브라우저·ffmpeg·Tailscale).
                 출력은 이 창에 보이고, 끝나면 다음 단계로 넘어갑니다(bat 의
                 마지막 pause 는 입력을 닫아 기다리지 않게 함).
  2. 라이브러리  requirements.txt 와 실제 설치(.venv)를 맞춰 보고(syncnote.reqcheck),
                 빠졌거나 버전이 안 맞는 줄만 pip install 한 뒤 다시 확인.
  3. 비밀번호    eClass 비밀번호가 OS 자격증명 저장소(keyring)에 있는지 확인하고,
                 없으면 scripts/set_password.bat 1 을 새 콘솔 창에 엽니다.
  4. 점검        scripts/doctor.bat --json, scripts/devices.bat --json 결과를 그림으로.

setup.bat 이 끝에 텍스트로 안내하던 '다음 순서'(설정 → 비밀번호 → 점검 → 오디오
장치)는 결과 화면 맨 위에 '했는지/안 했는지' 체크 목록으로 바뀝니다.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import shiboken6
from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import reqcheck, setup_failures, setup_history, setupcheck
from . import eclass_fetch

REPO_ROOT = eclass_fetch.REPO_ROOT
VC_REDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
# pip 가 파일 잠김으로 실패할 때(편집기·백신이 파일을 잡고 있음)
LOCK_HINT = re.compile(r"WinError (5|32)\b|Access is denied|액세스가 거부|being used by another "
                       r"process|다른 프로세스가 파일을 사용", re.I)
SCRIPTS = eclass_fetch.SCRIPTS

STEPS = (
    ("install", "1. 설치 (setup.bat)"),
    ("verify", "2. 라이브러리 확인 (requirements.txt)"),
    ("password", "3. eClass 비밀번호"),
    ("check", "4. 환경 점검 · 오디오 장치"),
)
ICON = {"wait": "○", "run": "⏳", "ok": "✔", "warn": "⚠", "fail": "✖", "skip": "–"}
COLOR = {"wait": "#6B7280", "run": "#2F6FB5", "ok": "#15803D", "warn": "#B45309",
         "fail": "#C0392B", "skip": "#6B7280"}
MARK = {"O": ("✔", "#15803D"), "X": ("✖", "#C0392B"), "-": ("–", "#6B7280"),
        "!": ("⚠", "#B45309")}


def last_json(text: str) -> Any:
    """출력에서 마지막 JSON 한 줄(bat 의 echo·pause 줄은 건너뜀)."""
    for line in reversed(text.replace("\r\n", "\n").splitlines()):
        s = line.strip()
        if s.startswith(("{", "[")):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                continue
    return None


def _esc(s: Any) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SetupFlow(QDialog):
    # 일시적 실패(파일 잠김·네트워크·멈춤)는 2→4→8초 뒤 다시, 최대 이 횟수까지.
    MAX_TRIES = 3
    BACKOFF_BASE = 2.0
    # (전체 시간 한도, 출력 없이 버틸 시간) 초. 멈춤 감지용.
    TIMEOUTS = {"setup": (1800, 300), "pip": (900, 300), "remedy": (900, 300),
                "quick": (120, 60)}

    # 비밀번호 창을 연 뒤 이만큼(초) 지나도 안 닫히면 '창을 찾으세요' + 다시 열기(C1).
    PASSWORD_NUDGE = 60.0

    def __init__(self, parent: QWidget | None, config_path: Path,
                 fix_config: Callable[[], bool] | None = None) -> None:
        super().__init__(parent)
        from .gui import ScreenKeeper

        self.config_path = Path(config_path)
        # 설정 오류로 doctor 가 멈추면 부르는 함수(편집기의 '설정 편집'). True = 저장함.
        self.fix_config = fix_config
        self._user_fixes: set[str] = set()
        self._pw_opened = 0.0
        self.setWindowTitle("설치 · 점검")
        self.resize(900, 640)
        ScreenKeeper.attach(self)

        self.state = {k: "wait" for k, _ in STEPS}
        self.notes: dict[str, str] = {}
        self.reqs: list[dict] = []
        self.secret_status: dict[str, str] = {}
        self.doctor: dict | None = None
        self.devices: dict | None = None
        self.proc: QProcess | None = None
        self.pw_proc: subprocess.Popen | None = None
        self._queue: list[str] = []
        self._pw_rounds = 0
        self.install_state: dict | None = None
        self._locked: set[str] = set()
        self.deferred: list[str] = []
        self.causes: dict[str, setup_failures.Cause] = {}
        self.attempts: dict[str, int] = {}
        self._remedies: set[str] = set()
        self.setup_out = ""
        self.history: list[dict] = []
        self.skipped_retry: dict[str, str] = {}
        self.deferred_force = False
        self.need_vcredist = False
        self.setup_rc = 0

        lay = QVBoxLayout(self)
        self.headline = QLabel("저장한 설정으로 설치와 점검을 이어서 진행합니다.")
        self.headline.setWordWrap(True)
        lay.addWidget(self.headline)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.step_list = QListWidget()
        self.step_list.setWordWrap(True)
        for key, title in STEPS:
            it = QListWidgetItem(f"{ICON['wait']}  {title}")
            it.setData(Qt.ItemDataRole.UserRole, key)
            self.step_list.addItem(it)
        split.addWidget(self.step_list)
        self.stack = QStackedWidget()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.stack.addWidget(self.log)
        self.result_scroll = QScrollArea()
        self.result_scroll.setWidgetResizable(True)
        self.stack.addWidget(self.result_scroll)
        split.addWidget(self.stack)
        split.setStretchFactor(1, 3)
        split.setSizes([240, 640])
        lay.addWidget(split, 1)

        bb = QDialogButtonBox()
        self.log_btn = QPushButton("출력 보기")
        self.log_btn.setCheckable(True)
        self.log_btn.toggled.connect(lambda on: self.stack.setCurrentIndex(0 if on else 1))
        self.log_btn.setEnabled(False)
        bb.addButton(self.log_btn, QDialogButtonBox.ButtonRole.ActionRole)
        self.reopen_btn = QPushButton("비밀번호 창 다시 열기")
        self.reopen_btn.setToolTip("검은 창이 안 보이면(다른 창 뒤에 숨었을 수 있음) 새로 엽니다")
        self.reopen_btn.clicked.connect(self.reopen_password_window)
        bb.addButton(self.reopen_btn, QDialogButtonBox.ButtonRole.ActionRole)
        self.reopen_btn.hide()        # addButton 이 다시 보이게 하므로 그 뒤에 숨김
        self.close_btn = QPushButton("중단")
        self.close_btn.clicked.connect(self.reject)
        bb.addButton(self.close_btn, QDialogButtonBox.ButtonRole.RejectRole)
        lay.addWidget(bb)

        self._poll = QTimer(self)
        self._poll.setInterval(400)
        self._poll.timeout.connect(self._check_password_window)

    # -- 실행할 명령(테스트에서 바꿔 끼움) ---------------------------------
    def venv_path(self) -> Path:
        return REPO_ROOT / ".venv" / "Scripts" / "python.exe"

    def venv_exists(self) -> bool:
        return self.venv_path().exists()

    def venv_python(self) -> str:
        return str(self.venv_path()) if self.venv_exists() else sys.executable

    def venv_create_command(self) -> list[str]:
        """'파이썬을 찾지 못함' 해결책: 편집기의 파이썬으로 .venv 를 먼저 만듦."""
        return [sys.executable, "-m", "venv", str(REPO_ROOT / ".venv")]

    def playwright_command(self) -> list[str]:
        return [self.venv_python(), "-m", "playwright", "install", "chromium"]

    def ffmpeg_command(self) -> list[str] | None:
        if os.name != "nt":
            return None
        return ["cmd.exe", "/d", "/c", str(SCRIPTS / "install_ffmpeg.bat")]

    def tailscale_command(self) -> list[str] | None:
        """송출 PC 에 Tailscale 설치(pip 패키지가 아니라 winget). 관리자 확인 창이 뜹니다."""
        if os.name != "nt":
            return None
        return ["winget", "install", "--id", "Tailscale.Tailscale", "-e", "--silent",
                "--accept-package-agreements", "--accept-source-agreements"]

    def state_command(self) -> list[str]:
        """설치가 이미 됐는지(setupcheck) — .venv 의 파이썬으로 봐야 그 환경을 봄."""
        return [str(self.venv_path()), "-m", "syncnote.setupcheck",
                str(REPO_ROOT / "requirements.txt")]

    def setup_command(self) -> list[str] | None:
        if os.name != "nt":
            return None                       # bat 은 Windows 전용
        return ["cmd.exe", "/d", "/c", str(SCRIPTS / "setup.bat")]

    def reqcheck_command(self) -> list[str]:
        # --import: 버전만 맞고 실제로는 import 가 안 되는(DLL 등) 경우까지 잡음
        return [self.venv_python(), "-m", "syncnote.reqcheck",
                str(REPO_ROOT / "requirements.txt"), "--import"]

    def pip_install_command(self, lines: list[str], force: bool = False) -> list[str]:
        flags = ["--force-reinstall", "--no-deps"] if force else []
        return [self.venv_python(), "-m", "pip", "install", *flags, *lines]

    def deferred_install_command(self, lines: list[str], force: bool) -> list[str]:
        """편집기를 닫은 뒤 설치하고 다시 여는 별도 콘솔(syncnote.deferred_install)."""
        return [self.venv_python(), "-m", "syncnote.deferred_install", "--pid", str(os.getpid()),
                *(["--force"] if force else []), "--relaunch", "--", *lines]

    # -- 편집기가 쓰는 중인 패키지(A1) -------------------------------------
    def editor_in_venv(self) -> bool:
        """이 편집기가 바로 그 .venv 의 파이썬으로 돌고 있나."""
        try:
            return Path(sys.prefix).resolve() == (REPO_ROOT / ".venv").resolve()
        except OSError:
            return False

    def editor_loaded(self) -> set[str]:
        """편집기 프로세스에 올라와 있는 배포 이름들(정규화)."""
        from importlib import metadata

        try:
            mapping = metadata.packages_distributions()
        except Exception:  # noqa: BLE001
            return set()
        loaded = {name.split(".")[0] for name in list(sys.modules)}
        return {reqcheck.norm(d) for mod, ds in mapping.items() if mod in loaded for d in ds}

    def editor_holds(self, dist: str) -> bool:
        """그 패키지를 지금 바꾸면 파일이 잠겨 실패할까(편집기가 같은 .venv 에서 씀)."""
        return self.editor_in_venv() and reqcheck.norm(dist) in self.editor_loaded()

    def status_command(self) -> list[str]:
        return [self.venv_python(), "-c",
                "import json; from syncnote import secrets; "
                "print(json.dumps(secrets.status(), ensure_ascii=False))"]

    def password_command(self) -> list[str]:
        if os.name == "nt":
            return ["cmd.exe", "/d", "/c", str(SCRIPTS / "set_password.bat"), "1"]
        return [self.venv_python(), "-m", "syncnote", "secrets", "set", "eclass"]

    def doctor_command(self) -> list[str]:
        if os.name == "nt":
            return ["cmd.exe", "/d", "/c", str(SCRIPTS / "doctor.bat"), "--json"]
        return [self.venv_python(), "-m", "syncnote", "doctor", "--json"]

    def devices_command(self) -> list[str]:
        if os.name == "nt":
            return ["cmd.exe", "/d", "/c", str(SCRIPTS / "devices.bat"), "--json", "--probe"]
        return [self.venv_python(), "-m", "syncnote", "devices", "--json", "--probe"]

    def history_path(self) -> Path | None:
        """<data_dir>/state/setup_history.json (config 의 data_dir 기준)."""
        from .. import config as config_module

        try:
            from .. import settings_schema as S

            raw = S.tomllib.loads(self.config_path.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001 — 설정이 없거나 깨져도 기본 data_dir 로
            raw = {}
        try:
            return config_module.Config(data=raw).data_dir / "state" / "setup_history.json"
        except Exception:  # noqa: BLE001
            return None

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["SYNCNOTE_CONFIG"] = str(self.config_path)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        return env

    # -- 프로세스 하나 돌리기 ---------------------------------------------
    def _run(self, cmd: list[str], done: Callable[[int, str], None],
             kind: str = "quick") -> None:
        """kind 의 시간 한도를 넘거나 출력 없이 오래 멈추면 끊고 종료 코드 -2 로 알림."""
        self._say(f"> {' '.join(cmd)}")
        total, idle = self.TIMEOUTS.get(kind, self.TIMEOUTS["quick"])
        p = QProcess(self)
        p.setWorkingDirectory(str(REPO_ROOT))
        env = QProcessEnvironment()
        for k, v in self._env().items():
            env.insert(k, v)
        p.setProcessEnvironment(env)
        p.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        buf: list[str] = []

        clock = {"start": time.monotonic(), "last": time.monotonic(), "stalled": False}
        watch = QTimer(p)
        watch.setInterval(500)

        def tick() -> None:
            now = time.monotonic()
            if now - clock["start"] > total or now - clock["last"] > idle:
                clock["stalled"] = True
                watch.stop()
                self._say(f"! {'시간 한도' if now - clock['start'] > total else '출력 없이'} "
                          "너무 오래 걸려 끊습니다.")
                eclass_fetch._kill_tree(int(p.processId()))
                p.kill()

        watch.timeout.connect(tick)

        def read() -> None:
            if not shiboken6.isValid(p):      # 창이 먼저 정리된 뒤 늦게 온 신호
                return
            chunk = bytes(p.readAllStandardOutput()).decode("utf-8", errors="replace")
            if chunk:
                clock["last"] = time.monotonic()
            buf.append(chunk)
            for line in chunk.replace("\r\n", "\n").splitlines():
                if line.strip() and not line.lstrip().startswith(("{", "[")):
                    self._say(line)

        def finished(code: int, _status) -> None:
            watch.stop()
            read()
            if self.proc is p:
                self.proc = None
            done(-2 if clock["stalled"] else int(code), "".join(buf))

        def failed(err) -> None:
            if err == QProcess.ProcessError.FailedToStart:
                self._say(f"! 실행하지 못했습니다: {p.errorString()}")
                if self.proc is p:
                    self.proc = None
                done(-1, "")

        p.readyReadStandardOutput.connect(read)
        p.finished.connect(finished)
        p.errorOccurred.connect(failed)
        self.proc = p
        p.start(cmd[0], cmd[1:])
        p.closeWriteChannel()             # bat 끝의 pause 가 기다리지 않게
        watch.start()

    # -- 원인 짐작 + 일시적 실패 다시 시도 --------------------------------
    def _cause(self, rc: int, out: str) -> setup_failures.Cause | None:
        if rc == -2:
            return setup_failures.BY_ID["stall"]
        return setup_failures.classify(out, rc)

    def _run_retrying(self, step: str, make_cmd: Callable[[], list[str]],
                      done: Callable[[int, str], None], kind: str = "quick",
                      attempt: int = 1) -> None:
        """실패 원인이 '일시적' 이면 백오프 후 같은 명령을 다시(최대 MAX_TRIES)."""
        def finished(rc: int, out: str) -> None:
            cause = self._cause(rc, out) if rc != 0 else None
            if (cause is not None and cause.kind == "transient" and attempt == 1
                    and setup_history.repeated(self.history, step, cause.id) >= 2):
                # 지난 두 번도 같은 이유로 실패 → 또 백오프해 봐야 같음. 바로 수동 방법을.
                self.skipped_retry[step] = cause.id
                self._say(f"! {cause.title} — 지난번에도 같은 이유로 실패해 자동 재시도를 생략합니다.")
                self.causes[step] = cause
                done(rc, out)
                return
            if cause is not None and cause.kind == "transient" and attempt < self.MAX_TRIES:
                wait = setup_failures.backoff(attempt, self.BACKOFF_BASE)
                self.attempts[step] = attempt + 1
                msg = f"{cause.title} — {wait:g}초 뒤 다시 ({attempt + 1}/{self.MAX_TRIES}회째)"
                self._say(f"! {msg}")
                self._mark(step, "run", msg)
                QTimer.singleShot(int(wait * 1000), lambda: self._run_retrying(
                    step, make_cmd, done, kind, attempt + 1))
                return
            if cause is not None:
                self.causes[step] = cause
            done(rc, out)
        self._run(make_cmd(), finished, kind)

    def _fail_note(self, step: str, fallback: str) -> str:
        c = self.causes.get(step)
        tries = self.attempts.get(step, 1)
        extra = f" ({tries}번 시도)" if tries > 1 else ""
        if step in self.skipped_retry and c is not None:
            return f"{c.title} — 지난번에도 같은 이유로 실패(자동 재시도 생략). {c.hint}"
        if c is None or c.id == "unknown":            # 모르는 원인보다 구체적인 사실이 낫다
            return (fallback + extra + " — 출력 보기") if fallback else c.title if c else ""
        return f"{c.title}{extra} — {c.hint}"

    def _say(self, text: str) -> None:
        self.log.appendPlainText(text.rstrip("\n"))

    # -- 단계 진행 --------------------------------------------------------
    def start(self) -> None:
        hp = self.history_path()
        self.history = setup_history.load(hp) if hp else []
        self._queue = [k for k, _ in STEPS]
        self.stack.setCurrentIndex(0)
        self._next()

    def _mark(self, key: str, state: str, note: str = "") -> None:
        self.state[key] = state
        if note:
            self.notes[key] = note
        for i in range(self.step_list.count()):
            it = self.step_list.item(i)
            if it.data(Qt.ItemDataRole.UserRole) == key:
                title = dict(STEPS)[key]
                it.setText(f"{ICON[state]}  {title}" + (f"\n     {note}" if note else ""))
                it.setToolTip(note)

    def _next(self) -> None:
        if not self._queue:
            self._finish()
            return
        key = self._queue.pop(0)
        self._mark(key, "run")
        self._say("")
        self._say(f"── {dict(STEPS)[key]} " + "─" * 20)
        getattr(self, f"_step_{key}")()

    # 1. 설치 — 먼저 '이미 됐나' 를 보고, 필요할 때만 setup.bat, 끝나면 실제 상태로 판정
    def _step_install(self) -> None:
        if not self.venv_exists():
            self._say("가상환경(.venv)이 없습니다. setup.bat 으로 설치합니다.")
            self._run_setup()
            return

        def pre(_rc: int, out: str) -> None:
            st = last_json(out)
            if isinstance(st, dict):
                self.install_state = st
                gaps = setupcheck.missing(st)
                if not gaps:
                    self._mark("install", "ok", "이미 설치됨 — setup.bat 생략")
                    self._next()
                    return
                self._say("설치가 덜 됐습니다: " + " · ".join(gaps))
            else:
                self._say("설치 상태를 읽지 못했습니다. setup.bat 으로 설치합니다.")
            self._run_setup()
        self._run(self.state_command(), pre)

    def _run_setup(self) -> None:
        cmd = self.setup_command()
        if cmd is None:
            self._mark("install", "skip", "Windows 가 아니라 setup.bat 을 건너뜀")
            self._next()
            return

        def done(rc: int, out: str) -> None:
            self.setup_rc = rc
            self.setup_out = out
            self._verify_install()
        self._run_retrying("install", lambda: cmd, done, kind="setup")

    def _try_install_remedy(self, st: dict) -> bool:
        """setup.bat 을 통째로 다시 돌리지 않고, 빠진 것만 따로 다시(A7·A8).
        각 해결책은 한 번만. 돌렸으면 True(끝나면 _verify_install 이 다시 판정)."""
        tried_before = {r for r in ("chromium", "ffmpeg", "tailscale")
                        if setup_history.remedy_failed(self.history, "install", r)}
        for r in tried_before - self._remedies:
            self._say(f"! {r} 다시 받기는 지난번에도 해결하지 못해 이번에는 생략합니다.")
            self._remedies.add(r)
        if not st.get("chromium") and "chromium" not in self._remedies:
            self._remedies.add("chromium")
            self._say("Playwright Chromium 만 다시 받습니다.")
            self._run_retrying("install", self.playwright_command,
                               lambda _rc, _o: self._verify_install(), kind="remedy")
            return True
        cmd = self.ffmpeg_command()
        if not st.get("ffmpeg") and cmd is not None and "ffmpeg" not in self._remedies:
            self._remedies.add("ffmpeg")
            self._say("ffmpeg 만 다시 받습니다.")

            def after_ffmpeg(_rc: int, out: str) -> None:
                # install_ffmpeg.bat(--auto)이 전부 실패하면 '다음 할 일: …' 한 줄을 남김
                hints = [ln.split(":", 1)[1].strip() for ln in out.splitlines()
                         if ln.strip().startswith("다음 할 일:")]
                if hints:
                    self._install_hint = "ffmpeg — " + hints[-1]
                self._verify_install()
            self._run_retrying("install", lambda: cmd, after_ffmpeg, kind="remedy")
            return True
        cmd = self.tailscale_command()
        if not st.get("tailscale") and cmd is not None and "tailscale" not in self._remedies:
            self._remedies.add("tailscale")
            self._say("Tailscale 만 다시 설치합니다(winget, 관리자 확인 창이 뜰 수 있음).")
            self._run_retrying("install", lambda: cmd,
                               lambda _rc, _o: self._verify_install(), kind="remedy")
            return True
        return False

    def _verify_install(self) -> None:
        """setup.bat 뒤: 종료 코드가 아니라 실제 설치 상태로 판정합니다.
        (setup.bat 은 Playwright 가 실패해도 0 으로 끝나고, 반대로 끝의 pause·
        echo 가 실패해도 0 이 아닐 수 있습니다.)"""
        rc = getattr(self, "setup_rc", 0)
        if not self.venv_exists():
            cause = setup_failures.classify(self.setup_out, rc or 1)
            if cause is not None and cause.id == "python_missing" and "venv" not in self._remedies:
                # 해결책: 편집기의 파이썬으로 .venv 를 만들고 setup.bat 을 다시(setup.bat 은
                # .venv 가 있으면 그 파이썬을 씀).
                self._remedies.add("venv")
                self._say(f"! {cause.title} — {cause.hint}")
                self._run(self.venv_create_command(), lambda _rc, _o: self._run_setup())
                return
            if cause is not None and cause.id != "unknown":
                self.causes["install"] = cause
            self._mark("install", "fail", self._fail_note(
                "install", f"가상환경(.venv)이 만들어지지 않음 (setup.bat 종료 코드 {rc})"))
            self._next()
            return

        def post(_rc: int, out: str) -> None:
            st = last_json(out)
            if not isinstance(st, dict):
                self._mark("install", "fail", "설치 뒤 상태를 읽지 못함 — 출력 보기")
                self._next()
                return
            self.install_state = st
            gaps = setupcheck.missing(st)
            code = f" (setup.bat 종료 코드 {rc})" if rc else ""
            if not gaps:
                self._mark("install", "ok", "설치 완료" + code)
            elif self._try_install_remedy(st):
                return                    # 해결책을 돌린 뒤 다시 _verify_install
            elif gaps == ["패키지: " + ", ".join(st.get("reqs_bad", []))]:
                # 패키지만 남음 → 바로 다음 '라이브러리 확인' 단계가 채우도록 만든 흐름.
                self._mark("install", "ok", "설치됨 — 남은 패키지는 다음 단계에서 채움" + code)
            else:
                hint = getattr(self, "_install_hint", "")
                self._mark("install", "warn", "덜 됨: " + " · ".join(gaps) + code
                           + (f"\n다음 할 일: {hint}" if hint else ""))
            self._next()
        self._run(self.state_command(), post)

    # 2. 라이브러리 — 확인 → (빠짐·버전·깨짐) 고치기 → 다시 확인
    def _step_verify(self, retried: bool = False) -> None:
        def done(_rc: int, out: str) -> None:
            data = last_json(out)
            if not isinstance(data, dict):
                self._mark("verify", "fail", "확인 결과를 읽지 못함 — 출력 보기")
                self._next()
                return
            self.reqs = data.get("items", [])
            for i in self.reqs:
                self._say(f"  {i['status']:<9} {i['name']:<16} {i.get('detail', '')}")
            bad = [i for i in self.reqs if i["status"] in ("missing", "mismatch", "broken")]
            # 편집기가 지금 쓰는 패키지는 바꾸면 잠겨 실패 → 닫은 뒤 설치로 미룸(A1).
            held = [i for i in bad if i["status"] != "missing" and self.editor_holds(i["name"])]
            held += [i for i in bad if i["name"] in self._locked and i not in held]
            todo = [i for i in bad if i not in held]
            if not bad:
                self._mark("verify", "ok", f"{len(self.reqs)}줄 모두 맞음")
                self._next()
            elif retried or not todo:
                self._finish_verify(bad, held)
            else:
                self._fix_packages(todo)
        self._run(self.reqcheck_command(), done)

    def _fix_packages(self, todo: list[dict]) -> None:
        plain = [i["line"] for i in todo if i["status"] != "broken"]
        broken = [i["line"] for i in todo if i["status"] == "broken"]

        def scan(out: str) -> None:
            # pip 가 파일 잠김으로 실패한 패키지는 '닫은 뒤 설치' 로(A1 을 미리 못 맞힌 경우).
            if LOCK_HINT.search(out):
                for i in todo:
                    self._locked.add(i["name"])
                self._say("! 파일이 잠겨 설치하지 못했습니다(편집기·백신이 쓰는 중일 수 있음).")

        def after_broken(_rc: int, out: str) -> None:
            scan(out)
            self._step_verify(retried=True)

        def after_plain(_rc: int, out: str) -> None:
            scan(out)
            if broken:
                self._say(f"import 가 안 되는 {len(broken)}개를 다시 설치합니다.")
                self._run_retrying("verify", lambda: self.pip_install_command(broken, force=True),
                                   after_broken, kind="pip")
            else:
                self._step_verify(retried=True)

        if plain:
            self._say(f"빠졌거나 버전이 안 맞는 {len(plain)}개를 설치합니다.")
            self._run_retrying("verify", lambda: self.pip_install_command(plain), after_plain,
                               kind="pip")
        else:
            after_plain(0, "")

    def _finish_verify(self, bad: list[dict], held: list[dict]) -> None:
        self.deferred = [i["line"] for i in held]
        self.deferred_force = any(i["status"] == "broken" for i in held)
        parts = []
        rest = [i for i in bad if i not in held]
        if rest:
            parts.append("고치지 못함: " + ", ".join(i["name"] for i in rest))
            if "verify" in self.causes:
                parts.append(self._fail_note("verify", ""))
            if any(i["status"] == "broken" for i in rest):
                parts.append("DLL 오류라면 VC++ 재배포 패키지를 설치하세요")
                self.need_vcredist = True
        if held:
            parts.append("편집기가 쓰는 중이라 닫은 뒤 설치: "
                         + ", ".join(i["name"] for i in held))
        self._mark("verify", "warn", " · ".join(parts))
        self._next()

    # 3. 비밀번호
    def _step_password(self) -> None:
        def done(_rc: int, out: str) -> None:
            data = last_json(out)
            if not isinstance(data, dict):
                self._mark("password", "fail", "자격증명 상태를 읽지 못함 — 출력 보기")
                self._next()
                return
            self.secret_status = data
            where = data.get("eclass", "없음")
            if where != "없음":
                self._mark("password", "ok", f"저장됨({where})")
                self._next()
            elif self._pw_rounds >= 1:
                self._mark("password", "warn", "아직 없음 — 나중에 scripts\\set_password.bat")
                self._next()
            else:
                self._open_password_window()
        self._run(self.status_command(), done)

    def _open_password_window(self, count_round: bool = True) -> None:
        if count_round:
            self._pw_rounds += 1
        self._pw_opened = time.monotonic()
        self.reopen_btn.hide()
        cmd = self.password_command()
        self._say("eClass 비밀번호가 저장돼 있지 않습니다. 비밀번호 저장 창을 엽니다.")
        self._say(f"> {' '.join(cmd)}")
        self.headline.setText("새로 뜬 검은 창(set_password)에 eClass 비밀번호를 입력하세요. "
                              "입력한 글자는 보이지 않습니다. 그 창을 닫으면 계속합니다.")
        try:
            self.pw_proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=self._env(),
                                            creationflags=eclass_fetch.NEW_CONSOLE)
        except OSError as exc:
            self._mark("password", "fail", f"비밀번호 창을 열지 못함: {exc}")
            self._next()
            return
        self._poll.start()

    def _check_password_window(self) -> None:
        if self.pw_proc is None:
            return
        if self.pw_proc.poll() is None:
            if (not self.reopen_btn.isVisible()
                    and time.monotonic() - self._pw_opened > self.PASSWORD_NUDGE):
                # C1: 콘솔이 편집기 뒤에 숨어 못 찾는 경우가 많음
                self.headline.setText("비밀번호 입력 창(검은 창)이 안 보이나요? 작업 표시줄에서 찾거나 "
                                      "'비밀번호 창 다시 열기' 를 누르세요.")
                self.reopen_btn.show()
            return
        self._poll.stop()
        self.reopen_btn.hide()
        self.pw_proc = None
        self.headline.setText("저장한 설정으로 설치와 점검을 이어서 진행합니다.")
        self._say("비밀번호 창이 닫혔습니다. 다시 확인합니다.")
        self._step_password()

    def reopen_password_window(self) -> None:
        """숨은 비밀번호 창을 닫고 새로 엶(같은 회차로 셈 — 반복 방지 횟수는 그대로)."""
        old, self.pw_proc = self.pw_proc, None
        if old is not None and old.poll() is None:
            eclass_fetch._kill_tree(old.pid)
        self._say("비밀번호 창을 다시 엽니다.")
        self._open_password_window(count_round=False)

    # 4. 점검
    def _step_check(self) -> None:
        def doctor_done(_rc: int, out: str) -> None:
            self.doctor = last_json(out)
            err = self.doctor.get("config_error") if isinstance(self.doctor, dict) else None
            if err and self.fix_config is not None and "config" not in self._user_fixes:
                # D1: 설정 오류는 다시 돌려도 같음 → 설정 창(오류 항목이 맨 위)을 엶.
                self._user_fixes.add("config")
                self._say("! 설정 오류로 점검이 멈췄습니다. 설정 창을 엽니다:")
                for line in str(err).splitlines():
                    self._say("  " + line)
                self.headline.setText("설정 오류를 고쳐 주세요 — 저장하면 점검을 다시 합니다.")
                if self.fix_config():
                    self._say("설정을 저장했습니다. 점검을 다시 합니다.")
                    self.headline.setText("저장한 설정으로 설치와 점검을 이어서 진행합니다.")
                    self._run(self.doctor_command(), doctor_done)
                    return
                self._say("설정을 고치지 않았습니다.")
            self._run(self.devices_command(), devices_done)

        def devices_done(_rc: int, out: str) -> None:
            self.devices = last_json(out)
            if not isinstance(self.doctor, dict):
                self._mark("check", "fail", "doctor 결과를 읽지 못함 — 출력 보기")
            elif self.doctor.get("ok") and self.mic_status()[0] == "X":
                self._mark("check", "warn", "점검은 정상이지만 마이크에 소리가 안 들어옴")
            elif self.doctor.get("ok"):
                self._mark("check", "ok", "모두 정상")
            else:
                self._mark("check", "warn", self.doctor.get("verdict") or "확인할 항목 있음")
            self._next()
        self._run(self.doctor_command(), doctor_done)

    # -- 끝: 결과 화면 ----------------------------------------------------
    def _record_history(self) -> None:
        hp = self.history_path()
        if hp is None:
            return
        steps = {k: {"state": self.state[k],
                     "cause": self.causes[k].id if k in self.causes else None,
                     "remedies": sorted(self._remedies) if k == "install" else []}
                 for k, _ in STEPS}
        self.history = setup_history.append(hp, steps)

    def mic_status(self) -> tuple[str, str]:
        """(표시, 설명) — 쓰는 마이크(설정 또는 기본)에 소리가 들어오나(D3)."""
        dev = self.devices if isinstance(self.devices, dict) else {}
        mics = next((g["devices"] for g in dev.get("groups", []) if g.get("kind") == "microphone"),
                    [])
        measured = [m for m in mics if "dead" in m]
        if not measured:
            return "-", "측정 안 함"
        want = str(self._config_values().get("recording.audio.mic_device", "") or "").lower()
        used = next((m for m in measured if want and want in m["name"].lower()), None) \
            or next((m for m in measured if m.get("default")), measured[0])
        if all(m["dead"] for m in measured):
            return "X", ("모든 마이크가 무음입니다 — Windows 설정 > 개인 정보 > 마이크 에서 "
                         "앱의 마이크 사용을 켰는지 확인하세요")
        if used["dead"]:
            alive = [m["name"] for m in measured if not m["dead"]]
            return "X", (f"쓰는 마이크 '{used['name']}' 에 소리가 안 들어옵니다(신호 경로 없음) — "
                         f"recording.audio.mic_device 를 소리가 들어오는 장치로: {', '.join(alive)}")
        return "O", f"'{used['name']}' 소리 들어옴(최대 {used['peak']:.3f})"

    def _finish(self) -> None:
        self._record_history()
        bad = [k for k, s in self.state.items() if s in ("warn", "fail")]
        self.headline.setText("설치와 점검을 마쳤습니다. " + (
            "아래 ⚠·✖ 항목을 확인하세요." if bad else "모두 정상입니다."))
        self.result_scroll.setWidget(self._build_results())
        self.stack.setCurrentIndex(1)
        self.log_btn.setEnabled(True)
        self.close_btn.setText("닫기")
        self.close_btn.clicked.disconnect()
        self.close_btn.clicked.connect(self.accept)

    def _row(self, grid: QGridLayout, r: int, mark: str, name: str, detail: str) -> None:
        """아이콘 | 굵은 이름 + (아래 줄) 회색 설명. 한 칸에 쌓아 긴 이름이 설명을
        밀어내지 않게 하고, 좁으면 줄바꿈됩니다."""
        icon, color = MARK.get(mark, ("•", "#6B7280"))
        a = QLabel(f"<span style='color:{color}; font-weight:bold'>{icon}</span>")
        a.setAlignment(Qt.AlignmentFlag.AlignTop)
        text = f"<b>{_esc(name)}</b>"
        if detail:
            text += f"<br><span style='color:#4B5563'>{_esc(detail)}</span>"
        b = QLabel(text)
        b.setTextFormat(Qt.TextFormat.RichText)
        b.setWordWrap(True)
        b.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        grid.addWidget(a, r, 0)
        grid.addWidget(b, r, 1, 1, 2)
    def _group(self, title: str) -> tuple[QGroupBox, QGridLayout]:
        box = QGroupBox(title)
        grid = QGridLayout(box)
        grid.setColumnStretch(2, 1)
        grid.setVerticalSpacing(6)
        grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        return box, grid

    def _config_values(self) -> dict:
        try:
            from .. import settings_schema as S

            return S.current_values(self.config_path.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001
            return {}

    def checklist(self) -> list[tuple[str, str, str]]:
        """setup.bat 이 끝에 안내하던 '다음 순서' 를 (상태, 할 일, 설명) 으로."""
        cfg = self._config_values()
        need = [k for k in ("github.owner", "github.repo", "eclass.username")
                if not str(cfg.get(k, "")).strip()]
        has_tt = bool(cfg.get("recording.timetable"))
        s1 = ("O" if not need and has_tt else "X",
              "1) config.toml 설정 (github.owner / repo, eclass.username, 시간표)",
              "비어 있음: " + ", ".join(need + ([] if has_tt else ["recording.timetable"]))
              if need or not has_tt else "채워져 있음")
        eclass_pw = self.secret_status.get("eclass", "")
        gh = self.secret_status.get("github", "")
        s2 = ("O" if eclass_pw and eclass_pw != "없음" else "X",
              "2) 비밀번호·토큰 저장 (scripts\\set_password.bat)",
              f"eClass: {eclass_pw or '확인 못 함'} · GitHub 토큰: {gh or '확인 못 함'}")
        d = self.doctor if isinstance(self.doctor, dict) else {}
        s3 = ("O" if d.get("ok") else "X", "3) 환경 점검 (scripts\\doctor.bat)",
              d.get("verdict") or "결과 없음")
        dev = self.devices if isinstance(self.devices, dict) else {}
        set_sys = str(cfg.get("recording.audio.system_device", "") or "")
        set_mic = str(cfg.get("recording.audio.mic_device", "") or "")
        mic_mark, mic_text = self.mic_status()
        s4 = ("O" if dev and not dev.get("audio_error") and mic_mark != "X" else "X",
              "4) 오디오 장치 이름 확인 (scripts\\devices.bat)",
              (f"시스템 소리: {set_sys or '기본 장치'} · 마이크: {set_mic or '기본 장치'}"
               + (f" · {mic_text}" if mic_mark != "-" else ""))
              if dev and not dev.get("audio_error") else (dev.get("audio_error") or "결과 없음"))
        return [s1, s2, s3, s4]

    def _build_results(self) -> QWidget:
        body = QWidget()
        lay = QVBoxLayout(body)

        # 다음 순서 체크 목록
        box, grid = self._group("다음 순서 — 했는지 확인")
        for r, (mark, what, detail) in enumerate(self.checklist()):
            self._row(grid, r, mark, what, detail)
        lay.addWidget(box)

        # 단계 요약
        box, grid = self._group("설치 단계")
        for r, (key, title) in enumerate(STEPS):
            st = self.state[key]
            mark = {"ok": "O", "warn": "!", "fail": "X", "skip": "-"}.get(st, "-")
            self._row(grid, r, mark, title, self.notes.get(key, ""))
        r = len(STEPS)
        if self.deferred:
            self.deferred_btn = QPushButton("편집기를 닫고 설치 (끝나면 다시 엶)")
            self.deferred_btn.setToolTip("새 콘솔 창이 편집기가 닫히기를 기다렸다가 설치합니다: "
                                         + " ".join(self.deferred))
            self.deferred_btn.clicked.connect(self.install_after_close)
            grid.addWidget(self.deferred_btn, r, 1, 1, 2)
            r += 1
        if self.need_vcredist:
            link = QLabel(f"DLL 오류: <a href='{VC_REDIST_URL}'>Microsoft VC++ 재배포 패키지</a> "
                          "를 설치한 뒤 다시 실행하세요.")
            link.setOpenExternalLinks(True)
            link.setWordWrap(True)
            grid.addWidget(link, r, 1, 1, 2)
        lay.addWidget(box)

        # 라이브러리
        if self.reqs:
            box, grid = self._group("라이브러리 (requirements.txt ↔ 설치됨)")
            for r, i in enumerate(self.reqs):
                mark = {"ok": "O", "skip": "-", "missing": "X", "mismatch": "!"}.get(i["status"], "X")
                need = i.get("spec") or "아무 버전"
                self._row(grid, r, mark, i["name"],
                          f"필요 {need} · 설치 {i.get('installed') or '없음'} · {i.get('detail', '')}")
            lay.addWidget(box)

        # doctor
        d = self.doctor if isinstance(self.doctor, dict) else None
        box, grid = self._group("환경 점검 (doctor.bat)")
        r = 0
        if d is None:
            self._row(grid, r, "X", "doctor", "결과를 읽지 못했습니다(출력 보기)")
        else:
            verdict = QLabel(f"<b style='color:{'#15803D' if d.get('ok') else '#C0392B'}'>"
                             f"{_esc(d.get('verdict', ''))}</b>")
            grid.addWidget(verdict, r, 0, 1, 3)
            r += 1
            for label, value in d.get("info", []):
                self._row(grid, r, "•", label, value)
                r += 1
            if d.get("config_error"):
                self._row(grid, r, "X", "설정 파일", d["config_error"])
                r += 1
            for sec in d.get("sections", []):
                head = QLabel(f"<b>{_esc(sec['title'])}</b>")
                head.setStyleSheet("margin-top: 6px;")
                grid.addWidget(head, r, 0, 1, 3)
                r += 1
                for it in sec.get("items", []):
                    self._row(grid, r, it.get("mark", "-"), it.get("name", ""), it.get("detail", ""))
                    r += 1
        lay.addWidget(box)

        # devices
        dev = self.devices if isinstance(self.devices, dict) else None
        box, grid = self._group("오디오 장치 · 화면 (devices.bat)")
        r = 0
        cfg = self._config_values()
        if dev is None:
            self._row(grid, r, "X", "devices", "결과를 읽지 못했습니다(출력 보기)")
        else:
            if dev.get("audio_error"):
                self._row(grid, r, "X", "오디오", dev["audio_error"])
                r += 1
            for g in dev.get("groups", []):
                key = g.get("config_key", "")
                want = str(cfg.get(key, "") or "") if key else ""
                title = g["title"] + (f"  → {key}" if key else "")
                head = QLabel(f"<b>{_esc(title)}</b>" + (
                    f" <span style='color:#6B7280'>(지금 설정: {_esc(want) or '기본 장치'})</span>"
                    if key else ""))
                head.setStyleSheet("margin-top: 6px;")
                grid.addWidget(head, r, 0, 1, 3)
                r += 1
                if not g.get("devices"):
                    self._row(grid, r, "-", "(없음)", "")
                    r += 1
                for d_ in g.get("devices", []):
                    tags = []
                    if d_.get("default"):
                        tags.append("기본")
                    if want and want.lower() in d_["name"].lower():
                        tags.append("← 지금 설정과 일치")
                    mark = "O" if tags else "•"
                    if "dead" in d_:
                        if d_["dead"]:
                            tags.append("⚠ 무음(신호 경로 없음)" + (f": {d_['error']}" if d_.get("error")
                                                                  else ""))
                            mark = "X"
                        else:
                            tags.append(f"소리 들어옴(최대 {d_.get('peak', 0):.3f})")
                    self._row(grid, r, mark, d_["name"], " · ".join(tags))
                    r += 1
            head = QLabel("<b>화면</b>  <span style='color:#6B7280'>"
                          "→ [recording.video] monitor (0 = 전체 바탕화면)</span>")
            head.setStyleSheet("margin-top: 6px;")
            grid.addWidget(head, r, 0, 1, 3)
            r += 1
            for m in dev.get("monitors", []):
                self._row(grid, r, "•", f"monitor = {m['index']}", m.get("text", ""))
                r += 1
            hint = QLabel("config.toml 에는 장치 이름의 일부만 적어도 됩니다(부분 일치). "
                          "파일 → 설정 편집 에서 바꿀 수 있습니다.")
            hint.setStyleSheet("color: #6B7280;")
            hint.setWordWrap(True)
            grid.addWidget(hint, r, 0, 1, 3)
        lay.addWidget(box)
        lay.addStretch(1)
        return body

    def install_after_close(self) -> None:
        """편집기를 닫은 뒤 설치하는 콘솔을 띄우고 편집기를 닫습니다(A1)."""
        cmd = self.deferred_install_command(self.deferred, self.deferred_force)
        self._say(f"> {' '.join(cmd)}")
        try:
            subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=self._env(),
                             creationflags=eclass_fetch.NEW_CONSOLE)
        except OSError as exc:
            self.headline.setText(f"설치 창을 열지 못했습니다: {exc}")
            return
        self.accept()
        win = self.parent()
        if win is not None:
            win.close()                   # 저장은 이미 끝났음(이 창은 저장 뒤에 뜸)
        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)

    # -- 정리 --------------------------------------------------------------
    def done(self, r: int) -> None:
        self._poll.stop()
        p, self.proc = self.proc, None
        if p is not None and p.state() != QProcess.ProcessState.NotRunning:
            try:
                p.finished.disconnect()
            except (RuntimeError, TypeError):
                pass
            eclass_fetch._kill_tree(int(p.processId()))
            p.kill()
            p.waitForFinished(3000)
        if self.pw_proc is not None and self.pw_proc.poll() is None:
            eclass_fetch._kill_tree(self.pw_proc.pid)
        super().done(r)

    def exec(self) -> int:
        QTimer.singleShot(0, self.start)
        return super().exec()
