"""ffmpeg 찾기와 공통 실행 유틸.

ffmpeg 를 쓰는 이유:
  순수 파이썬(mss + opencv + sounddevice)으로 장시간 녹화를 하면
  프레임 누락이 누적돼 영상과 소리가 점점 어긋납니다. 1시간이면
  수십 초까지 벌어집니다. ffmpeg 는 이 문제를 이미 풀어 놓았습니다.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path

from ..logging_setup import get as get_logger

log = get_logger("recorder.ffmpeg")


class FFmpegMissing(Exception):
    def __str__(self) -> str:
        return (
            "ffmpeg 를 찾지 못했습니다.\n"
            "  1) scripts\\install_ffmpeg.bat 실행 (pip -> winget -> 릴리스 목록 순으로 설치)\n"
            "  2) 또는 https://www.gyan.dev/ffmpeg/builds/ 에서 받아 bin 폴더를 PATH 에\n"
            "  3) 또는 환경변수 FFMPEG 에 ffmpeg.exe 전체 경로를 넣으세요."
        )


def find_ffmpeg() -> str:
    for candidate in (os.environ.get("FFMPEG"), "ffmpeg", "ffmpeg.exe"):
        if not candidate:
            continue
        found = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if found:
            return found
    # 레포 동봉 위치도 봅니다
    here = Path(__file__).resolve().parents[3]
    for rel in ("tools/ffmpeg/bin/ffmpeg.exe", "tools/ffmpeg/ffmpeg.exe", "tools/ffmpeg"):
        p = here / rel
        if p.exists():
            return str(p)
    found = _winget_ffmpeg() or pip_ffmpeg()
    if found:
        return found
    raise FFmpegMissing()


def reset_cache() -> None:
    """pip 설치 직후처럼 찾은 결과를 다시 봐야 할 때."""
    pip_ffmpeg.cache_clear()


@functools.lru_cache(maxsize=1)
def pip_ffmpeg() -> str | None:
    """pip 의 imageio-ffmpeg 가 동봉한 ffmpeg — 녹화에 필요한 기능이 있을 때만.

    우리가 고른 빌드가 아니라서(버전이 바뀌면 빌드도 바뀔 수 있음) 쓰기 전에
    gdigrab·libx264·aac 를 확인합니다. 실행이 들어가므로 프로세스당 한 번만.
    """
    try:
        import imageio_ffmpeg  # noqa: PLC0415 — 없으면 이 경로만 건너뜀
    except ImportError:
        return None
    try:
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 — 플랫폼에 맞는 바이너리가 없음 등
        return None
    if not exe or not Path(exe).exists():
        return None
    from ..ffmpeg_fetch import missing_features  # noqa: PLC0415 — 순환 import 회피

    lack = missing_features(Path(exe))
    if lack:
        log.warning("pip 의 ffmpeg(%s)에 녹화에 필요한 기능이 없어 쓰지 않습니다: %s",
                    exe, ", ".join(lack))
        return None
    return exe


def _winget_ffmpeg() -> str | None:
    """`winget install Gyan.FFmpeg` 이 둔 ffmpeg.exe.

    winget 은 PATH 를 바꾸지만, 설치 전에 열린 창(설치·점검 창, setup.bat 의 cmd,
    이미 떠 있는 녹화기)에는 반영되지 않아 which 로는 못 찾습니다(이슈 #59).
    그래서 winget 이 실제로 만드는 두 곳을 직접 봅니다.
      %LOCALAPPDATA%\\Microsoft\\WinGet\\Links\\ffmpeg.exe              (별칭)
      %LOCALAPPDATA%\\Microsoft\\WinGet\\Packages\\Gyan.FFmpeg*\\ffmpeg-*\\bin  (본체)
    """
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    root = Path(local) / "Microsoft" / "WinGet"
    link = root / "Links" / "ffmpeg.exe"
    if link.exists():
        return str(link)
    hits = sorted(root.glob("Packages/Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe"))
    return str(hits[-1]) if hits else None


def find_ffprobe() -> str | None:
    for candidate in (os.environ.get("FFPROBE"), "ffprobe", "ffprobe.exe"):
        if candidate and shutil.which(candidate):
            return shutil.which(candidate)
    try:
        ff = Path(find_ffmpeg())
        probe = ff.with_name("ffprobe" + ff.suffix)
        return str(probe) if probe.exists() else None
    except FFmpegMissing:
        return None


def _creationflags() -> int:
    """Windows 에서 콘솔 창이 깜빡이지 않게 합니다."""
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def run(args: list[str], *, timeout: float | None = None, check: bool = True) -> subprocess.CompletedProcess:
    log.debug("실행: %s", " ".join(args))
    proc = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, creationflags=_creationflags(),
    )
    if check and proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-15:])
        raise RuntimeError(f"ffmpeg 실패 (code {proc.returncode}):\n{tail}")
    return proc


def popen(args: list[str], *, stdin_pipe: bool = True,
          stderr_log: Path | None = None) -> subprocess.Popen:
    """오래 도는 ffmpeg 를 띄웁니다.

    **stderr 를 파이프로 받지 않습니다.** ffmpeg 는 -loglevel warning
    에서도 녹화 중에 계속 stderr 에 씁니다(프레임 드롭, 인코더 경고).
    아무도 읽지 않는 파이프는 버퍼가 차는 순간 ffmpeg 의 write 를
    막아 세웁니다. 윈도우 파이프 버퍼는 4KB 라 긴 녹화에서는 쉽게
    찹니다. 그러면 그 뒤 프레임이 파일에 들어가지 않는데 poll() 은
    None 이라 '녹화 중' 으로 보입니다. 사용자는 끝까지 찍힌 줄 알고
    있다가 토막 난 파일을 받습니다.

    진단이 필요하면 stderr_log 에 파일 경로를 주세요. 파일은 버퍼가
    차지 않습니다. 주지 않으면 버립니다.
    """
    log.debug("백그라운드 실행: %s", " ".join(args))
    if stderr_log is not None:
        Path(stderr_log).parent.mkdir(parents=True, exist_ok=True)
        # 핸들을 닫지 않습니다 — ffmpeg 가 살아 있는 동안 열려 있어야 합니다.
        err = Path(stderr_log).open("ab", buffering=0)  # noqa: SIM115
    else:
        err = subprocess.DEVNULL
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if stdin_pipe else subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=err,
        creationflags=_creationflags(),
    )
    # 파일 핸들을 프로세스에 묶어 둡니다. 여기서 닫으면 ffmpeg 가
    # 끝나기 전에 파일이 사라집니다.
    proc._syncnote_stderr = err if stderr_log is not None else None  # type: ignore[attr-defined]
    return proc


def read_stderr_log(path: Path | None, lines: int = 10) -> str:
    """ffmpeg 가 남긴 로그의 끝부분. 없으면 빈 문자열."""
    if path is None:
        return ""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


def duration_of(path: Path) -> float | None:
    """미디어 길이(초). ffprobe 가 없으면 None."""
    probe = find_ffprobe()
    if not probe:
        return None
    try:
        proc = run(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            timeout=30,
        )
        return float((proc.stdout or "").strip())
    except Exception:
        return None
