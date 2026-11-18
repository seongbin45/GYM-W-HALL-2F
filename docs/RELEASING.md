# 릴리스 자동화

세 도구를 **충돌 없이 맞물려** 씁니다. 역할이 안 겹칩니다.

| 도구 | 언제 | 하는 일 |
|---|---|---|
| **Release Drafter** (`.github/workflows/release-drafter.yml`) | main 에 PR 머지될 때마다 | 다음 릴리스 변경로그 **초안**을 누적 갱신(발행 안 함) |
| **release 워크플로** (`.github/workflows/release.yml`) | `vX.Y.Z` 태그 푸시 | 초안을 그 태그로 **발행** + 소스 zip·sha256 첨부 |
| **`scripts/release.bat`** | 로컬에서 릴리스 낼 때 | 버전 형식 검사 후 태그 생성·푸시(→ 위 워크플로 작동) |

semantic-release·Release Please·GoReleaser 는 **안 씁니다**: 각각 커밋 메시지 규약(영문 Conventional Commits)·전용 릴리스 PR·컴파일 바이너리를 전제해서, 한국어 커밋·PR 단위 작업·순수 파이썬 소스인 이 저장소와 안 맞습니다.

## 버전 정하기 (semver)
다음 버전은 **머지된 PR 의 라벨**로 자동 결정됩니다(`.github/release-drafter.yml`).

| 라벨 | 올림 |
|---|---|
| `major`, `breaking` | 1.2.3 → 2.0.0 |
| `minor`, `feature`, `enhancement`, `기능` | 1.2.3 → 1.3.0 |
| `patch`, `fix`, `수정`, `chore`, `정리`, `docs`, `문서` (기본) | 1.2.3 → 1.2.4 |
| `skip-changelog` | 변경로그에서 제외 |

Release Drafter 가 만든 **초안의 제목**(예: `v0.3.0`)이 그 자동 계산 결과입니다 — 태그를 그 값으로 달면 됩니다.

## 릴리스 내는 법

### 방법 1 — 로컬 헬퍼(권장)
```bat
scripts\release.bat v0.3.0
```
태그를 만들고 푸시합니다. 나머지(변경로그 발행 + zip 첨부)는 GitHub Actions 가 합니다.

### 방법 2 — 수동
```bash
git tag v0.3.0
git push origin v0.3.0
```

태그가 올라가면 `release` 워크플로가:
1. `release-drafter` 로 누적 초안을 **그 태그의 정식 릴리스로 발행**,
2. `git archive` 로 만든 `gymhall-v0.3.0.zip` 과 `.sha256` 을 그 릴리스에 첨부.

## 확인
- Actions 탭에서 `release-drafter`(머지마다)·`release`(태그마다)가 초록인지.
- Releases 페이지에 초안이 쌓이는지, 태그 후 정식 릴리스로 바뀌며 zip 이 붙는지.
- `gh release view v0.3.0` 로 CLI 확인 가능.

## 필요 권한
워크플로가 쓰는 `GITHUB_TOKEN` 은 Actions 가 자동 주입합니다. 저장소 설정의
**Settings → Actions → General → Workflow permissions** 가 *Read and write* 여야
초안 생성·발행·에셋 업로드가 됩니다(Read-only 면 403).
