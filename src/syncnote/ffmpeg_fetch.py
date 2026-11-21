"""ffmpeg 를 실제 릴리스 목록에서 받아 tools\\ffmpeg 에 둡니다 — 실패하면 이전 버전으로 롤백.

    python -m syncnote.ffmpeg_fetch [--dest tools] [--max 6] [--force] [--dry-run]

install_ffmpeg.bat 이 winget 이 없거나 실패했을 때 부릅니다. 고정 URL 하나만 믿지
않고, winget 의 Gyan.FFmpeg 가 실제로 받아 오는 GitHub 저장소(GyanD/codexffmpeg)의
릴리스 목록을 읽어 새 것부터 차례로 시도합니다.

  후보 순서   정식 버전(9.1, 9.0 …) 새 것부터 → 날짜별 git 빌드 새 것부터
              → (목록을 못 읽으면) gyan.dev 의 release-essentials
  후보마다    1) 내려받기 — 끊기면 2·4·8 초 쉬고 다시(일시적 오류만)
              2) SHA-256 — 릴리스가 알려 준 digest 와 다르면 버림
              3) 압축 풀기 — 레포 밖으로 나가는 경로는 거부
              4) 기능 확인 — 녹화에 꼭 필요한 gdigrab·libx264·aac 가 있어야 함
              하나라도 실패하면 그 후보를 지우고 **한 단계 이전 릴리스로 롤백**
  바꿔 끼우기 tools\\ffmpeg 를 tools\\ffmpeg.old 로 옮긴 뒤 새 것을 넣고, 도중에
              실패하면 옛것을 되돌려 놓습니다(쓰던 ffmpeg 를 잃지 않게).
  디스크 부족 다른 후보도 같으니 바로 멈춥니다.

표준 라이브러리만 씁니다(설치 단계라 requests 가 아직 없을 수 있음). 프록시는
urllib 가 HTTPS_PROXY 환경변수를 그대로 따릅니다.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASES_API = "https://api.github.com/repos/GyanD/codexffmpeg/releases?per_page=30"
GYAN_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
USER_AGENT = "syncnote-setup"
ASSET_SUFFIX = "-essentials_build.zip"      # 녹화에 필요한 것은 다 있고 full 의 절반 크기
BACKOFF = (2, 4, 8)
# 녹화에 꼭 필요한 것. 화면 캡처(gdigrab)는 Windows 녹화기에서만 씁니다.
NEEDED = {"gdigrab": "-devices", "libx264": "-encoders", "aac": "-encoders"}
if os.name != "nt":
    NEEDED = {k: v for k, v in NEEDED.items() if k != "gdigrab"}


def _say(msg: str) -> None:
    """바로 내보냅니다 — bat 창에서 pip·다운로드 출력과 순서가 섞이지 않게."""
    print(msg, flush=True)


class Fatal(Exception):
    """다른 후보로 넘어가도 소용없는 실패(디스크 부족 등)."""


class CandidateFailed(Exception):
    """이 후보만 못 씀 — 이전 릴리스로 롤백.

    kind: net(내려받기) · hash(해시 다름) · feature(기능 없음) · other
    """

    def __init__(self, msg: str, kind: str = "other") -> None:
        super().__init__(msg)
        self.kind = kind


@dataclass
class Candidate:
    name: str
    url: str
    version: str
    sha256: str = ""          # 비어 있으면 확인할 해시가 없음
    size: int = 0
    stable: bool = False
    published: str = ""


@dataclass
class Result:
    ok: bool
    ffmpeg: str = ""
    used: Candidate | None = None
    tried: list[tuple[str, str]] = field(default_factory=list)   # (버전, 실패 이유)
    kinds: list[str] = field(default_factory=list)                # tried 와 같은 순서
    list_failed: bool = False                                     # 릴리스 목록을 못 읽음


# ---------------------------------------------------------------------------
# 후보 목록
# ---------------------------------------------------------------------------
def _version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(x) for x in tag.split("."))


def candidates(releases: list[dict]) -> list[Candidate]:
    """GitHub 릴리스 JSON -> 시도할 순서대로(순수 함수)."""
    out: list[Candidate] = []
    for rel in releases or []:
        if rel.get("draft"):
            continue
        tag = str(rel.get("tag_name", ""))
        for a in rel.get("assets") or []:
            name = str(a.get("name", ""))
            if not name.endswith(ASSET_SUFFIX):
                continue
            digest = str(a.get("digest") or "")
            out.append(Candidate(
                name=name, url=str(a.get("browser_download_url", "")), version=tag,
                sha256=digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else "",
                size=int(a.get("size") or 0),
                stable=bool(re.fullmatch(r"\d+(\.\d+)+", tag)) and not rel.get("prerelease"),
                published=str(rel.get("published_at", ""))))
    stable = sorted((c for c in out if c.stable), key=lambda c: _version_key(c.version), reverse=True)
    nightly = sorted((c for c in out if not c.stable), key=lambda c: c.published, reverse=True)
    return [c for c in stable + nightly if c.url]


def _open(url: str, timeout: float = 30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 — https 고정 주소


def fetch_releases(opener: Callable = _open) -> list[dict]:
    with opener(RELEASES_API) as r:
        return json.loads(r.read().decode("utf-8"))


def gyan_candidate(opener: Callable = _open) -> Candidate:
    """목록을 못 읽었을 때의 마지막 후보. 해시는 gyan.dev 가 옆에 둔 .sha256 으로."""
    sha = ""
    try:
        with opener(GYAN_URL + ".sha256", timeout=15) as r:
            sha = r.read().decode("ascii", "replace").split()[0].strip().lower()
    except Exception:  # noqa: BLE001 — 없으면 기능 확인만으로 판단
        sha = ""
    return Candidate(name="ffmpeg-release-essentials.zip", url=GYAN_URL, version="gyan.dev",
                     sha256=sha if re.fullmatch(r"[0-9a-f]{64}", sha) else "", stable=True)


# ---------------------------------------------------------------------------
# 한 후보 처리
# ---------------------------------------------------------------------------
def _transient(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 429) or exc.code >= 500
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))


def _raise_if_disk_full(exc: OSError) -> None:
    if exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) == 112:
        raise Fatal("디스크 공간이 부족합니다. 공간을 비운 뒤 다시 실행하세요.") from exc


def download(c: Candidate, dest: Path, *, opener: Callable = _open,
             sleep: Callable[[float], None] = time.sleep, say: Callable[[str], None] = None) -> Path:
    """dest 로 받고 SHA-256 을 확인합니다. 일시적 오류만 2·4·8 초 뒤 다시."""
    say = say or _say
    part = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(len(BACKOFF) + 1):
        h = hashlib.sha256()
        try:
            with opener(c.url, timeout=60) as r, part.open("wb") as f:
                total = int(r.headers.get("Content-Length") or c.size or 0)
                got, shown = 0, -1
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    pct = got * 100 // total if total else -1
                    if pct >= 0 and pct // 20 != shown:
                        shown = pct // 20
                        say(f"      {pct:3d}%  {got / 1e6:,.0f} / {total / 1e6:,.0f} MB")
            break
        except OSError as exc:
            if not isinstance(exc, urllib.error.URLError):
                _raise_if_disk_full(exc)
            part.unlink(missing_ok=True)
            if attempt < len(BACKOFF) and _transient(exc):
                say(f"      내려받기 끊김({exc}) — {BACKOFF[attempt]}초 뒤 다시")
                sleep(BACKOFF[attempt])
                continue
            raise CandidateFailed(f"내려받기 실패: {exc}", "net") from exc
    if c.sha256 and h.hexdigest() != c.sha256:
        part.unlink(missing_ok=True)
        raise CandidateFailed("SHA-256 이 릴리스에 적힌 값과 다름", "hash")
    part.replace(dest)
    return dest


def extract(zip_path: Path, into: Path) -> Path:
    """압축을 풀고 ffmpeg 폴더(그 안에 bin\\ffmpeg.exe)를 돌려줍니다."""
    into.mkdir(parents=True, exist_ok=True)
    base = into.resolve()
    try:
        with zipfile.ZipFile(zip_path) as z:
            for m in z.namelist():
                target = (base / m).resolve()
                if base != target and base not in target.parents:
                    raise CandidateFailed(f"압축 안 경로가 바깥을 가리킴: {m}")
            z.extractall(base)
    except zipfile.BadZipFile as exc:
        raise CandidateFailed("zip 이 깨짐") from exc
    except OSError as exc:
        _raise_if_disk_full(exc)
        raise CandidateFailed(f"압축 풀기 실패: {exc}") from exc
    hits = sorted(base.glob("*/bin/ffmpeg.exe")) or sorted(base.glob("bin/ffmpeg.exe"))
    if not hits:
        raise CandidateFailed("압축 안에 bin\\ffmpeg.exe 가 없음")
    return hits[0].parent.parent


def missing_features(ffmpeg: Path, *, run: Callable = subprocess.run) -> list[str]:
    """녹화에 필요한데 이 ffmpeg 빌드에 없는 것. 실행조차 안 되면 ['실행 안 됨']."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    seen: dict[str, str] = {}
    for opt in sorted(set(NEEDED.values())):
        try:
            p = run([str(ffmpeg), "-hide_banner", opt], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=30, creationflags=flags)
        except (OSError, subprocess.SubprocessError):
            return ["실행 안 됨"]
        seen[opt] = (p.stdout or "") + (p.stderr or "")
    return [name for name, opt in NEEDED.items()
            if not re.search(rf"(^|\s){re.escape(name)}(\s|$)", seen[opt], re.M)]


def swap_in(new_dir: Path, dest: Path) -> Path:
    """dest\\ffmpeg 를 new_dir 로 바꿉니다. 도중에 실패하면 옛것을 되돌립니다."""
    final, old = dest / "ffmpeg", dest / "ffmpeg.old"
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    had_old = final.exists()
    if had_old:
        try:
            final.replace(old)
        except OSError as exc:              # 녹화 중이라 ffmpeg.exe 가 잠김 등 — 후보 탓 아님
            _raise_if_disk_full(exc)
            raise Fatal(f"쓰던 tools\\ffmpeg 를 옮기지 못했습니다({exc}). "
                        "녹화·송출을 끈 뒤 다시 실행하세요.") from exc
    try:
        shutil.move(str(new_dir), str(final))
    except OSError as exc:
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        if had_old:
            old.replace(final)                      # 롤백: 쓰던 것을 그대로
        _raise_if_disk_full(exc)
        raise CandidateFailed(f"tools\\ffmpeg 로 옮기지 못함: {exc}") from exc
    if had_old:
        shutil.rmtree(old, ignore_errors=True)
    return final / "bin" / "ffmpeg.exe"


# ---------------------------------------------------------------------------
# 전체
# ---------------------------------------------------------------------------
def install(dest: Path, *, max_tries: int = 6, force: bool = False,
            opener: Callable = _open, run: Callable = subprocess.run,
            sleep: Callable[[float], None] = time.sleep,
            say: Callable[[str], None] = None) -> Result:
    say = say or _say
    dest = Path(dest)
    current = dest / "ffmpeg" / "bin" / "ffmpeg.exe"
    if current.exists() and not force and not missing_features(current, run=run):
        say(f"이미 쓸 수 있는 ffmpeg 가 있습니다: {current}")
        return Result(True, str(current))

    try:
        cands = candidates(fetch_releases(opener))[:max_tries]
        say(f"릴리스 목록에서 후보 {len(cands)}개: " + ", ".join(c.version for c in cands))
    except Exception as exc:  # noqa: BLE001 — 목록을 못 읽어도 마지막 후보는 있음
        say(f"[!] 릴리스 목록을 읽지 못했습니다({exc}). gyan.dev 로 시도합니다.")
        cands = []
        listing_failed = True
    else:
        listing_failed = False
    cands.append(gyan_candidate(opener))

    result = Result(False, list_failed=listing_failed)
    dest.mkdir(parents=True, exist_ok=True)
    for i, c in enumerate(cands):
        if i:
            say(f"  -> 이전 릴리스로 롤백: {c.version}")
        size = f" ({c.size / 1e6:,.0f} MB)" if c.size else ""
        say(f"[{i + 1}/{len(cands)}] {c.version}  {c.name}{size}")
        work = Path(tempfile.mkdtemp(prefix="_ffmpeg_", dir=dest))
        try:
            z = download(c, work / c.name, opener=opener, sleep=sleep, say=say)
            say("      해시 확인 " + ("통과" if c.sha256 else "생략(릴리스에 해시 없음)"))
            exe = _install_zip(z, work, dest, run=run)
            say(f"완료: {exe}  ({c.version})")
            result.ok, result.ffmpeg, result.used = True, str(exe), c
            return result
        except CandidateFailed as exc:
            say(f"      [X] {exc}")
            result.tried.append((c.version, str(exc)))
            result.kinds.append(exc.kind)
        except Fatal as exc:
            say(f"[X] {exc}")
            result.tried.append((c.version, str(exc)))
            result.kinds.append("fatal")
            return result
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return result


def _install_zip(z: Path, work: Path, dest: Path, *, run: Callable = subprocess.run) -> Path:
    """받았거나 가져온 zip 하나: 압축 풀기 -> 기능 확인 -> tools\\ffmpeg 로 바꿔 끼우기."""
    folder = extract(z, work / "x")
    lack = missing_features(folder / "bin" / "ffmpeg.exe", run=run)
    if lack:
        raise CandidateFailed("녹화에 필요한 기능 없음: " + ", ".join(lack), "feature")
    return swap_in(folder, dest)


def install_from_zip(z: Path, dest: Path, *, sha256: str = "", run: Callable = subprocess.run,
                     say: Callable[[str], None] = None) -> str:
    """오프라인: 사용자가 USB 등으로 가져온 zip. 쓸 수 있게 되면 경로, 아니면 ''."""
    say = say or _say
    dest = Path(dest)
    say(f"가져온 zip 으로 설치합니다: {z}")
    if sha256:
        h = hashlib.sha256(Path(z).read_bytes()).hexdigest()
        if h != sha256.lower():
            say("      [X] SHA-256 이 적어 준 값과 다릅니다")
            return ""
    dest.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="_ffmpeg_", dir=dest))
    try:
        exe = _install_zip(Path(z), work, dest, run=run)
    except (CandidateFailed, Fatal) as exc:
        say(f"      [X] {exc}")
        return ""
    finally:
        shutil.rmtree(work, ignore_errors=True)
    say(f"완료: {exe}")
    return str(exe)


def offline_zips(*folders: Path) -> list[Path]:
    """가져다 둔 ffmpeg zip 들(새 것부터). 이름에 ffmpeg 가 든 .zip 만."""
    out: list[Path] = []
    for d in folders:
        if d and Path(d).is_dir():
            out += [p for p in Path(d).glob("*.zip") if "ffmpeg" in p.name.lower()]
    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


# ---------------------------------------------------------------------------
# winget — 없거나 정책으로 막혔으면 다음부터 건너뜀
# ---------------------------------------------------------------------------
WINGET_ARGS = ["install", "--id", "Gyan.FFmpeg", "-e", "--source", "winget", "--silent",
               "--disable-interactivity", "--accept-source-agreements",
               "--accept-package-agreements"]
_POLICY = re.compile(r"group polic|그룹 정책|disabled by .*polic|관리자가 .*차단", re.I)


@dataclass
class WingetOutcome:
    ffmpeg: str = ""
    reason: str = ""
    permanent: bool = False          # 다시 해도 같음 -> 다음 실행부터 건너뜀


def winget_install(*, which: Callable = shutil.which, run: Callable = subprocess.run,
                   find: Callable[[], str] = None,
                   say: Callable[[str], None] = None) -> WingetOutcome:
    say = say or _say
    find = find or (lambda: find_usable(say=lambda _m: None))
    exe = which("winget")
    if not exe:
        return WingetOutcome(reason="winget 이 없음(이 Windows 에는 앱 설치 관리자가 없음)",
                             permanent=True)
    say("winget 으로 ffmpeg 를 설치합니다... (약 250MB, 몇 분 걸립니다)")
    try:
        p = run([exe, *WINGET_ARGS], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=1800)
    except (OSError, subprocess.SubprocessError) as exc:
        return WingetOutcome(reason=f"winget 실행 실패: {exc}")
    out = (p.stdout or "") + (p.stderr or "")
    for line in out.splitlines()[-8:]:
        if line.strip():
            say("      " + line.strip()[:160])
    # 종료 코드가 실패여도 이미 설치돼 있을 수 있어, 성공은 실제로 찾아서 판정
    found = find()
    if found:
        return WingetOutcome(ffmpeg=found)
    if _POLICY.search(out):
        return WingetOutcome(reason="winget 이 정책으로 막혀 있음", permanent=True)
    return WingetOutcome(reason=f"winget 설치 실패 (종료 코드 {p.returncode})")


# ---------------------------------------------------------------------------
# 원인별 자동 흐름: 찾기 -> 가져온 zip -> pip -> winget -> 릴리스 -> 다운로드 폴더 zip
# ---------------------------------------------------------------------------
@dataclass
class Step:
    name: str
    status: str                     # ok · fail · skip · fatal
    reason: str = ""


@dataclass
class AutoResult:
    ok: bool
    ffmpeg: str = ""
    steps: list[Step] = field(default_factory=list)
    next_action: str = ""


def _state(path: Path | None) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: Path | None, data: dict) -> None:
    if not path:
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass                                # 기록 실패로 설치를 멈추지 않음


def next_action(steps: list[Step], kinds: list[str], *, list_failed: bool = False,
                proxy: str | None = None) -> str:
    """전부 실패했을 때 사람이 할 일 하나."""
    reasons = " ".join(s.reason for s in steps)
    if "디스크" in reasons:
        return "디스크 공간을 1GB 이상 비운 뒤 다시 실행하세요."
    if "옮기지 못했습니다" in reasons:
        return "녹화·송출을 끈 뒤 다시 실행하세요(쓰던 ffmpeg 가 잠겨 있음)."
    net = kinds.count("net") + (1 if list_failed else 0)
    if net and net >= max(1, len(kinds) - kinds.count("hash") - kinds.count("feature")):
        if proxy:
            return f"프록시 설정(HTTPS_PROXY={proxy})이 맞는지 확인한 뒤 다시 실행하세요."
        return ("인터넷에 닿지 않습니다. 학교·회사망이면 HTTPS_PROXY 환경변수에 프록시 주소를 "
                "넣고 다시 실행하세요. 아니면 인터넷 연결을 확인하세요.")
    if kinds and all(k == "hash" for k in kinds):
        return ("받는 도중 파일이 바뀝니다(보안 장비 등). 다른 망에서 받거나 USB 로 가져오세요: "
                "zip 을 tools 폴더에 넣고 다시 실행.")
    return ("다른 PC 에서 https://github.com/GyanD/codexffmpeg/releases 의 "
            "*-essentials_build.zip 을 받아 USB 로 tools 폴더에 넣고 다시 실행하세요.")


def auto(dest: Path, *, state_path: Path | None = None, retry_skipped: bool = False,
         downloads: Path | None = None,
         find: Callable[[], str] = None, pip: Callable[[], str] = None,
         winget: Callable[[], WingetOutcome] = None, release: Callable[[], Result] = None,
         from_zip: Callable[[Path], str] = None,
         say: Callable[[str], None] = None) -> AutoResult:
    say = say or _say
    dest = Path(dest)
    find = find or (lambda: find_usable(say=say))
    pip = pip or (lambda: pip_install(say=say))
    winget = winget or (lambda: winget_install(say=say))
    release = release or (lambda: install(dest, say=say))
    from_zip = from_zip or (lambda z: install_from_zip(z, dest, say=say))
    r = AutoResult(False)
    state = _state(state_path)

    def done(name: str, exe: str) -> AutoResult:
        r.steps.append(Step(name, "ok"))
        r.ok, r.ffmpeg = True, exe
        say(f"쓸 수 있는 ffmpeg: {exe}  ({name})")
        return r

    exe = find()
    if exe:
        return done("이미 있음", exe)
    r.steps.append(Step("이미 있음", "fail", "쓸 수 있는 ffmpeg 없음"))

    for z in offline_zips(dest):                    # tools 에 가져다 둔 zip 이 먼저
        exe = from_zip(z)
        if exe:
            return done(f"가져온 zip ({z.name})", exe)
        r.steps.append(Step(f"가져온 zip ({z.name})", "fail", "zip 으로 설치 실패"))

    exe = pip()
    if exe:
        return done("pip (imageio-ffmpeg)", exe)
    r.steps.append(Step("pip (imageio-ffmpeg)", "fail", "pip 설치 실패 또는 기능 부족"))

    skip = (state.get("winget") or {}) if not retry_skipped else {}
    if skip.get("skip"):
        say(f"winget 은 건너뜁니다: {skip.get('reason')} (지난 실행에서 확인)")
        r.steps.append(Step("winget", "skip", str(skip.get("reason", ""))))
    else:
        w = winget()
        if w.ffmpeg:
            return done("winget", w.ffmpeg)
        r.steps.append(Step("winget", "fail", w.reason))
        if w.permanent:
            state["winget"] = {"skip": True, "reason": w.reason,
                               "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            _save_state(state_path, state)

    rel = release()
    if rel.ok:
        return done(f"릴리스 목록 ({rel.used.version if rel.used else ''})", rel.ffmpeg)
    kinds = rel.kinds if len(rel.kinds) == len(rel.tried) else ["other"] * len(rel.tried)
    for (v, why), kind in zip(rel.tried, kinds, strict=True):
        r.steps.append(Step(f"릴리스 {v}", "fatal" if kind == "fatal" else "fail", why))
    if any(k == "fatal" for k in rel.kinds):
        r.next_action = next_action(r.steps, rel.kinds, list_failed=rel.list_failed)
        return r

    for z in offline_zips(downloads) if downloads else []:      # 마지막: 다운로드 폴더
        exe = from_zip(z)
        if exe:
            return done(f"다운로드 폴더 zip ({z.name})", exe)
        r.steps.append(Step(f"다운로드 폴더 zip ({z.name})", "fail", "zip 으로 설치 실패"))

    r.next_action = next_action(r.steps, rel.kinds, list_failed=rel.list_failed,
                                proxy=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))
    return r


def print_summary(r: AutoResult, say: Callable[[str], None] = None) -> None:
    say = say or _say
    if r.ok:
        return
    say("")
    say("[X] ffmpeg 를 설치하지 못했습니다. 시도한 순서와 이유:")
    for i, s in enumerate(r.steps, 1):
        mark = {"skip": "건너뜀", "fatal": "중단"}.get(s.status, "실패")
        say(f"  {i}. {s.name} — {mark}: {s.reason}")
    say(f"다음 할 일: {r.next_action}")


def find_usable(say: Callable[[str], None] = None) -> str:
    """0단계: 이미 쓸 수 있는 ffmpeg 가 있나(PATH·FFMPEG·tools·winget·pip). 없으면 ''."""
    say = say or _say
    from .recorder.ffmpeg_util import FFmpegMissing, find_ffmpeg

    try:
        exe = find_ffmpeg()
    except FFmpegMissing:
        return ""
    lack = missing_features(Path(exe))
    if lack:
        say(f"[!] 찾은 ffmpeg 에 녹화에 필요한 기능이 없습니다({', '.join(lack)}): {exe}\n"
            "    새로 설치해도 이것이 먼저 잡히면 쓰이지 않습니다. PATH 에서 빼거나\n"
            "    환경변수 FFMPEG 에 새 ffmpeg.exe 경로를 넣으세요.")
        return ""
    return exe


def pip_install(*, run: Callable = subprocess.run, say: Callable[[str], None] = None) -> str:
    """1단계: pip 의 imageio-ffmpeg(ffmpeg 실행 파일 동봉). 쓸 수 있으면 그 경로, 아니면 ''."""
    say = say or _say
    from .recorder import ffmpeg_util

    say("pip 로 imageio-ffmpeg 를 설치합니다... (약 40MB)")
    try:
        p = run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                 "imageio-ffmpeg>=0.5"], timeout=900)
    except (OSError, subprocess.SubprocessError) as exc:
        say(f"      [X] pip 를 실행하지 못했습니다: {exc}")
        return ""
    if p.returncode != 0:
        say(f"      [X] pip 설치 실패 (종료 코드 {p.returncode})")
        return ""
    ffmpeg_util.reset_cache()
    exe = ffmpeg_util.pip_ffmpeg()
    if not exe:
        say("      [X] 설치는 됐지만 쓸 수 있는 ffmpeg 가 없습니다(필요 기능 부족 등)")
    return exe or ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m syncnote.ffmpeg_fetch",
                                 description="릴리스 목록에서 ffmpeg 를 받아 tools\\ffmpeg 에 둡니다")
    ap.add_argument("--find", action="store_true",
                    help="이미 쓸 수 있는 ffmpeg 가 있으면 경로를 출력하고 0 (설치 안 함)")
    ap.add_argument("--pip", action="store_true", help="pip 의 imageio-ffmpeg 로 설치")
    ap.add_argument("--auto", action="store_true",
                    help="찾기 -> 가져온 zip -> pip -> winget -> 릴리스 목록 -> 다운로드 폴더 zip")
    ap.add_argument("--retry-skipped", action="store_true",
                    help="지난번에 '다시 해도 같음' 으로 건너뛴 winget 도 다시 시도")
    ap.add_argument("--from-zip", help="가져온 zip 으로 설치(오프라인)")
    ap.add_argument("--sha256", default="", help="--from-zip 의 SHA-256 (있으면 확인)")
    ap.add_argument("--dest", default=str(REPO_ROOT / "tools"))
    ap.add_argument("--max", type=int, default=6, help="롤백해 볼 릴리스 수 (기본 6)")
    ap.add_argument("--force", action="store_true", help="쓸 수 있는 ffmpeg 가 있어도 새로 받기")
    ap.add_argument("--dry-run", action="store_true", help="받지 않고 시도할 순서만 출력")
    a = ap.parse_args(argv)
    if a.from_zip:
        return 0 if install_from_zip(Path(a.from_zip), Path(a.dest), sha256=a.sha256) else 1
    if a.auto:
        downloads = Path(os.environ.get("USERPROFILE") or Path.home()) / "Downloads"
        r = auto(Path(a.dest), state_path=Path(a.dest) / ".ffmpeg_install.json",
                 retry_skipped=a.retry_skipped, downloads=downloads)
        print_summary(r)
        return 0 if r.ok else 1
    if a.find or a.pip:
        exe = find_usable() if a.find else pip_install()
        if exe:
            print(f"쓸 수 있는 ffmpeg: {exe}")
        return 0 if exe else 1
    if a.dry_run:
        cands = candidates(fetch_releases())[:a.max] + [gyan_candidate()]
        for i, c in enumerate(cands, 1):
            print(f"{i}. {c.version:<28} {c.name}  sha256={c.sha256[:12] or '-'}")
        return 0
    r = install(Path(a.dest), max_tries=a.max, force=a.force)
    if not r.ok and r.tried:
        print("\n시도한 순서와 실패 이유:")
        for v, why in r.tried:
            print(f"  - {v}: {why}")
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())
