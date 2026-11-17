"""Local 9:16 (1080x1920) Instagram Reels / Shorts MP4 renderer for Pick3."""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from providers import get_recommendations  # noqa: E402
from reels_script import build_reels_script  # noqa: E402

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def find_ffmpeg() -> str:
    """Locate system ffmpeg or bundled imageio-ffmpeg binary."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError(
            "ffmpeg not found. Install ffmpeg or run `pip install imageio-ffmpeg`."
        ) from exc


def get_media_duration(ffmpeg_exe: str, media_path: Path) -> float:
    """Read duration in seconds using ffmpeg stderr output."""
    proc = subprocess.run(
        [ffmpeg_exe, "-i", str(media_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    m = _DURATION_RE.search(proc.stderr or "")
    if not m:
        return 16.0
    h, mins, secs = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return max(1.0, h * 3600 + mins * 60 + secs)


async def synthesize_tts_edge(text: str, out_mp3: Path, voice: str = "ko-KR-SunHiNeural") -> None:
    import edge_tts

    communicate = edge_tts.Communicate(text, voice=voice, rate="+8%")
    await communicate.save(str(out_mp3))


def synthesize_tts_elevenlabs(text: str, out_mp3: Path, api_key: str) -> None:
    import httpx

    voice_id = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
    resp = httpx.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        json={
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    out_mp3.write_bytes(resp.content)


def _format_ass_time(seconds: float) -> str:
    cs = max(0, int(round(seconds * 100)))
    h = cs // 360000
    cs %= 360000
    m = cs // 6000
    cs %= 6000
    s = cs // 100
    c = cs % 100
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def extract_word_timings(audio_path: Path, full_text: str, duration: float) -> list[tuple[str, float, float]]:
    """Use faster-whisper if installed; otherwise fall back to proportional word timing."""
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel("small", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(audio_path), language="ko", word_timestamps=True)
        words: list[tuple[str, float, float]] = []
        for seg in segments:
            for w in getattr(seg, "words", []) or []:
                token = (w.word or "").strip()
                if token:
                    words.append((token, float(w.start), float(w.end)))
        if words:
            return words
    except Exception:
        pass

    tokens = [t for t in full_text.replace("\n", " ").split(" ") if t.strip()]
    if not tokens:
        return [("맛집 추천", 0.0, duration)]
    total_chars = sum(max(1, len(t)) for t in tokens)
    cursor = 0.15
    usable = max(1.0, duration - 0.3)
    timings: list[tuple[str, float, float]] = []
    for tok in tokens:
        span = usable * (max(1, len(tok)) / total_chars)
        timings.append((tok, cursor, cursor + span))
        cursor += span
    return timings


def build_ass_subtitles(
    title: str,
    word_timings: list[tuple[str, float, float]],
    total_duration: float,
    out_ass: Path,
) -> None:
    """Create 1080x1920 ASS subtitle file with top title and CapCut-style word highlight."""
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "WrapStyle: 2\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Title,Malgun Gothic,54,&H00FFFFFF,&H00FFFFFF,&H00141414,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,8,60,60,160,1\n"
        "Style: WordPop,Malgun Gothic,68,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,2,60,60,340,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    events: list[str] = [
        f"Dialogue: 1,0:00:00.00,{_format_ass_time(total_duration)},Title,,0,0,0,,{{\\c&H00356BFF}}{title}{{\\r}}"
    ]

    chunk_size = 4
    for i in range(0, len(word_timings), chunk_size):
        chunk = word_timings[i : i + chunk_size]
        for active_idx, (_, start_s, end_s) in enumerate(chunk):
            rendered_words = []
            for j, (w_text, _, _) in enumerate(chunk):
                if j == active_idx:
                    rendered_words.append(f"{{\\c&H0000D4FF\\fscx115\\fscy115}}{w_text}{{\\r}}")
                else:
                    rendered_words.append(w_text)
            line = " ".join(rendered_words)
            events.append(
                f"Dialogue: 0,{_format_ass_time(start_s)},{_format_ass_time(end_s)},WordPop,,0,0,0,,{line}"
            )

    out_ass.write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def _escape_ffmpeg_filter_path(path: Path) -> str:
    """Escape Windows drive colons and backslashes for ffmpeg -vf ass=... filter."""
    s = path.resolve().as_posix()
    return s.replace(":", "\\:")


def render_reels_mp4(
    ffmpeg_exe: str,
    broll_files: list[Path],
    audio_path: Path,
    ass_path: Path,
    total_duration: float,
    out_mp4: Path,
) -> None:
    """Assemble 3 B-roll clips (or dark fallback) + audio + ASS subtitles into 1080x1920 MP4."""
    ass_escaped = _escape_ffmpeg_filter_path(ass_path)
    cut_dur = total_duration / 3.0

    if len(broll_files) >= 1:
        selected = [broll_files[i % len(broll_files)] for i in range(3)]
        cmd = [ffmpeg_exe, "-y"]
        filter_parts: list[str] = []
        concat_inputs: list[str] = []

        for idx, clip in enumerate(selected):
            if clip.suffix.lower() in _IMAGE_EXTS:
                cmd.extend(["-loop", "1", "-t", f"{cut_dur:.2f}", "-i", str(clip)])
            else:
                cmd.extend(["-stream_loop", "-1", "-t", f"{cut_dur:.2f}", "-i", str(clip)])
            filter_parts.append(
                f"[{idx}:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                f"crop=1080:1920,setsar=1,fps=30,format=yuv420p[v{idx}]"
            )
            concat_inputs.append(f"[v{idx}]")

        audio_idx = len(selected)
        cmd.extend(["-i", str(audio_path)])
        filter_complex = (
            ";".join(filter_parts)
            + f";{''.join(concat_inputs)}concat=n=3:v=1:a=0[vcat];"
            + f"[vcat]ass='{ass_escaped}'[vout]"
        )
        cmd.extend(
            [
                "-filter_complex",
                filter_complex,
                "-map",
                "[vout]",
                "-map",
                f"{audio_idx}:a",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "22",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                str(out_mp4),
            ]
        )
    else:
        # Clean dark fallback background when broll/ has no media yet.
        cmd = [
            ffmpeg_exe,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x12151c:s=1080x1920:r=30:d={total_duration:.2f}",
            "-i",
            str(audio_path),
            "-vf",
            f"ass='{ass_escaped}'",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(out_mp4),
        ]

    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg render failed:\n{proc.stderr[-800:]}")


async def run_pipeline(
    query: str,
    area: str,
    situation: str,
    episode: int,
    provider: str,
    tts_engine: str,
    broll_dir: Path,
    out_dir: Path,
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    broll_dir.mkdir(parents=True, exist_ok=True)

    rec = await get_recommendations(
        query=query,
        situation=situation,  # type: ignore[arg-type]
        area=area,
        provider=provider,  # type: ignore[arg-type]
    )
    script = build_reels_script(rec, episode=episode)

    narration_parts = [
        script["hook_0_2s"],
        *[c["voiceover"] for c in script["cuts"]],
        script["outro_cta_14_18s"],
    ]
    narration_text = " ".join(narration_parts)

    safe_tag = re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", f"ep{episode}_{area or 'near'}_{query}").strip("_")
    audio_path = out_dir / f"{safe_tag}.mp3"
    ass_path = out_dir / f"{safe_tag}.ass"
    txt_path = out_dir / f"{safe_tag}_script.txt"
    mp4_path = out_dir / f"{safe_tag}.mp4"

    txt_path.write_text(script["full_text"], encoding="utf-8")

    if tts_engine == "elevenlabs":
        el_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
        if not el_key:
            raise RuntimeError("ELEVENLABS_API_KEY env var is required for --tts elevenlabs.")
        synthesize_tts_elevenlabs(narration_text, audio_path, el_key)
    else:
        await synthesize_tts_edge(narration_text, audio_path)

    ffmpeg_exe = find_ffmpeg()
    duration = get_media_duration(ffmpeg_exe, audio_path)
    timings = extract_word_timings(audio_path, narration_text, duration)
    build_ass_subtitles(script["title"], timings, duration, ass_path)

    broll_files = [
        p
        for p in sorted(broll_dir.iterdir())
        if p.is_file() and p.suffix.lower() in (_VIDEO_EXTS | _IMAGE_EXTS)
    ]
    random.shuffle(broll_files)

    render_reels_mp4(ffmpeg_exe, broll_files, audio_path, ass_path, duration, mp4_path)
    return {
        "mp4": mp4_path,
        "ass": ass_path,
        "audio": audio_path,
        "script": txt_path,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Pick3 9:16 Reels MP4 Auto Renderer")
    parser.add_argument("--query", default="돼지갈비", help="Menu query (e.g. 돼지갈비)")
    parser.add_argument("--area", default="강남", help="Area/district (e.g. 강남, 성수)")
    parser.add_argument("--situation", default="date", choices=["solo", "date", "group"])
    parser.add_argument("--episode", type=int, default=1, help="Series episode number")
    parser.add_argument("--provider", default="auto", choices=["auto", "kakao", "google", "naver"])
    parser.add_argument("--tts", default="edge", choices=["edge", "elevenlabs"])
    parser.add_argument("--broll-dir", default=str(BASE_DIR / "broll"))
    parser.add_argument("--out-dir", default=str(BASE_DIR / "output"))
    args = parser.parse_args()

    outputs = asyncio.run(
        run_pipeline(
            query=args.query,
            area=args.area,
            situation=args.situation,
            episode=args.episode,
            provider=args.provider,
            tts_engine=args.tts,
            broll_dir=Path(args.broll_dir),
            out_dir=Path(args.out_dir),
        )
    )
    print("DONE!")
    for k, v in outputs.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
