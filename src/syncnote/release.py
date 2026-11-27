"""GitHub 릴리스 업로더.

왜 이렇게 복잡한가:
  - 릴리스 에셋 1개의 상한이 2GiB 입니다. 1시간짜리 화면녹화는 이걸 넘길 수 있어
    자동 분할 + 복원 안내가 필요합니다.
  - 에셋 이름은 중복될 수 없고, GitHub 가 일부 문자를 마음대로 바꿉니다.
    (공백 -> '.', 비ASCII -> 제거) 한글 파일명이 깨지므로 안전한 이름으로
    바꿔 올리고, 원래 이름은 manifest 에 남깁니다.
  - 업로드 중 끊기면 '절반 올라간 에셋' 이 남습니다. 먼저 지우고 다시 올립니다.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import time
import unicodedata
import zipfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import secrets
from .logging_setup import get as get_logger
from .state import sha256_file

log = get_logger("release")

API = "https://api.github.com"
GITHUB_ASSET_LIMIT = 2 * 1024**3  # 2 GiB, GitHub 하드 리밋


class ReleaseError(Exception):
    pass


def check_token(token: str | None = None) -> tuple[str, str]:
    """설정된 GitHub 토큰이 '실제로' 통하는지 GitHub 에 물어봅니다.

    doctor 가 '토큰이 있다' 로 끝내지 않게(존재 != 유효) 쓰는 가벼운 점검.
    GET /user 한 번. 반환 (mark, detail):
      'O' 유효 · 'X' 거절/권한부족(=반드시 조치) · '!' 확인 불가(오프라인이거나
      이 세션에서 토큰이 안 보임). 'X' 만 doctor 를 실패로 봅니다 — 오프라인에서
      거짓 실패를 내지 않기 위함입니다.
    """
    if token is None:
        token = secrets.get("github", required=False) or ""
    if not token:
        return ("!", "이 세션에서 토큰이 안 보입니다 — 대화형 로그온에서 넣었다면 "
                     "SSH/원격 세션엔 안 보일 수 있습니다. 녹화 PC 본체에서 확인하세요")
    try:
        import requests  # type: ignore
    except ImportError:
        return ("!", "requests 없음 — 유효성 확인 못함")
    try:
        resp = requests.get(
            f"{API}/user",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "sync-note",
            },
            timeout=15,
        )
    except Exception as exc:  # 네트워크/타임아웃 — 토큰 탓이 아님
        return ("!", f"GitHub 에 못 닿음({exc.__class__.__name__}) — 오프라인? 유효성 확인 못함")
    if resp.status_code == 200:
        login = ""
        try:
            login = (resp.json() or {}).get("login", "")
        except Exception:
            pass
        scopes = resp.headers.get("X-OAuth-Scopes", "")
        extra = f", 권한범위: {scopes}" if scopes else ""
        return ("O", f"유효함(계정 {login or '?'}{extra})")
    if resp.status_code in (401, 403):
        return ("X", f"토큰 거절됨({resp.status_code}) — 만료됐거나 권한이 모자랍니다. "
                     "python -m syncnote secrets set github 로 갱신하세요")
    return ("!", f"확인 불가(HTTP {resp.status_code})")


# ---------------------------------------------------------------------------
# 에셋 이름
# ---------------------------------------------------------------------------
_ASSET_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def asset_name(original: str, prefix: str = "", unique_key: str | None = None) -> str:
    """GitHub 가 변형하지 않는 이름으로 바꿉니다.

    GitHub 는 에셋 이름의 공백을 '.' 으로 바꾸고 비ASCII 문자를 버립니다.
    한글 제목은 그대로 쓸 수 없으므로 안전한 글자만 남기는데, 그러면
    "운동과건강 1주차.pptx" 가 "1.pptx" 가 되어 다른 과목 파일과 충돌합니다.
    그래서 한글이 섞여 있으면 해시 8자리를 붙입니다.

    **unique_key 를 반드시 넘기세요.** 기본값(파일 이름)으로 두면 과목이
    달라도 같은 이름은 같은 해시가 됩니다. 2026-11-12 실제 실행에서
    9개 과목의 '강의목록.md' 가 전부 809b1351.md 가 되어 서로를
    덮어썼습니다. 16개를 올렸는데 릴리스에는 2개만 남았고, 로그에는
    '업로드 완료' 가 16번 찍혔습니다. unique_key 에는 학기 폴더 기준
    상대 경로를 넣습니다.
    """
    name = unicodedata.normalize("NFC", original)
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""

    safe_stem = _ASSET_SAFE.sub("_", stem).strip("_")
    safe_ext = _ASSET_SAFE.sub("", ext)[:12]
    safe_prefix = _ASSET_SAFE.sub("_", unicodedata.normalize("NFC", prefix)).strip("_")
    if safe_prefix:
        safe_prefix += "_"

    # 원본에 비ASCII 가 섞여 있었다면 정보가 날아간 것이므로 해시로 보강합니다.
    lost = any(ord(ch) > 127 for ch in stem)
    key = unique_key if unique_key is not None else stem
    if lost:
        # 한글이 '_' 로 바뀌며 생긴 '__' 를 하나로(`main_영어2` -> `main_2`).
        safe_stem = re.sub(r"_{2,}", "_", safe_stem)
    if lost or not safe_stem:
        digest = hashlib.sha1(unicodedata.normalize("NFC", key).encode("utf-8")).hexdigest()[:8]
        safe_stem = f"{safe_stem}_{digest}".strip("_") if safe_stem else digest

    out = f"{safe_prefix}{safe_stem}"
    if safe_ext:
        out = f"{out}.{safe_ext}"
    return out[:200]


# ---------------------------------------------------------------------------
# 태그 — 릴리스는 전부 일 단위(년-월-일)
# ---------------------------------------------------------------------------
def tag_for(prefix: str, when: date | None = None) -> str:
    """'recordings' -> 'recordings-2026-11-21'. 녹화·e-class·전사·홈 백업 모두 같은 규칙.

    예전의 월·주 단위나 고정 태그(tag_strategy)는 없앴습니다. 한 릴리스에 한 달치가
    쌓이면 언제 것인지 릴리스 이름만으로 알 수 없고 에셋 목록이 끝없이 길어집니다.
    """
    suffix = (when or date.today()).strftime("%Y-%m-%d")
    return f"{prefix}-{suffix}" if prefix else suffix


# ---------------------------------------------------------------------------
# 분할
# ---------------------------------------------------------------------------
@dataclass
class Part:
    path: Path
    index: int
    total: int


def split_file(src: Path, out_dir: Path, chunk_bytes: int) -> list[Part]:
    """src 를 chunk_bytes 크기로 쪼개 out_dir 에 .partNNN 파일로 씁니다."""
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    size = src.stat().st_size
    total = max(1, -(-size // chunk_bytes))  # ceil
    parts: list[Part] = []
    part_path: Path | None = None
    try:
        with src.open("rb") as fh:
            for idx in range(1, total + 1):
                part_path = out_dir / f"{src.name}.part{idx:03d}"
                written = 0
                with part_path.open("wb") as out:
                    while written < chunk_bytes:
                        block = fh.read(min(8 * 1024 * 1024, chunk_bytes - written))
                        if not block:
                            break
                        out.write(block)
                        written += len(block)
                parts.append(Part(part_path, idx, total))
    except BaseException:
        # 쪼개다 디스크가 꽉 차면, 이미 쓴 조각이 남아 남은 공간을 더 줄입니다.
        # 호출자(홈 백업)는 이 묶음만 실패로 세고 계속하므로 여기서 치웁니다.
        for p in [*(x.path for x in parts), part_path]:
            if p is not None:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass             # 원래 오류를 가리지 않습니다
        raise
    log.info("분할: %s -> %d 조각", src.name, total)
    return parts


RESTORE_SH = """#!/usr/bin/env bash
# 조각 파일을 원본으로 합칩니다.  사용법:  bash restore.sh <원본이름>
set -e
name="$1"
cat "${name}".part* > "${name}"
echo "복원 완료: ${name}"
"""

RESTORE_BAT = """@echo off
REM 조각 파일을 원본으로 합칩니다.  사용법:  restore.bat 원본이름
if "%~1"=="" (echo 사용법: restore.bat 원본파일이름 & exit /b 1)
copy /b "%~1.part*" "%~1"
echo 복원 완료: %~1
"""


# ---------------------------------------------------------------------------
# API 클라이언트
# ---------------------------------------------------------------------------
class ReleaseUploader:
    def __init__(
        self,
        owner: str,
        repo: str,
        token: str | None = None,
        *,
        retry_count: int = 4,
        retry_backoff: float = 2.0,
        split_size_mb: int = 1900,
        draft: bool = False,
        prerelease: bool = False,
        dry_run: bool = False,
    ) -> None:
        if not owner or not repo:
            raise ReleaseError("github.owner / github.repo 가 설정되지 않았습니다.")
        self.owner = owner
        self.repo = repo
        self.retry_count = max(0, int(retry_count))
        self.retry_backoff = float(retry_backoff)
        self.chunk_bytes = min(int(split_size_mb) * 1024 * 1024, GITHUB_ASSET_LIMIT - 1)
        self.draft = draft
        self.prerelease = prerelease
        self.dry_run = dry_run
        self._token = token
        self._session = None
        self._release_cache: dict[str, dict[str, Any]] = {}
        self._asset_cache: dict[int, list[dict[str, Any]]] = {}

    # -- 하부 ---------------------------------------------------------
    @property
    def token(self) -> str:
        if self._token is None:
            self._token = secrets.get("github") or ""
        if not self._token:
            raise ReleaseError("GitHub 토큰이 없습니다. python -m syncnote secrets set github")
        return self._token

    @property
    def session(self):
        if self._session is None:
            try:
                import requests  # type: ignore
            except ImportError as exc:
                raise ReleaseError("requests 패키지가 필요합니다: pip install requests") from exc
            s = requests.Session()
            s.headers.update(
                {
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "sync-note",
                }
            )
            self._session = s
        return self._session

    def _request(self, method: str, url: str, *,
                 data_factory: Callable[[], Any] | None = None, raw: bool = False,
                 **kwargs) -> Any:
        """재시도 포함 요청.

        raw 면 JSON 으로 풀지 않고 응답 바이트를 그대로 돌려줍니다(에셋 내려받기).
        빈 본문도 b"" 그대로입니다 — {} 로 바꾸면 '0바이트 에셋' 이 안 보입니다.

        data_factory 를 주면 '시도마다' 새로 호출해 본문을 만듭니다.
        파일 객체를 data= 로 한 번만 넘기면, 첫 시도에서 핸들이 끝까지
        읽힌 뒤 재시도에서 0바이트를 보내게 됩니다. 그러면 GitHub 는
        빈 에셋을 받아들이고 200 을 돌려주므로, 녹화본이 백업된 줄
        알았는데 0바이트인 상황이 조용히 만들어집니다.
        """
        import requests  # type: ignore

        timeout = kwargs.pop("timeout", 120)
        last_exc: Exception | None = None
        for attempt in range(self.retry_count + 1):
            body = None
            try:
                if data_factory is not None:
                    body = data_factory()
                    kwargs["data"] = body
                resp = self.session.request(method, url, timeout=timeout, **kwargs)
            except requests.RequestException as exc:
                _close(body)
                last_exc = exc
                if attempt >= self.retry_count:
                    break
                wait = self.retry_backoff ** (attempt + 1)
                log.warning("네트워크 오류(%s). %.0f초 후 재시도 %d/%d",
                            exc.__class__.__name__, wait, attempt + 1, self.retry_count)
                time.sleep(wait)
                continue
            else:
                _close(body)

            if resp.status_code == 401:
                raise ReleaseError("GitHub 토큰이 거부됐습니다(401). 토큰이 만료됐거나 권한이 없습니다.")
            if resp.status_code == 403 and "rate limit" in resp.text.lower():
                reset = resp.headers.get("X-RateLimit-Reset")
                raise ReleaseError(f"GitHub API 호출 한도 초과. 재설정 시각(epoch): {reset}")
            if resp.status_code == 404 and method == "GET":
                return None
            if 500 <= resp.status_code < 600 and attempt < self.retry_count:
                wait = self.retry_backoff ** (attempt + 1)
                log.warning("GitHub %d. %.0f초 후 재시도 %d/%d",
                            resp.status_code, wait, attempt + 1, self.retry_count)
                time.sleep(wait)
                continue
            if not resp.ok:
                raise ReleaseError(f"GitHub {method} {url} -> {resp.status_code}: {resp.text[:400]}")
            if raw:
                return bytes(resp.content or b"")
            if not resp.content:
                return {}
            try:
                return resp.json()
            except json.JSONDecodeError:
                return {}
        raise ReleaseError(f"재시도 {self.retry_count}회 모두 실패: {last_exc}")

    # -- 릴리스 -------------------------------------------------------
    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    def check_access(self) -> None:
        """올리기 전에 레포에 닿는지 봅니다.

        레포가 없을 때와 권한이 없을 때를 구분해서 알려 줍니다.
        구분하지 않으면 사용자는 'POST ... -> 404' 만 보고 무엇을
        고쳐야 할지 모릅니다. 새 학기에 과목 레포를 안 만들었을 때
        반드시 걸리는 길입니다.
        """
        if self.dry_run:
            return

        url = f"{API}/repos/{self.owner}/{self.repo}"
        try:
            resp = self.session.get(url, timeout=30)
        except Exception as exc:
            raise ReleaseError(f"{self.slug}: GitHub 에 닿지 못했습니다 - {exc}") from exc
        if resp.status_code == 200:
            # permissions.push 로 막지 않습니다. Actions 의 GITHUB_TOKEN 은
            # 릴리스 에셋을 올릴 수 있는데도 이 칸이 false 로 오는 경우가
            # 있습니다. 2026-11-12 실행에서 그 때문에 업로드가 통째로
            # 막혔습니다 — 바로 전 실행까지 같은 토큰으로 46개를 올렸는데
            # 제가 넣은 사전 점검이 그걸 거부했습니다.
            #
            # 못 올리면 실제 업로드가 403 으로 알려 줍니다. 사전 점검은
            # '레포가 없다' 처럼 **그것 말고는 알 길이 없는 것** 만 막습니다.
            if not resp.json().get("permissions", {}).get("push", True):
                log.warning(
                    "%s: 토큰에 쓰기 권한이 없다고 나옵니다. 그래도 올려 봅니다 "
                    "— 이 칸이 실제 권한과 다른 경우가 있습니다.", self.slug,
                )
            return
        if resp.status_code == 404:
            raise ReleaseError(
                f"{self.slug}: 레포가 없거나 토큰이 볼 수 없습니다.\n"
                "  - 레포를 아직 안 만들었다면 먼저 만드세요(과목 하나당 레포 하나).\n"
                "  - 조직 레포라면 토큰이 그 조직에 허용돼 있어야 합니다."
            )
        if resp.status_code in (401, 403):
            raise ReleaseError(
                f"{self.slug}: 토큰이 거절됐습니다({resp.status_code}). "
                "만료됐거나 권한 범위가 모자랍니다."
            )
        raise ReleaseError(f"{self.slug}: 확인 실패 {resp.status_code}")

    def is_public(self) -> bool | None:
        """레포가 public 이면 True, private/internal 이면 False, 모르면 None.

        올리기 전 '공개 저장소 차단' 가드가 씁니다. dry-run·접근 실패·필드
        없음은 None(모름) — 호출부는 None 을 '막지 않음' 으로 다룹니다(네트워크
        한 번 실패했다고 수집 자체를 세우진 않습니다).
        """
        if self.dry_run:
            return None
        url = f"{API}/repos/{self.owner}/{self.repo}"
        try:
            resp = self.session.get(url, timeout=30)
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        data = resp.json()
        vis = data.get("visibility")
        if vis:
            return vis == "public"          # public / private / internal
        if "private" in data:
            return data["private"] is False
        return None

    def _owner_is_org(self) -> bool:
        """개인 계정인지 조직인지. 레포 생성 주소가 달라집니다."""
        try:
            resp = self.session.get(f"{API}/users/{self.owner}", timeout=20)
        except Exception:
            return False
        if resp.status_code != 200:
            return False
        return str(resp.json().get("type", "")).lower() == "organization"

    def create_repo(self, *, private: bool = True, description: str = "") -> dict[str, Any]:
        """과목 레포를 만듭니다. 학기마다 새 레포를 쓰는 구조를 위해서입니다.

        CloneUp(app/github/api_client.py)의 422 처리를 가져왔습니다.
        다만 **auto_init 은 정반대로 켭니다.** CloneUp 은 새 로컬
        히스토리를 push 하므로 원격 첫 커밋이 있으면 non-fast-forward 로
        막힙니다. 우리는 push 를 하지 않고 릴리스만 만듭니다. 커밋이
        하나도 없는 레포는 기본 브랜치가 없어서 릴리스 태그가 가리킬
        곳이 없습니다. 그래서 README 커밋을 만들어 둡니다.
        (빈 레포에서 릴리스가 실제로 거부되는지는 실기로 확인하지
         못했습니다 — 되더라도 README 하나가 손해는 아닙니다.)

        조직 레포는 POST /orgs/{org}/repos 입니다. 토큰에 그 조직의
        레포 생성 권한이 있어야 합니다. 개인 계정은 POST /user/repos.
        """
        if self.dry_run:
            log.info("[dry-run] 레포 생성 생략: %s", self.slug)
            return {"full_name": self.slug, "permissions": {"push": True}}

        is_org = self._owner_is_org()
        url = f"{API}/orgs/{self.owner}/repos" if is_org else f"{API}/user/repos"
        payload = {
            "name": self.repo,
            "private": bool(private),
            "description": description or f"{self.repo} 자동 수집본",
            "auto_init": True,
        }
        log.info("레포를 만듭니다: %s (%s)", self.slug, "조직" if is_org else "개인")
        try:
            resp = self.session.post(url, json=payload, timeout=30)
        except Exception as exc:
            raise ReleaseError(f"{self.slug}: 레포 생성 요청 실패 - {exc}") from exc

        if resp.status_code in (200, 201):
            return resp.json()
        if resp.status_code == 422:
            message = ""
            try:
                message = str(resp.json().get("message", ""))
            except ValueError:
                message = resp.text[:200]
            if "already exists" in message.lower():
                # 다른 PC 가 방금 만들었을 수 있습니다(같은 과목이 두
                # 강의실에 배포된 경우). 다시 보고 쓸 수 있으면 그대로 씁니다.
                try:
                    self.check_access()
                except ReleaseError:
                    raise ReleaseError(
                        f"{self.slug}: 같은 이름의 레포가 이미 있는데 이 토큰으로는 "
                        "보이지 않습니다. 비공개 레포라면 토큰 권한을 확인하세요."
                    ) from None
                log.info("레포가 이미 있습니다(다른 PC 가 만든 듯): %s", self.slug)
                return {"full_name": self.slug, "permissions": {"push": True}}
            raise ReleaseError(f"{self.slug}: 레포 생성 거부(422) - {message}")
        if resp.status_code in (401, 403):
            raise ReleaseError(
                f"{self.slug}: 레포를 만들 권한이 없습니다({resp.status_code}). "
                "개인 계정이면 토큰의 repo 권한을, 조직이면 그 조직의 "
                "레포 생성 허용 여부를 확인하세요."
            )
        raise ReleaseError(f"{self.slug}: 레포 생성 실패 {resp.status_code}")

    def ensure_repo(self, *, create_missing: bool = False, private: bool = True) -> None:
        """올리기 전에 레포가 준비됐는지 봅니다.

        create_missing 이 꺼져 있으면(기본) 없는 레포를 만들지 않고
        무엇을 해야 하는지만 알려 줍니다. 레포 생성은 토큰에 더 센
        권한을 요구하고, 조직 정책으로 막혀 있는 경우가 흔합니다.
        """
        try:
            self.check_access()
            return
        except ReleaseError as exc:
            if not create_missing or "레포가 없거나" not in str(exc):
                raise
        self.create_repo(private=private)
        self.check_access()

    def get_or_create_release(self, tag: str, *, name: str | None = None,
                              body: str | None = None) -> dict[str, Any]:
        if tag in self._release_cache:
            return self._release_cache[tag]

        # dry-run 은 네트워크를 전혀 건드리지 않습니다.
        # 토큰이 없어도, 저장소가 아직 없어도 무엇이 올라갈지 볼 수 있어야 합니다.
        if self.dry_run:
            fake = {"id": 0, "tag_name": tag, "assets": [], "upload_url": "", "html_url": "(dry-run)"}
            self._release_cache[tag] = fake
            log.info("[dry-run] 릴리스 '%s' 를 쓰는 것으로 가정합니다.", tag)
            return fake

        existing = self._request("GET", f"{API}/repos/{self.owner}/{self.repo}/releases/tags/{tag}")
        if existing:
            self._release_cache[tag] = existing
            return existing

        log.info("릴리스 생성: %s", tag)
        created = self._request(
            "POST",
            f"{API}/repos/{self.owner}/{self.repo}/releases",
            json={
                "tag_name": tag,
                "name": name or tag,
                "body": body or _default_body(tag),
                "draft": self.draft,
                "prerelease": self.prerelease,
            },
        )
        self._release_cache[tag] = created
        return created

    def list_assets(self, release: dict[str, Any], *, refresh: bool = False) -> list[dict[str, Any]]:
        """릴리스의 에셋 목록. 한 번 받아 캐시합니다.

        파일 하나당 한 번씩 조회하면 수백 개를 올릴 때 API 호출이 두 배가
        됩니다. 업로드/삭제할 때 캐시를 직접 갱신해 일관성을 유지합니다.
        """
        rid = release.get("id")
        if not rid or self.dry_run:
            return []
        if not refresh and rid in self._asset_cache:
            return self._asset_cache[rid]

        assets: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._request(
                "GET",
                f"{API}/repos/{self.owner}/{self.repo}/releases/{rid}/assets",
                params={"per_page": 100, "page": page},
            )
            if not batch:
                break
            if not isinstance(batch, list):
                # 에셋 목록은 반드시 배열입니다. 아니면 API 응답이 예상과
                # 다른 것이므로, 문자열을 에셋으로 오인해 망가지기 전에 멈춥니다.
                log.warning("에셋 목록 응답이 배열이 아닙니다(%s). 목록을 비워 둡니다.", type(batch).__name__)
                assets = []
                break
            assets.extend(a for a in batch if isinstance(a, dict))
            if len(batch) < 100:
                break
            page += 1
            if page > 50:  # 5000개면 뭔가 잘못된 것입니다
                log.warning("에셋이 5000개를 넘습니다. 목록 조회를 멈춥니다.")
                break

        self._asset_cache[rid] = assets
        return assets

    # -- 읽기 전용 -----------------------------------------------------
    def get_release(self, tag: str) -> dict[str, Any] | None:
        """릴리스를 조회만 합니다. 없으면 None — 만들지 않습니다.

        정리(prune)는 사슬을 확인하러 가는 길에 릴리스를 만들면 안 됩니다.
        빈 릴리스가 생기면 '있긴 한데 에셋이 없다' 로 보여 원인을 흐립니다.
        dry-run 은 네트워크를 건드리지 않으므로 항상 None 입니다.
        """
        if self.dry_run:
            return None
        if tag in self._release_cache:
            return self._release_cache[tag]
        found = self._request("GET", f"{API}/repos/{self.owner}/{self.repo}/releases/tags/{tag}")
        if not found or not isinstance(found, dict):
            return None
        self._release_cache[tag] = found
        return found

    def find_asset(self, tag: str, name: str) -> dict[str, Any] | None:
        """tag 릴리스에서 이름이 name 인 에셋. 릴리스나 에셋이 없으면 None.

        목록은 캐시를 씁니다. 캐시에 없으면 한 번 새로 받아 봅니다 —
        같은 실행 안에서 다른 PC 가 올린 것을 못 보는 일을 줄입니다.
        """
        release = self.get_release(tag)
        if not release:
            return None
        for refresh in (False, True):
            for asset in self.list_assets(release, refresh=refresh):
                if asset.get("name") == name:
                    return asset
        return None

    def download_asset(self, asset: dict[str, Any]) -> bytes:
        """에셋 바이트를 내려받습니다.

        Accept: application/octet-stream 으로 요청하면 GitHub 가 저장소
        주소로 리다이렉트합니다(requests 가 따라가며, 호스트가 바뀌면
        Authorization 헤더는 떼어 냅니다). 없으면(404) ReleaseError —
        None 을 돌려주면 호출부가 '빈 매니페스트' 로 오인할 수 있습니다.
        """
        asset_id = asset.get("id") if isinstance(asset, dict) else None
        if not asset_id:
            raise ReleaseError(f"에셋 id 가 없습니다: {asset!r}")
        url = f"{API}/repos/{self.owner}/{self.repo}/releases/assets/{asset_id}"
        data = self._request(
            "GET", url, raw=True,
            headers={"Accept": "application/octet-stream"},
            allow_redirects=True, timeout=300,
        )
        if data is None:
            raise ReleaseError(f"에셋을 찾지 못했습니다(404): {asset.get('name', asset_id)}")
        return data

    def _remember_asset(self, release: dict[str, Any], asset: dict[str, Any]) -> None:
        rid = release.get("id")
        if rid in self._asset_cache:
            self._asset_cache[rid].append(asset)

    def _forget_asset(self, release: dict[str, Any], name: str) -> None:
        rid = release.get("id")
        if rid in self._asset_cache:
            self._asset_cache[rid] = [
                a for a in self._asset_cache[rid] if a.get("name") != name
            ]

    def delete_asset(self, asset_id: int) -> None:
        if self.dry_run:
            return
        self._request("DELETE", f"{API}/repos/{self.owner}/{self.repo}/releases/assets/{asset_id}")

    # -- 업로드 -------------------------------------------------------
    def upload_one(self, release: dict[str, Any], path: Path, *,
                   name: str | None = None, label: str | None = None) -> dict[str, Any]:
        """파일 하나를 에셋으로 올립니다. 같은 이름이 있으면 지우고 다시 올립니다."""
        path = Path(path)
        target = name or asset_name(path.name)

        if self.dry_run:
            log.info("[dry-run] 업로드 생략: %s -> %s (%s)", path.name, target, _human(path.stat().st_size))
            return {"name": target, "browser_download_url": "(dry-run)"}

        for asset in self.list_assets(release):
            if asset.get("name") == target:
                log.info("같은 이름의 에셋을 교체합니다: %s", target)
                self.delete_asset(asset["id"])
                self._forget_asset(release, target)
                break

        upload_url = str(release.get("upload_url", "")).split("{")[0]
        if not upload_url:
            raise ReleaseError("릴리스에 upload_url 이 없습니다.")

        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = path.stat().st_size
        log.info("업로드 시작: %s (%s)", target, _human(size))

        params = {"name": target}
        if label:
            params["label"] = label

        result = self._request(
            "POST",
            upload_url,
            params=params,
            # 재시도마다 파일을 다시 엽니다. 한 번만 열어서 넘기면
            # 재시도 때 0바이트가 올라갑니다.
            data_factory=lambda: path.open("rb"),
            headers={"Content-Type": ctype, "Content-Length": str(size)},
            timeout=None,
        )

        # GitHub 가 돌려준 크기를 로컬 파일과 대조합니다.
        # 이게 없으면 0바이트 에셋이 성공으로 보고돼도 알 수 없습니다.
        reported = result.get("size") if isinstance(result, dict) else None
        if isinstance(reported, int) and reported != size:
            raise ReleaseError(
                f"업로드된 크기가 다릅니다: {target} — "
                f"보낸 {size}바이트 / 저장된 {reported}바이트. "
                "업로드가 중간에 끊겼을 수 있으니 다시 시도하세요."
            )

        if isinstance(result, dict):
            self._remember_asset(release, result)
        log.info("업로드 완료: %s", target)
        return result

    def upload(self, path: Path, tag: str, *, prefix: str = "",
               keep_parts: bool = False, tmp_dir: Path | None = None,
               rel_path: str | None = None) -> dict[str, Any]:
        """파일 하나를 릴리스에 올립니다. 2GiB 를 넘으면 자동 분할합니다.

        반환: {"tag", "assets": [...], "split": bool, "sha256": str}
        """
        path = Path(path)
        if not path.exists():
            raise ReleaseError(f"올릴 파일이 없습니다: {path}")

        size = path.stat().st_size
        digest = sha256_file(path)
        release = self.get_or_create_release(tag)
        # 이름이 같은 파일이 과목마다 있습니다(강의목록.md 등). 상대 경로로
        # 구분하지 않으면 서로 덮어씁니다.
        base = asset_name(path.name, prefix=prefix, unique_key=rel_path or path.name)

        if size <= self.chunk_bytes:
            # label 에는 어느 과목의 무엇인지 보이도록 상대 경로를 넣습니다.
            # 에셋 이름은 해시라서 label 이 유일한 단서입니다.
            asset = self.upload_one(release, path, name=base, label=rel_path or path.name)
            return {"tag": tag, "assets": [asset], "split": False, "sha256": digest}

        # --- 분할 ---
        log.warning(
            "%s 가 %s 로 상한(%s)을 넘습니다. 분할 업로드합니다.",
            path.name, _human(size), _human(self.chunk_bytes),
        )
        work = Path(tmp_dir or path.parent) / f".parts-{path.stem}"
        parts = split_file(path, work, self.chunk_bytes)
        # 조각마다 해시를 남깁니다. 원본 해시만으로는 '같은 크기의 다른 조각' 을
        # 가려낼 수 없습니다(홈 정리 검토에서 재현).
        part_info = [{"name": f"{base}.part{p.index:03d}", "size": p.path.stat().st_size,
                      "sha256": sha256_file(p.path)} for p in parts]

        manifest = {
            "original_name": path.name,
            "original_size": size,
            "sha256": digest,
            "part_count": len(parts),
            "part_prefix": base,
            "parts": part_info,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "restore": {
                "windows": f'copy /b "{base}.part*" "{path.name}"',
                "linux_macos": f'cat {base}.part* > "{path.name}"',
            },
        }
        manifest_path = work / f"{path.name}.parts.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (work / "restore.sh").write_text(RESTORE_SH, encoding="utf-8", newline="\n")
        (work / "restore.bat").write_text(RESTORE_BAT, encoding="utf-8", newline="\r\n")

        uploaded = []
        try:
            for part in parts:
                uploaded.append(
                    self.upload_one(
                        release, part.path,
                        name=f"{base}.part{part.index:03d}",
                        label=f"{path.name} ({part.index}/{part.total})",
                    )
                )
            uploaded.append(
                self.upload_one(release, manifest_path, name=f"{base}.parts.json",
                                label=f"{path.name} 복원 정보")
            )
        finally:
            if not keep_parts:
                for part in parts:
                    part.path.unlink(missing_ok=True)
                manifest_path.unlink(missing_ok=True)
                (work / "restore.sh").unlink(missing_ok=True)
                (work / "restore.bat").unlink(missing_ok=True)
                try:
                    work.rmdir()
                except OSError:
                    pass

        return {"tag": tag, "assets": uploaded, "split": True, "sha256": digest,
                "parts": part_info}


# ---------------------------------------------------------------------------
def _close(obj: Any) -> None:
    """파일 객체면 닫습니다. 그 외에는 아무것도 하지 않습니다."""
    closer = getattr(obj, "close", None)
    if callable(closer):
        try:
            closer()
        except OSError:
            pass


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TiB"


def _default_body(tag: str) -> str:
    return (
        f"`{tag}` 자동 생성 릴리스입니다.\n\n"
        "이 릴리스의 에셋은 자동으로 누적됩니다.\n\n"
        "**2GiB 를 넘는 파일은 `.partNNN` 으로 분할되어 올라갑니다.**\n"
        "같이 올라온 `*.parts.json` 에 원본 이름과 SHA-256 이 들어 있고,\n"
        "복원은 아래와 같이 합니다.\n\n"
        "```bat\ncopy /b \"이름.part*\" \"원본이름.mkv\"\n```\n"
        "```bash\ncat 이름.part* > 원본이름.mkv\n```\n"
    )


def upload_many(
    uploader: ReleaseUploader,
    files: list[Path],
    tag: str,
    *,
    prefix: str = "",
    store=None,
    root: Path | None = None,
    on_done: Callable[[Path, dict], None] | None = None,
    tally: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """여러 파일을 올리되 이미 올린 것(해시 동일)은 건너뜁니다.

    root 를 주면 에셋 이름을 그 기준 상대 경로로 만듭니다. 주지 않으면
    파일 이름만 쓰므로, 과목마다 있는 같은 이름 파일이 서로 덮어씁니다.

    tally 를 주면 왜 안 올라갔는지를 종류별로 세어 담습니다. 돌려주는
    리스트에는 **성공한 것만** 들어가서, 호출부에서는 "전부 중복이라
    0건" 과 "전부 실패해서 0건" 이 똑같이 빈 리스트로 보입니다.
    2026-11-12 실행이 정확히 그 모양이었습니다 — 업로드 0건인데
    종료 코드 0, CI 초록불.
    """
    results = []
    counts = tally if tally is not None else {}
    for k in ("uploaded", "skipped", "failed", "collision", "missing"):
        counts.setdefault(k, 0)
    # 한 번의 실행 안에서 같은 에셋 이름이 두 번 나오면 반드시 버그입니다
    # (두 번째가 첫 번째를 지웁니다). 조용히 넘어가면 사용자는 전부
    # 올라간 줄 압니다 — 2026-11-12 실행에서 16개 중 14개가 이렇게
    # 사라졌고 로그에는 '업로드 완료' 가 16번 찍혔습니다.
    taken: dict[str, Path] = {}
    for path in files:
        path = Path(path)
        if not path.exists():
            log.warning("건너뜀(파일 없음): %s", path)
            counts["missing"] += 1
            continue
        rel = None
        if root is not None:
            try:
                rel = str(Path(path).relative_to(root))
            except ValueError:
                rel = path.name
        target = asset_name(path.name, prefix=prefix, unique_key=rel or path.name)
        if target in taken:
            log.error(
                "에셋 이름이 겹칩니다: %s <- %s / %s. 덮어쓰지 않고 건너뜁니다.",
                target, taken[target], path,
            )
            counts["collision"] += 1
            continue
        taken[target] = path

        digest = sha256_file(path)
        repo_slug = getattr(uploader, "slug", "")
        if store is not None and store.is_uploaded(digest, repo_slug):
            log.info("건너뜀(이미 업로드됨): %s", rel or path.name)
            counts["skipped"] += 1
            continue
        try:
            result = uploader.upload(path, tag, prefix=prefix, rel_path=rel)
        except ReleaseError as exc:
            log.error("업로드 실패: %s -> %s", rel or path.name, exc)
            counts["failed"] += 1
            continue
        if store is not None:
            store.mark_uploaded(
                digest, tag=tag, repo=repo_slug,
                asset_name=result["assets"][0].get("name", "") if result["assets"] else "",
                size=path.stat().st_size,
            )
            store.save()
        if on_done:
            on_done(path, result)
        results.append(result)
        counts["uploaded"] += 1
    return results


def machine_prefix(label: str) -> str:
    """에셋 이름 앞에 붙일 기기 표시.

    같은 과목이 두 강의실 PC 에 배포되면 두 PC 가 같은 레포에 올립니다.
    이름이 같으면 뒤에 올린 쪽이 앞엣것을 지웁니다(upload_one 이 같은
    이름을 교체합니다). 중복 제거 기록은 PC 안에만 있어서 서로를 못
    봅니다. 그래서 이름에 기기를 넣어 애초에 겹치지 않게 합니다.
    """
    norm = unicodedata.normalize("NFC", label or "")
    safe = _ASSET_SAFE.sub("_", norm).strip("_")
    # 한글이 깎이면 '공학관-401호' 와 '과학관-401호' 가 둘 다 '-401' 이
    # 됩니다. 글자가 하나라도 날아갔으면 해시를 붙여 구분합니다.
    if any(ord(ch) > 127 for ch in norm) or not safe:
        digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:6]
        safe = f"{safe}_{digest}".strip("_") if safe else digest
    return f"{safe}_"


def make_archive(root: Path, files: Iterable[Path], dest: Path) -> Path | None:
    """files 를 root 기준 상대 경로 그대로 담은 zip 을 만듭니다.

    **바이트까지 똑같이 나와야 합니다.** 내용이 그대로인데 zip 이
    달라지면 해시가 바뀌어 매번 다시 올라갑니다. 그래서
      - 항목을 경로 순으로 정렬하고
      - 파일을 zf.write() 가 아니라 ZipInfo + writestr 로 넣고
        (zf.write 는 디스크의 수정 시각을 그대로 담습니다)
      - 디렉터리 항목은 넣지 않습니다.
    zip 안의 이름은 한글 그대로 둡니다(UTF-8 플래그). 에셋 이름만
    GitHub 때문에 해시가 되고, 받아서 풀면 원래 이름이 나옵니다.
    """
    items = sorted({Path(f) for f in files}, key=lambda f: str(f.relative_to(root)))
    if not items:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in items:
            info = zipfile.ZipInfo(
                str(Path(f).relative_to(root).as_posix()),
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, Path(f).read_bytes())
    return dest


def group_by_top_folder(root: Path, files: Iterable[Path]) -> dict[str, list[Path]]:
    """root 바로 아래 폴더 이름으로 묶습니다(과목1, 과목2, 쪽지 ...)."""
    groups: dict[str, list[Path]] = {}
    for f in files:
        rel = Path(f).relative_to(root)
        if len(rel.parts) < 2:
            continue          # 학기 폴더 바로 아래 파일은 묶을 데가 없습니다
        groups.setdefault(rel.parts[0], []).append(Path(f))
    return groups


KIND_MAIN = "main"   # 실제 과목
KIND_SUB = "sub"     # 쪽지·계정 등 과목이 아닌 메뉴


def bundle_kind(folder: str) -> str:
    """학기 폴더 바로 아래 폴더가 과목이면 'main', 아니면 'sub'.

    접두어('_')로 추측하지 않고 paths.ECLASS_NON_COURSE_DIRS 목록으로만
    가릅니다. 그래야 이름이 '_' 로 시작하는 과목도 main 으로 남습니다.
    """
    from .paths import ECLASS_NON_COURSE_DIRS

    name = unicodedata.normalize("NFC", str(folder))
    return KIND_SUB if name in ECLASS_NON_COURSE_DIRS else KIND_MAIN


def bundle_filename(folder: str) -> str:
    """폴더 묶음 zip 의 로컬 파일 이름. 종류를 맨 앞에 붙입니다.

    GitHub 에셋 이름에서는 한글이 빠져 해시만 남으므로, 릴리스 화면에서
    과목 묶음과 쪽지·계정 묶음을 가를 수 있도록 `main_` / `sub_` 를
    해시 앞에 남깁니다. 예: `영어2_23` -> `main_2_23_84aadfbc.zip`,
    `_쪽지` -> `sub_56740f8b.zip` (기기 접두어는 그 앞에 붙습니다).
    zip 안의 폴더 이름은 원래대로입니다.
    """
    return f"{bundle_kind(folder)}_{folder}.zip"


def write_index(dest: Path, rows: list[tuple[str, str, int, int]], *, tag: str) -> Path:
    """에셋 이름 -> 폴더 이름 목차.

    GitHub 에셋 이름에서 한글이 빠지기 때문에 `main_56740f8b.zip` 같은
    이름만 남습니다. 릴리스 화면에서 어느 과목인지 알 수 없으므로
    목차를 같이 올립니다. rows = (에셋 이름, 폴더, 파일 수, 바이트).
    과목(main)을 먼저, 쪽지·계정 등(sub)을 뒤에 적습니다.
    """
    lines = [
        f"# {tag} 수집 목차",
        "",
        "에셋 이름에 한글이 들어가지 않아 해시로 올라갑니다.",
        "`main_` 은 과목, `sub_` 는 쪽지·계정 등 과목이 아닌 메뉴입니다.",
        "받아서 풀면 폴더 이름은 원래대로 나옵니다.",
        "",
        "| 구분 | 에셋 | 폴더 | 파일 수 | 크기 |",
        "|---|---|---|---:|---:|",
    ]
    order = {KIND_MAIN: 0, KIND_SUB: 1}
    for name, folder, count, size in sorted(
            rows, key=lambda r: (order[bundle_kind(r[1])], r[1])):
        lines.append(f"| {bundle_kind(folder)} | `{name}` | {folder} | {count} "
                     f"| {_human(size)} |")
    lines.append("")
    lines.append(f"- 만든 시각: {datetime.now().astimezone():%Y-%m-%d %H:%M:%S}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


def upload_filter(
    files: Iterable[Path],
    *,
    allow_extensions: Iterable[str] = (),
    max_file_mb: int = 0,
) -> tuple[list[Path], list[tuple[Path, str]]]:
    """업로드할 것과 뺄 것을 가릅니다. (올릴 목록, [(파일, 이유)])

    설정의 eclass.upload.allow_extensions / max_file_mb 가 여기서
    처음으로 실제 효과를 냅니다. 전에는 수집기가 두 값을 받아 로그만
    찍고 결과를 버려서, 화이트리스트를 적어도 전부 올라갔고 용량
    제한도 지켜지지 않았습니다.

    뺀 파일은 지우지 않습니다. 로컬에는 남고 릴리스에만 안 올라갑니다.
    """
    allow = {str(e).lower().lstrip(".") for e in allow_extensions if str(e).strip()}
    limit = int(max_file_mb) * 1024 * 1024 if max_file_mb else 0

    keep: list[Path] = []
    dropped: list[tuple[Path, str]] = []
    for path in files:
        ext = path.suffix.lower().lstrip(".")
        if allow and ext not in allow:
            dropped.append((path, f"확장자 '{ext or '없음'}' 가 허용 목록에 없음"))
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            dropped.append((path, f"크기를 읽지 못함: {exc}"))
            continue
        if limit and size > limit:
            dropped.append((path, f"{size / 1024 / 1024:.1f}MB > 제한 {max_file_mb}MB"))
            continue
        keep.append(path)
    return keep, dropped


def iter_files(root: Path, exclude_dirs: set[str] | None = None) -> Iterator[Path]:
    exclude_dirs = exclude_dirs or set()
    for p in sorted(Path(root).rglob("*")):
        if p.is_dir():
            continue
        if any(part in exclude_dirs for part in p.parts):
            continue
        if p.name.startswith("."):
            continue
        yield p
