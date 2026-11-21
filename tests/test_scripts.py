"""Windows 스크립트 정적 검사.

여기 있는 .bat / .vbs / .ps1 은 리눅스 CI 에서 실행할 수 없습니다.
그래서 '돌려 보고 아는' 대신 '글자로 확인할 수 있는 함정' 만 막습니다.
전부 실제로 깨졌던 것들입니다.
"""

from __future__ import annotations

import codecs
import re
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
BATS = sorted(SCRIPTS.glob("*.bat"))


def read(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8-sig")


# ---------------------------------------------------------------------------
# 1) `r%VAR%` — raw 문자열 접두사 뒤에 따옴표가 없어 SyntaxError 가 납니다.
#    r%REPO%\src 는 파이썬에게 rC:\repo\src 로 전달됩니다.
# ---------------------------------------------------------------------------
def code_lines(text: str) -> str:
    """REM / :: 주석을 뺀 줄만. 함정을 설명하는 주석까지 걸리면 곤란합니다."""
    keep = [ln for ln in text.splitlines()
            if not re.match(r"\s*(REM\b|::)", ln, re.I)]
    return "\n".join(keep)


@pytest.mark.parametrize("path", BATS, ids=lambda p: p.name)
def test_no_raw_prefix_before_a_bat_variable(path: Path):
    text = code_lines(path.read_text(encoding="utf-8"))
    bad = re.findall(r"(?<![A-Za-z0-9_])r%[A-Za-z_][A-Za-z0-9_]*%", text)
    assert not bad, f"{path.name}: r%VAR% 는 파이썬 SyntaxError 입니다 -> {bad}"


def test_the_python_snippet_in_open_data_folder_actually_compiles():
    """실제로 파이썬에 넘어가는 문자열을 뽑아 컴파일해 봅니다."""
    m = re.search(r'"%PY%" -c "([^"]+)"', read("open_data_folder.bat"))
    assert m, "python -c 호출을 찾지 못했습니다"
    compile(m.group(1), "<open_data_folder.bat>", "exec")


# ---------------------------------------------------------------------------
# 2) powershell -Command 안의 %VAR% 는 따옴표로 감싸야 합니다.
#    C:\Users\John Doe\... 가 두 인자로 쪼개집니다.
# ---------------------------------------------------------------------------
PS_PATH_OPTS = ("-OutFile", "-Path", "-DestinationPath", "-LiteralPath", "-FilePath")


@pytest.mark.parametrize("path", BATS, ids=lambda p: p.name)
def test_powershell_path_arguments_are_quoted(path: Path):
    text = code_lines(path.read_text(encoding="utf-8"))
    if "powershell" not in text.lower():
        pytest.skip("powershell 을 쓰지 않습니다")
    bad = []
    for opt in PS_PATH_OPTS:
        # -OutFile %ZIP%  (따옴표 없음) 을 잡습니다.
        bad += re.findall(rf"{opt}\s+(?![\"'])%[A-Za-z_][A-Za-z0-9_]*%\S*", text)
    assert not bad, (
        f"{path.name}: 경로에 공백이 있으면 PowerShell 이 쪼갭니다. "
        f"작은따옴표로 감싸세요 -> {bad}"
    )


# ---------------------------------------------------------------------------
# 3) hidden.vbs 가 기다리지 않으면 작업 스케줄러의 재시작·시간 제한이
#    전부 무용지물이 됩니다.
# ---------------------------------------------------------------------------
def test_hidden_vbs_waits_for_the_child():
    text = (SCRIPTS / "hidden.vbs").read_text(encoding="utf-8")
    runs = re.findall(r"shell\.Run\s*\(?\s*cmd\s*,\s*0\s*,\s*(True|False)", text)
    assert runs, "shell.Run 호출을 찾지 못했습니다"
    assert all(r == "True" for r in runs), (
        "기다리지 않으면 스케줄러가 곧바로 '성공' 으로 끝내고, "
        "-RestartCount / -ExecutionTimeLimit 가 죽은 설정이 됩니다."
    )


def test_hidden_vbs_runs_the_child_exactly_once():
    text = (SCRIPTS / "hidden.vbs").read_text(encoding="utf-8")
    assert text.count("shell.Run") == 1, "두 번 실행하면 작업이 두 벌 돕니다"


def test_hidden_vbs_returns_the_child_exit_code():
    text = (SCRIPTS / "hidden.vbs").read_text(encoding="utf-8")
    assert re.search(r"WScript\.Quit\s+rc", text), (
        "종료 코드를 돌려주지 않으면 스케줄러가 실패를 알아보지 못합니다"
    )


# ---------------------------------------------------------------------------
# 4) 폴더 업로드는 root 를 넘겨야 동명 파일이 안 빠집니다.
# ---------------------------------------------------------------------------
def test_manual_upload_passes_a_root_so_same_names_survive():
    src = (Path(__file__).resolve().parents[1]
           / "src/syncnote/cli.py").read_text(encoding="utf-8")
    body = src[src.index("def cmd_upload"):src.index("def cmd_config_check")]
    assert "root=" in body, (
        "root 가 없으면 에셋 이름이 파일 이름만으로 정해져 "
        "courseA/자료.md 와 courseB/자료.md 가 충돌합니다"
    )


# ---------------------------------------------------------------------------
# 5) 공통: 모든 .bat 이 _common.bat 을 부르는지(PYTHONPATH/인코딩)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", BATS, ids=lambda p: p.name)
def test_every_bat_sets_up_the_common_environment(path: Path):
    if path.name == "_common.bat":
        pytest.skip("공통 파일 자신")
    text = path.read_text(encoding="utf-8")
    assert "_common.bat" in text, f"{path.name}: _common.bat 을 부르지 않습니다"


# --- 문서 링크 -----------------------------------------------------
# 마크다운 링크는 어떤 테스트도 안 잡는 부류입니다. 파일 이름을 바꾸거나
# 문서를 옮기면 README 의 링크가 조용히 죽습니다.
def test_every_markdown_link_to_a_repo_file_resolves():
    import re

    root = Path(__file__).resolve().parents[1]
    bad = []
    for md in sorted(root.glob("*.md")) + sorted((root / "docs").glob("*.md")):
        text = md.read_text(encoding="utf-8")
        for target in re.findall(r"\]\((?!https?:|#)([^)#\s]+)", text):
            if (md.parent / target).resolve().exists():
                continue
            if (root / target).exists():
                continue
            bad.append(f"{md.relative_to(root)} -> {target}")
    assert not bad, "깨진 문서 링크:\n  " + "\n  ".join(bad)


# ---------------------------------------------------------------------------
# 한글이 든 .ps1 은 UTF-8 BOM 이 있어야 합니다.
#   Windows PowerShell 5.1(강의실 PC 의 'powershell -File') 은 BOM 없는 파일을
#   cp949 로 읽습니다. register_tasks.ps1 이 162줄에서 '문자열 끝이 없습니다'
#   로 멈춰 예약 작업을 아예 못 만들었습니다(재검토에서 재현). CI 의 pwsh(7)
#   는 UTF-8 로 읽어서 통과했습니다.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", sorted(SCRIPTS.glob("*.ps1")), ids=lambda p: p.name)
def test_non_ascii_powershell_scripts_have_a_utf8_bom(path: Path):
    data = path.read_bytes()
    if all(b < 128 for b in data):
        return
    assert data.startswith(codecs.BOM_UTF8), f"{path.name}: 한글이 있는데 UTF-8 BOM 이 없습니다"


# ---------------------------------------------------------------------------
# 6) 줄바꿈이 LF 뿐인 .bat — cmd 가 한글(UTF-8) 줄 근처에서 줄을 잘못 잘라
#    'M' 은(는) 내부 또는 외부 명령... 처럼 엉뚱한 조각을 실행합니다.
#    .gitattributes 는 체크아웃 때만 CRLF 로 바꿔 주므로, 새로 만든 파일은
#    작업 폴더에서 LF 인 채로 실행될 수 있습니다(timetable_edit.bat 에서 실제로 남).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", BATS, ids=lambda p: p.name)
def test_bat_uses_crlf_only(path: Path):
    raw = path.read_bytes()
    lone_lf = raw.replace(b"\r\n", b"").count(b"\n")
    assert lone_lf == 0, f"{path.name}: CRLF 가 아닌 줄 {lone_lf}개 — cmd 가 줄을 잘못 읽습니다"


# ---------------------------------------------------------------------------
# 7) 괄호 블록 안 echo 의 맨 ')' — 블록을 거기서 닫아 버려 cmd 가
#    '... was unexpected at this time.' 으로 멈춥니다. install_ffmpeg.bat 의
#    내려받기 실패 안내('1) ... 2) ...')가 이래서 한 번도 보이지 않았습니다(이슈 #59).
#    들여쓴 echo 는 블록 안이라고 보고 ^) 로 쓰게 합니다.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", BATS, ids=lambda p: p.name)
def test_no_unescaped_close_paren_in_block_echo(path: Path):
    bad = [f"{n}: {ln.strip()}" for n, ln in enumerate(code_lines(read(path.name)).splitlines(), 1)
           if re.match(r"\s+echo\b", ln, re.I) and re.search(r"(?<!\^)\)", ln)]
    assert not bad, f"{path.name}: 블록 안 echo 의 ')' 는 ^) 로 —\n" + "\n".join(bad)


# ---------------------------------------------------------------------------
# 8) ffmpeg 는 winget 으로 먼저 설치하고, 동의 질문에서 멈추지 않아야 합니다(이슈 #59).
# ---------------------------------------------------------------------------
def test_install_ffmpeg_uses_winget_without_prompts_then_falls_back_to_zip():
    text = code_lines(read("install_ffmpeg.bat"))
    line = next(ln for ln in text.splitlines() if "winget install" in ln)
    assert line.strip().lower().startswith("call ")        # .bat 별칭이어도 돌아오게
    for opt in ("--id Gyan.FFmpeg", "-e", "--source winget", "--accept-source-agreements",
                "--accept-package-agreements", "--disable-interactivity"):
        assert opt in line, opt
    # 결과는 PATH(이 창엔 반영 안 됨)가 아니라 winget 이 만든 파일로 확인
    assert r"WinGet\Links\ffmpeg.exe" in text and "Gyan.FFmpeg*" in text
    # 찾기 -> pip -> winget -> 릴리스 목록(롤백), 파이썬조차 없을 때만 PowerShell zip
    # 파이썬이 있으면 --auto 가 전부(찾기·zip·pip·winget·릴리스) 맡고, 없을 때만 bat 의 길
    assert text.index("ffmpeg_fetch --auto") < text.index(":nopython") < text.index(
        "winget install")
    assert (text.index("winget install") < text.index("syncnote.ffmpeg_fetch --dest")
            < text.index("Invoke-WebRequest"))
    assert "errorlevel 9009" in text
    # setup.bat 은 ffmpeg 가 없으면 이 스크립트를 부릅니다(그래서 winget 도 같이 탐)
    assert "install_ffmpeg.bat" in read("setup.bat")
