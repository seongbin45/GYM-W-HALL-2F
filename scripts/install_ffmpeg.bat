@echo off
REM =====================================================================
REM  ffmpeg 설치.  setup.bat 이 ffmpeg 가 없을 때 부릅니다.
REM    0) 이미 쓸 수 있는 ffmpeg 가 있으면 끝 (PATH·tools·winget·pip, 필요 기능 확인)
REM    1) pip 의 imageio-ffmpeg (ffmpeg 동봉, 관리자 권한·winget 불필요)
REM    2) winget install Gyan.FFmpeg  (동의 질문은 옵션으로 모두 자동 'Y')
REM    3) winget 이 없거나 실패하면 실제 릴리스 목록(GyanD/codexffmpeg)에서
REM       새 버전부터 받아 tools\ffmpeg 에 둠. 해시·필요 기능 확인에 실패하면
REM       이전 릴리스로 롤백 (python -m syncnote.ffmpeg_fetch)
REM    4) 파이썬조차 없으면 gyan.dev zip 을 PowerShell 로 직접 내려받음
REM  winget 은 PATH 를 바꾸지만 지금 열린 창에는 반영되지 않습니다. 그래서
REM  설치 결과는 PATH 가 아니라 winget 이 만든 파일 위치로 확인합니다
REM  (syncnote 도 같은 위치를 찾으므로 창을 다시 열지 않아도 됩니다).
REM =====================================================================
call "%~dp0_common.bat"
set "URL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
set "ZIP=%TEMP%\ffmpeg-syncnote.zip"
set "DEST=%REPO%\tools"
set "WG_LINK=%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"

"%PY%" -m syncnote.ffmpeg_fetch --find
if not errorlevel 1 exit /b 0
REM 9009 = 파이썬 없음 -> 파이썬이 필요한 단계(찾기·pip·릴리스)는 건너뜀
if errorlevel 9009 goto :nopython
"%PY%" -m syncnote.ffmpeg_fetch --pip
if not errorlevel 1 exit /b 0
echo [!] pip 로 설치하지 못했습니다. winget 으로 시도합니다.
goto :winget

:nopython
call :winget_found
if not errorlevel 1 (
    echo 이미 winget 으로 설치돼 있습니다: %WG_FOUND%
    exit /b 0
)

:winget

where winget >nul 2>&1
if errorlevel 1 (
    echo winget 이 없습니다. 릴리스 목록에서 받습니다.
    goto :release
)
echo winget 으로 ffmpeg 를 설치합니다... (약 250MB, 몇 분 걸립니다)
REM --accept-*-agreements : 'Do you agree ...? [Y]' 질문에 모두 Y
REM --source winget       : msstore 소스의 지역 동의 질문이 아예 나오지 않게
REM --disable-interactivity : 그래도 남은 질문이 있으면 기다리지 않고 끝냄
call winget install --id Gyan.FFmpeg -e --source winget --silent --disable-interactivity --accept-source-agreements --accept-package-agreements
call :winget_found
if not errorlevel 1 (
    echo 완료: %WG_FOUND%
    echo   ^(다른 창에서 ffmpeg 명령을 쓰려면 그 창을 새로 여세요^)
    exit /b 0
)
echo [!] winget 설치가 끝나지 않았습니다. 릴리스 목록에서 받습니다.

:release
"%PY%" -m syncnote.ffmpeg_fetch --dest "%DEST%"
if not errorlevel 1 exit /b 0
REM 9009 = 파이썬을 찾지 못함 -> 파이썬 없이 받는 길로
if errorlevel 9009 goto :zip
goto :fail

:zip
echo 파이썬이 없어 PowerShell 로 내려받습니다.
echo ffmpeg 를 내려받습니다... (약 110MB)
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "try { [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%URL%' -OutFile '%ZIP%' -UseBasicParsing } catch { Write-Host $_.Exception.Message; exit 1 }"
if errorlevel 1 goto :fail

echo 압축을 푸는 중...
if not exist "%DEST%" mkdir "%DEST%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Expand-Archive -Path '%ZIP%' -DestinationPath '%DEST%\_ffmpeg_tmp' -Force"

for /d %%D in ("%DEST%\_ffmpeg_tmp\ffmpeg-*") do (
    if exist "%DEST%\ffmpeg" rmdir /s /q "%DEST%\ffmpeg"
    move "%%D" "%DEST%\ffmpeg" >nul
)
rmdir /s /q "%DEST%\_ffmpeg_tmp" 2>nul
del "%ZIP%" 2>nul

if exist "%DEST%\ffmpeg\bin\ffmpeg.exe" (
    echo 완료: %DEST%\ffmpeg\bin\ffmpeg.exe
) else (
    echo [X] 압축 해제 후 ffmpeg.exe 를 찾지 못했습니다.
    exit /b 1
)
exit /b 0

:fail
echo.
echo [X] ffmpeg 를 받지 못했습니다. 직접 설치하세요:
echo     1^) https://github.com/GyanD/codexffmpeg/releases 에서 *-essentials_build.zip 을 받습니다
echo        ^(또는 %URL%^)
echo     2^) 압축을 풀어 bin\ffmpeg.exe 가
echo        %DEST%\ffmpeg\bin\ffmpeg.exe 가 되도록 둡니다
echo     또는 PowerShell 에서: winget install Gyan.FFmpeg
pause & exit /b 1

REM winget 이 만든 ffmpeg.exe 가 있으면 WG_FOUND 에 넣고 errorlevel 0
:winget_found
set "WG_FOUND="
if exist "%WG_LINK%" set "WG_FOUND=%WG_LINK%"
if not defined WG_FOUND for /d %%P in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*") do (
    for /d %%B in ("%%P\ffmpeg-*") do if exist "%%B\bin\ffmpeg.exe" set "WG_FOUND=%%B\bin\ffmpeg.exe"
)
if defined WG_FOUND exit /b 0
exit /b 1
