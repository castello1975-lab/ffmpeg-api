# FFmpeg + Whisper API

API para generar videos con subtítulos automáticos.

## Endpoints

- `GET /health` - Estado del servicio
- `POST /transcribe` - Transcribir audio (devuelve segmentos con timestamps)
- `POST /render` - Generar video completo (audio + fondo + subtítulos)

## Uso desde n8n

### Transcribir:
```
POST http://ffmpeg-api:8000/transcribe
Body: form-data, campo "audio" = archivo mp3
```

### Renderizar video:
```
POST http://ffmpeg-api:8000/render
Body: form-data, campo "audio" = archivo mp3, campo "title" = título del video
```
