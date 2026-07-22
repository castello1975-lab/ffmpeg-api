from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
import subprocess
import tempfile
import os
import json
import random
import shutil
import math
import whisper

app = FastAPI()
model = whisper.load_model("base")

BACKGROUND_VIDEOS = [
    "https://videos.pexels.com/video-files/35379336/14990716_640_360_30fps.mp4",
    "https://videos.pexels.com/video-files/5466780/5466780-sd_640_360_25fps.mp4",
    "https://videos.pexels.com/video-files/3255384/3255384-sd_640_360_25fps.mp4"
]

SEGMENT_DURATION = 30

def generate_ass_subtitles(segments):
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
        audio_path = os.path.join(work_dir, "audio.mp3")
        with open(audio_path, "wb") as f:
            f.write(await audio.read())

        result = model.transcribe(audio_path, word_timestamps=True)

        ass_content = generate_ass_subtitles(result["segments"])
        ass_path = os.path.join(work_dir, "subs.ass")
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_content)

        probe = subprocess.run([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", audio_path
        ], capture_output=True, text=True)
        duration = float(json.loads(probe.stdout)["format"]["duration"])

        num_segments = max(1, math.ceil(duration / SEGMENT_DURATION))
        segments = []
        for i in range(num_segments):
            seg_start = i * SEGMENT_DURATION
            seg_end = min((i + 1) * SEGMENT_DURATION, duration)
            seg_duration = seg_end - seg_start
            bg_url = random.choice(BACKGROUND_VIDEOS)
            segments.append((bg_url, seg_duration))

        segment_files = []
        for idx, (bg_url, seg_dur) in enumerate(segments):
            bg_raw = os.path.join(work_dir, f"bg_raw_{idx}.mp4")
            bg_seg = os.path.join(work_dir, f"bg_seg_{idx}.mp4")

            subprocess.run([
                "ffmpeg", "-y",
                "-stream_loop", "-1", "-i", bg_url,
                "-t", str(seg_dur + 5),
                "-c", "copy", bg_raw
            ], check=True, capture_output=True)

            subprocess.run([
                "ffmpeg", "-y",
                "-stream_loop", "-1", "-i", bg_raw,
                "-t", str(seg_dur),
                "-vf", "scale=1280:720,setsar=1",
                "-an",
                "-c:v", "libx264", "-preset", "fast",
                bg_seg
            ], check=True, capture_output=True)

            segment_files.append(bg_seg)

        concat_list = os.path.join(work_dir, "concat.txt")
        with open(concat_list, "w") as f:
            for sf in segment_files:
                f.write(f"file '{sf}'\n")

        bg_combined = os.path.join(work_dir, "bg_combined.mp4")
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", concat_list,
            "-c", "copy",
            bg_combined
        ], check=True, capture_output=True)

        output_path = os.path.join(work_dir, "output.mp4")
        subprocess.run([
            "ffmpeg", "-y",
            "-i", bg_combined,
            "-i", audio_path,
            "-t", str(duration),
            "-vf", f"ass={ass_path}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            output_path
        ], check=True, capture_output=True)

        return FileResponse(output_path, media_type="video/mp4", filename="video.mp4")

    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Error FFmpeg: {e.stderr.decode()[-2000:]}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
def health():
    return {"status": "ok"}

