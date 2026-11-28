"""원격 git 작업(src/syncnote/remote_ops.py).

사용자 요청: 레포 소유자만 올린, 실행 순서를 담은 서명된 JSON 계획을 받아 정규화한 git 작업만
실행. 실제 git 으로 복구 흐름을 시험하고, 서명·소유자·재생·허용 목록 네 문을 모두 시험합니다.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import types
from pathlib import Path

import pytest

from syncnote import remote_ops as R

SECRET = "pre-shared-secret"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git 이 없습니다")


def signed(**kw):
    kw.setdefault("steps", [{"op": "status"}])
    return R.sign_plan(R.new_plan(**kw), SECRET)


def st(tmp_path, **kw):
    return R.State(tmp_path / "state.json", **kw)


# --- 서명 --------------------------------------------------------------------------

def test_sign_and_verify():
    p = signed()
    assert R.verify_signature(p, SECRET)
    assert not R.verify_signature(p, "other")
    assert not R.verify_signature({**p, "reason": "바뀜"}, SECRET)      # 본문 변조
    assert not R.verify_signature({k: v for k, v in p.items() if k != "sig"}, SECRET)


# --- 검증(네 문) ---------------------------------------------------------------------

def test_valid_plan_passes(tmp_path):
    R.validate(signed(target="all"), machine="PC1", secret=SECRET, state=st(tmp_path), repo=tmp_path)


def test_no_secret_refuses_everything(tmp_path):
    with pytest.raises(R.RemoteOpsError, match="서명 비밀이 없"):
        R.validate(signed(), machine="PC1", secret="", state=st(tmp_path), repo=tmp_path)


def test_bad_signature_refused(tmp_path):
    with pytest.raises(R.RemoteOpsError, match="서명"):
        R.validate({**signed(), "sig": "00"}, machine="PC1", secret=SECRET, state=st(tmp_path), repo=tmp_path)


def test_replay_and_duplicate_refused(tmp_path):
    p = signed(seq=50)
    with pytest.raises(R.RemoteOpsError, match="지난 계획"):
        R.validate(p, machine="PC1", secret=SECRET, state=st(tmp_path, last_seq=50), repo=tmp_path)
    with pytest.raises(R.RemoteOpsError, match="이미 실행"):
        R.validate(p, machine="PC1", secret=SECRET,
                   state=st(tmp_path, last_seq=1, done_ids=[p["id"]]), repo=tmp_path)


def test_expiry_refused(tmp_path):
    p = signed()
    with pytest.raises(R.RemoteOpsError, match="만료"):
        R.validate(p, machine="PC1", secret=SECRET, state=st(tmp_path), repo=tmp_path,
                   now=time.time() + 10 * 86400)


def test_target_must_match(tmp_path):
    with pytest.raises(R.RemoteOpsError, match="대상이 아닙"):
        R.validate(signed(target="HUM-4F-1436"), machine="HUM-2F-1244", secret=SECRET,
                   state=st(tmp_path), repo=tmp_path)
    R.validate(signed(target=["HUM-2F-1244", "HUM-4F-1436"]), machine="HUM-2F-1244", secret=SECRET,
               state=st(tmp_path), repo=tmp_path)


def test_destructive_needs_confirm(tmp_path):
    steps = [{"op": "reset-hard", "ref": "origin/main"}]
    with pytest.raises(R.RemoteOpsError, match="confirm_destructive"):
        R.validate(signed(steps=steps, confirm_destructive=False), machine="PC1", secret=SECRET,
                   state=st(tmp_path), repo=tmp_path)
    R.validate(signed(steps=steps, confirm_destructive=True), machine="PC1", secret=SECRET,
               state=st(tmp_path), repo=tmp_path)


# --- 허용 목록·인자 검증 --------------------------------------------------------------

def test_unknown_op_refused(tmp_path):
    with pytest.raises(R.RemoteOpsError, match="허용하지 않는"):
        R.validate(signed(steps=[{"op": "exec", "cmd": "rm -rf /"}]), machine="PC1", secret=SECRET,
                   state=st(tmp_path), repo=tmp_path)


def test_every_op_has_a_class_and_builds():
    for op in R.ALLOWED_OPS:
        assert op in R.OP_CLASS and R.OP_CLASS[op] in (R.READ, R.SYNC, R.SAFE, R.DESTRUCTIVE)


@pytest.mark.parametrize("step", [
    {"op": "reset-hard", "ref": "--upload-pack=touch x"},   # 옵션 주입
    {"op": "reset-hard", "ref": "a b; rm"},                 # 공백·세미콜론
    {"op": "fetch", "remote": "--exec=x"},
    {"op": "checkout-file", "path": "../../etc/passwd"},     # 레포 밖
    {"op": "checkout-file", "path": "-R"},
    {"op": "branch-create", "name": "-D", "sha": "HEAD"},
])
def test_dangerous_arguments_rejected(step, tmp_path):
    with pytest.raises(R.RemoteOpsError):
        R._build(step["op"], step, tmp_path)


def test_clean_never_removes_ignored_files(tmp_path):
    argv = R._build("clean", {"op": "clean"}, tmp_path)
    assert argv == ["clean", "-f", "-d"] and "-x" not in argv and "-X" not in argv
    assert R._build("clean", {"op": "clean", "dry_run": True}, tmp_path) == ["clean", "-f", "-d", "-n"]


def test_read_ops_are_bounded(tmp_path):
    assert R._build("log", {"op": "log", "count": 99999}, tmp_path)[-1] == "200"      # 상한
    assert R._build("reflog", {"op": "reflog", "count": -5}, tmp_path)[-1] == "1"     # 하한
    assert R._build("reflog", {"op": "reflog", "count": "x"}, tmp_path)[-1] == "30"   # 기본(숫자 아님)


# --- 실행 가둠 ----------------------------------------------------------------------

def test_destructive_skipped_without_allow_or_while_recording(tmp_path):
    plan = {"steps": [{"op": "status"}, {"op": "reset-hard", "ref": "origin/main"}],
            "id": "x", "seq": 1, "stop_on_error": False}
    r = R.execute(plan, tmp_path, allow_destructive=False, recording=False, dry_run=True)
    assert r.steps[1].status == "skipped" and "꺼져" in r.steps[1].note
    r = R.execute(plan, tmp_path, allow_destructive=True, recording=True, dry_run=True)
    assert r.steps[1].status == "skipped" and "녹화 중" in r.steps[1].note


def test_stop_on_error_skips_the_rest(tmp_path):
    plan = {"steps": [{"op": "checkout-file", "path": "-bad"}, {"op": "status"}],
            "id": "x", "seq": 1, "stop_on_error": True}
    r = R.execute(plan, tmp_path, allow_destructive=True, recording=False)
    assert r.steps[0].status == "failed" and r.steps[1].status == "skipped" and not r.ok


# --- 실제 git 복구 ------------------------------------------------------------------

def sh(cwd, *a):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env).stdout


@pytest.fixture
def diverged(tmp_path):
    origin = tmp_path / "o.git"
    sh(tmp_path, "init", "--bare", "-b", "main", str(origin))
    dev = tmp_path / "dev"
    sh(tmp_path, "clone", str(origin), str(dev))
    (dev / "a.py").write_text("v=1\n", encoding="utf-8")
    sh(dev, "add", "-A")
    sh(dev, "commit", "-m", "1")
    sh(dev, "push", "origin", "HEAD:main")
    (dev / "a.py").write_text("v=2\n", encoding="utf-8")
    sh(dev, "add", "-A")
    sh(dev, "commit", "-m", "2")
    sh(dev, "push", "origin", "HEAD:main")
    pc = tmp_path / "pc"
    sh(tmp_path, "clone", str(origin), str(pc))
    sh(pc, "reset", "--hard", "HEAD~1")                 # 뒤처짐
    (pc / "a.py").write_text("LOCAL\n", encoding="utf-8")   # 충돌날 로컬 수정
    (pc / "junk.txt").write_text("x\n", encoding="utf-8")   # untracked
    return pc


@needs_git
def test_full_recovery_resets_to_origin_and_backs_up_local_work(diverged, tmp_path):
    plan = signed(target="all", confirm_destructive=True, steps=[
        {"op": "status"}, {"op": "stash-push", "msg": "backup"},
        {"op": "fetch", "remote": "origin", "branch": "main", "prune": True},
        {"op": "reset-hard", "ref": "origin/main"}])
    R.validate(plan, machine="PC1", secret=SECRET, state=st(tmp_path), repo=diverged)
    res = R.execute(plan, diverged, allow_destructive=True, recording=False)
    assert res.ok and (diverged / "a.py").read_text(encoding="utf-8") == "v=2\n"
    assert "backup" in sh(diverged, "stash", "list")    # 로컬 수정은 잃지 않고 백업됨


@needs_git
def test_pull_ff_fails_without_touching_anything_on_divergence(diverged):
    # 로컬에 독자 커밋을 만들어 fast-forward 불가로
    sh(diverged, "add", "-A")
    sh(diverged, "commit", "-m", "local")
    r = R.execute({"steps": [{"op": "pull-ff", "remote": "origin", "branch": "main"}],
                   "id": "x", "seq": 1}, diverged, allow_destructive=False, recording=False)
    assert r.steps[0].status == "failed"                # --ff-only 라 안전하게 실패
    assert sh(diverged, "log", "--oneline", "-1").split()[1:] == ["local"]   # 그대로


@needs_git
def test_reflog_recovery_ops(diverged):
    sh(diverged, "add", "-A")
    sh(diverged, "commit", "-m", "local")               # 잃을 로컬 커밋을 만들고
    lost = sh(diverged, "rev-parse", "HEAD").strip()
    sh(diverged, "reset", "--hard", "HEAD~1")
    r = R.execute({"steps": [{"op": "reflog", "count": 10},
                             {"op": "branch-create", "name": "recover", "sha": lost}],
                   "id": "x", "seq": 1}, diverged, allow_destructive=False, recording=False)
    assert all(s.status == "ok" for s in r.steps)
    assert lost[:8] in sh(diverged, "rev-parse", "recover").strip()


# --- 상태 ---------------------------------------------------------------------------

def test_state_roundtrip(tmp_path):
    s = R.State(tmp_path / "s.json", last_seq=7, done_ids=["a", "b"])
    s.save()
    again = R.State.load(tmp_path / "s.json")
    assert again.last_seq == 7 and again.done_ids == ["a", "b"]


# --- GitHub 에서 받기(가짜 전송) ------------------------------------------------------

class FakeTransport:
    def __init__(self, issues=None, gists=None, admins=("owner",)):
        self.repo_full = "owner/repo"
        self.issues = issues or []
        self.gists = gists or {}
        self.admins = set(admins)
        self.posted = []
        self.patched = []

    def get_json(self, path):
        if path.endswith("/permission"):
            login = path.split("/collaborators/")[1].split("/")[0]
            return {"permission": "admin" if login in self.admins else "read"}
        if "/issues?" in path:
            return self.issues
        if path.startswith("/gists/"):
            return self.gists.get(path.split("/gists/")[1])
        return None

    def post(self, path, payload):
        self.posted.append((path, payload))

    def patch(self, path, payload):
        self.patched.append((path, payload))


def issue(n, author, body):
    return {"number": n, "user": {"login": author}, "body": body}


def test_inline_plan_from_admin_is_read():
    plan = signed()
    body = "복구 요청\n```json\n" + json.dumps(plan) + "\n```"
    src = R.GitHubSource(FakeTransport(issues=[issue(5, "owner", body)]))
    cands = src.poll()
    assert len(cands) == 1 and cands[0].plan["id"] == plan["id"] and cands[0].author == "owner"


def test_non_admin_issue_is_ignored():
    body = "```json\n" + json.dumps(signed()) + "\n```"
    src = R.GitHubSource(FakeTransport(issues=[issue(5, "randoms", body)], admins=("owner",)))
    assert src.poll() == []


def test_owner_login_pin_rejects_other_admins():
    body = "```json\n" + json.dumps(signed()) + "\n```"
    t = FakeTransport(issues=[issue(5, "maintainer2", body)], admins=("owner", "maintainer2"))
    assert R.GitHubSource(t, owner_login="owner").poll() == []


def test_gist_owner_must_match_poster():
    plan = signed()
    gists = {"abc123def4567890abcd": {"owner": {"login": "attacker"},
             "files": {"p.json": {"filename": "p.json", "content": json.dumps(plan)}}}}
    body = "계획: https://gist.github.com/owner/abc123def4567890abcd"
    src = R.GitHubSource(FakeTransport(issues=[issue(5, "owner", body)], gists=gists))
    assert src.poll() == []                              # gist 소유자(attacker) ≠ 올린 사람(owner)


def test_gist_plan_from_matching_owner():
    plan = signed()
    gists = {"abc123def4567890abcd": {"owner": {"login": "owner"},
             "files": {"p.json": {"filename": "p.json", "content": json.dumps(plan)}}}}
    body = "https://gist.github.com/owner/abc123def4567890abcd"
    src = R.GitHubSource(FakeTransport(issues=[issue(5, "owner", body)], gists=gists))
    cands = src.poll()
    assert len(cands) == 1 and cands[0].source == "gist"


# --- poll_once(끝에서 끝) -------------------------------------------------------------

class Cfg:
    def __init__(self, **d):
        self._d = {"remote_ops.enabled": True, "remote_ops.allow_destructive": True, **d}

    def get(self, k, default=None):
        return self._d.get(k, default)

    machine_label = "PC1"


def layout(tmp_path):
    (tmp_path / "state").mkdir(exist_ok=True)
    return types.SimpleNamespace(state=tmp_path / "state")


def test_poll_once_runs_validates_reports_and_records(tmp_path):
    plan = signed(target="PC1", steps=[{"op": "status"}])
    src = R.GitHubSource(FakeTransport(issues=[issue(9, "owner",
                         "```json\n" + json.dumps(plan) + "\n```")]))
    out = R.poll_once(Cfg(), layout(tmp_path), source=src, secret=SECRET, recording=False)
    assert out.ran == 1 and not out.error
    assert src.t.posted and src.t.patched                 # 결과 댓글 + 닫음(단일 대상)
    state = R.State.load(tmp_path / "state" / "remote_ops.json")
    assert state.last_seq == plan["seq"] and plan["id"] in state.done_ids
    # 다시 돌리면 재생 방지로 실행 안 함
    out2 = R.poll_once(Cfg(), layout(tmp_path), source=src, secret=SECRET, recording=False)
    assert out2.ran == 0 and any("지난 계획" in r or "이미" in r for r in out2.rejected)


def test_poll_once_bad_signature_comments_and_does_not_run(tmp_path):
    bad = {**signed(target="PC1"), "sig": "deadbeef"}
    src = R.GitHubSource(FakeTransport(issues=[issue(9, "owner",
                         "```json\n" + json.dumps(bad) + "\n```")]))
    out = R.poll_once(Cfg(), layout(tmp_path), source=src, secret=SECRET, recording=False)
    assert out.ran == 0 and src.t.posted and "서명" in src.t.posted[0][1]["body"]
    assert not src.t.patched                               # 닫지 않음


def test_poll_once_disabled_or_no_secret(tmp_path):
    src = R.GitHubSource(FakeTransport())
    assert "꺼져" in R.poll_once(Cfg(**{"remote_ops.enabled": False}), layout(tmp_path),
                                source=src, secret=SECRET, recording=False).error


def test_report_text_is_redacted(tmp_path):
    plan = signed(reason="로그인 2601057")
    res = R.RunResult(ran=True, plan_id="id1", seq=1,
                      steps=[R.StepResult("status", R.READ, "ok", 0, r"C:\Users\1121\x 2601057")])
    body = R.report_text(plan, res, "PC1")
    assert "1121" not in body and "2601057" not in body and "status" in body


# --- CLI·작업·문서 -------------------------------------------------------------------

def test_cli_parser_and_bat_and_task():
    from syncnote import cli, tasks

    for argv in (["remote-ops", "poll", "--dry-run"], ["remote-ops", "sign", "p.json"],
                 ["remote-ops", "new", "--target", "PC1"], ["remote-ops", "status"]):
        assert cli.build_parser().parse_args(argv).func is cli.cmd_remote_ops
    root = Path(__file__).resolve().parents[1]
    bat = (root / "scripts" / "remote_ops.bat").read_bytes()
    assert b"-m syncnote remote-ops poll" in bat and bat.isascii()
    ps = (root / "scripts" / "register_tasks.ps1").read_text(encoding="utf-8-sig")
    assert 'Register-One "RemoteOps" (New-HiddenAction "remote_ops.bat"' in ps
    assert "RemoteOps" in tasks.EXPECTED


# ---------------------------------------------------------------------------
# remote_ops 비밀 슬롯(#103 회귀): 등록이 빠져 폴링마다 트레이스백이 쌓였다
# ---------------------------------------------------------------------------
def test_remote_ops_is_a_known_secret_slot():
    from syncnote import secrets

    assert "remote_ops" in secrets.SLOTS


def test_get_remote_ops_optional_returns_none_not_raises(monkeypatch):
    """poll_once 는 secrets.get("remote_ops", required=False) 를 부른다.
    슬롯이 없으면 required=False 여도 '알 수 없는 항목' 으로 터져(폴링마다 트레이스백),
    서명 검증이 전혀 안 됐다. 이제는 조용히 None 이어야 한다."""
    from syncnote import secrets

    monkeypatch.delenv("SYNCNOTE_REMOTE_OPS_SECRET", raising=False)
    monkeypatch.setattr(secrets, "_keyring", lambda: (_ for _ in ()).throw(secrets.SecretsError("no backend")))
    assert secrets.get("remote_ops", required=False) is None
