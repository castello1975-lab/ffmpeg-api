from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
import subprocess
import tempfile
import os
import json
import random
import shutil
import whisper

app = FastAPI()
model = whisper.load_model("base")

BACKGROUND_VIDEOS = [
    "https://videos.pexels.com/video-files/35379336/14990716_640_360_30fps.mp4",
    "https://videos.pexels.com/video-files/5466780/5466780-sd_640_360_25fps.mp4",
    "https://videos.pexels.com/video-files/3255384/3255384-sd_640_360_25fps.mp4"
]

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
        
        # Pick random background
        bg_url = random.choice(BACKGROUND_VIDEOS)
        bg_path = os.path.join(work_dir, "bg.mp4")
        
        # Download background
        subprocess.run([
            "ffmpeg", "-y", "-i", bg_url,
            "-t", "9999", "-c", "copy", bg_path
        ], check=True, capture_output=True)
        
        # Get audio duration
        probe = subprocess.run([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", audio_path
        ], capture_output=True, text=True)
        duration = float(json.loads(probe.stdout)["format"]["duration"])
        
        # Render final video with FFmpeg
        output_path = os.path.join(work_dir, "output.mp4")
        subprocess.run([
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", bg_path,
            "-i", audio_path,
            "-t", str(duration),
            "-vf", f"scale=1280:720,ass={ass_path}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-shortest",
            output_path
        ], check=True, capture_output=True)
        
        return FileResponse(output_path, media_type="video/mp4", filename="video.mp4")
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Cleanup handled after response
        pass

@app.get("/health")
def health():
    return {"status": "ok"}
