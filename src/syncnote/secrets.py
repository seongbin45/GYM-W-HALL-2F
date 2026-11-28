"""자격증명 저장/조회.

우선순위:
  1) 환경변수   (CI, 임시 실행용)
  2) OS 자격증명 저장소 (Windows 자격 증명 관리자 = DPAPI 암호화)
  3) 없음 -> 사용자에게 안내

비밀번호를 config.toml 이나 .bat 파일에 적지 마세요.
.bat 파일은 평문이고, 백업/동기화 폴더를 타고 그대로 퍼집니다.
"""

from __future__ import annotations

import getpass
import os
from dataclasses import dataclass

SERVICE = "sync-note"


@dataclass(frozen=True)
class Slot:
    """저장소의 한 칸."""

    key: str          # keyring username
    env: str          # 대체 환경변수 이름
    label: str        # 사용자에게 보여줄 설명


SLOTS: dict[str, Slot] = {
    "eclass": Slot("eclass_password", "SYNCNOTE_ECLASS_PASSWORD", "Eclass 로그인 비밀번호"),
    "github": Slot("github_token", "GITHUB_TOKEN", "GitHub Personal Access Token (repo 권한)"),
    "openai": Slot("openai_api_key", "OPENAI_API_KEY", "OpenAI API 키"),
    "icecast": Slot("icecast_source_password", "SYNCNOTE_ICECAST_PASSWORD",
                    "Icecast 소스 비밀번호 (stream.server = \"icecast\" 일 때만)"),
    # 원격 작업(remote_ops): 소유자가 서명한 계획을 검증하는 HMAC 공유 비밀.
    # 등록돼 있지 않으면 get("remote_ops", required=False) 가 '알 수 없는 항목' 으로
    # 예외를 던져, 폴링마다 트레이스백이 쌓이고 서명 검증이 전혀 안 됐습니다(#103 회귀).
    "remote_ops": Slot("remote_ops_secret", "SYNCNOTE_REMOTE_OPS_SECRET",
                       "원격 작업 서명 검증용 공유 비밀(HMAC)"),
}


class SecretsError(Exception):
    pass


def _keyring():
    try:
        import keyring  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise SecretsError(
            "keyring 패키지가 없습니다.  pip install keyring\n"
            "설치가 어렵다면 환경변수를 대신 쓰세요."
        ) from exc
    return keyring


def get(slot_name: str, required: bool = True) -> str | None:
    """비밀값을 읽습니다. 환경변수가 우선입니다."""
    slot = SLOTS.get(slot_name)
    if slot is None:
        raise SecretsError(f"알 수 없는 항목: {slot_name}")

    value = os.environ.get(slot.env)
    if value:
        return value

    # GITHUB_TOKEN 외에 흔히 쓰는 이름도 봐 줍니다
    if slot_name == "github":
        for alt in ("GH_TOKEN", "GITHUB_PAT"):
            if os.environ.get(alt):
                return os.environ[alt]

    try:
        value = _keyring().get_password(SERVICE, slot.key)
    except SecretsError:
        value = None
    except Exception:
        value = None

    if value:
        return value

    if required:
        raise SecretsError(
            f"{slot.label} 을(를) 찾을 수 없습니다.\n"
            f"  저장:   python -m syncnote secrets set {slot_name}\n"
            f"  또는 환경변수 {slot.env} 를 설정하세요."
        )
    return None


def set_interactive(slot_name: str) -> None:
    """콘솔에서 비밀값을 입력받아 OS 저장소에 넣습니다. 화면에 찍히지 않습니다."""
    slot = SLOTS.get(slot_name)
    if slot is None:
        raise SecretsError(f"알 수 없는 항목: {slot_name}. 가능: {', '.join(SLOTS)}")

    value = getpass.getpass(f"{slot.label} 입력 (입력 내용은 보이지 않습니다): ")
    if not value:
        raise SecretsError("빈 값은 저장하지 않습니다.")
    _keyring().set_password(SERVICE, slot.key, value)
    print(f"저장 완료: {slot_name}")


def delete(slot_name: str) -> None:
    slot = SLOTS.get(slot_name)
    if slot is None:
        raise SecretsError(f"알 수 없는 항목: {slot_name}")
    try:
        _keyring().delete_password(SERVICE, slot.key)
        print(f"삭제 완료: {slot_name}")
    except Exception as exc:
        print(f"삭제 실패(저장된 값이 없을 수 있습니다): {exc}")


def status() -> dict[str, str]:
    """각 항목이 어디에서 왔는지 보여줍니다. 값 자체는 절대 반환하지 않습니다."""
    out: dict[str, str] = {}
    for name, slot in SLOTS.items():
        if os.environ.get(slot.env):
            out[name] = f"환경변수 {slot.env}"
            continue
        try:
            found = _keyring().get_password(SERVICE, slot.key)
        except Exception:
            found = None
        out[name] = "OS 자격증명 저장소" if found else "없음"
    return out
