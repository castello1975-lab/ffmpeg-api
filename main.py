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

# Used when no title is supplied, or mixed in for extra variety alongside the
# title-derived query.
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
MAX_SEGMENTS = 14      # cap on distinct clips per video, to bound render time/API calls


def _title_to_query(title: str) -> str:
    """Turn a video title into a short, Pexels-friendly search phrase."""
    stopwords = {
        "el", "la", "los", "las", "un", "una", "de", "del", "y", "o", "que",
        "para", "por", "con", "en", "a", "tu", "su", "es", "como", "si", "no",
    }
    words = [w.strip(".,:;!?¿¡\"'").lower() for w in title.split()]
    keywords = [w for w in words if w and w not in stopwords]
    return " ".join(keywords[:4]) if keywords else ""


def fetch_pexels_video_urls(count, title=""):
    """Fetch up to `count` distinct landscape video URLs from Pexels, mixing
    the video's own title (as a search query) with generic finance/business
    queries for variety. Returns [] if PEXELS_API_KEY is not set or every
    request fails."""
    if not PEXELS_API_KEY:
        return []

    queries = list(FALLBACK_QUERIES)
    title_query = _title_to_query(title)
    if title_query:
        queries = [title_query, title_query] + queries  # weight it higher
    random.shuffle(queries)

    headers = {"Authorization": PEXELS_API_KEY}
    urls = []
    seen = set()

    for q in queries:
        if len(urls) >= count:
            break
        try:
            resp = requests.get(
                "https://api.pexels.com/videos/search",
                headers=headers,
                params={"query": q, "orientation": "landscape", "size": "medium", "per_page": 15},
                timeout=10,
            )
            resp.raise_for_status()
            videos = resp.json().get("videos", [])
            random.shuffle(videos)
            for video in videos:
                files = [f for f in video.get("video_files", []) if f.get("width")]
                files.sort(key=lambda f: abs(f["width"] - 1280))
                hd_candidates = [f["link"] for f in files if 960 <= f["width"] <= 1920]
                if not hd_candidates:
                    continue
                link = hd_candidates[0]
                if link in seen:
                    continue
                seen.add(link)
                urls.append(link)
                if len(urls) >= count:
                    break
        except requests.RequestException:
            continue

    random.shuffle(urls)
    return urls[:count]


def build_background_list(num_segments, title=""):
    """Return exactly `num_segments` background video URLs, avoiding the same
    clip appearing twice in a row when possible."""
    urls = fetch_pexels_video_urls(num_segments, title=title)

    pool = urls if urls else list(STATIC_FALLBACK_VIDEOS)
    while len(urls) < num_segments:
        candidate = random.choice(pool)
        if urls and candidate == urls[-1] and len(pool) > 1:
            continue
        urls.append(candidate)

    return urls[:num_segments]


def generate_ass_subtitles(segments):
    """Generate ASS subtitle file with karaoke-style highlighting"""
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,1,2,10,10,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = ""
    for seg in segments:
        start = seg['start']
        end = seg['end']
        text = seg['text'].strip()

        def fmt_time(t):
            h = int(t // 3600)
            m = int((t % 3600) // 60)
            s = t % 60
            return f"{h}:{m:02d}:{s:05.2f}"

        events += f"Dialogue: 0,{fmt_time(start)},{fmt_time(end)},Default,,0,0,0,,{text}\n"

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
async def render(audio: UploadFile = File(...), title: str = Form("")):
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
        num_segments = max(1, min(MAX_SEGMENTS, math.ceil(duration / SEGMENT_DURATION)))
        segment_durations = [SEGMENT_DURATION] * (num_segments - 1)
        segment_durations.append(round(duration - SEGMENT_DURATION * (num_segments - 1), 2))
        segment_durations[-1] = max(segment_durations[-1], 1.0)

        background_urls = build_background_list(num_segments, title=title)

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

        # Build the ffmpeg filter graph: normalize + trim each background
        # clip to its segment length, concatenate them, then burn subtitles
        # on top of the concatenated video.
        cmd = ["ffmpeg", "-y"]
        for bg_path in bg_paths:
            cmd += ["-stream_loop", "-1", "-i", bg_path]
        cmd += ["-i", audio_path]

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
        filter_parts.append(f"[vcat]ass={ass_escaped}[vout]")

        filter_complex = ";".join(filter_parts)
        audio_input_index = len(bg_paths)

        output_path = os.path.join(work_dir, "output.mp4")
        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", f"{audio_input_index}:a",
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
