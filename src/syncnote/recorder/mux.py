"""영상 + 오디오 트랙을 하나의 파일로 합칩니다.

영상과 오디오를 서로 다른 프로세스/스레드가 받으므로 시작 시각이
조금씩 다릅니다(보통 0.1~1.5초). 각자의 시작 타임스탬프를 기록해
두었다가 -itsoffset 으로 보정합니다. 이걸 안 하면 1시간 뒤에도
입모양과 소리가 어긋난 채로 남습니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..logging_setup import get as get_logger
from .ffmpeg_util import FfmpegError, duration_of, find_ffmpeg, run

log = get_logger("recorder.mux")

TRACK_TITLES = {
    "system": "시스템 소리",
    "mic": "마이크",
    "mix": "합친 소리",
    "stereomix": "스테레오 믹스",
}


@dataclass
class MuxInput:
    path: Path
    name: str          # "system" | "mic"
    started_at: float  # time.time() 기준


def _write_muxfail_log(out_path: Path, cmd: list[str], exc: FfmpegError,
                       video: Path | None, audio: list[MuxInput],
                       track_names: list[str]) -> None:
    """합치기 실패 시 원인 캐기용 사이드카(<파일>.muxfail.log). 실패해도 조용히."""
    import datetime as _dt

    sidecar = out_path.with_suffix(out_path.suffix + ".muxfail.log")
    try:
        lines = [
            f"# 합치기 실패 {_dt.datetime.now().isoformat(timespec='seconds')}",
            f"# 결과(목표): {out_path}",
            f"# ffmpeg 종료코드: {exc.returncode}",
            f"# 트랙: {', '.join(track_names) or '없음'}",
            "",
            "## 입력",
        ]
        if video is not None:
            sz = video.stat().st_size if video.exists() else -1
            lines.append(f"  video {video}  ({sz} B, {duration_of(video)} s)")
        for a in audio:
            sz = a.path.stat().st_size if a.path.exists() else -1
            lines.append(f"  {a.name:<8} {a.path}  ({sz} B, {duration_of(a.path)} s)")
        lines += [
            "",
            "## 실행한 명령",
            " ".join(cmd),
            "",
            "## ffmpeg stderr (전체)",
            exc.stderr.rstrip() or "(비어 있음)",
            "",
        ]
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("\n".join(lines), encoding="utf-8")
        log.error("합치기 실패 상세를 남겼습니다: %s", sidecar)
    except Exception as e:   # 진단 로그 쓰기가 본류를 망치면 안 됨
        log.warning("합치기 실패 상세 기록 실패: %s", e)


def mux(
    out_path: Path,
    video: Path | None,
    video_started_at: float,
    audio: list[MuxInput],
    *,
    layout: str = "separate",
    audio_bitrate: str = "128k",
    delete_video: bool = True,
    delete_audio: bool = True,
    meta=None,
    segment_index: int = 1,
    ended_at: float | None = None,
    mic_gain_filter: str = "",
) -> Path:
    """최종 파일을 만듭니다.

    meta(recorder.meta.RecordingMeta)를 주면 파일 안에 태그·챕터를 넣습니다.
    챕터는 이 파일의 시작(가장 먼저 시작한 트랙) 기준 상대 시각, ended_at 까지.

    layout:
      separate - 오디오 트랙을 각각 따로
      mixed    - 하나로 합침
      both     - 각각 + 합친 것
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    audio = [a for a in audio if a.path.exists() and a.path.stat().st_size > 44]
    if video is None and not audio:
        raise RuntimeError("합칠 영상도 오디오도 없습니다.")

    # 가장 먼저 시작한 것을 기준점으로 삼습니다.
    starts = [t for t in ([video_started_at] if video else []) + [a.started_at for a in audio] if t]
    base = min(starts) if starts else 0.0

    cmd = [find_ffmpeg(), "-hide_banner", "-loglevel", "warning", "-y"]
    index = 0
    video_index = None
    audio_indices: list[tuple[int, str]] = []

    if video is not None and Path(video).exists():
        offset = max(0.0, video_started_at - base)
        if offset > 0.01:
            cmd += ["-itsoffset", f"{offset:.3f}"]
        cmd += ["-i", str(video)]
        video_index = index
        index += 1

    for track in audio:
        offset = max(0.0, track.started_at - base)
        if offset > 0.01:
            cmd += ["-itsoffset", f"{offset:.3f}"]
        cmd += ["-i", str(track.path)]
        audio_indices.append((index, track.name))
        index += 1

    # 메타데이터(태그·챕터)는 FFMETADATA 파일을 입력 하나로 붙여 넣습니다.
    meta_file: Path | None = None
    meta_index: int | None = None
    if meta is not None:
        from datetime import datetime

        from .meta import ffmetadata

        end = float(ended_at or 0.0) or base
        tags = meta.tags(datetime.fromtimestamp(base), datetime.fromtimestamp(end), segment_index)
        chapters = meta.chapters(base, end)
        meta_file = out_path.with_name(out_path.name + ".ffmeta.txt")
        meta_file.write_text(ffmetadata(tags, chapters), encoding="utf-8")
        cmd += ["-f", "ffmetadata", "-i", str(meta_file)]
        meta_index = index
        index += 1

    # 스테레오 믹스 트랙은 마이크와 겹쳐 하나의 '마이크' 소리로 냅니다(둘 다 받은 소리가 겹쳐
    # 들리게). 마이크 트랙이 없으면(마이크 입력 없음) 스테레오 믹스가 그 자리를 대신합니다.
    filters: list[str] = []
    streams = [(f"{i}:a", f"[{i}:a]", name) for i, name in audio_indices]
    # 마이크·스테레오 믹스는 자동 증폭(recorder/autogain.py) — 수업 중 실측 -58 ~ -79 dBFS 로 들리지 않았음
    if mic_gain_filter:
        from .autogain import GAIN_TRACKS

        boosted = []
        for spec, label, name in streams:
            if name in GAIN_TRACKS:
                out = f"[g{spec.split(':')[0]}]"
                filters.append(f"{label}{mic_gain_filter}{out}")
                boosted.append((out, out, name))
            else:
                boosted.append((spec, label, name))
        streams = boosted
    names = [n for *_x, n in streams]
    if "stereomix" in names:
        mic = next((s for s in streams if s[2] == "mic"), None)
        smix = next(s for s in streams if s[2] == "stereomix")
        if mic is not None:
            filters.append(f"{mic[1]}{smix[1]}amix=inputs=2:duration=longest:normalize=0[micmix]")
            streams = [s for s in streams if s[2] != "stereomix"]
            streams = [("[micmix]", "[micmix]", "mic") if s[2] == "mic" else s for s in streams]
        else:
            streams = [(s[0], s[1], "mic") if s[2] == "stereomix" else s for s in streams]

    want_mix = layout in {"mixed", "both"} and len(streams) >= 2
    want_separate = layout in {"separate", "both"} or len(streams) < 2

    if want_mix:
        if want_separate and any(s[0] == "[micmix]" for s in streams):
            # 같은 필터 출력을 두 번 쓰려면 갈라야 합니다(별도 트랙 + 합친 소리, layout both).
            # mixed 는 한 번만 쓰므로 가르지 않음 — 안 쓰는 출력이 남으면 ffmpeg 가 거부합니다
            # ('asplit has output 0 unconnected', CI 의 ffmpeg 에서).
            filters.append("[micmix]asplit=2[micmix_a][micmix_b]")
            mix_in = "".join("[micmix_b]" if s[0] == "[micmix]" else s[1] for s in streams)
            streams = [("[micmix_a]", "[micmix_a]", n) if s == "[micmix]" else (s, lb, n)
                       for s, lb, n in streams]
        else:
            mix_in = "".join(lb for _s, lb, _n in streams)
        filters.append(f"{mix_in}amix=inputs={len(streams)}:duration=longest:normalize=0[mixed]")
    if filters:
        cmd += ["-filter_complex", ";".join(filters)]

    if video_index is not None:
        cmd += ["-map", f"{video_index}:v"]

    out_track_names: list[str] = []
    if want_separate:
        for spec, _label, name in streams:
            cmd += ["-map", spec]
            out_track_names.append(name)
    if want_mix:
        cmd += ["-map", "[mixed]"]
        out_track_names.append("mix")

    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", audio_bitrate]

    for position, name in enumerate(out_track_names):
        title = TRACK_TITLES.get(name, name)
        cmd += [f"-metadata:s:a:{position}", f"title={title}",
                f"-metadata:s:a:{position}", "language=kor"]

    if meta_index is not None:
        cmd += ["-map_metadata", str(meta_index), "-map_chapters", str(meta_index)]
        if out_path.suffix.lower() in (".mp4", ".m4a", ".mov"):
            # mp4 는 이 옵션이 없으면 정해진 몇 개 말고는 태그를 버립니다.
            cmd += ["-movflags", "+use_metadata_tags"]

    cmd += ["-fflags", "+genpts", str(out_path)]

    log.info("합치는 중 -> %s (트랙: %s)", out_path.name, ", ".join(out_track_names) or "없음")
    try:
        run(cmd, timeout=3600)
    except FfmpegError as exc:
        # 실패 원인을 다음 번에도 캐낼 수 있게 전체 stderr·명령·입력을 파일로.
        # 앱 로그에는 꼬리 15줄만 남고 그마저 회전돼 사라집니다. 실제로 한 번
        # 일어난 '합치기 실패(code 1) -> 영상만' 의 원인을 끝내 모른 건 전체
        # stderr 가 어디에도 안 남아서였습니다.
        _write_muxfail_log(out_path, cmd, exc, video, audio, out_track_names)
        raise
    finally:
        if meta_file is not None:
            meta_file.unlink(missing_ok=True)

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"합치기 결과 파일이 비어 있습니다: {out_path}")

    log.info("완료: %s (%.1f MB)", out_path.name, out_path.stat().st_size / 1024 / 1024)

    # 영상 원본은 합친 파일에 그대로 복사(-c:v copy)됐으므로 지워도 됩니다.
    # 오디오 WAV 는 전사에 쓰이므로 호출자가 따로 결정합니다.
    doomed: list[Path] = []
    if delete_video and video is not None:
        doomed.append(Path(video))
    if delete_audio:
        doomed.extend(a.path for a in audio)
    for victim in doomed:
        try:
            victim.unlink(missing_ok=True)
        except OSError as exc:
            log.debug("원본 삭제 실패(무시): %s", exc)

    return out_path
