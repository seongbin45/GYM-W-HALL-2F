"""명령줄 진입점.

  python -m syncnote doctor                환경 점검 (제일 먼저 돌려 보세요)
  python -m syncnote devices               오디오 장치 목록
  python -m syncnote secrets set eclass    비밀번호 저장
  python -m syncnote eclass probe          페이지 구조 탐색
  python -m syncnote eclass sync           자료 수집 + 업로드
  python -m syncnote record start          수동 녹화
  python -m syncnote record schedule       시간표 대기
  python -m syncnote timetable edit        시간표 편집기 (GUI, PySide6)
  python -m syncnote record watch          프로그램 감지 대기
  python -m syncnote audio record          소리만 녹음 (FFmpeg / SoX / arecord)
  python -m syncnote audio tools           터미널 녹음 도구 점검
  python -m syncnote stream start          실시간 송출 (Tailscale, 링크 -> 이슈)
  python -m syncnote stream schedule       시간표대로 수업마다 송출
  python -m syncnote stream link           송출 링크 미리 보기
  python -m syncnote transcribe queue      대기열 전사
  python -m syncnote upload <경로>         임의 파일 업로드
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config as config_module
from . import secrets
from .logging_setup import get as get_logger
from .logging_setup import setup as setup_logging
from .paths import Layout
from .state import Store

log = get_logger("cli")


# ---------------------------------------------------------------------------
def _load(args) -> tuple[config_module.Config, Layout]:
    """설정을 읽습니다.

    녹화·송출 명령(파서에서 lenient_config=True 로 표시)은 설정 일부가 틀려도 멈추지 않고
    고쳐 씁니다(config.load(lenient=True)) — 설정 오타 하나로 그날 수업이 통째로 비지 않게.
    """
    cfg = config_module.load(args.config, lenient=bool(getattr(args, "lenient_config", False)))
    layout = Layout(cfg.data_dir)
    layout.ensure()
    setup_logging(
        layout.logs,
        level=args.log_level or str(cfg.get("general.log_level", "INFO")),
        retention_days=int(cfg.get("general.log_retention_days", 30)),
        console=not args.quiet,
    )
    # 실패·오류를 빠짐없이 GitHub 이슈로(src/syncnote/incidents.py). 잡기는 로컬 대기열만, 올리기는 뒤에서.
    try:
        from . import incidents

        incidents.install(cfg, layout)
    except Exception as exc:            # noqa: BLE001 — 이것 때문에 명령이 멈추면 안 됨
        log.debug("이슈 보고를 켜지 못했습니다: %s", exc)
    for note in getattr(cfg, "load_notes", []):
        log.error("%s", note)
    for note in config_module.warnings_for(cfg):
        log.warning(note)
    return cfg, layout


def _store(layout: Layout, name: str) -> Store:
    return Store.open(layout.state / f"{name}.json")


def _uploader(cfg, *, dry_run: bool = False, course=None, what: str = ""):
    """어디로 올릴지 정합니다. **모르면 None 입니다.**

    네 경우가 있습니다.

        course_repos 비었음 + 과목 모름  -> github.repo   (기존 동작)
        course_repos 비었음 + 과목 있음  -> github.repo   (1과목 PC)
        course_repos 있음  + 과목 맞음   -> 그 과목의 레포
        course_repos 있음  + 과목 모름   -> None (올리지 않음)

    마지막이 중요합니다. 여기서 기본 레포로 흘리면 **A과목 녹화가
    B과목 레포로 갑니다.** 안 올라가는 쪽이 낫습니다.
    """
    from . import courses as courses_mod
    from .release import ReleaseUploader

    if course is None and courses_mod.load(cfg):
        log.warning(
            "업로드 건너뜀%s: 이 자료의 과목을 알 수 없습니다. "
            "--course 로 지정하거나 [[eclass.course_repos]] 에 매핑을 "
            "추가하세요. 파일은 로컬에 그대로 있습니다.",
            f"({what})" if what else "",
        )
        return None

    return ReleaseUploader(
        owner=str(getattr(course, "owner", "") or cfg.get("github.owner", "")),
        repo=str(getattr(course, "repo", "") or cfg.get("github.repo", "")),
        retry_count=int(cfg.get("github.retry_count", 4)),
        retry_backoff=float(cfg.get("github.retry_backoff", 2.0)),
        split_size_mb=int(cfg.get("github.split_size_mb", 1900)),
        draft=bool(cfg.get("github.draft", False)),
        prerelease=bool(cfg.get("github.prerelease", False)),
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
def cmd_doctor(args) -> int:
    """환경 점검. --json 이면 같은 내용을 구조화해 한 줄 JSON 으로(시간표 편집기가 그림)."""
    import contextlib
    import io
    import json

    rep: dict = {"ok": True, "info": [], "config_error": None, "sections": [], "verdict": ""}
    if getattr(args, "json", False):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = _doctor(args, rep)
        print(json.dumps(rep, ensure_ascii=False))
        return rc
    return _doctor(args, rep)


def _doctor(args, rep: dict) -> int:
    def info(label: str, value) -> None:
        rep["info"].append([label, str(value)])

    def section(title: str) -> list:
        items: list = []
        rep["sections"].append({"title": title, "items": items})
        return items

    print("환경 점검")
    print("=" * 62)
    ok = True

    print(f"  Python          : {sys.version.split()[0]}")
    info("Python", sys.version.split()[0])
    if sys.version_info < (3, 10):  # noqa: UP036 - pip 를 거치지 않고 실행한 경우 안내
        print("    ! Python 3.10 이상을 권장합니다.")
        section("Python").append({"mark": "!", "name": "Python", "detail": "3.10 이상을 권장합니다"})
        ok = False

    # 설정
    try:
        cfg, layout = _load(args)
        print(f"  설정 파일        : {cfg.path}")
        print(f"  데이터 폴더      : {cfg.data_dir}")
        print(f"  기기 이름        : {cfg.machine_label}")
        info("설정 파일", cfg.path)
        info("데이터 폴더", cfg.data_dir)
        info("기기 이름", cfg.machine_label)
        # 녹화 지킴이 장부: 아직 올라가 확인되지 않은 녹화가 있는지(알림만, 판정에는 안 씀)
        from .recorder import guard as _guard
        _c = _guard.Ledger(layout.state / _guard.LEDGER).counts()
        _todo = sum(n for s, n in _c.items() if s not in (_guard.CONFIRMED, _guard.HELD))
        _line = (f"올라가 확인되지 않은 녹화 {_todo}개 (격리 {_c.get(_guard.QUARANTINE, 0)}, "
                 f"막힘 {_c.get(_guard.BLOCKED, 0)}) — record guard --status"
                 if _todo else "못 올린 녹화 없음")
        print(f"  녹화 지킴이      : {_line}")
        info("녹화 지킴이", _line)
    except config_module.ConfigError as exc:
        print(f"  설정 파일        : 실패\n{exc}")
        rep["config_error"] = str(exc)
        rep["ok"] = False
        rep["verdict"] = "설정 파일을 읽지 못해 점검을 멈췄습니다."
        return 1

    # 패키지
    checks = [
        ("requests", "GitHub 업로드", True),
        ("keyring", "비밀번호 저장", True),
        ("numpy", "오디오 처리", bool(cfg.get("recording.enabled"))),
        ("soundcard", "시스템 소리 캡처", bool(cfg.get("recording.audio.enabled"))),
        ("playwright", "Eclass 수집", bool(cfg.get("eclass.enabled"))),
        ("faster_whisper", "음성 전사", bool(cfg.get("transcribe.enabled"))),
        ("psutil", "프로그램 감지(선택)", bool(cfg.get("recording.triggers.watch", True))),
    ]
    print("\n  파이썬 패키지")
    pkgs = section("파이썬 패키지")
    for module, purpose, required in checks:
        try:
            __import__(module)
            print(f"    [O] {module:<16} {purpose}")
            pkgs.append({"mark": "O", "name": module, "detail": purpose})
        except ImportError:
            mark = "[X]" if required else "[-]"
            fix = f"pip install {module.replace('_','-')}"
            print(f"    {mark} {module:<16} {purpose}  -> {fix}")
            pkgs.append({"mark": mark[1], "name": module, "detail": f"{purpose} — {fix}"})
            if required:
                ok = False

    # ffmpeg
    print("\n  외부 도구")
    tools = section("외부 도구")
    try:
        from .recorder.ffmpeg_util import find_ffmpeg, find_ffprobe, version as _ffver

        ff = find_ffmpeg()
        ver = _ffver(ff)
        # 경로만 보면 PATH 에 걸린 제3자/오래된 ffmpeg(예: DocZoom 2018)를 못 가립니다.
        suffix = f"  (버전 {ver})" if ver else ""
        print(f"    [O] ffmpeg           {ff}{suffix}")
        tools.append({"mark": "O", "name": "ffmpeg", "detail": f"{ff}{suffix}"})
        probe = find_ffprobe()
        print(f"    {'[O]' if probe else '[-]'} ffprobe          {probe or '없음(길이 계산만 영향)'}")
        tools.append({"mark": "O" if probe else "-", "name": "ffprobe",
                      "detail": str(probe) if probe else "없음(길이 계산만 영향)"})
    except Exception as exc:
        print(f"    [X] ffmpeg           {exc}")
        tools.append({"mark": "X", "name": "ffmpeg", "detail": str(exc)})
        if cfg.get("recording.enabled"):
            ok = False

    # SoX(모든 OS) · arecord(리눅스) — 녹음 방법 사슬(soundcard → ffmpeg → SoX → arecord)의
    # 대체 경로이자 소리 감지 녹음(--vad)의 도구. 필수입니다.
    from . import sox_fetch

    sox = sox_fetch.find_sox(check=False)
    if sox:
        print(f"    [O] sox              {sox}")
        tools.append({"mark": "O", "name": "sox", "detail": sox})
    else:
        fix = ("scripts\\setup.bat 을 다시 실행하거나 python -m syncnote.sox_fetch"
               if sys.platform == "win32" else
               (sox_fetch.MAC_HINT if sys.platform == "darwin" else sox_fetch.LINUX_HINT))
        print(f"    [X] sox              없음 -> {fix}")
        tools.append({"mark": "X", "name": "sox", "detail": f"없음 — {fix}"})
        ok = False
    if sys.platform.startswith("linux"):
        import shutil as _sh

        ar = _sh.which("arecord")
        print(f"    {'[O]' if ar else '[X]'} arecord          {ar or '없음 -> ' + sox_fetch.LINUX_HINT}")
        tools.append({"mark": "O" if ar else "X", "name": "arecord",
                      "detail": ar or f"없음 — {sox_fetch.LINUX_HINT}"})
        if not ar:
            ok = False

    # PowerShell 실행 정책 — 확인만(바꾸는 것은 doctor.bat·모든 스크립트의 _common.bat 과
    # 자동 등록이 함). 막혀 있으면 .ps1 을 직접 실행할 때 창이 바로 닫힙니다.
    from . import exec_policy as _ep

    if _ep.supported():
        print("\n  PowerShell 실행 정책")
        pol_items = section("PowerShell 실행 정책")
        pol = _ep.status()
        mark = "O" if pol.ok else ("-" if pol.gpo_blocked else "X")
        fix = "" if pol.ok or pol.gpo_blocked else (
            " -> 아무 스크립트(doctor.bat 등)나 실행하면 RemoteSigned 로 영구 설정합니다")
        print(f"    [{mark}] 실행 정책         {pol.describe()}{fix}")
        pol_items.append({"mark": mark, "name": "실행 정책", "detail": pol.describe() + fix})
        if mark == "X":
            ok = False

    # 작업 스케줄러 — 등록이 빠지면 시간표·감지 녹화와 지킴이가 한 번도 돌지 않습니다.
    from . import tasks as _tasks

    if _tasks.supported():
        print("\n  작업 스케줄러")
        sched_items = section("작업 스케줄러")
        st = _tasks.check()
        if st.ok:
            print(f"    [O] Sync Note 작업   {st.describe()}")
            sched_items.append({"mark": "O", "name": "Sync Note 작업", "detail": st.describe()})
        else:
            fix = "doctor.bat·setup.bat 이 이어서 등록합니다(python -m syncnote tasks ensure)"
            print(f"    [X] Sync Note 작업   {st.describe()} -> {fix}")
            sched_items.append({"mark": "X", "name": "Sync Note 작업",
                                "detail": f"{st.describe()} — {fix}"})
            ok = False

    # git · gh — 필수(자동 업데이트, 오류 이슈 보고)
    import shutil as _shutil

    for exe, purpose in (("git", "자동 업데이트(git pull origin main)"),
                         ("gh", "오류 이슈 보고(토큰이 없을 때)")):
        found = _shutil.which(exe)
        mark = "O" if found else "X"
        missing = "없음 -> scripts\\setup.bat 을 다시 실행(winget 으로 설치)"
        print(f"    [{mark}] {exe:<16} {found or missing}")
        tools.append({"mark": mark, "name": exe, "detail": found or f"없음 — {purpose}"})
        if not found:
            ok = False
    if _shutil.which("gh"):
        from . import incidents as _inc

        authed = _inc.gh_ready()
        print(f"    [{'O' if authed else '-'}] gh 로그인        "
              f"{'됨' if authed else '안 됨 — 토큰(secrets github)이 있으면 그것으로 올림, 없으면 gh auth login'}")

    # 비밀값
    print("\n  자격증명")
    creds = section("자격증명")
    for name, where in secrets.status().items():
        mark = "[O]" if where != "없음" else "[-]"
        print(f"    {mark} {name:<16} {where}")
        creds.append({"mark": mark[1], "name": name, "detail": where})

    # github 토큰은 '있다' 로 끝내지 않고 실제로 GitHub 에 물어봅니다.
    # 존재 != 유효 — 만료된 토큰이 [O] 로 보이면 업로드가 전멸인데도 doctor 가
    # '모두 정상' 이라고 합니다(실측된 증상). GET /user 한 번.
    # --json(편집기 폴링)에서는 매번 네트워크를 타지 않도록 건너뜁니다.
    if not getattr(args, "json", False):
        from .release import check_token

        tmark, detail = check_token()
        print(f"    [{tmark}] github 유효성     {detail}")
        creds.append({"mark": tmark, "name": "github 유효성", "detail": detail})
        if tmark == "X":
            ok = False

    # 오디오 장치
    if cfg.get("recording.audio.enabled"):
        print("\n  오디오 장치")
        audio = section("오디오 장치")
        try:
            from .recorder import devices as dev

            found = dev.list_devices()
            loop = [d for d in found if d.kind == "loopback"]
            mics = [d for d in found if d.kind == "microphone"]
            print(f"    루프백 {len(loop)}개 / 마이크 {len(mics)}개")
            audio.append({"mark": "O" if loop or mics else "-", "name": "장치",
                          "detail": f"루프백 {len(loop)}개 / 마이크 {len(mics)}개"})
            if not loop and cfg.get("recording.audio.system_enabled"):
                print("    ! 시스템 소리를 잡을 루프백 장치가 없습니다.")
                audio.append({"mark": "X", "name": "루프백",
                              "detail": "시스템 소리를 잡을 루프백 장치가 없습니다"})
                ok = False
            help_ = dev.mix_only_help() if cfg.get("recording.audio.mic_enabled", True) else None
            if help_:
                guide, url = help_
                print("    [X] 마이크 입력 없음 — 기본 녹음 장치가 스테레오 믹스")
                for line in guide.splitlines():
                    print("      " + line)
                audio.append({"mark": "X", "name": "마이크 입력", "detail": guide})
                ok = False
                if not getattr(args, "json", False):   # 편집기가 읽는 --json 에서는 띄우지 않음
                    from .recorder import driver_help

                    how = driver_help.open_search(url)
                    if how:
                        print(f"      → {how} 에 이 PC 메인보드의 오디오 드라이버 검색을 열었습니다.")
        except Exception as exc:
            print(f"    [X] {exc}")
            audio.append({"mark": "X", "name": "장치", "detail": str(exc)})

    print("\n" + "=" * 62)
    verdict = "모두 정상입니다." if ok else "위의 [X] 항목을 해결하세요."
    print(verdict)
    rep["ok"] = ok
    rep["verdict"] = verdict
    return 0 if ok else 1


# ---------------------------------------------------------------------------
def _allow_mic() -> int:
    """Windows 마이크 권한을 확인하고 꺼져 있으면 켭니다(devices --allow-mic, setup 에서 부름)."""
    from .recorder import devices

    denied = devices.mic_permission_denied()
    if denied is None:
        print("마이크 권한: 확인할 수 없습니다(Windows 가 아니거나 설정이 없음).")
        return 0
    if not denied:
        print("마이크 권한: 켜져 있습니다.")
        _print_mix_guidance()
        return 0
    print("마이크 권한: 꺼져 있습니다 — 켭니다.")
    changed, problems = devices.enable_mic_permission()
    for c in changed:
        print(f"  [OK] {c} 를 켰습니다.")
    for p in problems:
        print(f"  [X] {p}")
    still = devices.mic_permission_denied()
    print("마이크 권한: " + ("아직 꺼진 곳이 있습니다." if still else "이제 켜져 있습니다."))
    _print_mix_guidance()
    return 1 if still else 0


def _print_mix_guidance(*, open_browser: bool = True, full: bool = True) -> bool:
    """기본 녹음 장치가 스테레오 믹스이고 마이크 입력이 없으면 드라이버 안내를 보여 주고,
    이 PC 메인보드의 오디오 드라이버 검색을 Chromium 으로 띄움(사람이 실행한 명령에서만)."""
    from .recorder import devices, driver_help

    help_ = devices.mix_only_help()
    if not help_:
        return False
    guide, url = help_
    print()
    if full:
        print(guide)
    else:                          # 목록(describe)에 안내가 이미 있음 — 메인보드·검색만
        print("\n".join(ln for ln in guide.splitlines() if "메인보드" in ln or "검색:" in ln))
    if open_browser:
        how = driver_help.open_search(url)
        print(f"  → {how} 에 드라이버 검색을 열었습니다." if how
              else f"  → 브라우저를 열지 못했습니다. 직접 여세요: {url}")
    return True


def cmd_devices(args) -> int:
    from .recorder.devices import describe
    from .recorder.monitors import enumerate_monitors

    if getattr(args, "allow_mic", False):
        return _allow_mic()
    if getattr(args, "json", False):
        print(_devices_json(enumerate_monitors, probe=getattr(args, "probe", False)))
        return 0
    print(describe())
    _print_mix_guidance(full=False)   # 목록에 안내는 이미 있음 — 메인보드·드라이버 검색을 띄움
    print()
    print("화면")
    print("=" * 40)
    mons = enumerate_monitors()
    if not mons:
        print("  (목록을 읽을 수 없습니다. Windows 가 아니거나 권한 문제입니다.)")
        print("  monitor = 0 이면 전체 바탕화면을 잡습니다.")
    else:
        for m in mons:
            print(f"  {m.describe()}")
        print()
        print("  [recording.video] monitor = <번호>  (0 = 전체 바탕화면)")
        print("  window_title 을 적어 두면 monitor 는 무시됩니다.")
    return 0


def _devices_json(enumerate_monitors, probe: bool = False) -> str:
    """오디오 장치·화면 목록을 구조화(describe() 와 같은 분류·같은 설정 키).

    probe=True 면 **마이크만** 잠깐(1초) 열어 최대 레벨을 잽니다(디스크에 안 씀,
    devices.probe_mic). 정확히 0 이면 '신호 경로 없음'(가상 장치·권한 꺼짐).
    시스템 소리(루프백)는 재생 중인 게 없으면 0 이 정상이라 재지 않습니다.
    """
    import json

    from .recorder import devices as dev

    out: dict = {"audio_error": None, "groups": [], "monitors": [], "probed": bool(probe)}
    try:
        found = dev.list_devices()
    except Exception as exc:  # noqa: BLE001 — soundcard 없음 등도 화면에 보여 줌
        out["audio_error"] = str(exc)
        found = []
    levels: dict[str, dict] = {}
    if probe and found:
        try:
            for mic in dev._sc().all_microphones(include_loopback=False):
                r = dev.probe_mic(mic, duration_sec=1.0)
                levels[str(mic.name)] = {"peak": r.peak, "dead": r.dead, "error": r.error}
        except Exception as exc:  # noqa: BLE001
            out["probe_error"] = str(exc)
    for kind, title, key in (
        ("loopback", "시스템 소리 캡처용", "recording.audio.system_device"),
        ("microphone", "마이크", "recording.audio.mic_device"),
        ("speaker", "재생 장치 (참고용)", ""),
    ):
        out["groups"].append({
            "kind": kind, "title": title, "config_key": key,
            "devices": [{"name": d.name, "default": bool(d.is_default),
                         **(levels.get(d.name, {}) if kind == "microphone" else {})}
                        for d in found if d.kind == kind]})
    try:
        out["monitors"] = [{"index": m.index, "text": m.describe(), "primary": bool(m.primary)}
                           for m in enumerate_monitors()]
    except Exception:  # noqa: BLE001
        out["monitors"] = []
    return json.dumps(out, ensure_ascii=False)


def cmd_secrets(args) -> int:
    if args.action == "set":
        secrets.set_interactive(args.name)
    elif args.action == "delete":
        secrets.delete(args.name)
    else:
        print("자격증명 상태")
        print("=" * 40)
        for name, where in secrets.status().items():
            print(f"  {name:<10} {where}")
        print("\n값 자체는 절대 표시되지 않습니다.")
    return 0


# ---------------------------------------------------------------------------
# eclass
# ---------------------------------------------------------------------------
def cmd_eclass_probe(args) -> int:
    from .eclass.probe import run as probe_run
    from .eclass.urls import Site

    cfg, layout = _load(args)
    site = Site(str(cfg.get("eclass.base_url", "https://eclass.kunsan.ac.kr")))
    probe_run(
        site,
        str(cfg.get("eclass.username", "")),
        layout.state / "probe",
        headless=not args.no_headless and bool(cfg.get("eclass.headless", True)),
        timeout_ms=int(cfg.get("eclass.timeout_ms", 30000)),
        request_delay_sec=float(cfg.get("eclass.request_delay_sec", 1.0)),
        state_path=layout.state / "eclass_session.json",
        course_limit=args.courses,
        do_redact=not args.no_redact,
        include_risky=False,
        catalog_dir=layout.state,           # 시간표 편집기가 과목코드를 채우는 데 씀
        term=_term_label(),
    )
    return 0


def _term_label(when: date | None = None) -> str:
    when = when or date.today()
    return f"{when.year}-{1 if when.month <= 7 else 2}학기"


def cmd_eclass_sync(args) -> int:
    from .eclass.browser import EclassBrowser
    from .eclass.crawler import Crawler
    from .eclass.urls import Site

    cfg, layout = _load(args)
    if not cfg.get("eclass.enabled"):
        log.info("eclass.enabled = false 입니다. 아무것도 하지 않습니다.")
        return 0

    site = Site(str(cfg.get("eclass.base_url", "https://eclass.kunsan.ac.kr")))
    store = _store(layout, "eclass")
    term = args.term or _term_label()
    log.info("Eclass 수집 시작 (%s). %s", term, store.summary())

    with EclassBrowser(
        site,
        str(cfg.get("eclass.username", "")),
        headless=not args.no_headless and bool(cfg.get("eclass.headless", True)),
        timeout_ms=int(cfg.get("eclass.timeout_ms", 30000)),
        request_delay_sec=float(cfg.get("eclass.request_delay_sec", 1.0)),
        state_path=layout.state / "eclass_session.json",
    ) as browser:
        browser.login()
        crawler = Crawler(
            browser, site, layout, store,
            term=term,
            menus=cfg.section("eclass.menus"),
            risky=cfg.section("eclass.risky"),
            menu_id_overrides=cfg.section("eclass.menu_ids"),
            message_boxes=cfg.get("eclass.message_boxes", ["RECEIVE"]),
            screenshots=bool(cfg.get("eclass.screenshots", True)),
            capture_html=bool(cfg.get("eclass.capture_html", True)),
        )
        # Actions 등에서 SYNCNOTE_ECLASS_COURSES 를 주면 config 보다 우선
        # (콤마·세미콜론·줄바꿈 구분, 띄어쓰기·음절 순서 무관 퍼지 매칭).
        from .eclass.coursematch import split_selectors
        env_courses = split_selectors(os.environ.get("SYNCNOTE_ECLASS_COURSES", ""))
        stats = crawler.run(
            env_courses or list(cfg.get("eclass.courses", [])),
            list(cfg.get("eclass.exclude_courses", [])),
        )

    log.info("수집 결과: %s", stats.line())
    for err in stats.errors[:10]:
        log.warning("  ! %s", err)
    # 숫자만 보면 성공인데 실제로는 아무것도 못 받은 경우를 짚어 줍니다.
    suspicions = stats.suspicions()
    for note in suspicions:
        log.warning("※ %s", note)
    if suspicions and os.environ.get("GITHUB_ACTIONS") == "true":
        # Actions 요약에 노란 경고로 뜹니다. 초록불만 보고 넘어가지
        # 않도록 하는 것이 목적입니다.
        for note in suspicions:
            print(f"::warning title=수집 결과가 의심스럽습니다::{note}", flush=True)

    if args.no_upload or not cfg.get("eclass.upload.enabled") or not cfg.get("github.enabled"):
        log.info("업로드는 생략합니다.")
        return 0

    return _upload_eclass(cfg, layout, store, term, dry_run=args.dry_run)


def _upload_eclass(cfg, layout: Layout, store: Store, term: str, *, dry_run: bool) -> int:
    """과목(=레포)마다 따로 올립니다. 종료 코드를 돌려줍니다.

    한 과목이 실패해도 나머지는 그대로 올라가야 합니다. 예전에는 학기
    폴더 전체를 한 레포로 한 번에 올려서, 중간에 하나가 터지면 그 뒤가
    전부 멈췄습니다.

    **0 을 아무 때나 돌려주면 안 됩니다.** check_access 의 사전 차단을
    경고로 낮췄으므로(Actions 토큰은 contents:write 가 있어도
    /repos/ 응답의 permissions.push 가 false 로 옵니다), 이제 실패를
    알아채는 자리는 여기 하나뿐입니다. 2026-11-12 실행은 업로드 0건에
    종료 코드 0, CI 초록불이었습니다.
    """
    import tempfile

    from . import courses as courses_mod
    from .eclass.urls import MENU_LABELS
    from .paths import safe_name
    from .release import (
        ReleaseError,
        _human,
        bundle_filename,
        group_by_top_folder,
        iter_files,
        machine_prefix,
        make_archive,
        tag_for,
        upload_filter,
        upload_many,
        write_index,
    )
    from .release import asset_name as _asset_name

    excluded_labels = {
        MENU_LABELS.get(m, m) for m in cfg.get("eclass.upload.exclude_menus", [])
    }

    # 이번 학기 폴더만 올립니다. layout.eclass 전체를 훑으면 지난 학기
    # 파일까지 매번 다시 해시 계산하게 됩니다.
    root = layout.eclass / safe_name(term)
    if not root.exists():
        log.info("업로드할 폴더가 없습니다: %s", root)
        return 0

    files = [p for p in iter_files(root) if not (set(p.parts) & excluded_labels)]
    files, dropped = upload_filter(
        files,
        allow_extensions=cfg.get("eclass.upload.allow_extensions", []),
        max_file_mb=int(cfg.get("eclass.upload.max_file_mb", 0)),
    )
    for path, why in dropped:
        log.info("업로드 제외: %s (%s)", path.name, why)
    if dropped:
        log.info("업로드에서 뺀 파일 %d개 — 로컬에는 그대로 있습니다.", len(dropped))
    if not files:
        log.info("새로 올릴 파일이 없습니다.")
        return 0

    groups = group_by_top_folder(root, files)
    if not groups:
        log.info("새로 올릴 폴더가 없습니다.")
        return 0

    tag = tag_for(str(cfg.get("github.tag_prefix_eclass", "eclass")))
    course_list = courses_mod.load(cfg)
    # 같은 과목이 두 강의실 PC 에 배포될 수 있습니다. 이름이 같으면 뒤에
    # 올린 쪽이 앞엣것을 지웁니다(중복 제거 기록은 PC 안에만 있어 서로를
    # 못 봅니다). 기기 이름을 에셋 이름에 넣어 애초에 겹치지 않게 합니다.
    prefix = machine_prefix(cfg.machine_label)

    # 폴더 -> 과목(레포). 과목을 안 적었으면 전부 기본 레포 한 곳으로.
    plan: dict[object, dict[str, list[Path]]] = {}
    unknown: list[str] = []
    for folder, members in sorted(groups.items()):
        course = courses_mod.by_name(course_list, folder) if course_list else None
        if course_list and course is None:
            unknown.append(folder)
            continue
        plan.setdefault(course, {})[folder] = members
    for folder in unknown:
        # 담당이 아닌 과목입니다. 받아는 뒀지만 올릴 레포가 없습니다.
        log.info("업로드 건너뜀(이 PC 담당 과목이 아님): %s", folder)

    ok, failed = 0, []
    tally: dict[str, int] = {"uploaded": 0, "skipped": 0, "failed": 0,
                             "collision": 0, "missing": 0}
    for course, folders in plan.items():
        where = course.slug if course is not None else "기본 레포"
        try:
            uploader = _uploader(cfg, dry_run=dry_run, course=course)
            uploader.ensure_repo(
                create_missing=bool(cfg.get("github.create_missing_repos", False)),
                private=bool(cfg.get("github.repo_private", True)),
            )
            # 공개 저장소 차단 가드(config 로 켜고 끔). eClass 자료에는 동급생
            # 이름·화면·파일명 등 개인정보가 섞일 수 있어, 대상 레포가 public
            # 이면 올리지 않습니다. is_public()==None(모름)이면 막지 않습니다
            # (네트워크 한 번 실패로 수집을 세우지 않음). 끄려면 config 에서
            # eclass.upload.block_public = false.
            is_pub_fn = getattr(uploader, "is_public", None)
            if bool(cfg.get("eclass.upload.block_public", True)) \
                    and callable(is_pub_fn) and is_pub_fn() is True:
                skipped_n = sum(len(v) for v in folders.values())
                msg = (f"{where}: 대상 저장소가 public 이라 올리지 않았습니다. "
                       "eClass 자료에는 개인정보가 섞일 수 있습니다. 저장소를 "
                       "private 로 바꾸거나, 정말 공개하려면 config 의 "
                       "eclass.upload.block_public = false 로 끄세요.")
                log.error("%s", msg)
                failed.append(where)
                tally["skipped"] += skipped_n
                continue
            total = sum(len(v) for v in folders.values())
            log.info("[%s] 릴리스 '%s' 로 폴더 %d개 (파일 %d개)",
                     where, tag, len(folders), total)
            with tempfile.TemporaryDirectory(prefix="syncnote-zip-") as work:
                bundles: list[Path] = []
                rows: list[tuple[str, str, int, int]] = []
                for name, members in sorted(folders.items()):
                    # 과목이면 main_, 쪽지·계정 등이면 sub_ (해시 앞에 남음)
                    zip_path = Path(work) / bundle_filename(name)
                    if make_archive(root, members, zip_path) is None:
                        continue
                    size = zip_path.stat().st_size
                    log.info("  %s: 파일 %d개 -> %s", name, len(members), _human(size))
                    bundles.append(zip_path)
                    rows.append((_asset_name(zip_path.name, prefix=prefix,
                                             unique_key=zip_path.name),
                                 name, len(members), size))
                if rows:
                    # 목차도 PC 마다 따로 올라가야 서로 덮어쓰지 않습니다.
                    index = Path(work) / f"INDEX_{cfg.machine_label or 'this-pc'}.md"
                    bundles.append(write_index(index, rows, tag=tag))
                done = upload_many(uploader, bundles, tag, prefix=prefix,
                                   store=store, root=Path(work), tally=tally)
            ok += len(done)
        except ReleaseError as exc:
            # 한 과목이 막혀도 나머지는 올라가야 합니다.
            log.error("[%s] 업로드 실패 — 다른 과목은 계속합니다: %s", where, exc)
            failed.append(where)
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] 업로드 중 예상 못한 오류: %s", where, exc)
            failed.append(where)

    log.info("업로드 완료 %d건 (중복/실패 제외)", ok)
    if failed:
        log.error("업로드하지 못한 레포 %d곳: %s", len(failed), ", ".join(failed))

    # 사전 점검을 없앴으면 사후 검증이 반드시 있어야 합니다.
    blocked = (tally["failed"] + tally["collision"] + tally["missing"]
               + len(failed))
    if blocked:
        detail = (f"성공 {ok}건 / 실패 {tally['failed']} / 이름충돌 "
                  f"{tally['collision']} / 파일없음 {tally['missing']} / "
                  f"레포단위실패 {len(failed)}")
        if ok == 0:
            log.error("업로드 0건 — 한 개도 못 올렸습니다. %s", detail)
            _actions_error("업로드 전부 실패", f"{detail}. 로그의 첫 오류를 보세요.")
        else:
            log.error("일부만 올라갔습니다. %s", detail)
            _actions_error("업로드 일부 실패", detail)
        return 1
    if ok == 0 and tally["skipped"]:
        log.info("새로 올릴 것이 없습니다(전부 이미 올라가 있음 %d건).",
                 tally["skipped"])
    return 0


def _actions_error(title: str, message: str) -> None:
    """GitHub Actions 요약에 빨간 줄로 띄웁니다.

    warning 은 초록불 옆에 작게 뜨고 아무도 안 봅니다. 결정적 실패는
    ::error:: 여야 리뷰어 눈에 들어옵니다.
    """
    if os.environ.get("GITHUB_ACTIONS") == "true":
        clean = str(message).replace("\n", " ")[:400]
        print(f"::error title={title}::{clean}", flush=True)


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------
def _upload_recording_file(cfg, store: Store, uploader, path: Path, tag: str) -> None:
    """녹화 파일 하나를 릴리스에 올립니다(이미 올렸으면 건너뜀). 실패하면 예외."""
    from .state import sha256_file

    digest = sha256_file(path)
    if store.is_uploaded(digest):
        return
    _check_recording_public(cfg, uploader)
    result = uploader.upload(path, tag, prefix=f"{cfg.machine_label}_")
    store.mark_uploaded(
        digest, tag=tag,
        asset_name=result["assets"][0].get("name", "") if result["assets"] else "",
        size=path.stat().st_size,
    )
    store.save()
    if cfg.get("github.delete_local_after_upload"):
        path.unlink(missing_ok=True)
        log.info("업로드 후 로컬 파일 삭제: %s", path.name)


class PublicRepoBlocked(Exception):
    """recording.upload.block_public 이 켜져 있고 대상 저장소가 public."""


_PUBLIC_CACHE: dict[str, bool | None] = {}


def _check_recording_public(cfg, uploader) -> None:
    """녹화를 public 저장소에 올리지 않는 안전장치(기본 꺼짐). 막아야 하면 예외."""
    if not bool(cfg.get("recording.upload.block_public", False)):
        return
    fn = getattr(uploader, "is_public", None)
    if not callable(fn):
        return
    slug = str(getattr(uploader, "slug", "") or id(uploader))
    if slug not in _PUBLIC_CACHE:
        _PUBLIC_CACHE[slug] = fn()
    if _PUBLIC_CACHE[slug] is True:
        raise PublicRepoBlocked(
            f"{slug} 는 public 저장소라 녹화를 올리지 않았습니다(PC 에는 남아 있음). 저장소를 "
            "private 로 바꾸거나 config 의 recording.upload.block_public = false 로 끄세요.")


def _recording_tag(cfg, day) -> str:
    """녹화한 날의 릴리스(올린 날이 아니라). 다음 날 복구해 올려도 그날 릴리스로 갑니다."""
    from .release import tag_for

    return tag_for(str(cfg.get("github.tag_prefix_recording", "recordings")), day)


def _post_segment_factory(cfg, layout: Layout, store: Store, label: str, *,
                          course=None,
                          upload: bool, dry_run: bool, language: str | None = None,
                          defer: bool = False):
    """세그먼트가 끝나면: 전사 대기열 → 올릴 목록(outbox)에 적기 → 올리기 → 목록에서 지우기.

    올리다 실패하거나 그 사이 컴퓨터가 꺼져도 목록에 남아 다음 시작 때 다시 올립니다.
    defer=True(꺼진 녹화 복구)면 목록에만 적고, 올리기는 _drain_outbox 가 합니다.
    """
    from .recorder import outbox
    from .transcribe.batch import enqueue_segment

    uploader = _uploader(cfg, dry_run=dry_run, course=course, what=label) if upload else None
    course_id = str(getattr(course, "id", "") or "")

    def on_done(segment) -> None:
        try:
            enqueue_segment(cfg, layout, segment, label, language=language,
                            course=course)
        except Exception:
            log.exception("전사 대기열 등록 실패")

        # 무음이어도 올립니다. 예전에는 '장치 무음으로 중단된 세그먼트' 를 올리지 않았는데,
        # 그러면 조용했던 수업·마이크를 늦게 꽂은 수업이 통째로 빠집니다(사용자 요청: 무음
        # 감지 시 녹화·업로드 차단 금지). 무음 여부는 로그·경고로만 알립니다.
        if uploader is None or not segment.path.exists() or segment.path.is_dir():
            return
        day = segment.started_at.date()
        if not dry_run:
            outbox.add(layout.state, segment.path, course_id=course_id, label=label,
                       day=day.isoformat())
        if defer:
            return
        try:
            _upload_recording_file(cfg, store, uploader, segment.path, _recording_tag(cfg, day))
            outbox.remove(layout.state, segment.path)
        except PublicRepoBlocked as exc:
            log.error("%s", exc)
        except Exception:
            log.exception("세그먼트 업로드 실패 (로컬 파일은 남아 있고, 녹화 지킴이가 "
                          "녹화가 끝난 뒤 다시 올립니다)")

    return on_done


def _drain_outbox(cfg, layout: Layout, *, dry_run: bool = False) -> tuple[int, int]:
    """올릴 목록에 남은 녹화 파일을 올립니다. (올림, 실패)."""
    from datetime import date as _date

    from .recorder import outbox

    pending = outbox.items(layout.state)
    if not pending:
        return 0, 0
    log.info("지난번에 올리지 못한 녹화 %d개를 올립니다.", len(pending))
    store = _store(layout, "recordings")
    ok = failed = 0
    for it in pending:
        path = Path(it["path"])
        if not path.exists():
            log.info("올릴 목록에서 뺍니다(파일이 없음): %s", path)
            outbox.remove(layout.state, path)
            continue
        try:
            day = _date.fromisoformat(it.get("day") or "")
        except ValueError:
            day = datetime.fromtimestamp(path.stat().st_mtime).date()
        course = _resolve_course(cfg, course_id=str(it.get("course_id") or ""),
                                 label=str(it.get("label") or ""),
                                 when=datetime.combine(day, datetime.min.time()))
        uploader = _uploader(cfg, dry_run=dry_run, course=course, what=path.name)
        if uploader is None:
            continue
        try:
            _upload_recording_file(cfg, store, uploader, path, _recording_tag(cfg, day))
            outbox.remove(layout.state, path)
            ok += 1
        except PublicRepoBlocked as exc:
            log.error("%s", exc)
        except Exception:
            failed += 1
            log.exception("다시 올리기 실패(목록에 남겨 다음에 또 시도): %s", path.name)
    log.info("지난 녹화 다시 올리기: 올림 %d, 실패 %d", ok, failed)
    return ok, failed


def _recover_recordings(cfg, layout: Layout, *, upload: bool, dry_run: bool) -> int:
    """녹화 중에 컴퓨터가 꺼져 남은 조각을 파일로 만들고, 못 올린 녹화를 올립니다.

    record schedule / record watch 가 시작할 때(=로그온 직후) 뒤에서 돌고,
    `record recover` 로 직접 돌릴 수도 있습니다. 복구한 세그먼트 수를 돌려줍니다.
    """
    from .recorder import lock as rec_lock
    from .recorder import outbox, recover

    info = rec_lock.read(layout.state / "recording.lock")
    busy = info is not None and not rec_lock._stale(info)
    try:
        guard = rec_lock.hold(layout.state / "recover.lock", "녹화 복구")
        guard.__enter__()
    except rec_lock.RecordingBusy:
        log.info("다른 프로그램이 이미 녹화 복구를 하고 있습니다.")
        return 0
    try:
        store = _store(layout, "recordings")
        done = recover.recover_all(layout.recordings, cfg, busy=busy)
        for r in done:
            if not r.finals:
                continue
            j = r.leftover.journal
            label = str(j.get("label") or r.leftover.out_dir.name)
            course = _resolve_course(cfg, course_id=str(j.get("course_id") or ""),
                                     label=label, when=r.leftover.started)
            on_done = _post_segment_factory(cfg, layout, store, label, course=course,
                                            upload=upload, dry_run=dry_run, defer=True)
            seg = r.segment()
            on_done(seg)
            if upload and not dry_run:
                for extra in r.finals[1:]:             # 모니터가 여럿이면 나머지 화면 파일도
                    outbox.add(layout.state, extra, course_id=str(getattr(course, "id", "") or ""),
                               label=label, day=seg.started_at.date().isoformat())
        if upload:
            _drain_outbox(cfg, layout, dry_run=dry_run)
        return sum(1 for r in done if r.finals)
    finally:
        guard.__exit__(None, None, None)


def _recover_on_start(cfg, layout: Layout, args, *, exiting: bool = False) -> None:
    """녹화 프로그램이 시작할 때(=로그온 직후) 지난번 꺼진 녹화를 마무리합니다.

    곧 끝나는 경우(스위치 꺼짐·시간표 없음)에는 뒤에서 돌리면 프로그램과 함께 끊기므로
    그 자리에서 끝까지 하고, 계속 대기하는 경우에는 녹화를 늦추지 않게 뒤에서 합니다.
    """
    upload = not getattr(args, "no_upload", False)
    dry_run = bool(getattr(args, "dry_run", False))
    if exiting:
        try:
            _recover_recordings(cfg, layout, upload=upload, dry_run=dry_run)
        except Exception:
            log.exception("녹화 복구 중 오류")
    else:
        _recover_in_background(cfg, layout, upload=upload, dry_run=dry_run)


def _recover_in_background(cfg, layout: Layout, *, upload: bool, dry_run: bool) -> None:
    """녹화 대기를 늦추지 않게 뒤에서. 실패해도 녹화 프로그램은 계속 돕니다."""
    import threading

    def run() -> None:
        try:
            _recover_recordings(cfg, layout, upload=upload, dry_run=dry_run)
        except Exception:
            log.exception("녹화 복구 중 오류(녹화 대기는 계속합니다)")

    threading.Thread(target=run, name="record-recover", daemon=True).start()


def _recording_meta(cfg, label: str, trigger: str, *, occ=None, course=None,
                    language: str | None = None, when=None):
    """녹화 파일 안에 남길 메타데이터. 시간표 녹화는 계획의 수업들(겹치면 둘 이상),
    수동·감지 녹화는 지금 시간표에 걸린 수업(있으면)을 담습니다."""
    from . import __version__
    from .recorder import schedule as sched
    from .recorder.meta import ClassMeta, RecordingMeta

    when = when or datetime.now()
    pre = float(cfg.get("recording.pre_roll_minutes", 0) or 0)
    post = float(cfg.get("recording.post_roll_minutes", 0) or 0)
    classes = list(getattr(occ, "classes", ()) or ())
    if not classes and occ is None:
        # 수동·감지: 지금 진행 중인 시간표 수업
        try:
            for sl in sched.parse(cfg.get("recording.timetable", []) or []):
                if sl.weekday == when.weekday():
                    cs, ce = sl.occurrence(when.date())
                    if cs <= when < ce:
                        classes.append((sl, cs, ce))
        except Exception:  # noqa: BLE001 — 메타데이터는 부가 정보, 녹화를 막지 않음
            classes = []
    metas = []
    for i, (sl, cs, ce) in enumerate(classes):
        # 과목(레포)은 이 녹화의 첫 수업 것입니다. 합친 녹화의 둘째 수업에 그 과목을
        # 붙이면 다른 과목으로 잘못 표시되므로, 둘째부터는 시간표에 적힌 것만.
        c = course if i == 0 else None
        metas.append(ClassMeta(
            name=sl.name, start=cs, end=ce,
            course_id=str(getattr(sl, "course_id", "") or getattr(c, "id", "") or ""),
            course_name=str(getattr(c, "name", "") or ""),
            repo=str(getattr(c, "slug", "") or ""),
            room=str(getattr(sl, "room", "") or ""),
            professor=str(getattr(sl, "professor", "") or ""),
            language=str(getattr(sl, "language", "") or language or "")))
    return RecordingMeta(
        label=label, trigger=trigger, classes=metas,
        policy=str(cfg.get("recording.overlap", sched.DEFAULT_POLICY)) if len(metas) > 1 else "",
        window_start=getattr(occ, "start", None), window_end=getattr(occ, "end", None),
        pre_roll_min=pre if trigger == "schedule" else 0.0,
        post_roll_min=post if trigger == "schedule" else 0.0,
        machine=str(getattr(cfg, "machine_label", "") or ""), version=__version__)


HANDOVER_WAIT_SEC = 240     # 낮은 녹화가 지금 세그먼트를 마무리(합치기·올리기)하고 비킬 때까지


def _run_recording(cfg, layout: Layout, label: str, *, minutes: float | None,
                   upload: bool, dry_run: bool, stop_when=None,
                   language: str | None = None, course=None,
                   stop_on_silence: bool = False, video_not_before: float = 0.0,
                   meta=None, trigger: str = "manual"):
    from .recorder import lock as rec_lock

    # 한 PC 에서 녹화는 한 번에 하나만. RecordSchedule 과 RecordWatch 는
    # 서로 다른 작업이라 스케줄러가 둘 사이를 막아 주지 않습니다. 둘이
    # 같이 돌면 ffmpeg 두 개가 같은 오디오 장치를 열어 양쪽 다 무음이
    # 되는데, 둘 다 '녹화 중' 으로 보입니다.
    lock_path = layout.state / "recording.lock"
    guard = None
    try:
        guard = rec_lock.hold(lock_path, label, trigger)
        guard.__enter__()
    except rec_lock.RecordingBusy as exc:
        # 우선순위가 낮은 녹화(감지)가 쥐고 있으면 넘겨 달라고 하고 기다립니다(시간표·수동 녹화).
        holder = rec_lock.request_handover(lock_path, label, trigger)
        if holder is None:
            log.warning("녹화를 시작하지 않습니다: %s", exc)
            return None
        log.warning("'%s' 가 녹화 중이라 넘겨 달라고 요청했습니다 — '%s' 를 녹화합니다(최대 %d초 기다림).",
                    holder.label, label, HANDOVER_WAIT_SEC)
        deadline = time.time() + HANDOVER_WAIT_SEC
        guard = None
        while time.time() < deadline:
            time.sleep(2)
            try:
                guard = rec_lock.hold(lock_path, label, trigger)
                guard.__enter__()
                break
            except rec_lock.RecordingBusy:
                guard = None
        rec_lock.clear_handover(lock_path)
        if guard is None:
            log.error("'%s' 가 %d초 안에 비키지 않았습니다. 잠시 뒤 다시 시도합니다.",
                      holder.label, HANDOVER_WAIT_SEC)
            return None
        log.info("'%s' 에게서 녹화를 넘겨받았습니다.", holder.label)

    try:
        return _run_recording_locked(
            cfg, layout, label, minutes=minutes, upload=upload,
            dry_run=dry_run, stop_when=stop_when, language=language,
            course=course, stop_on_silence=stop_on_silence,
            video_not_before=video_not_before, meta=meta,
            trigger=trigger, lock_path=lock_path,
        )
    finally:
        guard.__exit__(None, None, None)


def _run_recording_locked(cfg, layout: Layout, label: str, *, minutes: float | None,
                          upload: bool, dry_run: bool, stop_when=None,
                          language: str | None = None, course=None,
                          stop_on_silence: bool = False,
                          video_not_before: float = 0.0, meta=None,
                          trigger: str = "manual", lock_path: Path | None = None):
    from .recorder.session import RecordingSession
    from .transcribe import realtime

    store = _store(layout, "recordings")
    out_dir = layout.session_dir(datetime.now(), course, label)
    out_dir.mkdir(parents=True, exist_ok=True)

    rt = realtime.maybe_start(cfg, out_dir, label)
    session = RecordingSession(
        cfg, layout, label,
        course=course,
        stop_on_silence=stop_on_silence,
        video_not_before=video_not_before,
        realtime_queue=rt.audio_queue if rt else None,
        on_segment_done=_post_segment_factory(cfg, layout, store, label,
                                              upload=upload, dry_run=dry_run,
                                              language=language, course=course),
        meta=meta if meta is not None else _recording_meta(
            cfg, label, "manual", course=course, language=language),
    )

    if stop_when is not None:
        import threading

        def watchdog():
            stop_when()
            session.request_stop()

        threading.Thread(target=watchdog, daemon=True).start()

    if lock_path is not None:
        import threading

        from .recorder import lock as rec_lock

        done = threading.Event()

        def yield_watch():
            # 우선순위가 높은 녹화(수업)가 넘겨 달라고 하면 지금 세그먼트를 마무리하고 비킴
            while not done.wait(2.0):
                by = rec_lock.handover_requested(lock_path, trigger, label)
                if by:
                    log.warning("'%s' 녹화가 시작돼 '%s' 를 마무리하고 넘깁니다.", by, label)
                    session.request_stop()
                    return

        threading.Thread(target=yield_watch, name="handover-watch", daemon=True).start()

    try:
        result = session.run(duration_minutes=minutes)
    finally:
        if lock_path is not None:
            done.set()
        if rt:
            rt.stop()
            rt.join(timeout=30)

    from .transcribe.batch import QUEUE_FILE, Queue

    print()
    print(result.summary())
    queued = len(Queue(layout.state / QUEUE_FILE))
    if queued:
        print(f"전사 대기 {queued}건.  python -m syncnote transcribe queue  로 처리하세요.")
    return result


def _resolve_course(cfg, *, course_id: str = "", label: str = "", when=None):
    """이 녹화가 어느 과목인지. 판단은 courses.resolve 한 곳에서만 합니다."""
    from . import courses as courses_mod

    return courses_mod.resolve(
        cfg, courses_mod.load(cfg), course_id=course_id, label=label, when=when
    )


_TRIGGER_NAMES = {"manual": ("수동 녹화", "record start"),
                  "schedule": ("시간표 자동 녹화", "record schedule"),
                  "watch": ("프로그램 감지 녹화", "record watch")}


def _trigger_off(cfg, key: str) -> bool:
    """[recording.triggers] 의 스위치가 꺼져 있으면 알리고 True.

    예전 schedule_enabled·watch_enabled 는 어디서도 읽지 않아 false 로 적어도 녹화가
    돌았습니다. 기존 config.toml 들에 false 로 남아 있어 새 이름(schedule·watch)으로
    진짜 스위치를 만들었고, 기본은 true(예전 동작 그대로)입니다.
    """
    if cfg.get(f"recording.triggers.{key}", True) is not False:
        return False
    what, cmd = _TRIGGER_NAMES[key]
    msg = (f"{what}가 꺼져 있어 {cmd} 를 하지 않습니다 "
           f"(config.toml [recording.triggers] {key} = false). 켜려면 true 로 바꾸세요.")
    log.warning("%s", msg)
    print(msg)
    return True


def cmd_record_start(args) -> int:
    cfg, layout = _load(args)
    if _trigger_off(cfg, "manual"):
        return 1                        # 사람이 누른 명령 — 안 됐다는 것을 알 수 있게
    label = args.label or f"수동_{datetime.now():%H%M}"
    _recover_on_start(cfg, layout, args)
    course = _resolve_course(cfg, course_id=getattr(args, "course", "") or "",
                             label=label, when=datetime.now())
    # _run_recording 은 다른 녹화가 이미 돌고 있으면 None 을 돌려줍니다.
    # 그걸 버리고 0 을 돌려주면, 한 프레임도 안 찍고 "성공" 으로
    # 끝납니다. 사용자가 직접 누른 명령이니 더더욱 알려야 합니다.
    result = _run_recording(cfg, layout, label, minutes=args.minutes,
                            upload=not args.no_upload, dry_run=args.dry_run,
                            language=args.language, course=course,
                            meta=_recording_meta(cfg, label, "manual", course=course,
                                                 language=args.language))
    if result is None:
        log.error("녹화를 하나도 하지 못했습니다(다른 녹화가 돌고 있습니다).")
        return 1
    return 0


def cmd_timetable_edit(args) -> int:
    """시간표 편집기. config 전체 검증(_load)은 하지 않습니다 — 설정 다른 곳이
    틀려 있어도 시간표는 고칠 수 있어야 합니다."""
    path = (Path(args.config).expanduser().resolve() if args.config
            else config_module.default_config_path())
    try:
        from .timetable import gui
    except ImportError as exc:
        print(missing_pyside_message(exc))
        return 1
    return gui.run(path)


def missing_pyside_message(exc: BaseException) -> str:
    """PySide6 가 없을 때. '어느 파이썬에' 깔아야 하는지가 핵심입니다 —
    py 런처·가상환경이 여럿이면 다른 파이썬에 깔고도 계속 없다고 나옵니다."""
    exe = sys.executable or "python"
    return (
        "시간표 편집기에는 PySide6 가 필요한데 이 파이썬에는 설치되어 있지 않습니다.\n"
        f"  파이썬: {exe}\n"
        f'  설치:   "{exe}" -m pip install PySide6\n'
        "  또는 scripts\\setup.bat 을 다시 실행하세요(requirements.txt 에 포함).\n"
        f"  (원인: {exc})"
    )


def cmd_record_schedule(args) -> int:
    from .recorder import schedule as sched

    cfg, layout = _load(args)
    show = getattr(args, "show", False)
    if not show and _trigger_off(cfg, "schedule"):
        _recover_on_start(cfg, layout, args, exiting=True)   # 녹화는 안 해도 지난 녹화는 마무리
        return 0                        # 작업 스케줄러가 띄움 — 실패로 보이지 않게
    slots = sched.parse(cfg.get("recording.timetable", []))
    off = sched.holidays(cfg.get("recording.holidays", []))
    if not slots:
        print(sched.describe(slots))
        if not show:
            _recover_on_start(cfg, layout, args, exiting=True)
        return 1

    pre = float(cfg.get("recording.pre_roll_minutes", 0))
    post = float(cfg.get("recording.post_roll_minutes", 0))
    # 겹치는 수업은 정책대로 나눈 '녹화 창' 을 따릅니다(시간표 편집기 경고와 같은 계산).
    policy = str(cfg.get("recording.overlap", sched.DEFAULT_POLICY))

    print(sched.describe(slots, off, pre_roll_min=pre, post_roll_min=post, policy=policy))
    if args.show:
        return 0
    # 지난번 녹화 중에 컴퓨터가 꺼졌으면 그 조각을 파일로 만들어 올립니다(로그온 직후 한 번).
    _recover_on_start(cfg, layout, args)

    # 녹화가 시작조차 못 했거나(다른 녹화가 잠금을 쥠) 금방 끝나면, 수업 창이 아직 남아 있는 한
    # 같은 창을 다시 녹화합니다(next_window 는 끝나지 않은 창을 돌려줌). 그 사이 간격을
    # 5 → 15 → 30 → 60초로 늘려 재시작이 폭주하지 않게 합니다.
    quick_ends = 0

    while True:
        nxt = sched.next_window(slots, pre_roll_min=pre, post_roll_min=post,
                                skip_days=off, policy=policy)
        if nxt is None:
            log.info("예정된 수업이 없습니다.")
            return 0
        log.info("다음 녹화: %s  %s ~ %s", nxt.name,
                 nxt.start.strftime("%m-%d %H:%M"), nxt.end.strftime("%H:%M"))
        if not sched.wait_until(nxt.start):
            return 0
        minutes = max(1.0, (nxt.end - datetime.now()).total_seconds() / 60)
        # nxt.start 는 pre-roll 만큼 당겨진 시각입니다. 화면은 당기지
        # 않고 진짜 수업 시작에 맞춰 켭니다 — 그 사이 강의실 PC 앞에
        # 앉은 사람의 화면이 찍혀 올라가면 안 됩니다. 소리는 당겨서
        # 받으므로 수업이 일찍 시작해도 말은 놓치지 않습니다.
        # 계획이 정한 화면 시작(넘겨받은 창이면 그 시각)이 있으면 그것을.
        if getattr(nxt, "video_from", None) is not None:
            video_at = nxt.video_from.timestamp()
        else:
            video_at = (nxt.start + timedelta(minutes=pre)).timestamp() if pre > 0 else 0.0
        course = _resolve_course(
            cfg,
            course_id=str(getattr(nxt.slot, "course_id", "") or ""),
            label=nxt.slot.name, when=nxt.start,
        )
        # 상시 실행이라 종료 코드가 신호가 아닙니다. 로그가 유일한
        # 신호입니다 — 이게 없으면 다음 날 아침 로그를 봐도 그 수업이
        # 유실된 줄 모릅니다. '녹화 시작' 은 찍히고 '녹화 종료' 는
        # 안 찍히는 모양이라 눈에 안 띕니다.
        run_t0 = time.monotonic()
        result = _run_recording(cfg, layout, nxt.name, minutes=minutes,
                                upload=not args.no_upload, dry_run=args.dry_run,
                                language=nxt.slot.language, course=course,
                                stop_on_silence=True, video_not_before=video_at,
                                meta=_recording_meta(cfg, nxt.name, "schedule", occ=nxt,
                                                     course=course),
                                trigger="schedule")
        if result is None:
            log.error("[%s] 녹화를 시작하지 못했습니다(다른 녹화가 돌고 있습니다). %s", nxt.name,
                      "이 수업은 녹화되지 않았습니다." if args.once
                      else "수업이 끝나기 전까지 잠시 뒤 다시 시도합니다(그 사이는 녹화되지 않았습니다).")
        if args.once:
            return 0 if result is not None else 1
        if result is None or time.monotonic() - run_t0 < 60:
            quick_ends += 1
            if datetime.now() < nxt.end:
                wait = (5, 15, 30, 60)[min(quick_ends - 1, 3)]
                until = min(nxt.end, datetime.now() + timedelta(seconds=wait))
                log.warning("[%s] 녹화가 곧바로 끝나 %.0f초 뒤 남은 수업을 다시 녹화합니다.",
                            nxt.name, (until - datetime.now()).total_seconds())
                if not sched.wait_until(until, poll_sec=1.0):
                    return 0
        else:
            quick_ends = 0


class _WatchOnceDone(Exception):
    """--once 로 한 번 감지하고 끝났습니다.

    예전에는 여기서 KeyboardInterrupt 를 던졌습니다. 그러면 바깥의
    except 가 사용자의 진짜 Ctrl+C 와 구별하지 못해, '사용자 중단'
    과 '녹화 실패' 가 같은 종료 코드로 나갔습니다. watch --once 를
    스케줄 작업으로 걸면 이게 치명적입니다.
    """


def _file_origin(layout: Layout, path: Path) -> tuple[str, str, object]:
    """녹화 파일의 (과목 코드, 라벨, 날짜). 올릴 목록에 적힌 것이 있으면 그것, 없으면 경로에서.

    경로: recordings/<YYYY-MM-DD>[/<과목>]/<라벨>/<stem>.mkv, stem = YYYYmmdd_HHMMSS_partNNN.
    """
    from datetime import date as _date

    from .recorder import outbox

    item = next((i for i in outbox.items(layout.state) if Path(i["path"]) == Path(path)), None)
    label = str((item or {}).get("label") or Path(path).parent.name)
    course_id = str((item or {}).get("course_id") or "")
    day = None
    try:
        day = _date.fromisoformat(str((item or {}).get("day") or ""))
    except ValueError:
        try:
            day = datetime.strptime(Path(path).name[:15], "%Y%m%d_%H%M%S").date()
        except ValueError:
            day = datetime.fromtimestamp(Path(path).stat().st_mtime).date()
    return course_id, label, day


def _guard_hooks(cfg, layout: Layout, *, dry_run: bool = False):
    """녹화 지킴이가 쓰는 실제 동작들(복구·검사·업로드·릴리스 확인)."""
    from .recorder import guard as G
    from .recorder import lock as rec_lock
    from .recorder import outbox
    from .recorder import schedule as sched
    from .release import ReleaseError
    from .state import sha256_file

    store_box: dict[str, Store] = {}
    uploaders: dict[str, object] = {}

    def store() -> Store:
        if "s" not in store_box:
            store_box["s"] = _store(layout, "recordings")
        return store_box["s"]

    def uploader_for(path: Path):
        course_id, label, day = _file_origin(layout, path)
        key = f"{course_id}|{label}|{day}"
        if key not in uploaders:
            course = _resolve_course(cfg, course_id=course_id, label=label,
                                     when=datetime.combine(day, datetime.min.time()))
            uploaders[key] = _uploader(cfg, dry_run=dry_run, course=course, what=Path(path).name)
        return uploaders[key], day

    def busy() -> str | None:
        info = rec_lock.read(layout.state / "recording.lock")
        if info is not None and not rec_lock._stale(info):
            return "녹화 중"
        quiet = float(cfg.get("recording.guard.quiet_before_class_minutes", 10))
        slots = sched.parse(cfg.get("recording.timetable", []) or [])
        if slots and quiet > 0:
            nxt = sched.next_window(
                slots, pre_roll_min=float(cfg.get("recording.pre_roll_minutes", 0)),
                post_roll_min=float(cfg.get("recording.post_roll_minutes", 0)),
                skip_days=sched.holidays(cfg.get("recording.holidays", [])),
                policy=str(cfg.get("recording.overlap", sched.DEFAULT_POLICY)))
            if nxt is not None:
                left = (nxt.start - datetime.now()).total_seconds() / 60
                if 0 < left <= quiet:
                    return f"다음 수업({nxt.name})이 {left:.0f}분 뒤 시작"
        return None

    def record(digest: str) -> dict | None:
        rec = store().data["uploaded"].get(digest)
        return rec if isinstance(rec, dict) else None

    def upload(path: Path) -> dict:
        up, day = uploader_for(path)
        if up is None:
            raise G.Blocked("과목을 알 수 없어 올릴 곳이 없습니다([[eclass.course_repos]] 확인)")
        try:
            _upload_recording_file(cfg, store(), up, path, _recording_tag(cfg, day))
        except PublicRepoBlocked as exc:
            raise G.Blocked(str(exc)) from None
        outbox.remove(layout.state, path)
        if dry_run:
            return {"tag": _recording_tag(cfg, day), "asset": "(dry-run)", "size": path.stat().st_size}
        return record(sha256_file(path)) or {}

    def remote(path: Path, rec: dict) -> bool | None:
        up, _day = uploader_for(path)
        if up is None or dry_run or not rec.get("tag") or not rec.get("asset"):
            return None
        try:
            asset = up.find_asset(rec["tag"], rec["asset"])
            if asset is None:
                return False
            name = str(rec["asset"])
            if name.endswith(".part000"):                 # 나눠 올린 큰 파일: 복원 정보까지 있어야
                return up.find_asset(rec["tag"], name[: -len(".part000")] + ".parts.json") is not None
            return int(asset.get("size", -1)) == Path(path).stat().st_size
        except ReleaseError as exc:
            log.info("릴리스 확인 실패(나중에 다시): %s", exc)
            return None

    def forget(digest: str) -> None:
        s = store()
        s.forget("uploaded", digest)
        s.save()

    return G.Hooks(
        busy=busy,
        recover=lambda: _recover_recordings(cfg, layout, upload=False, dry_run=dry_run),
        verify=G.verify_media, rebuild=G.rebuild_media, digest=sha256_file,
        record=record, upload=upload, remote=remote, forget=forget)


def cmd_record_guard(args) -> int:
    """녹화 지킴이: 녹화 중이 아닐 때 몇 분마다 녹화 파일을 확인하고 못 올린 것을 올립니다."""
    from .recorder import guard as G
    from .recorder import lock as rec_lock

    cfg, layout = _load(args)
    ledger = G.Ledger(layout.state / G.LEDGER)
    if args.status:
        print(G.status_text(ledger))
        return 0
    if not cfg.get("recording.guard.enabled", True):
        msg = "녹화 지킴이가 꺼져 있습니다(config.toml [recording.guard] enabled = false)."
        log.info("%s", msg)
        print(msg)
        return 0
    try:
        hold = rec_lock.hold(layout.state / "guard.lock", "녹화 지킴이")
        hold.__enter__()
    except rec_lock.RecordingBusy:
        print("녹화 지킴이가 이미 돌고 있습니다.")
        return 0
    try:
        g = G.Guard(layout.recordings, ledger, _guard_hooks(cfg, layout, dry_run=args.dry_run),
                    max_attempts=int(cfg.get("recording.guard.max_attempts", 6)),
                    verify_remote=bool(cfg.get("recording.guard.verify_remote", True)))
        interval = max(1.0, float(cfg.get("recording.guard.interval_minutes", 5))) * 60
        while True:
            s = g.tick()
            if s.skipped:
                log.info("녹화 지킴이: 쉼 — %s", s.skipped)
            if args.once:
                print(G.status_text(ledger))
                c = s.counts
                return 1 if (c.get(G.QUARANTINE) or c.get(G.BLOCKED)) else 0
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        hold.__exit__(None, None, None)


def cmd_record_recover(args) -> int:
    """녹화 중에 꺼져 남은 조각을 파일로 만들고, 못 올린 녹화를 올립니다."""
    from .recorder import outbox, recover

    cfg, layout = _load(args)
    left = recover.find_leftovers(layout.recordings)
    waiting = outbox.items(layout.state)
    print(f"끝나지 않은 녹화 조각 {len(left)}개, 올릴 목록에 남은 파일 {len(waiting)}개")
    for lo in left:
        print(f"  - {lo.out_dir.relative_to(layout.recordings)} / {lo.stem}"
              f"  (화면 {len(lo.videos)}, 소리 {len(lo.wavs)}, 일지 {'있음' if lo.journal else '없음'})")
    for it in waiting:
        print(f"  - 올릴 것: {it['path']}")
    if args.list:
        return 0
    n = _recover_recordings(cfg, layout, upload=not args.no_upload, dry_run=args.dry_run)
    left_after = outbox.items(layout.state)
    print(f"복구한 세그먼트 {n}개, 아직 못 올린 파일 {len(left_after) if not args.no_upload else '-'}개")
    return 1 if (left_after and not args.no_upload and not args.dry_run) else 0


def cmd_record_remux(args) -> int:
    """ffmpeg 없이 소리만 녹음한 세그먼트를 파일(mkv/mp4)로 만듭니다."""
    from .recorder import remux
    from .recorder.ffmpeg_util import FFmpegMissing, find_ffmpeg

    cfg, layout = _load(args)
    pend = remux.find_pending(layout.recordings)
    if not pend:
        print("파일로 만들 녹음이 없습니다.")
        return 0
    print(f"소리만 녹음해 둔 세그먼트 {len(pend)}개:")
    for p in pend:
        print(f"  - {p.relative_to(layout.recordings)}")
    if args.dry_run:
        return 0
    try:
        print(f"ffmpeg: {find_ffmpeg()}")
    except FFmpegMissing as exc:
        print(f"[X] {exc}")
        return 1
    done, failed = remux.remux_all(layout.recordings)
    if args.upload and done:
        _upload_remuxed(cfg, layout, done, dry_run=args.upload_dry_run)
    print(f"만듦 {len(done)}개, 실패 {len(failed)}개")
    return 0 if not failed else 1


def _upload_remuxed(cfg, layout, done, *, dry_run: bool) -> None:
    """녹화 때와 같은 규칙으로 올립니다: 과목을 모르면(course_repos 가 있을 때) 올리지 않음."""
    from . import courses as courses_mod
    from .release import tag_for

    store = _store(layout, "recordings")
    courses = courses_mod.load(cfg)
    for r in done:
        course = next((c for c in courses if str(getattr(c, "id", "")) == r.course_id), None)
        uploader = _uploader(cfg, dry_run=dry_run, course=course, what=r.final.name)
        if uploader is None or not r.final.exists():
            continue
        tag = tag_for(str(cfg.get("github.tag_prefix_recording", "recordings")),
                      r.started.date() if r.started else None)
        try:
            _upload_recording_file(cfg, store, uploader, r.final, tag)
        except PublicRepoBlocked as exc:
            log.error("%s", exc)
        except Exception:
            log.exception("업로드 실패 (로컬 파일은 남아 있습니다): %s", r.final.name)


def cmd_record_watch(args) -> int:
    from .recorder.watcher import Watcher

    cfg, layout = _load(args)
    if _trigger_off(cfg, "watch"):
        _recover_on_start(cfg, layout, args, exiting=True)
        return 0                        # 작업 스케줄러가 띄움 — 실패로 보이지 않게
    _recover_on_start(cfg, layout, args)
    watcher = Watcher(
        processes=cfg.get("recording.triggers.watch_processes", []),
        window_titles=cfg.get("recording.triggers.watch_window_titles", []),
        interval_sec=float(cfg.get("recording.triggers.watch_interval_sec", 15)),
        stop_grace_sec=float(cfg.get("recording.triggers.watch_stop_grace_sec", 60)),
    )

    # --once 는 한 번 감지하고 끝납니다. 그 한 번이 실패했으면
    # 종료 코드로 알려야 합니다 — 지금은 KeyboardInterrupt 로 빠져나가
    # '사용자 중단' 과 '녹화 실패' 가 같은 코드였습니다.
    failures: list[str] = []

    def on_start(hit: str) -> None:
        label = args.label or f"감지_{Path(hit).stem}_{datetime.now():%H%M}"
        # 감지 트리거는 과목을 모릅니다. 시간표로 되짚고, 그래도 모르면
        # None — 로컬에만 저장하고 올리지 않습니다. '첫 과목으로' 같은
        # 폴백은 두지 않습니다(운영체제 녹화가 자료구조 레포로 갑니다).
        course = _resolve_course(cfg, course_id=getattr(args, "course", "") or "",
                                 label=label, when=datetime.now())
        result = _run_recording(
            cfg, layout, label, minutes=None,
            upload=not args.no_upload, dry_run=args.dry_run,
            stop_when=watcher.wait_until_gone,
            meta=_recording_meta(cfg, label, "watch", course=course, language=args.language),
            language=args.language, course=course, trigger="watch",
            stop_on_silence=True,
        )
        if result is None:
            log.error("'%s' 를 감지했지만 녹화를 시작하지 못했습니다"
                      "(다른 녹화가 돌고 있습니다).", hit)
            failures.append(hit)
        if args.once:
            # 진짜 Ctrl+C 와 섞으면 안 됩니다. 사용자 중단은 130,
            # 녹화 실패는 1 이어야 스케줄러가 구분합니다.
            raise _WatchOnceDone

    try:
        watcher.watch(on_start)
    except _WatchOnceDone:
        pass
    except KeyboardInterrupt:
        log.info("감시를 종료합니다.")
        raise
    if failures:
        log.error("감지했지만 녹화하지 못한 것 %d건: %s",
                  len(failures), ", ".join(failures))
        return 1
    return 0


# ---------------------------------------------------------------------------
def cmd_transcribe_queue(args) -> int:
    from .transcribe.batch import Queue, run_queue

    cfg, layout = _load(args)
    queue = Queue(layout.state / "transcribe_queue.json")
    if args.show:
        from .transcribe.engine import pick_device
        from .transcribe.perf import Profile

        device, _ = pick_device(str(cfg.get("transcribe.batch.device", "auto")))
        profile = Profile(layout.state / "perf_profile.json")
        print(queue.describe(profile,
                             model=str(cfg.get("transcribe.batch.model", "small")),
                             device=device))
        return 0
    tally: dict[str, int] = {}
    results = run_queue(cfg, layout, limit=args.limit, language=args.language,
                        tally=tally)
    # "큐가 비어서 0건" 과 "20건을 전부 실패해서 0건" 은 다릅니다.
    # 밤새 돌려 놓은 무인 작업이 조용히 성공으로 끝나면 안 됩니다.
    if tally.get("failed") or tally.get("missing"):
        log.error("전사 실패 %d건 / 파일 사라짐 %d건 (성공 %d건). "
                  "실패한 것은 대기열에 남아 있습니다.",
                  tally.get("failed", 0), tally.get("missing", 0), len(results))
        _actions_error(
            "전사 실패",
            f"시도 {tally.get('attempted', 0)}건 중 성공 {len(results)}건",
        )
        _print_transcribe_results(results)
        return 1
    if not results:
        return 0

    _print_transcribe_results(results)

    if not args.no_upload and cfg.get("github.enabled"):
        from .release import tag_for, upload_many

        store = _store(layout, "recordings")
        tag = tag_for(str(cfg.get("github.tag_prefix_recording", "recordings")))
        files = [p for r in results for p in r.outputs]
        uploader = _uploader(cfg, dry_run=args.dry_run, what="전사 결과")
        if uploader is None:
            log.info("전사 결과 %d개는 로컬에만 둡니다.", len(files))
        else:
            up_tally: dict[str, int] = {}
            got = upload_many(uploader, files, tag,
                              prefix=f"{cfg.machine_label}_", store=store,
                              tally=up_tally)
            blocked = (up_tally.get("failed", 0) + up_tally.get("collision", 0)
                       + up_tally.get("missing", 0))
            if blocked:
                detail = (f"성공 {len(got)}건 / 실패 {up_tally.get('failed', 0)} / "
                          f"이름충돌 {up_tally.get('collision', 0)} / "
                          f"파일없음 {up_tally.get('missing', 0)}")
                log.error("전사 결과를 올리지 못했습니다. %s", detail)
                _actions_error("전사 결과 업로드 실패", detail)
                return 1
    return 0


def _print_transcribe_results(results) -> None:
    if not results:
        return
    print(f"\n전사 완료 {len(results)}건")
    for r in results:
        print(f"  {r.audio.name}  ({r.speed:.1f}x)")
        for out in r.outputs:
            print(f"    -> {out}")


def cmd_transcribe_warmup(args) -> int:
    from .transcribe.engine import EngineSpec

    cfg, _ = _load(args)
    for key in ("batch", "realtime"):
        if not cfg.get(f"transcribe.{key}.enabled", key == "batch"):
            continue
        spec = EngineSpec(
            model=str(cfg.get(f"transcribe.{key}.model", "small")),
            device=str(cfg.get(f"transcribe.{key}.device", "auto")),
            compute_type=str(cfg.get(f"transcribe.{key}.compute_type", "int8")),
        )
        print(f"{key} 모델 내려받는 중: {spec.model} ...")
        spec.load()
        print("  완료")
    print("\n이제 인터넷이 없는 곳에서도 전사할 수 있습니다.")
    return 0


def cmd_upload(args) -> int:
    from .release import iter_files, tag_for, upload_many

    cfg, layout = _load(args)
    target = Path(args.path).expanduser().resolve()
    if not target.exists():
        print(f"경로가 없습니다: {target}")
        return 1

    is_dir = target.is_dir()
    files = [target] if not is_dir else list(iter_files(target))
    if not files:
        print("올릴 파일이 없습니다.")
        return 0

    tag = args.tag or tag_for(str(cfg.get("github.tag_prefix_recording", "recordings")))
    store = _store(layout, "manual_upload")
    print(f"릴리스 '{tag}' 에 {len(files)}개 파일을 올립니다.")
    # 폴더를 올릴 때 root 를 넘기지 않으면 에셋 이름이 '파일 이름' 만으로
    # 정해져서, 하위 폴더가 다른 동명 파일(courseA/자료.md, courseB/자료.md)이
    # 같은 이름이 됩니다. 실행 내 중복 검사에 걸려 두 번째가 통째로 빠집니다.
    from . import courses as courses_mod

    course_list = courses_mod.load(cfg)
    course = courses_mod.resolve(
        cfg, course_list, course_id=getattr(args, "course", "") or ""
    )
    uploader = _uploader(cfg, dry_run=args.dry_run, course=course, what=str(target))
    if uploader is None:
        print("과목을 알 수 없어 올리지 않았습니다. --course 로 지정하세요.")
        print(f"고를 수 있는 과목: {', '.join(c.name for c in course_list) or '(없음)'}")
        return 1
    tally: dict[str, int] = {}
    results = upload_many(uploader, files, tag,
                          prefix=f"{cfg.machine_label}_", store=store,
                          root=target if is_dir else target.parent,
                          tally=tally)
    print(f"완료 {len(results)}건")

    # _upload_eclass 와 같은 자리입니다. 여기도 오래 0 을 돌려주고
    # 있었습니다 — 전부 실패해도 "완료 0건" 을 찍고 종료 코드 0.
    # 변이 테스트 세트가 eclass 경로만 봐서 이쪽은 안 걸렸습니다.
    blocked = (tally.get("failed", 0) + tally.get("collision", 0)
               + tally.get("missing", 0))
    if blocked:
        detail = (f"성공 {len(results)}건 / 실패 {tally.get('failed', 0)} / "
                  f"이름충돌 {tally.get('collision', 0)} / "
                  f"파일없음 {tally.get('missing', 0)}")
        print(f"올리지 못한 파일이 있습니다. {detail}")
        _actions_error("업로드 실패", detail)
        return 1
    return 0


def cmd_config_check(args) -> int:
    try:
        cfg = config_module.load(args.config)
    except config_module.ConfigError as exc:
        print(exc)
        return 1
    print(f"설정 파일 정상: {cfg.path}")
    notes = config_module.warnings_for(cfg)
    if notes:
        print("\n확인할 점:")
        for note in notes:
            print(f"  - {note}")
    print(f"\n데이터 폴더: {cfg.data_dir}")
    return 0


# ---------------------------------------------------------------------------
# backup (강의실 PC 홈 디렉터리)
# ---------------------------------------------------------------------------
def _home_uploader(cfg, *, dry_run: bool):
    """홈 백업은 과목과 무관합니다. course_repos 가 있어도 기본 레포로 갑니다."""
    from .release import ReleaseUploader

    return ReleaseUploader(
        owner=str(cfg.get("github.owner", "")),
        repo=str(cfg.get("github.repo", "")),
        retry_count=int(cfg.get("github.retry_count", 4)),
        retry_backoff=float(cfg.get("github.retry_backoff", 2.0)),
        split_size_mb=int(cfg.get("github.split_size_mb", 1900)),
        draft=bool(cfg.get("github.draft", False)),
        prerelease=bool(cfg.get("github.prerelease", False)),
        dry_run=dry_run,
    )


def _home_state_path(layout: Layout) -> Path:
    # Store(JSON) 가 아니라 SQLite 입니다. 홈 하나에 파일이 수십만 개라
    # JSON 한 덩어리로는 저장할 때마다 수백 MB 를 다시 씁니다.
    return layout.state / "home_backup.sqlite"


def _home_root(cfg) -> Path:
    raw = str(cfg.get("backup.home.root", "") or "").strip()
    return Path(raw).expanduser() if raw else Path.home()


def _check_home_root(home: Path) -> str | None:
    """홈 백업/정리 대상이 사용자 홈 안인지. 아니면 이유를 돌려줍니다.

    root 를 C:/Users 나 C:/ 로 적으면 AppData 가 '최상위' 가 아니게 되어
    다른 계정의 AppData, Program Files 까지 훑고, 정리가 지울 수 있는
    것은 다 지웁니다(검토에서 지적). 홈이거나 그 아래만 허용합니다.
    """
    if not home.is_dir():
        return f"홈 디렉터리가 없습니다: {home}"
    real = home.resolve()
    user_home = Path.home().resolve()
    if real != user_home and user_home not in real.parents:
        return (f"backup.home.root 가 사용자 홈({user_home}) 밖입니다: {real}. "
                "홈이거나 그 아래 폴더만 쓸 수 있습니다.")
    return None


def _home_identity(cfg, state) -> tuple[str, str]:
    """(사슬의 machine, 에셋 이름 접두사). 둘 다 설치 ID 를 품습니다."""
    from .release import machine_prefix

    iid = state.install_id()
    return f"{cfg.machine_label}#{iid}", f"{machine_prefix(cfg.machine_label)}{iid}_"


# 백업이 정리(보통 몇 분)를 기다리는 최대 시간.
HOME_LOCK_WAIT_SEC = 1800


def _home_lock_path(layout: Layout) -> Path:
    return layout.state / "home_backup.lock"


def cmd_backup_home(args) -> int:
    from .backup.homestate import HomeState
    from .backup.lock import exclusive
    from .backup.run import run_backup
    from .release import tag_for

    cfg, layout = _load(args)
    if not cfg.get("backup.home.enabled", False):
        log.info("홈 백업이 꺼져 있습니다 (backup.home.enabled = false).")
        return 0
    home = _home_root(cfg)
    why = _check_home_root(home)
    if why:
        log.error("홈 백업: %s", why)
        return 1

    tag = tag_for(str(cfg.get("backup.home.tag_prefix", "home")))
    # 정리가 돌고 있으면 끝나기를 기다립니다(정리는 보통 몇 분).
    with exclusive(_home_lock_path(layout), wait_sec=HOME_LOCK_WAIT_SEC) as got:
        if not got:
            detail = "다른 홈 백업/정리가 30분 넘게 끝나지 않아 이번 백업을 건너뜁니다."
            log.error("홈 백업: %s", detail)
            _actions_error("홈 백업 건너뜀", detail)
            return 1
        with HomeState.open(_home_state_path(layout)) as state:
            machine, prefix = _home_identity(cfg, state)
            result = run_backup(
                home=home,
                uploader=_home_uploader(cfg, dry_run=args.dry_run),
                state=state,
                layout=layout,
                machine=machine,
                prefix=prefix,
                tag=tag,
                bundle_mb=int(cfg.get("backup.home.bundle_mb", 1800)),
                exclude_names=list(cfg.get("backup.home.exclude_names", [])),
                dry_run=args.dry_run,
            )
    if args.dry_run:
        return 0
    if result.failed_bundles:
        detail = f"묶음 {result.failed_bundles}개를 올리지 못했습니다. 다음 실행에서 다시 시도합니다."
        log.error("홈 백업 일부 실패: %s", detail)
        _actions_error("홈 백업 실패", detail)
        return 1
    if result.manifest_failed:
        detail = "파일은 올렸지만 사슬(매니페스트)을 올리지 못했습니다. 정리는 일어나지 않습니다."
        log.error("홈 백업: %s", detail)
        _actions_error("홈 백업 사슬 실패", detail)
        return 1
    if not getattr(result, "ok", True):
        detail = "홈 백업이 끝까지 가지 못했습니다(연결/권한 확인 실패 등). 로그를 확인하세요."
        log.error("홈 백업: %s", detail)
        _actions_error("홈 백업 실패", detail)
        return 1
    return 0


def _prune_protect(cfg, args) -> list[Path]:
    """절대 지우면 안 되는 곳: 이 프로그램 자신, 그걸 돌리는 Python, 데이터 폴더, 설정 파일.

    Python 이 홈 안(~/anaconda3, ~/scoop, 홈 안의 venv)에 깔려 있으면 그것도
    백업 대상이라 지워질 수 있습니다. 지우면 다음 날 예약 작업이 못 돕니다.
    """
    repo_root = Path(__file__).resolve().parents[2]
    cfg_path = (Path(args.config).expanduser().resolve() if args.config
                else config_module.default_config_path())
    out = [repo_root, Path(cfg.data_dir).resolve(), cfg_path]
    for raw in {sys.prefix, sys.base_prefix, sys.exec_prefix}:
        out.append(Path(raw).resolve())
    out.append(Path(sys.executable).resolve().parent)
    return out


def cmd_backup_prune(args) -> int:
    from .backup.homestate import HomeState
    from .backup.lock import exclusive
    from .backup.prune import run_prune

    cfg, layout = _load(args)
    if not cfg.get("backup.home.prune.enabled", False) and not args.dry_run:
        log.info("홈 정리가 꺼져 있습니다 (backup.home.prune.enabled = false). "
                 "--dry-run 으로 지울 목록만 볼 수 있습니다.")
        return 0
    home = _home_root(cfg)
    why = _check_home_root(home)
    if why:
        log.error("홈 정리: %s", why)
        return 1

    # 백업이 돌고 있으면(첫 백업은 몇 시간) 오늘은 정리하지 않습니다.
    with exclusive(_home_lock_path(layout), wait_sec=0) as got:
        if not got:
            log.warning("홈 백업이 아직 돌고 있어 오늘은 정리하지 않습니다.")
            return 0
        with HomeState.open(_home_state_path(layout)) as state:
            machine, _prefix = _home_identity(cfg, state)
            result = run_prune(
                home=home,
                # 정리는 사슬을 **읽어야** 합니다. dry-run 이어도 네트워크 읽기는 합니다.
                uploader=_home_uploader(cfg, dry_run=False),
                state=state,
                layout=layout,
                machine=machine,
                min_age_hours=float(cfg.get("backup.home.prune.min_age_hours", 24)),
                protect=_prune_protect(cfg, args),
                dry_run=args.dry_run,
            )
    if not result.chain_ok:
        detail = "GitHub 의 사슬을 확인하지 못해 아무것도 지우지 않았습니다."
        log.error("홈 정리: %s", detail)
        _actions_error("홈 정리 중단", detail)
        return 1
    if result.errors:
        log.error("홈 정리 중 오류 %d건 — 로그를 확인하세요.", result.errors)
        return 1
    if getattr(result, "missing_remote", 0):
        # 지우지 않았으니 안전하지만, GitHub 쪽 묶음이 없거나 검증이 안 된다는
        # 뜻입니다. 조용히 넘기면 그 파일은 영원히 정리도 재업로드도 안 됩니다.
        detail = (f"GitHub 에서 묶음을 확인하지 못한 파일 {result.missing_remote}개를 "
                  "지우지 않았습니다. 릴리스를 확인하세요.")
        log.error("홈 정리: %s", detail)
        _actions_error("홈 정리 확인 실패", detail)
        return 1
    return 0


# ---------------------------------------------------------------------------
# audio — 터미널 녹음기 (FFmpeg / SoX / arecord)
# ---------------------------------------------------------------------------
def _terminal_plan(cfg, args):
    from .recorder import terminal

    t = cfg.section("recording.terminal")

    def pick(name, key):
        value = getattr(args, name, None)
        return t.get(key) if value is None else value

    return terminal.Plan(
        backend=str(pick("backend", "backend")),
        source=str(pick("source", "source")),
        device=str(pick("device", "device") or ""),
        seconds=float(pick("seconds", "seconds")),
        fmt=str(pick("format", "format")),
        vad=bool(args.vad or t.get("vad", False)),
        vad_threshold=str(pick("threshold", "vad_threshold")),
        vad_start_sec=float(t.get("vad_start_sec", 0.1)),
        vad_stop_sec=float(pick("stop_after", "vad_stop_sec")),
        samplerate=int(t.get("samplerate", 44100)),
        channels=int(t.get("channels", 2)),
    )


def _terminal_out_dir(cfg, layout, args) -> Path:
    from .recorder import terminal

    raw = args.out or str(cfg.get("recording.terminal.out_dir", "") or "")
    if raw:
        return Path(os.path.expandvars(raw)).expanduser()
    return terminal.default_out_dir(layout.recordings)


def cmd_audio_record(args) -> int:
    from .recorder import terminal

    cfg, layout = _load(args)
    plan = _terminal_plan(cfg, args)
    pattern = args.name or str(cfg.get("recording.terminal.filename", "record_%Y%m%d_%H%M%S"))
    saved = 0

    try:
        if args.print:
            out = _terminal_out_dir(cfg, layout, args) / terminal.output_name(pattern, plan.fmt)
            backend, cmd = terminal.build(plan, out)
            print(f"# {backend}")
            print(subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd))
            return 0

        while True:
            out = terminal.record_once(plan, _terminal_out_dir(cfg, layout, args),
                                       pattern=pattern)
            if out is not None:
                saved += 1
                print(out)
            elif not args.loop:
                return 1
            if not args.loop:
                return 0
            # 장치가 사라져 곧바로 실패하는 경우 CPU 를 태우며 돌지 않게 합니다.
            if out is None:
                time.sleep(5)
    except terminal.TerminalAudioError as exc:
        print(f"\n{exc}")
        return 2
    except KeyboardInterrupt:
        log.info("터미널 녹음을 멈췄습니다. 이번 실행에서 %d개 저장.", saved)
        return 0


def cmd_audio_tools(args) -> int:
    from .recorder import terminal

    cfg, layout = _load(args)
    found = terminal.available()
    plat = terminal.platform_key()
    print(f"터미널 녹음 도구 ({plat})")
    print("=" * 62)
    for name, path in found.items():
        if name == "arecord" and plat != "linux":
            print(f"  [-] {name:<8} 리눅스 전용")
            continue
        print(f"  {'[O]' if path else '[-]'} {name:<8} {path or '없음'}")

    plan = _terminal_plan(cfg, args)
    out = _terminal_out_dir(cfg, layout, args) / terminal.output_name(
        str(cfg.get("recording.terminal.filename", "record_%Y%m%d_%H%M%S")), plan.fmt)
    print("\n지금 설정으로 실행될 명령")
    try:
        backend, cmd = terminal.build(plan, out, found=found)
    except terminal.TerminalAudioError as exc:
        print(f"  (만들 수 없음) {exc}")
        return 1
    print(f"  [{backend}] " + (subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)))
    return 0


# ---------------------------------------------------------------------------
# stream — 실시간 오디오 송출 (Tailscale)
# ---------------------------------------------------------------------------
_STREAM_FLAGS = {
    "server": "server", "source": "source", "device": "device", "codec": "codec",
    "bitrate": "bitrate_kbps", "port": "port", "minutes": "max_duration_minutes",
    "idle_stop": "idle_stop_minutes", "mode": "ts_mode",
}


def _stream_settings(cfg, args):
    from .stream import live

    s = live.settings_from(cfg)
    for flag, attr in _STREAM_FLAGS.items():
        value = getattr(args, flag, None)
        if value is not None:
            setattr(s, attr, value)
    if getattr(args, "minutes", None):
        # 명령줄의 -m 은 '이만큼만 하고 끝' 이라는 직접 지시입니다. 설정의
        # max_duration_minutes(기본: 롤백 순환)와 달리 멈춥니다. 시간표 수업
        # 중이면 그래도 시간표가 우선입니다.
        s.on_max_duration = "stop"
    if getattr(args, "no_tailscale", False):
        s.ts_enabled = False
    if getattr(args, "no_issue", False):
        s.issue.enabled = False
    return s


def _stream_notifier(cfg, layout, s, *, dry_run: bool):
    from .release import ReleaseError, ReleaseUploader
    from .stream import notify

    if not s.issue.enabled or not s.ts_enabled:
        return None
    try:
        client = ReleaseUploader(
            owner=s.issue.owner, repo=s.issue.repo,
            retry_count=int(cfg.get("github.retry_count", 4)),
            retry_backoff=float(cfg.get("github.retry_backoff", 2.0)),
            dry_run=dry_run,
        )
    except ReleaseError as exc:
        log.warning("송출 링크를 이슈로 올리지 않습니다: %s ([stream.issue] owner/repo)", exc)
        return None
    return notify.IssueNotifier(s.issue, client=client, store=_store(layout, "stream"),
                                dry_run=dry_run)


def cmd_stream_start(args) -> int:
    from .stream import capture, live, tailnet

    cfg, layout = _load(args)
    s = _stream_settings(cfg, args)
    session = live.Session(settings=s, label=args.label or "", machine=cfg.machine_label,
                           dry_run=args.dry_run, slots=live.timetable(cfg, s))
    try:
        live.check(s)
        notifier = _stream_notifier(cfg, layout, s, dry_run=args.dry_run)
        return session.run(notifier)
    except (live.StreamError, tailnet.TailscaleError, capture.CaptureError) as exc:
        print(f"\n{exc}")
        return 2


def cmd_stream_schedule(args) -> int:
    """시간표대로 기다렸다가 수업마다 송출합니다. 수업 끝 = 송출 끝."""
    from .recorder import schedule as sched
    from .stream import capture, live, tailnet

    cfg, layout = _load(args)
    s = _stream_settings(cfg, args)
    slots = live.timetable(cfg, s)
    off = sched.holidays(cfg.get("recording.holidays", []))
    policy = str(cfg.get("recording.overlap", sched.DEFAULT_POLICY))
    print(sched.describe(slots, off, pre_roll_min=s.pre_roll_minutes,
                         post_roll_min=s.post_roll_minutes, policy=policy)
          .replace("다음 녹화", "다음 송출"))
    if not slots or args.show:
        return 0 if slots else 1
    live.check(s)

    while True:
        nxt = sched.next_window(slots, pre_roll_min=s.pre_roll_minutes,
                                post_roll_min=s.post_roll_minutes, skip_days=off,
                                policy=policy)
        if nxt is None:
            log.info("예정된 수업이 없습니다.")
            return 0
        log.info("다음 송출: %s  %s ~ %s", nxt.name,
                 nxt.start.strftime("%m-%d %H:%M"), nxt.end.strftime("%H:%M"))
        if not sched.wait_until(nxt.start):
            return 0
        session = live.Session(settings=s, label=nxt.name, machine=cfg.machine_label,
                               dry_run=args.dry_run, until=nxt.end, slots=slots)
        try:
            code = session.run(_stream_notifier(cfg, layout, s, dry_run=args.dry_run))
        except (live.StreamError, tailnet.TailscaleError, capture.CaptureError) as exc:
            # 상시 실행이라 한 수업이 실패해도 다음 수업은 기다립니다.
            log.error("[%s] 송출하지 못했습니다: %s", nxt.name, exc)
            code = 2
        if args.once:
            return code
        # 같은 수업을 바로 다시 잡지 않게 끝 시각을 넘깁니다.
        sched.wait_until(nxt.end)


def cmd_stream_link(args) -> int:
    from .stream import live, tailnet

    cfg, layout = _load(args)
    s = _stream_settings(cfg, args)
    print("실시간 송출 점검")
    print("=" * 62)
    print(f"  서버       : {s.server}   소리: {s.source} ({s.capture})   {s.codec} {s.bitrate_kbps}kbps")
    if not s.ts_enabled:
        print("  Tailscale  : 꺼짐 — 127.0.0.1 에서만 열리고 이슈는 올리지 않습니다.")
        return 0
    try:
        exe = tailnet.find_exe(s.ts_exe)
        node = tailnet.status(exe)
    except tailnet.TailscaleError as exc:
        print(f"  [X] {exc}")
        return 1
    ok = node.running and node.online
    print(f"  Tailscale  : {'[O]' if ok else '[X]'} {node.backend_state}  {exe}")
    print(f"  이 기기    : {node.dns_name or '(MagicDNS 없음)'}  {node.ipv4}")
    print(f"  tailnet    : {node.tailnet}")
    print(f"  노출 방식  : {s.ts_mode}" + (
        "  (Tailscale 앱 없이 토큰 링크로)" if s.ts_mode == "funnel" else "  (tailnet 전용)"))
    try:
        live.check(s)
        port = s.ic_port if s.server == "icecast" else s.port
        path = s.ic_mount if s.server == "icecast" else s.path
        link = tailnet.build_link(node, mode=s.ts_mode, port=port, https_port=s.ts_https_port,
                                  path=path, use_magicdns=s.ts_use_magicdns)
        if s.require_token and s.server != "icecast":
            link += "?t=<고정 토큰>" if s.token else "?t=<송출마다 새 토큰>"
        print(f"  링크       : {link}")
        if s.ts_mode != "direct":
            busy = tailnet.port_in_use(tailnet.serve_status(exe), s.ts_https_port)
            print(f"  serve 포트 : {s.ts_https_port} " + (
                ("[!] 이미 사용 중 — override_existing 이 켜져 있으면(기본) 덮어쓰고, 꺼져 있으면 "
                 "시작하지 않습니다") if busy else "비어 있음"))
    except (live.StreamError, tailnet.TailscaleError) as exc:
        print(f"  [X] {exc}")
        ok = False
    iss = s.issue
    if iss.enabled:
        print(f"  이슈       : {iss.owner}/{iss.repo}  labels={iss.labels}  on_change={iss.on_change}"
              f"  토큰 {'포함' if iss.include_token else '가림'}")
    else:
        print("  이슈       : 올리지 않음")
    return 0 if ok else 1


def cmd_stream_cleanup(args) -> int:
    from .stream import live, tailnet

    cfg, _ = _load(args)
    s = live.settings_from(cfg)
    if s.ts_mode == "direct":
        print("direct 모드는 정리할 tailscale serve 가 없습니다.")
        return 0
    exe = tailnet.find_exe(s.ts_exe)
    tailnet.unexpose(exe, s.ts_mode, s.ts_https_port)
    print(f"tailscale {s.ts_mode} --https={s.ts_https_port} off 를 실행했습니다.")
    return 0


# ---------------------------------------------------------------------------
# activity — 활동 기록 (PDF 폼 필드 값). 지금은 하드웨어/OS 게이트 점검만.
# ---------------------------------------------------------------------------
def cmd_activity_gate(args) -> int:
    from .activity import gate as gate_mod

    cfg, _ = _load(args)
    gate_cfg = cfg.section("activity.gate")
    print("활동 기록 — 하드웨어/OS 무결성 게이트")
    print("=" * 62)
    print(f"  전용 기기(machine_is_private): {bool(cfg.get('activity.machine_is_private'))}")
    res = gate_mod.check_gate(gate_cfg)
    for code, detail in res.details.items():
        failed = f"gate_fail:{code}" in res.failures
        print(f"  {'[X]' if failed else '[O]'} {code:<14} {detail}")
    if "gate_fail:attestation_not_implemented" in res.failures:
        print("  [X] attestation    require_attestation=true 인데 미구현 → fail-closed")
    print("\n" + "=" * 62)
    if res.ok:
        print("통과 — 이 기기에서는 봉인 저장이 허용됩니다.")
    else:
        print("실패 — 저장이 거부됩니다. 위 [X] 를 해결하고 다시 검사하세요.")
        print("  (예: TPM 2.0 기기, Secure Boot·HVCI 켜기, 관리자 아닌 창에서 실행)")
    return 0 if res.ok else 1


def cmd_activity_export(args) -> int:
    import json
    from pathlib import Path

    from .activity import gate as gate_mod
    from .activity import kms
    from .activity.store import SealedStore, paths

    cfg, _ = _load(args)
    # 읽기도 게이트 적용(대칭 차단). 실패면 --break-glass 1회로만.
    try:
        gate_mod.require_for_read(cfg.section("activity.gate"), break_glass=args.break_glass)
    except gate_mod.GateError as exc:
        print(f"\n{exc}")
        return 2
    db_path, dek_path = paths(cfg)
    if not Path(dek_path).exists() or not Path(db_path).exists():
        print("아직 저장된 활동이 없습니다.")
        return 0
    store = SealedStore.open(db_path, kms.load_or_create(dek_path))
    n = 0
    for event in store.decrypt_all():
        print(json.dumps(event, ensure_ascii=False))
        n += 1
    store.close()
    print(f"# {n}건 복호", file=sys.stderr)
    return 0


def cmd_activity_watch_acrobat(args) -> int:
    from .activity import sidecar

    cfg, _ = _load(args)
    if not cfg.get("activity.enabled") or not cfg.get("activity.adapters.acrobat_js"):
        print("activity.enabled 와 activity.adapters.acrobat_js 를 켜야 합니다.")
        return 1
    try:
        return sidecar.run(cfg, once=args.once)
    except Exception as exc:
        print(f"\n{exc}")
        return 2


def cmd_remote_ops(args) -> int:
    """원격 git 작업: 소유자가 GitHub 로 올린 서명된 계획을 받아 정규화한 git 명령만 실행.
    poll(받아 실행) · sign(계획에 서명) · new(예시 계획) · status. src/syncnote/remote_ops.py."""
    import json as _json

    from . import remote_ops as R

    if args.action == "sign":
        from . import secrets
        secret = secrets.get("remote_ops")
        plan = _json.loads(Path(args.file).read_text(encoding="utf-8"))
        print(_json.dumps(R.sign_plan(plan, secret), ensure_ascii=False, indent=1))
        return 0
    if args.action == "new":
        plan = R.new_plan(target=args.target or "all", reason=args.reason or "",
                          steps=[{"op": "status"},
                                 {"op": "stash-push", "msg": "remote-ops backup"},
                                 {"op": "fetch", "remote": "origin", "branch": "main", "prune": True},
                                 {"op": "reset-hard", "ref": "origin/main"}],
                          confirm_destructive=True)
        print(_json.dumps(plan, ensure_ascii=False, indent=1))
        print("\n# 위 계획을 고친 뒤:  python -m syncnote remote-ops sign <파일>  로 서명해 Gist 에 올리고,"
              "\n# 그 Gist 링크를 remote-ops 라벨 이슈에 올리세요(소유자 계정으로).", file=sys.stderr)
        return 0

    cfg, layout = _load(args)
    if args.action == "status":
        st = R.State.load(layout.state / "remote_ops.json")
        on = bool(cfg.get("remote_ops.enabled", False))
        print(f"원격 작업: {'켜짐' if on else '꺼짐'}, 마지막 seq {st.last_seq}, 실행한 계획 {len(st.done_ids)}개")
        print("허용 작업: " + ", ".join(R.ALLOWED_OPS))
        return 0
    out = R.poll_once(cfg, layout, dry_run=args.dry_run)
    line = f"원격 작업: 확인 {out.checked}건, 실행 {out.ran}건"
    if out.rejected:
        line += " | 거부 " + "; ".join(out.rejected[:5])
    if out.error:
        line += f" | {out.error}"
    print(line)
    return 0


def cmd_incidents(args) -> int:
    """실패·오류 이슈 대기열: status(몇 개 남았는지) · flush(지금 올리기)."""
    from . import incidents

    cfg, layout = _load(args)
    rep = incidents.install(cfg, layout, background=False)
    if rep is None:
        print("이슈 보고가 꺼져 있습니다([incidents] enabled = false 또는 SYNCNOTE_NO_INCIDENTS).")
        return 0
    if args.action == "status":
        left = rep._read_queue()
        print(f"올릴 대기: {len(left)}건")
        for it in left[-10:]:
            print(f"  {it['code']:<26} {it['severity']:<8} {incidents.redact(it['message'])[:80]}")
        return 0
    r = rep.flush()
    print(f"올림 {r.sent}건 (새 이슈 {r.created or '-'}, 댓글 {r.commented or '-'}), 남음 {r.kept}건"
          + (f" — {r.error}" if r.error else ""))
    return 1 if r.error and r.kept else 0


def cmd_update(args) -> int:
    """레포 자동 업데이트(git pull origin main). scripts\\update.bat · AutoUpdate 작업. src/syncnote/updater.py."""
    from . import updater

    state_dir = None
    cfg = None
    try:
        cfg, layout = _load(args)
        state_dir = layout.state
    except Exception as exc:            # noqa: BLE001 — 설정이 깨져도 업데이트는 함(고친 코드를 받으려고)
        log.warning("설정을 읽지 못했지만 업데이트는 합니다: %s", exc)
    def get(key, default):
        return cfg.get(key, default) if cfg is not None else default

    if not args.force and not bool(get("update.enabled", True)):
        print("자동 업데이트가 꺼져 있습니다(config.toml [update] enabled = false).")
        return 0
    res = updater.update(state_dir=state_dir, remote=str(get("update.remote", "origin")),
                         branch=str(get("update.branch", "main")),
                         install_requirements=bool(get("update.install_requirements", True)),
                         restart_tasks=bool(get("update.restart_tasks", True)) and not args.no_restart,
                         dry_run=args.dry_run)
    line = f"업데이트: {res.status} — {res.message}"
    if res.pip:
        line += f" | 패키지 설치 {res.pip}"
    if res.restarted:
        line += " | 다시 시작: " + ", ".join(res.restarted)
    if res.status == "failed":
        msg = res.message
        code = ("UPDATE.MERGE_CONFLICT" if any(k in msg for k in ("CONFLICT", "Automatic merge failed"))
                else "UPDATE.FETCH_FAILED" if "가져오기 실패" in msg else "UPDATE.PULL_FAILED")
        log.error("%s", line, extra={"incident": code})
    else:
        log.info("%s", line)
    if res.pip == "failed":
        log.error("업데이트 뒤 패키지 설치 실패(requirements.txt)", extra={"incident": "UPDATE.PIP_FAILED"})
    print(line)
    return 1 if res.status == "failed" else 0


def cmd_audio_check(args) -> int:
    """소리 입출력 단계별 점검(scripts\\audio_check.bat). src/syncnote/recorder/audio_check.py."""
    import tempfile

    from .recorder import audio_check as AC

    try:
        _cfg, layout = _load(args)
        base = layout.logs / "audio_check"
    except Exception as exc:            # noqa: BLE001 — 설정이 없어도 점검은 함
        log.debug("설정을 읽지 못해 임시 폴더에 남깁니다: %s", exc)
        base = Path(tempfile.gettempdir()) / "syncnote-audio-check"
    out = Path(args.out) if args.out else base / datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        io = AC.SoundcardIO()
    except Exception as exc:            # noqa: BLE001
        print(f"소리 장치를 열 수 없습니다: {exc}")
        return 1
    steps = tuple(int(s) for s in args.steps.split(",")) if args.steps else AC.STEPS
    print("소리 입출력 점검 — 단계마다 Enter 로 진행합니다. 녹음은 이 PC 에만 남습니다.")
    if not args.no_sound:
        print("4·5단계는 스피커로 소리를 냅니다(--no-sound 로 끌 수 있음).")
    rep = AC.Check(io, out, auto=args.yes, no_sound=args.no_sound, steps=steps,
                   gain_db=args.gain_db, silence_sec=args.silence_sec,
                   speech_sec=args.speech_sec).run()
    return 0 if "error" not in json.dumps(rep, ensure_ascii=False) else 1


def cmd_tasks(args) -> int:
    """작업 스케줄러 등록 확인(status)·자동 등록(ensure). src/syncnote/tasks.py."""
    from . import tasks

    auto = getattr(args, "auto", False)
    try:
        _load(args)                     # 파일 로그를 켜려고(설정이 틀려도 등록은 함)
    except Exception as exc:            # noqa: BLE001
        log.debug("설정을 읽지 못했지만 작업 확인은 계속합니다: %s", exc)
    if not tasks.supported():
        print("작업 스케줄러: " + ("SYNCNOTE_NO_TASK_CHECK 로 꺼져 있어 확인하지 않습니다."
                                  if os.environ.get("SYNCNOTE_NO_TASK_CHECK")
                                  else "Windows 의 레포 폴더에서 실행할 때만 확인합니다."))
        return 0
    if args.action == "status":
        st = tasks.check()
        print(f"작업 스케줄러: {st.describe()}")
        return 0 if st.ok else 1
    # 사람이 실행한 것(setup.bat·doctor.bat)은 묻고, 뒤에서 하는 자동 등록(--auto)은 묻지 않음
    # (사용자가 정함). --yes 면 묻지 않음.
    ask = None if (auto or getattr(args, "yes", False)) else tasks.console_ask
    st = tasks.ensure(elevate=not auto and not getattr(args, "no_elevate", False),
                      force=getattr(args, "force", False), ask=ask)
    if not auto:
        print(f"작업 스케줄러: {st.describe()}")
    return 0 if st.ok else 1


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="syncnote",
        description="Eclass 자료 수집 + 화면/음성 녹화 + 전사 + GitHub 릴리스 업로드",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-c", "--config", help="config.toml 경로")
    parser.add_argument("--log-level", help="DEBUG / INFO / WARNING / ERROR")
    parser.add_argument("-q", "--quiet", action="store_true", help="콘솔 출력 끄기(파일 로그는 유지)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="환경 점검")
    p.add_argument("--json", action="store_true", help="결과를 JSON 한 줄로(시간표 편집기용)")
    p.set_defaults(func=cmd_doctor)
    p = sub.add_parser("remote-ops", help="원격 git 작업(소유자가 GitHub 로 올린 서명된 계획을 실행)")
    p.add_argument("action", choices=["poll", "sign", "new", "status"])
    p.add_argument("file", nargs="?", help="sign 할 계획 JSON 파일")
    p.add_argument("--dry-run", action="store_true", help="실행하지 않고 할 일만")
    p.add_argument("--target", default="", help="new: 대상 PC 이름(기본 all)")
    p.add_argument("--reason", default="", help="new: 사유")
    p.set_defaults(func=cmd_remote_ops)
    p = sub.add_parser("incidents", help="실패·오류 이슈: status(대기열) · flush(지금 올리기)")
    p.add_argument("action", choices=["status", "flush"])
    p.set_defaults(func=cmd_incidents)
    p = sub.add_parser("update", help="레포 업데이트(git pull origin main) — 녹화 중이 아닐 때만")
    p.add_argument("--dry-run", action="store_true", help="받을 커밋 수만 확인")
    p.add_argument("--no-restart", action="store_true", help="받은 뒤 녹화 프로그램을 다시 시작하지 않음")
    p.add_argument("--force", action="store_true", help="[update] enabled = false 여도 함")
    p.set_defaults(func=cmd_update)
    p = sub.add_parser("audio-check", help="소리 입출력 단계별 점검(마이크가 실제로 들어오는지)")
    p.add_argument("--yes", action="store_true",
                   help="묻지 않고 모든 단계 진행(원격 실행용, '들어 보기' 는 건너뜀)")
    p.add_argument("--no-sound", action="store_true", help="스피커로 소리를 내는 단계(3·4·5) 건너뜀")
    p.add_argument("--steps", default="", help="할 단계만, 예: 0,1,2,6")
    p.add_argument("--gain-db", type=float, default=30.0, help="증폭본 WAV 의 증폭(dB)")
    p.add_argument("--silence-sec", type=float, default=8.0)
    p.add_argument("--speech-sec", type=float, default=10.0)
    p.add_argument("--out", default="", help="결과 폴더(기본: 데이터 폴더의 logs\\audio_check\\<시각>)")
    p.set_defaults(func=cmd_audio_check)
    p = sub.add_parser("tasks", help="작업 스케줄러 등록 확인·자동 등록")
    p.add_argument("action", choices=["status", "ensure"],
                   help="status: 확인만 / ensure: 빠졌으면 등록(권한이 없으면 UAC)")
    p.add_argument("--auto", action="store_true",
                   help="뒤에서 자동으로(창·UAC 없이). 모든 명령의 시작에서 띄움")
    p.add_argument("--no-elevate", action="store_true", help="UAC 로 다시 시도하지 않음")
    p.add_argument("--force", action="store_true", help="정상이어도 다시 등록")
    p.add_argument("--yes", action="store_true", help="'등록할까요?' 를 묻지 않고 등록")
    p.set_defaults(func=cmd_tasks)
    p = sub.add_parser("devices", help="오디오 장치 목록")
    p.add_argument("--json", action="store_true", help="결과를 JSON 한 줄로(시간표 편집기용)")
    p.add_argument("--probe", action="store_true",
                   help="--json 과 함께: 마이크를 1초씩 열어 소리가 들어오는지 잼(저장 안 함)")
    p.add_argument("--allow-mic", action="store_true",
                   help="Windows 마이크 권한이 꺼져 있으면 켬(이 PC 전체 항목은 관리자 권한 필요)")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("secrets", help="비밀번호/토큰 관리")
    p.add_argument("action", choices=["set", "delete", "status"], nargs="?", default="status")
    p.add_argument("name", nargs="?", choices=list(secrets.SLOTS), default="eclass")
    p.set_defaults(func=cmd_secrets)

    p = sub.add_parser("config", help="설정 검증")
    p.add_argument("action", choices=["check"], nargs="?", default="check")
    p.set_defaults(func=cmd_config_check)

    # --- eclass ---
    ec = sub.add_parser("eclass", help="Eclass 수집").add_subparsers(dest="sub", required=True)

    p = ec.add_parser("probe", help="페이지 구조 탐색(디버그용)")
    p.add_argument("--no-headless", action="store_true", help="브라우저 창을 띄워 눈으로 확인")
    p.add_argument("--courses", type=int, default=1, help="탐색할 과목 수 (기본 1)")
    p.add_argument("--no-redact", action="store_true", help="학번/이메일을 가리지 않음")
    p.set_defaults(func=cmd_eclass_probe)

    p = ec.add_parser("sync", help="자료 수집 후 릴리스 업로드")
    p.add_argument("--no-headless", action="store_true")
    p.add_argument("--no-upload", action="store_true", help="수집만 하고 올리지 않음")
    p.add_argument("--dry-run", action="store_true", help="업로드 흉내만 냄")
    p.add_argument("--term", help="학기 라벨 (기본: 자동)")
    p.set_defaults(func=cmd_eclass_sync)

    # --- record ---
    rec = sub.add_parser("record", help="화면/음성 녹화").add_subparsers(dest="sub", required=True)

    p = rec.add_parser("start", help="지금 바로 녹화")
    p.add_argument("-l", "--label", help="세션 이름 (폴더명에 쓰임)")
    p.add_argument("--course", default="",
                   help="과목(코드 또는 이름). [[eclass.course_repos]] 를 쓸 때 필요합니다.")
    p.add_argument("-m", "--minutes", type=float, help="녹화 길이(분). 생략 시 config 의 최대값")
    p.add_argument("--language", help="이 녹화의 전사 언어 (ko / en / auto / ko+en)")
    p.add_argument("--no-upload", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="업로드 흉내만 냄 (녹화·전사는 실제로 합니다)")
    p.set_defaults(func=cmd_record_start, lenient_config=True)   # 설정이 틀려도 녹화·송출은 계속

    p = rec.add_parser("schedule", help="시간표대로 대기하다 녹화")
    p.add_argument("--show", action="store_true", help="시간표만 출력하고 종료")
    p.add_argument("--once", action="store_true", help="한 번만 녹화하고 종료")
    p.add_argument("--no-upload", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="업로드 흉내만 냄 (녹화·전사는 실제로 합니다)")
    p.set_defaults(func=cmd_record_schedule, lenient_config=True)   # 설정이 틀려도 녹화·송출은 계속

    tt = sub.add_parser("timetable", help="시간표 편집기(GUI)").add_subparsers(
        dest="sub", required=True)
    tt.add_parser("edit", help="PySide6 편집기로 [[recording.timetable]] 편집").set_defaults(
        func=cmd_timetable_edit)

    p = rec.add_parser("guard", help="녹화 지킴이: 녹화 중이 아닐 때 녹화 파일을 확인하고 못 올린 것을 올림")
    p.add_argument("--once", action="store_true", help="한 번만 확인하고 끝냄")
    p.add_argument("--status", action="store_true", help="지금 상태만 보여 줌")
    p.add_argument("--dry-run", action="store_true", help="업로드 흉내만 냄")
    p.set_defaults(func=cmd_record_guard, lenient_config=True)   # 설정이 틀려도 녹화·송출은 계속

    p = rec.add_parser("recover", help="녹화 중에 컴퓨터가 꺼져 남은 조각을 파일로 만들어 올리기")
    p.add_argument("--list", action="store_true", help="무엇이 남았는지 보여 주기만")
    p.add_argument("--no-upload", action="store_true", help="파일만 만들고 올리지 않음")
    p.add_argument("--dry-run", action="store_true", help="업로드 흉내만 냄")
    p.set_defaults(func=cmd_record_recover, lenient_config=True)   # 설정이 틀려도 녹화·송출은 계속

    p = rec.add_parser("remux", help="ffmpeg 없이 소리만 녹음해 둔 것을 파일로 만들기")
    p.add_argument("--upload", action="store_true", help="만든 파일을 녹화 때처럼 올림")
    p.add_argument("--upload-dry-run", action="store_true", help="업로드 흉내만")
    p.add_argument("--dry-run", action="store_true", help="목록만 보여 줌")
    p.set_defaults(func=cmd_record_remux, lenient_config=True)   # 설정이 틀려도 녹화·송출은 계속

    p = rec.add_parser("watch", help="프로그램이 뜨면 자동 녹화")
    p.add_argument("-l", "--label")
    p.add_argument("--course", default="",
                   help="과목(코드 또는 이름). [[eclass.course_repos]] 를 쓸 때 필요합니다.")
    p.add_argument("--language", help="전사 언어 (ko / en / auto / ko+en)")
    p.add_argument("--once", action="store_true")
    p.add_argument("--no-upload", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="업로드 흉내만 냄 (녹화·전사는 실제로 합니다)")
    p.set_defaults(func=cmd_record_watch, lenient_config=True)   # 설정이 틀려도 녹화·송출은 계속

    # --- activity (활동 기록) ---
    ac = sub.add_parser(
        "activity", help="활동 기록 — PDF 폼 필드 값 (지금은 게이트 점검만)"
    ).add_subparsers(dest="sub", required=True)
    ac.add_parser("gate", help="하드웨어/OS 무결성 게이트 점검(저장 가능 여부)").set_defaults(
        func=cmd_activity_gate)
    p = ac.add_parser("export", help="봉인된 활동 로그를 복호해 JSONL 로 출력(게이트 적용)")
    p.add_argument("--break-glass", action="store_true", dest="break_glass",
                   help="게이트 실패 상태에서도 1회 복호 허용(감사 로그에 남음)")
    p.set_defaults(func=cmd_activity_export)
    p = ac.add_parser("watch-acrobat", help="데스크톱 Acrobat 사이드카를 감시해 봉인(C 어댑터)")
    p.add_argument("--once", action="store_true", help="대기 중인 사이드카만 한 번 처리하고 종료")
    p.set_defaults(func=cmd_activity_watch_acrobat)

    # --- stream (실시간 송출) ---
    st = sub.add_parser(
        "stream", help="실시간 오디오 송출 (Tailscale 망, 링크를 GitHub 이슈로)"
    ).add_subparsers(dest="sub", required=True)

    def _stream_opts(p):
        p.add_argument("--server", choices=["builtin", "icecast"])
        p.add_argument("--source", choices=["system", "mic"])
        p.add_argument("-d", "--device", help="장치 이름 (비우면 기본 장치)")
        p.add_argument("--codec", choices=["mp3", "aac"])
        p.add_argument("--bitrate", type=int, help="kbps")
        p.add_argument("--port", type=int, help="내장 서버 포트")
        p.add_argument("--mode", choices=["direct", "serve", "funnel"],
                       help="Tailscale 노출 방식 (기본 funnel: 앱 없는 사람도 토큰 링크로)")
        p.add_argument("--no-tailscale", action="store_true", help="127.0.0.1 에서만 (이슈 없음)")
        p.add_argument("--no-issue", action="store_true", help="이슈를 올리지 않음")

    p = st.add_parser("start", help="지금 송출 시작 (설정은 [stream])")
    _stream_opts(p)
    p.add_argument("-l", "--label", help="이슈 제목 등에 쓰는 이름")
    p.add_argument("-m", "--minutes", type=float, help="이만큼 송출하고 멈춤")
    p.add_argument("--idle-stop", type=float, dest="idle_stop",
                   help="청취자가 이만큼(분) 없으면 멈춤")
    p.add_argument("--dry-run", action="store_true",
                   help="송출은 하되 이슈는 만들지 않고 내용만 출력")
    p.set_defaults(func=cmd_stream_start, lenient_config=True)   # 설정이 틀려도 녹화·송출은 계속

    p = st.add_parser("schedule", help="시간표([[recording.timetable]])대로 수업마다 송출")
    _stream_opts(p)
    p.add_argument("--show", action="store_true", help="송출할 시간표만 출력")
    p.add_argument("--once", action="store_true", help="한 수업만 송출하고 종료")
    p.add_argument("--dry-run", action="store_true", help="이슈는 만들지 않고 내용만 출력")
    p.set_defaults(func=cmd_stream_schedule, lenient_config=True)   # 설정이 틀려도 녹화·송출은 계속

    p = st.add_parser("link", help="Tailscale 상태와 만들어질 링크 미리 보기")
    _stream_opts(p)
    p.set_defaults(func=cmd_stream_link)

    st.add_parser("cleanup", help="비정상 종료로 남은 tailscale serve/funnel 끄기").set_defaults(
        func=cmd_stream_cleanup)

    # --- audio (터미널 녹음기) ---
    au = sub.add_parser(
        "audio", help="소리만 녹음 (FFmpeg / SoX / arecord, cron·작업 스케줄러용)"
    ).add_subparsers(dest="sub", required=True)

    def _audio_opts(p):
        p.add_argument("-b", "--backend", choices=["auto", "ffmpeg", "sox", "arecord"])
        p.add_argument("--source", choices=["mic", "system"],
                       help="mic(기본 입력) 또는 system(스피커로 나가는 소리)")
        p.add_argument("-d", "--device", help="장치 이름/번호 (비우면 기본 장치)")
        p.add_argument("-t", "--seconds", type=float,
                       help="녹음 길이(초). --vad 와 함께면 최대 길이")
        p.add_argument("-f", "--format", choices=["wav", "mp3", "flac"])
        p.add_argument("--vad", action="store_true",
                       help="소리가 날 때만 녹음 (SoX silence 효과)")
        p.add_argument("--threshold", help="소리 감지 문턱값 (예: 3%%, -40d)")
        p.add_argument("--stop-after", type=float, dest="stop_after",
                       help="이만큼(초) 조용하면 끝 (기본 3)")
        p.add_argument("-o", "--out", help="저장 폴더")

    p = au.add_parser("record", help="지금 녹음 (설정은 [recording.terminal])")
    _audio_opts(p)
    p.add_argument("-n", "--name", help="파일 이름 형식 (strftime, 예: lecture_%%Y%%m%%d_%%H%%M)")
    p.add_argument("--loop", action="store_true",
                   help="끝나면 새 파일로 다시 녹음 (--vad 와 함께 상시 감시)")
    p.add_argument("--print", action="store_true", help="실행하지 않고 명령만 출력")
    p.set_defaults(func=cmd_audio_record, lenient_config=True)   # 설정이 틀려도 녹화·송출은 계속

    p = au.add_parser("tools", help="설치된 도구와 실행될 명령 보기")
    _audio_opts(p)
    p.set_defaults(func=cmd_audio_tools)

    # --- transcribe ---
    tr = sub.add_parser("transcribe", help="음성 전사").add_subparsers(dest="sub", required=True)

    p = tr.add_parser("queue", help="대기열 전사")
    p.add_argument("--show", action="store_true", help="대기열만 보기")
    p.add_argument("--limit", type=int, default=0, help="이번에 처리할 개수")
    p.add_argument("--language", help="대기열 전체에 적용할 언어 (ko / en / auto / ko+en)")
    p.add_argument("--no-upload", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="업로드 흉내만 냄 (녹화·전사는 실제로 합니다)")
    p.set_defaults(func=cmd_transcribe_queue)

    tr.add_parser("warmup", help="모델 미리 내려받기").set_defaults(func=cmd_transcribe_warmup)

    # --- backup ---
    bk = sub.add_parser("backup", help="강의실 PC 홈 디렉터리 백업/정리").add_subparsers(
        dest="sub", required=True)

    p = bk.add_parser("home", help="홈 디렉터리를 home-날짜 릴리스에 올림")
    p.add_argument("--dry-run", action="store_true", help="올릴 목록만 보여 줌")
    p.set_defaults(func=cmd_backup_home)

    p = bk.add_parser("prune", help="GitHub 사슬로 확인된 파일을 로컬에서 지움")
    p.add_argument("--dry-run", action="store_true",
                   help="사슬은 확인하되 지우지 않고 목록만 보여 줌")
    p.set_defaults(func=cmd_backup_prune)

    # --- upload ---
    p = sub.add_parser("upload", help="임의 파일/폴더를 릴리스에 올림")
    p.add_argument("path")
    p.add_argument("--tag", help="릴리스 태그 (기본: 설정의 전략대로)")
    p.add_argument("--course", default="",
                   help="과목(코드 또는 이름). [[eclass.course_repos]] 를 쓸 때 필요합니다.")
    p.add_argument("--dry-run", action="store_true",
                   help="업로드 흉내만 냄 (녹화·전사는 실제로 합니다)")
    p.set_defaults(func=cmd_upload)

    return parser


def _safe_console() -> None:
    """콘솔·로그 파일이 UTF 가 아닌 인코딩이어도 출력 때문에 죽지 않게.

    영문 Windows(cp1252)나 작업 스케줄러가 출력을 파일로 돌릴 때, 한글 한 글자에
    UnicodeEncodeError 로 프로그램이 끝났습니다(CI 의 `--help` 에서 재현).
      - 파일·파이프로 돌린 출력이고 PYTHONIOENCODING 을 따로 정하지 않았으면 UTF-8
      - 그 밖(진짜 콘솔, 사용자가 정한 인코딩)은 그대로 두고 못 쓰는 글자만 ? 로
    """
    import codecs

    for stream in (sys.stdout, sys.stderr):
        enc = getattr(stream, "encoding", None) or "ascii"
        try:
            if codecs.lookup(enc).name.startswith("utf"):
                continue                              # UTF-8/16/32 은 무엇이든 씀
        except LookupError:
            pass
        # cp949 도 한글은 되지만 '—' 같은 글자에서 죽습니다(한국어 Windows 에서 파일로
        # 돌렸을 때 실제로 재현). 그래서 UTF 가 아니면 모두 보호합니다.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            redirected = not stream.isatty()
        except (AttributeError, ValueError, OSError):
            redirected = True
        if redirected and not os.environ.get("PYTHONIOENCODING"):
            reconfigure(encoding="utf-8", errors="replace")
        else:
            reconfigure(errors="replace")


def main(argv: list[str] | None = None) -> int:
    _safe_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.func not in (cmd_tasks, cmd_doctor):
        # 작업 스케줄러에 빠진 작업이 있으면 뒤에서(창 없이) 등록합니다 — 등록을 잊어 녹화가
        # 한 번도 돌지 않는 일을 막으려고. 명령을 늦추지 않고, 몇 시간에 한 번만 확인합니다.
        try:
            from . import tasks

            tasks.spawn_background()
        except Exception as exc:        # noqa: BLE001 — 무슨 일이 있어도 명령은 계속
            log.debug("작업 스케줄러 확인을 띄우지 못했습니다: %s", exc)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n중단했습니다.")
        return 130
    except config_module.ConfigError as exc:
        print(f"\n설정 오류:\n{exc}")
        return 2
    except secrets.SecretsError as exc:
        print(f"\n{exc}")
        return 3
    except Exception as exc:
        log.error("예기치 못한 오류: %s", exc)
        if getattr(args, "log_level", "") == "DEBUG":
            traceback.print_exc()
        else:
            print(f"\n오류: {exc}")
            print("자세한 내용은 로그 폴더를 보거나 --log-level DEBUG 로 다시 실행하세요.")
        return 1
