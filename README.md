# FFmpeg + Whisper API

API para generar videos con subtítulos automáticos.

## Configuración

- `PEXELS_API_KEY` (variable de entorno, recomendada): clave gratuita de
  [pexels.com/api](https://www.pexels.com/api/) para buscar clips de fondo
  variados y relacionados con el título de cada video. Sin esta variable, el
  servicio sigue funcionando pero usa un set fijo de 3 videos de respaldo
  (menos variedad).
- `MAX_SEGMENTS` (variable de entorno, opcional, por defecto 14): máximo de
  clips de fondo distintos por video.

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

Campos opcionales de `/render`:

- `queries`: lista ORDENADA de escenas separadas por comas, de principio a
  fin de la historia (p. ej. `empty office exterior,financial documents on
  desk,police car lights night,courtroom empty benches`). El fondo va
  siguiendo estas escenas en orden en vez de mezclarlas al azar — así el
  video visualmente sigue el hilo del guion. Con menos de 2 escenas distintas
  se usa el modo aleatorio antiguo (mezcla el título del video con búsquedas
  genéricas).
- `max_segments`: clips distintos para este video (máx. 60). Útil en videos
  largos: 40 clips × 45 s cubren 30 minutos sin repetir fondos.
- `music_url`: URL de una pista de música de fondo (con licencia/royalty-free
  — tú la eliges, la API no incluye ninguna) que se mezcla muy baja bajo la
  narración con fundido de salida al final. Si no se indica o falla la
  descarga, el video se genera igual sin música.

## Subtítulos

Los subtítulos se queman con resaltado de palabra por palabra (estilo
karaoke), en grupos cortos de 4-5 palabras, en vez de frases largas —
el formato que usan la mayoría de canales para mantener la atención.

## Grado de color

Se aplica automáticamente un contraste ligeramente más alto, algo menos de
saturación y una viñeta suave para dar un aspecto más cinematográfico/
documental al fondo de stock.
