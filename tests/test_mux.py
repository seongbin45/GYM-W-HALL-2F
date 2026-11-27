"""영상+오디오 합치기. 실제 ffmpeg 를 돌립니다.

ffmpeg 가 없으면 건너뜁니다.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from syncnote.recorder.ffmpeg_util import duration_of
from syncnote.recorder.mux import MuxInput, mux

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg 가 없습니다"
)


def _probe(path, entries):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", entries,
         "-of", "csv=p=0", str(path)],
        # encoding 을 안 적으면 Windows 가 로캘(한글 PC 는 cp949)로
        # 디코드합니다. ffprobe 출력에 cp949 로 못 읽는 바이트가 섞이면
        # UnicodeDecodeError 로 테스트가 죽습니다 — 제품 버그가 아니라
        # 테스트 버그이고, 리눅스 CI 에서는 영영 안 보입니다.
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=True,
    ).stdout
    return [line for line in out.strip().splitlines() if line]


@pytest.fixture
def media(tmp_path):
    """10초 영상, 10초 440Hz, 9초 880Hz 를 만듭니다."""
    video = tmp_path / "v.mkv"
    system = tmp_path / "s.wav"
    mic = tmp_path / "m.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10", "-t", "10",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(video)],
        check=True,
    )
    for path, freq, dur in ((system, 440, 10), (mic, 880, 9)):
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000",
             "-t", str(dur), "-ac", "2", str(path)],
            check=True,
        )
    return video, system, mic


def test_mux_failure_writes_sidecar_log(media, tmp_path):
    """합치기 실패 시 전체 stderr·명령을 <파일>.muxfail.log 로 남깁니다.

    실제로 한 번 일어난 '합치기 실패(code 1) -> 영상만' 의 원인을 끝내 모른 건
    전체 stderr 가 어디에도 안 남아서였습니다. 그 재발을 진단 가능하게 합니다.
    """
    from syncnote.recorder.ffmpeg_util import FfmpegError

    video, _system, _mic = media
    bad = tmp_path / "bad.wav"          # 44바이트 초과라 mux 가 거르지 않지만, 열 수는 없음
    bad.write_bytes(b"this is not a real wav payload at all" * 4)
    out = tmp_path / "out.mkv"

    with pytest.raises(FfmpegError) as ei:
        mux(out, video, 0.0, [MuxInput(bad, "mic", 0.0)],
            delete_video=False, delete_audio=False)

    # RuntimeError 하위라 기존 except RuntimeError 도 그대로 잡습니다.
    assert isinstance(ei.value, RuntimeError)
    assert ei.value.stderr                       # 전체 stderr 를 들고 있음
    assert ei.value.cmd and ei.value.cmd[0]

    sidecar = out.with_suffix(out.suffix + ".muxfail.log")
    assert sidecar.exists()
    text = sidecar.read_text(encoding="utf-8")
    assert "ffmpeg stderr" in text
    assert "실행한 명령" in text
    assert str(bad.name) in text                 # 입력 목록이 들어감


def test_separate_tracks_are_named(media, tmp_path):
    video, system, mic = media
    out = tmp_path / "out.mkv"
    mux(out, video, 1000.0,
        [MuxInput(system, "system", 1000.0), MuxInput(mic, "mic", 1000.0)],
        layout="separate", delete_video=False, delete_audio=False)

    titles = _probe(out, "stream_tags=title")
    assert "시스템 소리" in titles
    assert "마이크" in titles

    kinds = _probe(out, "stream=codec_type")
    assert kinds.count("audio") == 2
    assert kinds.count("video") == 1


def test_mixed_produces_one_track(media, tmp_path):
    video, system, mic = media
    out = tmp_path / "out.mkv"
    mux(out, video, 1000.0,
        [MuxInput(system, "system", 1000.0), MuxInput(mic, "mic", 1000.0)],
        layout="mixed", delete_video=False, delete_audio=False)
    assert _probe(out, "stream=codec_type").count("audio") == 1


def test_both_produces_three_tracks(media, tmp_path):
    video, system, mic = media
    out = tmp_path / "out.mkv"
    mux(out, video, 1000.0,
        [MuxInput(system, "system", 1000.0), MuxInput(mic, "mic", 1000.0)],
        layout="both", delete_video=False, delete_audio=False)
    assert _probe(out, "stream=codec_type").count("audio") == 3


def test_start_time_offset_is_applied(media, tmp_path):
    """영상과 오디오가 다른 시각에 시작하므로 보정하지 않으면
    1시간 뒤에도 입모양과 소리가 어긋난 채 남습니다."""
    video, system, mic = media
    out = tmp_path / "out.mkv"
    mux(out, video, 1000.0,
        [MuxInput(system, "system", 1000.4), MuxInput(mic, "mic", 1001.2)],
        layout="separate", delete_video=False, delete_audio=False)

    starts = [float(x) for x in _probe(out, "stream=start_time")]
    video_start, system_start, mic_start = starts
    # AAC 인코더 프라이밍으로 수십 ms 오차가 있으므로 여유를 둡니다.
    assert abs(video_start - 0.0) < 0.05
    assert abs(system_start - 0.4) < 0.08, starts
    assert abs(mic_start - 1.2) < 0.08, starts


def test_video_only(media, tmp_path):
    video, _, _ = media
    out = tmp_path / "out.mkv"
    mux(out, video, 1000.0, [], layout="separate",
        delete_video=False, delete_audio=False)
    assert _probe(out, "stream=codec_type") == ["video"]


def test_audio_only(media, tmp_path):
    _, system, _ = media
    out = tmp_path / "out.mkv"
    mux(out, None, 0.0, [MuxInput(system, "system", 1000.0)],
        layout="separate", delete_video=False, delete_audio=False)
    assert _probe(out, "stream=codec_type") == ["audio"]


def test_nothing_to_mux_raises(tmp_path):
    with pytest.raises(RuntimeError):
        mux(tmp_path / "out.mkv", None, 0.0, [])


def test_empty_wav_is_ignored(media, tmp_path):
    """44바이트 헤더만 있는 WAV(= 녹음 실패)는 트랙으로 넣지 않습니다."""
    video, system, _ = media
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"\0" * 44)
    out = tmp_path / "out.mkv"
    mux(out, video, 1000.0,
        [MuxInput(system, "system", 1000.0), MuxInput(empty, "mic", 1000.0)],
        layout="separate", delete_video=False, delete_audio=False)
    assert _probe(out, "stream=codec_type").count("audio") == 1


def test_delete_flags_are_independent(media, tmp_path):
    """회귀 방지: 영상 원본은 지우고 전사용 WAV 는 남겨야 합니다.
    하나의 플래그로 묶으면 작업 폴더에 영상이 계속 쌓입니다."""
    video, system, mic = media
    out = tmp_path / "out.mkv"
    mux(out, video, 1000.0,
        [MuxInput(system, "system", 1000.0), MuxInput(mic, "mic", 1000.0)],
        layout="separate", delete_video=True, delete_audio=False)
    assert not video.exists(), "영상 원본이 남았습니다"
    assert system.exists() and mic.exists(), "전사용 WAV 가 지워졌습니다"


def test_duration_of(media):
    video, _, _ = media
    assert 9.5 < duration_of(video) < 10.5
