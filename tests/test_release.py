"""릴리스 업로더.

여기 있는 테스트 대부분은 '조용한 데이터 손실' 을 막기 위한 것입니다.
녹화본이 백업됐다고 믿었는데 0바이트였다는 상황이 가장 나쁩니다.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest

from syncnote.release import (
    GITHUB_ASSET_LIMIT,
    ReleaseError,
    ReleaseUploader,
    asset_name,
    check_token,
    split_file,
    tag_for,
)


# ---------------------------------------------------------------------------
# 가짜 GitHub
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status: int, body: dict | list | None = None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body) if body else "error"
        self.content = b"x"
        self.headers: dict[str, str] = {}

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        return self._body


class FakeSession:
    """요청을 기록하고, 미리 정한 응답을 돌려줍니다."""

    def __init__(self, script=None):
        self.headers: dict[str, str] = {}
        self.calls: list[dict] = []
        self.script = script or []

    def request(self, method, url, **kw):
        data = kw.get("data")
        body = data.read() if hasattr(data, "read") else (data or b"")
        self.calls.append({"method": method, "url": url, "sent": len(body), "kw": kw})
        if self.script:
            handler = self.script.pop(0)
            return handler(len(body))
        # 에셋 목록 조회는 배열을 돌려줍니다(실제 GitHub 와 동일).
        if method == "GET" and "/assets" in url:
            return FakeResponse(200, [])
        if method == "DELETE":
            return FakeResponse(204)
        return FakeResponse(200, {"name": "x", "size": len(body)})


def make_uploader(script=None, **kw) -> tuple[ReleaseUploader, FakeSession]:
    up = ReleaseUploader("owner", "repo", token="t", retry_backoff=0.001, **kw)
    session = FakeSession(script)
    up._session = session
    return up, session


def fake_release(up: ReleaseUploader, tag: str = "tag") -> dict:
    release = {"id": 1, "tag_name": tag, "upload_url": "https://u/assets{?name}"}
    up._release_cache[tag] = release
    up._asset_cache[1] = []
    return release


# ---------------------------------------------------------------------------
# 에셋 이름
# ---------------------------------------------------------------------------
def test_asset_name_keeps_ascii_untouched():
    assert asset_name("lecture_01.mp4") == "lecture_01.mp4"
    assert asset_name("20261111_0900_part001.mkv") == "20261111_0900_part001.mkv"


def test_korean_names_get_unique_hash():
    """GitHub 는 비ASCII 를 버립니다. 해시 없이는 서로 다른 파일이 충돌합니다."""
    a = asset_name("운동과건강 1주차.pptx")
    b = asset_name("생명과학의이해 1주차.pptx")
    assert a != b, "서로 다른 과목 자료가 같은 이름이 됐습니다"
    assert a.endswith(".pptx") and b.endswith(".pptx")
    assert all(ord(c) < 128 for c in a)


def test_asset_name_is_stable():
    """같은 입력은 항상 같은 이름 — 중복 업로드 판단이 흔들리면 안 됩니다."""
    assert asset_name("과제 1.hwp") == asset_name("과제 1.hwp")


def test_asset_name_prefix_and_length():
    assert asset_name("a.bin", prefix="pc1").startswith("pc1_")
    assert len(asset_name("x" * 400 + ".mkv")) <= 200


# ---------------------------------------------------------------------------
# 태그
# ---------------------------------------------------------------------------
def test_tag_for_is_always_year_month_day():
    """릴리스는 전부 일 단위 — 월이 바뀌는 날도, 한 자리 월·일도 년-월-일."""
    assert tag_for("rec", date(2026, 11, 11)) == "rec-2026-11-11"
    assert tag_for("eclass", date(2026, 3, 1)) == "eclass-2026-03-01"
    assert tag_for("rec", date(2026, 11, 30)) != tag_for("rec", date(2026, 11, 29))
    assert tag_for("", date(2026, 12, 31)) == "2026-12-31"
    assert tag_for("home") == f"home-{date.today():%Y-%m-%d}"


# ---------------------------------------------------------------------------
# 분할
# ---------------------------------------------------------------------------
def test_split_restore_roundtrip_is_byte_exact(tmp_path):
    """쪼갠 조각을 순서대로 붙이면 원본과 바이트 단위로 같아야 합니다."""
    data = os.urandom(5_000_000)
    src = tmp_path / "big.mkv"
    src.write_bytes(data)

    parts = split_file(src, tmp_path / "p", 1_500_000)
    assert len(parts) == 4
    joined = b"".join(p.path.read_bytes() for p in sorted(parts, key=lambda x: x.index))
    assert joined == data


def test_split_exact_multiple(tmp_path):
    src = tmp_path / "x.bin"
    src.write_bytes(b"A" * 3000)
    parts = split_file(src, tmp_path / "p", 1000)
    assert len(parts) == 3
    assert all(p.path.stat().st_size == 1000 for p in parts)


def test_chunk_size_never_exceeds_github_limit():
    up, _ = make_uploader(split_size_mb=99999)
    assert up.chunk_bytes < GITHUB_ASSET_LIMIT


# ---------------------------------------------------------------------------
# 업로드: 조용한 데이터 손실 방지
# ---------------------------------------------------------------------------
def test_retry_resends_the_whole_file(tmp_path):
    """회귀 방지: 파일 핸들을 한 번만 넘기면 재시도가 0바이트를 보냅니다.

    GitHub 는 빈 에셋도 받아들이고 200 을 돌려주므로, 사용자는 녹화본이
    백업된 줄 알지만 실제로는 0바이트입니다.
    """
    src = tmp_path / "a.bin"
    src.write_bytes(b"X" * 5000)

    script = [
        lambda n: FakeResponse(500),                        # 첫 시도 실패
        lambda n: FakeResponse(200, {"name": "a.bin", "size": n}),
    ]
    up, session = make_uploader(script, retry_count=2)
    up.upload_one(fake_release(up), src, name="a.bin")

    sent = [c["sent"] for c in session.calls]
    assert sent == [5000, 5000], f"재시도가 전체를 다시 보내지 않았습니다: {sent}"


def test_network_error_retry_resends_whole_file(tmp_path):
    import requests

    src = tmp_path / "a.bin"
    src.write_bytes(b"Y" * 4096)

    class Flaky(FakeSession):
        def request(self, method, url, **kw):
            data = kw.get("data")
            body = data.read() if hasattr(data, "read") else (data or b"")
            self.calls.append({"method": method, "url": url, "sent": len(body), "kw": kw})
            if len(self.calls) == 1:
                raise requests.ConnectionError("끊김")
            return FakeResponse(200, {"name": "a.bin", "size": len(body)})

    up = ReleaseUploader("o", "r", token="t", retry_count=2, retry_backoff=0.001)
    session = Flaky()
    up._session = session
    up.upload_one(fake_release(up), src, name="a.bin")
    assert [c["sent"] for c in session.calls] == [4096, 4096]


def test_size_mismatch_is_an_error(tmp_path):
    """GitHub 가 보고한 크기가 다르면 성공으로 넘기지 않습니다."""
    src = tmp_path / "a.bin"
    src.write_bytes(b"X" * 5000)
    up, _ = make_uploader([lambda n: FakeResponse(200, {"name": "a.bin", "size": 0})])
    with pytest.raises(ReleaseError, match="크기가 다릅니다"):
        up.upload_one(fake_release(up), src, name="a.bin")


def test_asset_list_is_fetched_once(tmp_path):
    """파일마다 에셋 목록을 조회하면 API 호출이 두 배가 됩니다."""
    src = tmp_path / "a.bin"
    src.write_bytes(b"X" * 10)
    up, session = make_uploader()
    release = fake_release(up)
    del up._asset_cache[1]  # 캐시를 비워 실제 조회가 일어나게 합니다

    for i in range(10):
        up.upload_one(release, src, name=f"f{i}.bin")

    gets = [c for c in session.calls if c["method"] == "GET" and "/assets" in c["url"]]
    assert len(gets) == 1, f"에셋 목록을 {len(gets)}번 조회했습니다"


def test_existing_asset_is_replaced(tmp_path):
    """같은 이름이 있으면 지우고 다시 올립니다(GitHub 는 덮어쓰기를 막습니다)."""
    src = tmp_path / "a.bin"
    src.write_bytes(b"X" * 10)
    up, session = make_uploader()
    release = fake_release(up)
    up._asset_cache[1] = [{"id": 77, "name": "a.bin"}]

    up.upload_one(release, src, name="a.bin")
    deletes = [c for c in session.calls if c["method"] == "DELETE"]
    assert len(deletes) == 1 and "77" in deletes[0]["url"]


def test_401_is_not_retried(tmp_path):
    """토큰이 틀렸으면 재시도해도 같습니다. 바로 알려 줘야 합니다."""
    src = tmp_path / "a.bin"
    src.write_bytes(b"X")
    up, session = make_uploader([lambda n: FakeResponse(401)], retry_count=4)
    with pytest.raises(ReleaseError, match="401"):
        up.upload_one(fake_release(up), src, name="a.bin")
    assert len(session.calls) == 1


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------
def test_dry_run_makes_no_network_calls(tmp_path):
    """토큰이 없어도, 저장소가 없어도 무엇이 올라갈지 볼 수 있어야 합니다."""
    src = tmp_path / "a.bin"
    src.write_bytes(b"X" * 100)

    class Exploding:
        headers: dict[str, str] = {}

        def request(self, *a, **kw):
            raise AssertionError("dry-run 이 네트워크를 호출했습니다")

    up = ReleaseUploader("o", "r", token="t", dry_run=True)
    up._session = Exploding()
    result = up.upload(src, "recordings-2026-11")
    assert result["split"] is False


def test_dry_run_splits_and_cleans_up(tmp_path):
    src = tmp_path / "big.mkv"
    src.write_bytes(os.urandom(5_000_000))
    up = ReleaseUploader("o", "r", token="t", split_size_mb=1, dry_run=True)
    result = up.upload(src, "tag", prefix="pc1")
    assert result["split"] is True
    assert len(result["assets"]) == 6          # 조각 5개 + manifest
    assert not list(tmp_path.rglob("*.part*")), "임시 조각이 남았습니다"


def test_upload_missing_file_raises(tmp_path):
    up = ReleaseUploader("o", "r", token="t", dry_run=True)
    with pytest.raises(ReleaseError, match="없습니다"):
        up.upload(tmp_path / "ghost.mkv", "tag")


def test_owner_repo_required():
    with pytest.raises(ReleaseError):
        ReleaseUploader("", "repo")


# ---------------------------------------------------------------------------
# 업로드 필터
#
# eclass.upload.allow_extensions 와 max_file_mb 는 설정 파일에 적혀
# 있고 CLI 가 읽어서 넘기기까지 했지만, 받는 쪽이 결과를 버려서
# **아무 효과가 없었습니다.** 화이트리스트를 적어도 전부 올라갔고,
# 용량 제한을 걸어도 2GB 파일이 그대로 올라갔습니다.
# ---------------------------------------------------------------------------
def _make(tmp_path, name: str, size: int = 10):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p


def test_upload_filter_passes_everything_by_default(tmp_path):
    from syncnote.release import upload_filter

    files = [_make(tmp_path, "a.pdf"), _make(tmp_path, "b.zip")]
    keep, dropped = upload_filter(files)
    assert keep == files
    assert dropped == []


def test_allow_extensions_actually_drops_other_files(tmp_path):
    from syncnote.release import upload_filter

    pdf = _make(tmp_path, "강의안.pdf")
    exe = _make(tmp_path, "setup.exe")
    keep, dropped = upload_filter([pdf, exe], allow_extensions=["pdf", ".PPTX"])
    assert keep == [pdf]
    assert [p for p, _ in dropped] == [exe]
    assert "exe" in dropped[0][1]


def test_max_file_mb_actually_drops_big_files(tmp_path):
    from syncnote.release import upload_filter

    small = _make(tmp_path, "small.pdf", 1024)
    big = _make(tmp_path, "big.pdf", 3 * 1024 * 1024)
    keep, dropped = upload_filter([small, big], max_file_mb=2)
    assert keep == [small]
    assert [p for p, _ in dropped] == [big]
    assert "제한 2MB" in dropped[0][1]


def test_dropped_files_stay_on_disk(tmp_path):
    """뺀 파일은 지우지 않습니다. 로컬 보관이 목적입니다."""
    from syncnote.release import upload_filter

    big = _make(tmp_path, "big.pdf", 2 * 1024 * 1024)
    upload_filter([big], max_file_mb=1)
    assert big.exists()


def test_missing_file_is_dropped_not_raised(tmp_path):
    from syncnote.release import upload_filter

    gone = tmp_path / "없는파일.pdf"
    keep, dropped = upload_filter([gone], max_file_mb=1)
    assert keep == []
    assert len(dropped) == 1


def test_extension_matching_ignores_case_and_dots(tmp_path):
    from syncnote.release import upload_filter

    f = _make(tmp_path, "자료.PDF")
    keep, _ = upload_filter([f], allow_extensions=[".pdf"])
    assert keep == [f]


# ---------------------------------------------------------------------------
# 에셋 이름 충돌
#
# 2026-11-12 실제 GitHub Actions 실행 기록:
#   업로드 시작: 809b1351.md (880 B)      <- 어느 과목의 강의목록.md
#   업로드 완료: 809b1351.md
#   같은 이름의 에셋을 교체합니다: 809b1351.md
#   업로드 시작: 809b1351.md (4.3 KiB)    <- 다른 과목의 강의목록.md
#   ...
# 9개 과목 × 2종 = 16개를 올렸는데 이름이 두 개뿐이라 서로 덮어썼고,
# 릴리스에는 2개만 남았습니다. 로그에는 '업로드 완료' 가 16번 찍혔고
# 작업은 성공으로 끝났습니다.
# ---------------------------------------------------------------------------
REAL_COURSES = [
    "기초웹프로그래밍_05", "생명과학의이해_01", "스키_01", "승마입문_01",
    "영어2_23", "운동과건강_01", "프로그래밍언어및실습_04",
    "[이수필수] 2026년 재학생 법정의무교육(성폭력·가정폭력)",
    "학생생활관 안내 및 안전교육(2)",
]


def _term_tree(tmp_path):
    """실제 실행과 같은 모양의 학기 폴더를 만듭니다."""
    root = tmp_path / "2026-2"
    made = []
    for i, course in enumerate(REAL_COURSES):
        for menu, stem in (("강의목록", "강의목록"), ("강의계획서", "강의계획서")):
            d = root / course / menu
            d.mkdir(parents=True, exist_ok=True)
            f = d / f"{stem}.md"
            f.write_text(f"# {course} {menu}\n" + "x" * (i + 1), encoding="utf-8")
            made.append(f)
    return root, made


def test_same_filename_in_different_courses_got_one_asset_name(tmp_path):
    """고치기 전 동작을 못으로 박아 둡니다 — 파일 이름만 보면 전부 같습니다."""
    from syncnote.release import asset_name

    names = {asset_name("강의목록.md") for _ in REAL_COURSES}
    assert names == {"809b1351.md"}, names


def test_relative_path_makes_every_asset_name_unique(tmp_path):
    from syncnote.release import asset_name

    root, files = _term_tree(tmp_path)
    names = [
        asset_name(f.name, unique_key=str(f.relative_to(root)))
        for f in files
    ]
    assert len(names) == 18, len(names)
    assert len(set(names)) == len(names), "에셋 이름이 겹칩니다"


def test_upload_many_uploads_every_file_under_a_distinct_name(tmp_path):
    """실제 실행을 그대로 재현합니다. 하나도 덮어쓰지 않아야 합니다."""
    from syncnote.release import upload_many

    root, files = _term_tree(tmp_path)
    seen: list[str] = []

    class Spy:
        def upload(self, path, tag, *, prefix="", rel_path=None, **kw):
            import pathlib

            from syncnote.release import asset_name
            name = asset_name(pathlib.Path(path).name, prefix=prefix,
                              unique_key=rel_path or pathlib.Path(path).name)
            seen.append(name)
            return {"tag": tag, "assets": [{"name": name}], "split": False, "sha256": "x"}

    results = upload_many(Spy(), files, "tag", root=root)
    assert len(results) == len(files)
    assert len(set(seen)) == len(seen), f"덮어쓴 이름이 있습니다: {len(seen) - len(set(seen))}건"


def test_upload_many_refuses_to_overwrite_within_one_run(tmp_path, caplog):
    """이름이 겹치면 두 번째를 올리지 않고 기록을 남깁니다.

    root 를 주지 않으면 예전과 같은 상황이 됩니다. 그때도 조용히
    덮어쓰는 대신 멈춰야 합니다.
    """
    import logging

    from syncnote.release import upload_many

    _root, files = _term_tree(tmp_path)

    class Spy:
        def __init__(self):
            self.n = 0

        def upload(self, path, tag, *, prefix="", rel_path=None, **kw):
            self.n += 1
            return {"tag": tag, "assets": [{"name": "x"}], "split": False, "sha256": "x"}

    spy = Spy()
    with caplog.at_level(logging.ERROR):
        upload_many(spy, files, "tag")          # root 없음 = 예전 동작
    assert spy.n == 2, f"겹치는 이름을 {spy.n}번 올렸습니다"
    assert "겹칩니다" in caplog.text


def test_label_carries_the_relative_path(tmp_path):
    """에셋 이름이 해시라서, 어느 과목 파일인지는 label 이 유일한 단서입니다."""
    up, _ = make_uploader()
    rel = "2026-2/영어2_23/강의목록/강의목록.md"
    f = tmp_path / "강의목록.md"
    f.write_text("x", encoding="utf-8")
    seen = {}

    def fake_upload_one(release, path, *, name=None, label=None):
        seen.update(name=name, label=label)
        return {"name": name}

    up.upload_one = fake_upload_one
    up.get_or_create_release = lambda tag: {"id": 1, "upload_url": "u"}
    up.upload(f, "tag", rel_path=rel)
    assert seen["label"] == rel
    assert seen["name"].endswith(".md") and seen["name"] != "강의목록.md"


def test_upload_derives_the_asset_name_from_the_relative_path(tmp_path):
    """upload() 가 rel_path 를 실제로 이름에 쓰는지.

    label 만 보면 이름은 여전히 파일명 해시일 수 있습니다. 그러면
    과목이 달라도 같은 이름이 나와 서로 덮어씁니다.
    """
    up, _ = make_uploader()
    f = tmp_path / "강의목록.md"
    f.write_text("x", encoding="utf-8")
    names = []

    up.upload_one = lambda release, path, *, name=None, label=None: (
        names.append(name) or {"name": name}
    )
    up.get_or_create_release = lambda tag: {"id": 1, "upload_url": "u"}
    for course in ("영어2_23", "운동과건강_01", "스키_01"):
        up.upload(f, "tag", rel_path=f"2026-2/{course}/강의목록/강의목록.md")
    assert len(set(names)) == 3, names


# ---------------------------------------------------------------------------
# 과목 폴더 단위 zip 업로드
#
# 파일 하나씩 올리면 에셋 이름이 전부 해시(809b1351.md)라 릴리스 화면에서
# 어느 과목 것인지 알 수 없고, 개수도 금방 수백 개가 됩니다. 최상위 폴더
# (과목1, 과목2, 쪽지 ...)마다 zip 하나로 묶습니다.
# ---------------------------------------------------------------------------
def _tree(tmp_path):
    root = tmp_path / "2026-2"
    made = []
    plan = {
        "영어2_23": {"공지사항": ["중간고사 안내.md"], "강의자료실": ["1주차 자료.pdf"]},
        "운동과건강_01": {"강의목록": ["강의목록.md"]},
        "_쪽지": {"쪽지": ["28_스키 수업 공지.md"]},
    }
    for course, menus in plan.items():
        for menu, names in menus.items():
            d = root / course / menu
            d.mkdir(parents=True, exist_ok=True)
            for n in names:
                f = d / n
                f.write_text(f"{course}/{menu}/{n}", encoding="utf-8")
                made.append(f)
    return root, made


def test_files_are_grouped_by_their_top_folder(tmp_path):
    from syncnote.release import group_by_top_folder

    root, files = _tree(tmp_path)
    groups = group_by_top_folder(root, files)
    assert set(groups) == {"영어2_23", "운동과건강_01", "_쪽지"}
    assert len(groups["영어2_23"]) == 2


def test_a_file_directly_under_the_term_folder_is_not_grouped(tmp_path):
    from syncnote.release import group_by_top_folder

    root, files = _tree(tmp_path)
    loose = root / "읽어보세요.md"
    loose.write_text("x", encoding="utf-8")
    groups = group_by_top_folder(root, [*files, loose])
    assert all(loose not in v for v in groups.values())


def test_archive_keeps_korean_paths_inside(tmp_path):
    import zipfile

    from syncnote.release import group_by_top_folder, make_archive

    root, files = _tree(tmp_path)
    members = group_by_top_folder(root, files)["영어2_23"]
    z = make_archive(root, members, tmp_path / "out" / "영어2_23.zip")
    names = zipfile.ZipFile(z).namelist()
    assert "영어2_23/공지사항/중간고사 안내.md" in names, names
    assert "영어2_23/강의자료실/1주차 자료.pdf" in names, names


def test_archive_contents_round_trip(tmp_path):
    import zipfile

    from syncnote.release import group_by_top_folder, make_archive

    root, files = _tree(tmp_path)
    members = group_by_top_folder(root, files)["영어2_23"]
    z = make_archive(root, members, tmp_path / "out" / "a.zip")
    with zipfile.ZipFile(z) as zf:
        body = zf.read("영어2_23/공지사항/중간고사 안내.md").decode("utf-8")
    assert body == "영어2_23/공지사항/중간고사 안내.md"


def test_archive_is_byte_identical_for_the_same_input(tmp_path):
    """내용이 그대로인데 zip 이 달라지면 매번 다시 올라갑니다.

    zip 은 기본적으로 수정 시각을 담으므로 그냥 만들면 실행마다
    바이트가 달라집니다. 해시 중복 제거가 통째로 무력화됩니다.
    """
    import os
    import time

    from syncnote.release import group_by_top_folder, make_archive

    root, files = _tree(tmp_path)
    members = group_by_top_folder(root, files)["영어2_23"]
    first = make_archive(root, members, tmp_path / "1.zip").read_bytes()
    # 파일 수정 시각을 바꿔도 내용이 같으면 같은 zip 이어야 합니다.
    later = time.time() + 86400
    for f in members:
        os.utime(f, (later, later))
    second = make_archive(root, list(reversed(members)), tmp_path / "2.zip").read_bytes()
    assert first == second


def test_archive_changes_when_a_file_changes(tmp_path):
    from syncnote.release import group_by_top_folder, make_archive

    root, files = _tree(tmp_path)
    members = group_by_top_folder(root, files)["영어2_23"]
    first = make_archive(root, members, tmp_path / "1.zip").read_bytes()
    members[0].write_text("바뀐 내용", encoding="utf-8")
    second = make_archive(root, members, tmp_path / "2.zip").read_bytes()
    assert first != second


def test_archive_of_nothing_is_none(tmp_path):
    from syncnote.release import make_archive

    assert make_archive(tmp_path, [], tmp_path / "x.zip") is None


def test_each_course_zip_gets_its_own_asset_name(tmp_path):
    from syncnote.release import asset_name

    names = {asset_name(f"{c}.zip", unique_key=f"{c}.zip")
             for c in ("영어2_23", "운동과건강_01", "_쪽지")}
    assert len(names) == 3, names


def test_index_maps_asset_names_back_to_folders(tmp_path):
    """에셋 이름이 해시라 목차 없이는 어느 과목인지 알 수 없습니다."""
    from syncnote.release import write_index

    rows = [("main_2_23_84aadfbc.zip", "영어2_23", 3, 571),
            ("sub_56740f8b.zip", "_쪽지", 1, 221),
            ("main_01_aaaa1111.zip", "운동과건강_01", 2, 100)]
    text = write_index(tmp_path / "INDEX.md", rows, tag="eclass-2026-11").read_text("utf-8")
    assert "eclass-2026-11" in text
    for name, folder, _n, _s in rows:
        assert name in text and folder in text
    # 과목(main) 먼저 폴더 이름 순, 그다음 쪽지·계정(sub).
    assert text.index("영어2_23") < text.index("운동과건강_01") < text.index("_쪽지")
    assert "| sub | `sub_56740f8b.zip` | _쪽지 |" in text


def test_bundle_kind_main_for_courses_sub_for_menus():
    from syncnote.release import bundle_filename, bundle_kind

    assert bundle_kind("영어2_23") == "main"
    assert bundle_kind("_쪽지") == "sub"
    assert bundle_kind("_계정") == "sub"
    assert bundle_filename("영어2_23") == "main_영어2_23.zip"
    assert bundle_filename("_쪽지") == "sub__쪽지.zip"
    # 접두어로 추측하지 않음: '_' 로 시작해도 목록에 없으면 과목.
    assert bundle_kind("_특강") == "main"
    # macOS 등에서 NFD 로 들어와도 같은 판정.
    import unicodedata
    assert bundle_kind(unicodedata.normalize("NFD", "_쪽지")) == "sub"


def test_crawler_writes_term_level_folders_only_from_the_registry():
    """크롤러가 목록 밖 비과목 폴더를 만들면 업로드에서 main 으로 섞입니다."""
    import re
    from pathlib import Path

    import syncnote.eclass.crawler as crawler

    src = Path(crawler.__file__).read_text(encoding="utf-8")
    literals = re.findall(r"(?:menu_dir|course_dir)\(\s*self\.term,\s*(['\"][^'\"]*['\"])", src)
    assert literals == [], f"폴더 이름을 문자열로 직접 씀: {literals} — paths.py 상수를 쓰세요"


def test_bundle_asset_names_carry_main_or_sub_before_the_hash():
    """릴리스 화면에서 과목 zip 과 쪽지·계정 zip 을 이름만으로 가릅니다."""
    import re

    from syncnote.release import asset_name, bundle_filename, machine_prefix

    prefix = machine_prefix("actions")
    def name(folder):
        f = bundle_filename(folder)
        return asset_name(f, prefix=prefix, unique_key=f)

    assert re.fullmatch(r"actions_main_2_23_[0-9a-f]{8}\.zip", name("영어2_23"))
    assert re.fullmatch(r"actions_main_[0-9a-f]{8}\.zip", name("운동과건강"))
    assert re.fullmatch(r"actions_sub_[0-9a-f]{8}\.zip", name("_쪽지"))
    assert re.fullmatch(r"actions_sub_[0-9a-f]{8}\.zip", name("_계정"))
    assert name("_쪽지") != name("_계정")


def test_index_counts_and_sizes_are_shown(tmp_path):
    from syncnote.release import write_index

    text = write_index(tmp_path / "I.md", [("a.zip", "과목가", 7, 2048)],
                       tag="t").read_text("utf-8")
    assert "| 7 |" in text
    assert "KiB" in text


# ---------------------------------------------------------------------------
# 과목 = 레포지터리일 때의 중복 제거
#
# Store 가 digest 만으로 기억하면, A레포에 올린 파일과 내용이 같은 것이
# B레포에서 '이미 올렸다' 로 걸려 조용히 빠집니다. 같은 강의자료가 두
# 과목에 다 올라가는 것이 정상입니다.
# ---------------------------------------------------------------------------
class _RepoSpy:
    def __init__(self, slug):
        self.slug = slug
        self.got: list[str] = []

    def upload(self, path, tag, *, prefix="", rel_path=None, **kw):
        import pathlib

        self.got.append(rel_path or pathlib.Path(path).name)
        return {"tag": tag, "assets": [{"name": "x"}], "split": False, "sha256": "x"}


def test_the_same_file_goes_to_every_repo(tmp_path):
    from syncnote.release import upload_many
    from syncnote.state import Store

    root = tmp_path / "r"
    root.mkdir()
    f = root / "자료.pdf"
    f.write_bytes(b"same bytes")
    store = Store.open(tmp_path / "s.json")

    a, b = _RepoSpy("org/datastructure"), _RepoSpy("org/os")
    upload_many(a, [f], "tag", store=store, root=root)
    upload_many(b, [f], "tag", store=store, root=root)
    assert a.got == ["자료.pdf"]
    assert b.got == ["자료.pdf"], "두 번째 레포에서 조용히 빠졌습니다"


def test_the_same_file_is_still_skipped_within_one_repo(tmp_path):
    from syncnote.release import upload_many
    from syncnote.state import Store

    root = tmp_path / "r"
    root.mkdir()
    f = root / "자료.pdf"
    f.write_bytes(b"same bytes")
    store = Store.open(tmp_path / "s.json")

    a = _RepoSpy("org/datastructure")
    upload_many(a, [f], "tag", store=store, root=root)
    upload_many(a, [f], "tag", store=store, root=root)
    assert a.got == ["자료.pdf"]


def test_the_upload_record_remembers_which_repo(tmp_path):
    from syncnote.state import Store

    store = Store.open(tmp_path / "s.json")
    store.mark_uploaded("d1", tag="t", asset_name="a.zip", repo="org/ds")
    assert store.is_uploaded("d1", "org/ds")
    assert not store.is_uploaded("d1", "org/os")
    entry = next(iter(store.data["uploaded"].values()))
    assert entry["repo"] == "org/ds"


# ---------------------------------------------------------------------------
# 레포 준비 (CloneUp 의 422 처리 참고, auto_init 은 정반대)
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Session:
    def __init__(self, script):
        self.script = script
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return self.script(("GET", url))

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw.get("json")))
        return self.script(("POST", url))


def _up(script):
    up, _ = make_uploader()
    up._session = _Session(script)      # session 은 읽기 전용 프로퍼티입니다
    up.owner, up.repo = "org", "ds-2026-2"
    up.dry_run = False
    return up


def test_access_check_distinguishes_missing_repo_from_no_permission():
    from syncnote.release import ReleaseError

    missing = _up(lambda c: _Resp(404))
    with pytest.raises(ReleaseError, match="레포가 없거나"):
        missing.check_access()

    rejected = _up(lambda c: _Resp(403))
    with pytest.raises(ReleaseError, match="토큰이 거절"):
        rejected.check_access()

    # permissions.push 가 false 여도 막지 않습니다. Actions 의
    # GITHUB_TOKEN 은 올릴 수 있는데도 false 로 오는 경우가 있고,
    # 그걸로 막았더니 업로드가 통째로 멈췄습니다.
    readonly = _up(lambda c: _Resp(200, {"permissions": {"push": False}}))
    readonly.check_access()

    ok = _up(lambda c: _Resp(200, {"permissions": {"push": True}}))
    ok.check_access()


def test_ensure_repo_does_not_create_by_default():
    from syncnote.release import ReleaseError

    up = _up(lambda c: _Resp(404))
    with pytest.raises(ReleaseError, match="레포가 없거나"):
        up.ensure_repo(create_missing=False)
    assert all(m != "POST" for m, *_ in up.session.calls), "만들지 말아야 합니다"


def test_ensure_repo_creates_with_auto_init_when_allowed():
    """CloneUp 은 auto_init 을 금지하지만 우리는 켭니다.

    push 가 아니라 릴리스를 만들기 때문입니다. 커밋이 하나도 없으면
    태그가 가리킬 기본 브랜치가 없습니다.
    """
    state = {"created": False}

    def script(call):
        method, url = call[0], call[1]
        if method == "GET" and url.endswith("/users/org"):
            return _Resp(200, {"type": "Organization"})
        if method == "GET":
            return _Resp(200, {"permissions": {"push": True}}) if state["created"] else _Resp(404)
        state["created"] = True
        return _Resp(201, {"full_name": "org/ds-2026-2"})

    up = _up(script)
    up.ensure_repo(create_missing=True)
    posts = [c for c in up.session.calls if c[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][1].endswith("/orgs/org/repos"), "조직은 /orgs/{org}/repos"
    assert posts[0][2]["auto_init"] is True


def test_a_personal_account_uses_user_repos():
    state = {"created": False}

    def script(call):
        method, url = call[0], call[1]
        if method == "GET" and url.endswith("/users/org"):
            return _Resp(200, {"type": "User"})
        if method == "GET":
            return _Resp(200, {"permissions": {"push": True}}) if state["created"] else _Resp(404)
        state["created"] = True
        return _Resp(201, {"full_name": "org/ds-2026-2"})

    up = _up(script)
    up.ensure_repo(create_missing=True)
    posts = [c for c in up.session.calls if c[0] == "POST"]
    assert posts[0][1].endswith("/user/repos")


def test_name_collision_is_explained_not_a_raw_422():
    from syncnote.release import ReleaseError

    def script(call):
        if call[0] == "GET" and call[1].endswith("/users/org"):
            return _Resp(200, {"type": "User"})
        if call[0] == "GET":
            return _Resp(404)
        return _Resp(422, {"message": "Repository creation failed: name already exists on this account"})

    up = _up(script)
    with pytest.raises(ReleaseError, match="이미 있는데"):
        up.ensure_repo(create_missing=True)


def test_no_permission_to_create_is_explained():
    from syncnote.release import ReleaseError

    def script(call):
        if call[0] == "GET" and call[1].endswith("/users/org"):
            return _Resp(200, {"type": "Organization"})
        if call[0] == "GET":
            return _Resp(404)
        return _Resp(403)

    up = _up(script)
    with pytest.raises(ReleaseError, match="만들 권한이 없습니다"):
        up.ensure_repo(create_missing=True)


def test_a_readable_repo_is_never_recreated():
    """레포가 보이면 만들려 들면 안 됩니다(권한 칸이 뭐라 하든)."""
    up = _up(lambda c: _Resp(200, {"permissions": {"push": False}}))
    up.ensure_repo(create_missing=True)
    assert all(m != "POST" for m, *_ in up.session.calls)


def test_a_false_push_permission_does_not_block_the_upload(caplog):
    """2026-11-12: 이 칸 하나로 46개 올리던 업로드가 통째로 막혔습니다."""
    import logging

    up = _up(lambda c: _Resp(200, {"permissions": {"push": False}}))
    with caplog.at_level(logging.WARNING):
        up.check_access()
    assert "그래도 올려" in caplog.text


def test_a_concurrent_creation_by_another_pc_is_not_an_error():
    """같은 과목이 두 강의실에 배포되면 두 PC 가 같은 레포를 만들려 듭니다.

    늦은 쪽은 422 'already exists' 를 받는데, 그때 쓸 수 있으면
    그대로 쓰면 됩니다. 오류로 올리면 그 PC 만 영영 못 올립니다.
    """
    state = {"n": 0}

    def script(call):
        if call[0] == "GET" and call[1].endswith("/users/org"):
            return _Resp(200, {"type": "User"})
        if call[0] == "GET":
            state["n"] += 1
            # 첫 확인은 없음, 생성 실패 뒤 재확인은 있음(남이 만듦)
            return _Resp(404) if state["n"] == 1 else _Resp(200, {"permissions": {"push": True}})
        return _Resp(422, {"message": "name already exists on this account"})

    up = _up(script)
    up.ensure_repo(create_missing=True)      # 예외가 나면 안 됩니다


def test_a_422_we_cannot_recover_from_is_still_an_error():
    from syncnote.release import ReleaseError

    def script(call):
        if call[0] == "GET" and call[1].endswith("/users/org"):
            return _Resp(200, {"type": "User"})
        if call[0] == "GET":
            return _Resp(404)                # 재확인해도 안 보임
        return _Resp(422, {"message": "name already exists on this account"})

    up = _up(script)
    with pytest.raises(ReleaseError, match="보이지 않습니다"):
        up.ensure_repo(create_missing=True)


# ---------------------------------------------------------------------------
# 읽기 전용: get_release / find_asset / download_asset (홈 정리가 씁니다)
# ---------------------------------------------------------------------------
class _BytesResponse(FakeResponse):
    def __init__(self, status: int, content: bytes):
        super().__init__(status, {})
        self.content = content
        self.text = "error" if status >= 400 else ""


def test_get_release_never_creates():
    up, session = make_uploader([lambda n: FakeResponse(404)])
    assert up.get_release("home-2026-11-13") is None
    assert [c["method"] for c in session.calls] == ["GET"], "조회만 해야 합니다"
    assert session.calls[0]["url"].endswith("/releases/tags/home-2026-11-13")


def test_get_release_returns_and_caches_found_release():
    rel = {"id": 7, "tag_name": "t", "upload_url": "https://u/assets{?name}"}
    up, session = make_uploader([lambda n: FakeResponse(200, rel)])
    assert up.get_release("t") == rel
    assert up.get_release("t") == rel
    assert len(session.calls) == 1


def test_get_release_in_dry_run_touches_no_network():
    up, session = make_uploader(dry_run=True)
    assert up.get_release("t") is None
    assert session.calls == []


def test_find_asset_by_name():
    rel = {"id": 7, "tag_name": "t"}
    assets = [{"id": 1, "name": "a.zip"}, {"id": 2, "name": "pc_home-chain-000000.json"}]
    up, _ = make_uploader([lambda n: FakeResponse(200, rel),
                           lambda n: FakeResponse(200, assets)])
    assert up.find_asset("t", "pc_home-chain-000000.json") == assets[1]


def test_find_asset_missing_release_or_name_is_none():
    up, _ = make_uploader([lambda n: FakeResponse(404)])
    assert up.find_asset("nope", "a.zip") is None

    rel = {"id": 7, "tag_name": "t"}
    up2, session = make_uploader([lambda n: FakeResponse(200, rel),
                                  lambda n: FakeResponse(200, [{"id": 1, "name": "a.zip"}]),
                                  lambda n: FakeResponse(200, [{"id": 1, "name": "a.zip"}])])
    assert up2.find_asset("t", "b.zip") is None
    # 캐시에 없으면 한 번 새로 받아 봅니다.
    assert sum(1 for c in session.calls if "/assets" in c["url"]) == 2


def test_download_asset_returns_raw_bytes_with_octet_stream_accept():
    payload = b'{"kind":"syncnote-home-chain"}\x00\xff'
    up, session = make_uploader([lambda n: _BytesResponse(200, payload)])
    got = up.download_asset({"id": 42, "name": "m.json"})
    assert got == payload
    call = session.calls[0]
    assert call["method"] == "GET"
    assert call["url"].endswith("/releases/assets/42")
    assert call["kw"]["headers"]["Accept"] == "application/octet-stream"
    assert call["kw"].get("allow_redirects") is True


def test_download_asset_empty_body_is_empty_bytes_not_dict():
    """빈 본문을 {} 로 바꾸면 0바이트 에셋이 '정상' 으로 보입니다."""
    up, _ = make_uploader([lambda n: _BytesResponse(200, b"")])
    assert up.download_asset({"id": 1}) == b""


def test_download_asset_404_is_an_error():
    up, _ = make_uploader([lambda n: _BytesResponse(404, b"")])
    with pytest.raises(ReleaseError):
        up.download_asset({"id": 9, "name": "gone.json"})


def test_download_asset_retries_server_errors():
    up, session = make_uploader([lambda n: _BytesResponse(502, b""),
                                 lambda n: _BytesResponse(200, b"ok")], retry_count=2)
    assert up.download_asset({"id": 3}) == b"ok"
    assert len(session.calls) == 2


def test_download_asset_without_id_is_an_error():
    up, session = make_uploader()
    with pytest.raises(ReleaseError):
        up.download_asset({"name": "x"})
    assert session.calls == []


def test_a_disk_full_split_leaves_no_parts_behind(tmp_path, monkeypatch):
    """3차 검토: 쪼개다 실패하면 이미 쓴 조각이 남아 꽉 찬 디스크를 더 채웠습니다."""
    from syncnote import release as rel

    src = tmp_path / "big.zip"
    src.write_bytes(b"x" * 3000)
    work = tmp_path / "w"
    calls = {"n": 0}
    real_open = Path.open

    def flaky(self, mode="r", *a, **k):
        if "wb" in mode and ".part" in self.name:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(28, "No space left on device")
        return real_open(self, mode, *a, **k)

    monkeypatch.setattr(Path, "open", flaky)
    with pytest.raises(OSError):
        rel.split_file(src, work, 1000)
    assert list(work.iterdir()) == []


# ---------------------------------------------------------------------------
# check_token — doctor 의 '토큰 유효성' 점검(존재 != 유효)
# ---------------------------------------------------------------------------
def _patch_requests_get(monkeypatch, resp=None, exc=None):
    import requests

    def fake_get(url, **kw):
        assert url.endswith("/user")
        assert "Bearer" in kw["headers"]["Authorization"]
        if exc is not None:
            raise exc
        return resp

    monkeypatch.setattr(requests, "get", fake_get)


def test_check_token_valid_is_O(monkeypatch):
    r = FakeResponse(200, {"login": "someone"})
    r.headers = {"X-OAuth-Scopes": "repo"}
    _patch_requests_get(monkeypatch, resp=r)
    mark, detail = check_token("ghp_fake")
    assert mark == "O"
    assert "someone" in detail


def test_check_token_401_is_X_and_fails_doctor(monkeypatch):
    # 만료/거절은 doctor 를 빨갛게 — 이게 '모두 정상인데 업로드 전멸' 을 막는 핵심.
    _patch_requests_get(monkeypatch, resp=FakeResponse(401))
    mark, detail = check_token("ghp_expired")
    assert mark == "X"
    assert "갱신" in detail


def test_check_token_403_is_X(monkeypatch):
    _patch_requests_get(monkeypatch, resp=FakeResponse(403))
    assert check_token("ghp_x")[0] == "X"


def test_check_token_network_error_is_warn_not_fail(monkeypatch):
    # 오프라인/타임아웃은 토큰 탓이 아니므로 거짓 실패(X)를 내면 안 됩니다.
    import requests

    _patch_requests_get(monkeypatch, exc=requests.ConnectionError("끊김"))
    assert check_token("ghp_x")[0] == "!"


def test_check_token_missing_is_warn_with_logon_note(monkeypatch):
    # 이 세션에서 안 보여도(= SSH/서비스 세션 차이) 확정 실패로 보지 않습니다.
    monkeypatch.setattr("syncnote.release.secrets.get", lambda *a, **k: None)
    mark, detail = check_token(None)
    assert mark == "!"
    assert "세션" in detail
