@echo off
REM =====================================================================
REM  릴리스 헬퍼: vX.Y.Z 태그를 만들고 푸시합니다.
REM  푸시되면 GitHub Actions 의 release 워크플로가
REM  변경로그 발행 + 소스 zip 첨부를 합니다.
REM  사용법:  scripts\release.bat v0.3.0
REM =====================================================================
setlocal
call "%~dp0_common.bat"

set "TAG=%~1"
if "%TAG%"=="" (
    echo 사용법: scripts\release.bat vX.Y.Z   ^(예: v0.3.0^)
    endlocal & exit /b 1
)

REM vX.Y.Z 형식만 허용 — 오타로 엉뚱한 태그가 발행되는 걸 막습니다.
echo %TAG%|findstr /r "^v[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*$" >nul
if errorlevel 1 (
    echo [오류] 태그는 vX.Y.Z 형식이어야 합니다. 받은 값: %TAG%
    endlocal & exit /b 1
)

cd /d "%REPO%"

REM 이미 있는 태그면 중단 — 기존 릴리스를 덮어쓰지 않도록.
git rev-parse -q --verify "refs/tags/%TAG%" >nul 2>&1
if not errorlevel 1 (
    echo [오류] 태그 %TAG% 가 이미 있습니다.
    endlocal & exit /b 1
)

echo 태그 %TAG% 를 만들고 origin 에 푸시합니다...
git tag "%TAG%" || ( echo [오류] 태그 생성 실패 & endlocal & exit /b 1 )
git push origin "%TAG%" || ( echo [오류] 푸시 실패 ^(태그는 로컬에 남아있음: git tag -d %TAG%^) & endlocal & exit /b 1 )

echo.
echo 완료. GitHub Actions 의 release 워크플로가 릴리스를 만듭니다.
echo   https://github.com/CLASS-FROM-KUNSAN/gym-w-hall-2f/actions
endlocal & exit /b 0
