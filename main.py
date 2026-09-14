from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
import subprocess
import tempfile
import os
import json
import math
import random
import requests
import whisper

app = FastAPI()
model = whisper.load_model("base")

PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")

# Used when no title/queries are supplied, or mixed in for extra variety
# alongside the title-derived query in the legacy random picker.
FALLBACK_QUERIES = [
    "finance", "money success", "stock market", "business office",
    "investment growth", "entrepreneur working", "city skyline business",
    "financial freedom", "office laptop working", "coins money",
]

# Last-resort background if Pexels can't be reached at all (network down,
# bad/missing API key, rate limited). Keeps the service working, just less varied.
STATIC_FALLBACK_VIDEOS = [
    "https://videos.pexels.com/video-files/35379336/14990716_640_360_30fps.mp4",
    "https://videos.pexels.com/video-files/5466780/5466780-sd_640_360_25fps.mp4",
    "https://videos.pexels.com/video-files/3255384/3255384-sd_640_360_25fps.mp4",
]

SEGMENT_DURATION = 45  # seconds of background video per cut
# Default cap on distinct clips per video, to bound render time/API calls.
# Long-form requests can raise it with the `max_segments` form field, up to
# HARD_MAX_SEGMENTS (40 clips x 45s covers a 30-minute video).
MAX_SEGMENTS = int(os.environ.get("MAX_SEGMENTS", "14"))
HARD_MAX_SEGMENTS = 60

# How many distinct clips to fetch per narrative "beat" when background
# footage is matched to an ordered scene list (see build_ordered_background_list).
POOL_SIZE_PER_BEAT = 3

# Caption chunking: short, punchy on-screen groups instead of whisper's
# often-long sentence segments.
MAX_WORDS_PER_CHUNK = 5
MAX_CHUNK_DURATION = 2.6


def _title_to_query(title: str) -> str:
    """Turn a video title into a short, Pexels-friendly search phrase."""
    stopwords = {
        "el", "la", "los", "las", "un", "una", "de", "del", "y", "o", "que",
        "para", "por", "con", "en", "a", "tu", "su", "es", "como", "si", "no",
        "the", "an", "of", "and", "or", "to", "in", "for", "with", "who",
        "how", "his", "her", "was", "is", "that", "this", "they", "he", "she",
    }
    words = [w.strip(".,:;!?¿¡\"'").lower() for w in title.split()]
    keywords = [w for w in words if w and w not in stopwords]
    return " ".join(keywords[:4]) if keywords else ""


def _pexels_search(query, per_page=15):
    """Raw Pexels video search for one query. Returns [] on any failure or
    if PEXELS_API_KEY is not set."""
    if not PEXELS_API_KEY:
        return []
    headers = {"Authorization": PEXELS_API_KEY}
    try:
        resp = requests.get(
            "https://api.pexels.com/videos/search",
            headers=headers,
            params={"query": query, "orientation": "landscape", "size": "medium", "per_page": per_page},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("videos", [])
    except requests.RequestException:
        return []


def _videos_to_links(videos):
    """Pick one reasonably-HD landscape file URL per Pexels video result."""
    links = []
    for video in videos:
        files = [f for f in video.get("video_files", []) if f.get("width")]
        files.sort(key=lambda f: abs(f["width"] - 1280))
        hd_candidates = [f["link"] for f in files if 960 <= f["width"] <= 1920]
        if hd_candidates:
            links.append(hd_candidates[0])
    return links


def fetch_pexels_video_urls(count, title="", custom_queries=None):
    """Legacy random picker: fetch up to `count` distinct landscape video
    URLs from Pexels, mixing the video's own title with either the caller's
    `custom_queries` or the generic finance/business queries, in random
    order. Used when the caller doesn't provide an ordered narrative
    (see build_ordered_background_list for that case). Returns [] if
    PEXELS_API_KEY is not set or every request fails."""
    if not PEXELS_API_KEY:
        return []

    queries = list(custom_queries) if custom_queries else list(FALLBACK_QUERIES)
    title_query = _title_to_query(title)
    if title_query:
        queries = [title_query, title_query] + queries  # weight it higher
    random.shuffle(queries)

    urls, seen = [], set()
    for q in queries:
        if len(urls) >= count:
            break
        videos = _pexels_search(q)
        random.shuffle(videos)
        for link in _videos_to_links(videos):
            if link in seen:
                continue
            seen.add(link)
            urls.append(link)
            if len(urls) >= count:
                break

    random.shuffle(urls)
    return urls[:count]


def build_background_list(num_segments, title="", custom_queries=None):
    """Return exactly `num_segments` background video URLs, avoiding the same
    clip appearing twice in a row when possible. Random order (legacy path)."""
    urls = fetch_pexels_video_urls(num_segments, title=title, custom_queries=custom_queries)

    pool = urls if urls else list(STATIC_FALLBACK_VIDEOS)
    while len(urls) < num_segments:
        candidate = random.choice(pool)
        if urls and candidate == urls[-1] and len(pool) > 1:
            continue
        urls.append(candidate)

    return urls[:num_segments]


def fetch_beat_pool(query):
    """A small pool of distinct clips for one narrative beat, so consecutive
    segments on the same beat don't all show the identical clip."""
    links = _videos_to_links(_pexels_search(query, per_page=12))
    seen, pool = set(), []
    for link in links:
        if link in seen:
            continue
        seen.add(link)
        pool.append(link)
        if len(pool) >= POOL_SIZE_PER_BEAT:
            break
    return pool


def build_ordered_background_list(num_segments, beats):
    """Map background clips to `beats` -- an ordered list of short search
    phrases describing the story chronologically (e.g. ["empty office
    exterior", "financial documents on desk", "police car lights night",
    "courtroom empty benches"]) -- so the footage follows the narrative
    instead of being randomly shuffled like the legacy picker.

    Returns None (caller should fall back to build_background_list) when
    PEXELS_API_KEY is missing or `beats` has fewer than 2 distinct entries,
    since ordering doesn't mean anything with 0-1 categories."""
    beats = [b for b in beats if b]
    if not PEXELS_API_KEY or len(beats) < 2:
        return None

    pools = {}
    urls = [None] * num_segments
    for i in range(num_segments):
        beat_idx = min(int(i * len(beats) / num_segments), len(beats) - 1)
        if beat_idx not in pools:
            pools[beat_idx] = fetch_beat_pool(beats[beat_idx])
        pool = pools[beat_idx]
        if not pool:
            continue
        candidate = pool[i % len(pool)]
        if i > 0 and urls[i - 1] == candidate and len(pool) > 1:
            candidate = pool[(i + 1) % len(pool)]
        urls[i] = candidate

    resolved = [u for u in urls if u]
    fallback_pool = resolved or list(STATIC_FALLBACK_VIDEOS)
    for i, u in enumerate(urls):
        if u is not None:
            continue
        candidate = random.choice(fallback_pool)
        if i > 0 and candidate == urls[i - 1] and len(fallback_pool) > 1:
            alternatives = [c for c in fallback_pool if c != candidate]
            candidate = random.choice(alternatives) if alternatives else candidate
        urls[i] = candidate

    return urls


def _fmt_time(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def generate_ass_subtitles(segments):
    """Generate an ASS subtitle file with word-by-word karaoke-style
    highlighting: short punchy caption chunks (a few words each) where the
    current word lights up as it's spoken, in the style most short/long-form
    narration channels use for retention. Falls back to plain per-segment
    lines if word timestamps aren't available."""
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,52,&H0000D7FF,&H00FFFFFF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,3,6,0,2,20,20,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    words = []
    for seg in segments:
        for w in (seg.get("words") or []):
            text = (w.get("word") or "").strip()
            if not text:
                continue
            words.append({"text": text, "start": w["start"], "end": w["end"]})

    if not words:
        # Fallback: no word-level timestamps, use plain per-segment lines.
        events = ""
        for seg in segments:
            text = seg["text"].strip()
            events += f"Dialogue: 0,{_fmt_time(seg['start'])},{_fmt_time(seg['end'])},Default,,0,0,0,,{text}\n"
        return header + events

    # Group words into short on-screen chunks.
    chunks = []
    current = []
    for w in words:
        if current and (
            len(current) >= MAX_WORDS_PER_CHUNK
            or (w["end"] - current[0]["start"]) > MAX_CHUNK_DURATION
        ):
            chunks.append(current)
            current = []
        current.append(w)
    if current:
        chunks.append(current)

    events = ""
    for chunk in chunks:
        chunk_start = chunk[0]["start"]
        chunk_end = chunk[-1]["end"]
        parts = []
        for w in chunk:
            dur_cs = max(1, round((w["end"] - w["start"]) * 100))
            parts.append("{\\k" + str(dur_cs) + "}" + w["text"])
        text = " ".join(parts)
        events += f"Dialogue: 0,{_fmt_time(chunk_start)},{_fmt_time(chunk_end)},Default,,0,0,0,,{text}\n"

    return header + events


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
        tmp.write(await audio.read())
        tmp_path = tmp.name

    try:
        result = model.transcribe(tmp_path, word_timestamps=True)
        return {"segments": result["segments"], "text": result["text"]}
    finally:
        os.unlink(tmp_path)


@app.post("/render")
async def render(
    audio: UploadFile = File(...),
    title: str = Form(""),
    queries: str = Form(""),
    max_segments: int = Form(0),
    music_url: str = Form(""),
):
    work_dir = tempfile.mkdtemp()
    try:
        # Save audio
        audio_path = os.path.join(work_dir, "audio.mp3")
        with open(audio_path, "wb") as f:
            f.write(await audio.read())

        # Transcribe
        result = model.transcribe(audio_path, word_timestamps=True)

        # Generate subtitles
        ass_content = generate_ass_subtitles(result["segments"])
        ass_path = os.path.join(work_dir, "subs.ass")
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)

        # Get audio duration
        probe = subprocess.run([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", audio_path
        ], capture_output=True, text=True)
        duration = float(json.loads(probe.stdout)["format"]["duration"])

        # Split the video into several SEGMENT_DURATION-second cuts, each with
        # a different background clip, instead of looping a single clip for
        # the whole runtime.
        segment_cap = max_segments if max_segments > 0 else MAX_SEGMENTS
        segment_cap = min(segment_cap, HARD_MAX_SEGMENTS)
        num_segments = max(1, min(segment_cap, math.ceil(duration / SEGMENT_DURATION)))
        segment_durations = [SEGMENT_DURATION] * (num_segments - 1)
        segment_durations.append(round(duration - SEGMENT_DURATION * (num_segments - 1), 2))
        segment_durations[-1] = max(segment_durations[-1], 1.0)

        # `queries`, if given as an ORDERED comma-separated list of short
        # narrative beats (earliest-to-latest in the story), drives which
        # background footage plays during which part of the video. If it's
        # empty or just one generic category, fall back to the old random
        # picker (title + a shuffled query pool) -- order doesn't matter then.
        beats = [q.strip() for q in queries.split(",") if q.strip()]
        background_urls = build_ordered_background_list(num_segments, beats)
        if background_urls is None:
            background_urls = build_background_list(num_segments, title=title, custom_queries=beats)

        # Download each background clip (looping isn't needed at download
        # time; ffmpeg loops the file at render time if it's shorter than
        # its segment).
        bg_paths = []
        for i, url in enumerate(background_urls):
            bg_path = os.path.join(work_dir, f"bg_{i}.mp4")
            try:
                subprocess.run([
                    "ffmpeg", "-y", "-i", url,
                    "-t", "60", "-c", "copy", bg_path
                ], check=True, capture_output=True, timeout=60)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                # Skip a clip that fails to download rather than failing the
                # whole render; re-use the previous good clip if any.
                if bg_paths:
                    bg_path = bg_paths[-1]
                else:
                    fallback_url = random.choice(STATIC_FALLBACK_VIDEOS)
                    subprocess.run([
                        "ffmpeg", "-y", "-i", fallback_url,
                        "-t", "60", "-c", "copy", bg_path
                    ], check=True, capture_output=True)
            bg_paths.append(bg_path)

        # Optional background music bed, mixed low under the narration.
        # Silently skipped (never fails the render) if music_url is empty
        # or can't be downloaded -- caller must supply their own licensed /
        # royalty-free track URL.
        music_path = None
        if music_url:
            try:
                candidate_path = os.path.join(work_dir, "music.mp3")
                with requests.get(music_url, stream=True, timeout=20) as r:
                    r.raise_for_status()
                    with open(candidate_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                music_path = candidate_path
            except requests.RequestException:
                music_path = None

        # Build the ffmpeg filter graph: normalize + trim each background
        # clip to its segment length, concatenate them, apply a subtle
        # cinematic color grade, then burn subtitles on top.
        cmd = ["ffmpeg", "-y"]
        for bg_path in bg_paths:
            cmd += ["-stream_loop", "-1", "-i", bg_path]
        cmd += ["-i", audio_path]
        audio_input_index = len(bg_paths)

        music_input_index = None
        if music_path:
            cmd += ["-stream_loop", "-1", "-i", music_path]
            music_input_index = audio_input_index + 1

        filter_parts = []
        concat_labels = ""
        for i, seg_dur in enumerate(segment_durations):
            filter_parts.append(
                f"[{i}:v]scale=1280:720:force_original_aspect_ratio=increase,"
                f"crop=1280:720,fps=30,setsar=1,trim=duration={seg_dur:.2f},"
                f"setpts=PTS-STARTPTS[v{i}]"
            )
            concat_labels += f"[v{i}]"
        filter_parts.append(f"{concat_labels}concat=n={num_segments}:v=1:a=0[vcat]")

        ass_escaped = ass_path.replace("\\", "/").replace(":", "\\:")
        # Slight desaturation + contrast + vignette for a documentary look,
        # then burn the karaoke captions on top, all in one filter step.
        filter_parts.append(
            f"[vcat]eq=contrast=1.08:saturation=0.82:brightness=-0.02,"
            f"vignette=PI/4,ass={ass_escaped}[vout]"
        )

        if music_input_index is not None:
            fade_start = max(duration - 2, 0)
            filter_parts.append(
                f"[{music_input_index}:a]volume=0.07,afade=t=out:st={fade_start:.2f}:d=2[music]"
            )
            filter_parts.append(
                f"[{audio_input_index}:a][music]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            )
            audio_map = "[aout]"
        else:
            audio_map = f"{audio_input_index}:a"

        filter_complex = ";".join(filter_parts)

        output_path = os.path.join(work_dir, "output.mp4")
        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", audio_map,
            "-t", str(duration),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-shortest",
            output_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True)

        return FileResponse(output_path, media_type="video/mp4", filename="video.mp4")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Cleanup handled after response
        pass


@app.get("/health")
def health():
    return {"status": "ok"}
