# FFmpeg + Whisper API

API para generar videos con subtítulos automáticos.

## Configuración

- `PEXELS_API_KEY` (variable de entorno, recomendada): clave gratuita de
  [pexels.com/api](https://www.pexels.com/api/) para buscar clips de fondo
  variados y relacionados con el título de cada video. Sin esta variable, el
  servicio sigue funcionando pero usa un set fijo de 3 videos de respaldo
  (menos variedad).

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
