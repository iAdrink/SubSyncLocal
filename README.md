# SubSync Local

Sincroniza subtítulos (`.srt`, `.ass`, `.ssa`) con el **audio real** de un vídeo, **todo en tu PC y sin internet**.
Le das el MKV (o carpeta de vídeos) y el subtítulo, y te devuelve el subtítulo ajustado a la voz. Además,
si detecta tramos con voz y sin subtítulo, puede transcribirlos con Whisper y traducirlos en local.

> No hace falta extraer el audio ni transcribir nada antes: se le da el vídeo directamente.
> El subtítulo original **nunca se modifica**; el resultado se guarda en una carpeta `sincronizados`.

## Qué hace

1. **Extrae el audio** con ffmpeg (canal central si es 5.1, donde va el diálogo) y calcula dónde hay voz cada 10 ms.
2. **Busca el desfase global** (hasta ±5 min) y el **cambio de velocidad** (25 ↔ 23,976 ↔ 24 fps, etc.).
3. **Detecta saltos, cortes y derivas** por tramos, en dos niveles (grueso y fino).
4. **Ajusta el inicio y el final** de cada línea al arranque y final reales de la voz.
5. **Frases que faltan** (`fillgaps.py`): tras sincronizar, busca tramos con voz y sin subtítulo. Si no hay
   ninguno, no se ejecuta nada pesado. Si los hay, pasa Whisper solo sobre esos tramos, traduce las frases
   reales (Ollama en local, IA externa por copiar/pegar, o ambas) y te las muestra en una ventana con el minuto,
   el original y la traducción editable. Hay modo automático para añadirlas sin preguntar.

Detalle útil: en muchos MKV los tiempos de los paquetes de audio están estirados ≈0,1 % respecto al audio
decodificado (1,5–1,9 s al final de un episodio). El reproductor sincroniza con los tiempos del MKV, así que el
programa convierte la voz a esa línea de tiempo antes de alinear (opción «Tiempos del MKV», activada por defecto).

## Características

- Ventana con **procesamiento por lotes**: varios archivos a la vez o una carpeta de vídeos y otra de subtítulos.
  Se emparejan solos por temporada y episodio (`04x02`, `S04E02`, `Serie 02`…) y una pestaña **Emparejado**
  muestra qué vídeo va con qué subtítulo antes de empezar.
- **Selector de pista de audio** (automática por idioma, `jpn` por defecto, o la que elijas).
- **Formato de salida**: igual que el original, solo SRT o solo ASS (conserva cursiva, negrita, subrayado y color).
- Avisos de fiabilidad al terminar cada episodio (poca coincidencia, desfase poco claro, tramos con desfase residual).
- Arrastrar y soltar dentro de la ventana (opcional, con `tkinterdnd2`).
- Uso por **línea de comandos** además de la ventana.

## Requisitos

- Windows con **Python 3.8 o superior** (con `tkinter`).
- [`numpy`](https://numpy.org/) (`pip install numpy`; el lanzador lo instala si falta).
- **ffmpeg** y **ffprobe**. No tienen que estar en el PATH: el programa los busca solo (PATH de Windows,
  winget, Chocolatey, Scoop, `C:\ffmpeg`…). Si no los encuentra, usa el botón *Buscar ffmpeg…* o `--ffmpeg-dir`.
- Opcional: `pip install tkinterdnd2` para arrastrar archivos a la ventana.
- Opcional, para la detección de frases que faltan:
  - **Whisper** de Subtitle Edit (Const-me: `main.exe` + `Models\large.bin`), detectado automáticamente.
  - **[Ollama](https://ollama.com/)** con un modelo para traducir (por ejemplo `ollama pull qwen3:8b`).

## Uso

### Ventana

Doble clic en `SubSync.bat` (o en `SubSyncLocal.pyw`). Se abre solo la ventana, sin consola. Si algo falla al
arrancar, se escribe `subsync_error.log` junto al programa.

### Línea de comandos

```bash
python subsync.py video.mkv subtitulo.srt
python subsync.py "D:\Serie\Videos" "D:\Serie\Subs" -o "D:\Serie\OK"
python subsync.py video.mkv --list-tracks            # ver las pistas de audio
python subsync.py video.mkv sub.srt --track 2        # usar la segunda pista
python subsync.py --ffmpeg-dir "C:\ruta\a\ffmpeg\bin"  # indicar dónde está ffmpeg
```

Opciones principales:

| Opción | Para qué sirve |
|---|---|
| `--lang jpn` / `--track N` | Elegir la pista de audio por idioma o por número |
| `--timeline container\|audio` | Usar los tiempos del MKV (por defecto) o los del audio decodificado |
| `--format auto\|srt\|ass` | Formato del subtítulo de salida |
| `--no-snap` | No ajustar el inicio y el final de cada línea a la voz |
| `--no-cuts` | No buscar saltos ni cortes |
| `--mix` | Usar la mezcla completa del audio en vez del canal central |
| `-r`, `--recursive` | Buscar también en subcarpetas |
| `-o`, `--out carpeta` | Carpeta de salida (por defecto, `sincronizados` junto al subtítulo) |
| `--no-fill`, `--fill-auto` | Desactivar o automatizar la detección de frases que faltan |
| `--translator local\|ia\|both` | Cómo traducir las frases detectadas |

## Archivos

| Archivo | Descripción |
|---|---|
| `subsync.py` | Programa principal: sincronización, interfaz y línea de comandos |
| `fillgaps.py` | Detección y traducción de frases que faltan (debe estar junto a `subsync.py`) |
| `SubSyncLocal.pyw` | Lanzador sin consola |
| `SubSync.bat` | Lanzador para Windows |

El programa crea `subsync_config.json` junto a los scripts para recordar tus ajustes (rutas de ffmpeg y Whisper,
modelo de Ollama, tamaño de la ventana…). Está en `.gitignore`.

## Límites

- Mide contra la **actividad de voz** del audio; no entiende el idioma. Revisa a ojo un par de momentos
  (sobre todo el final del episodio) la primera vez con una serie nueva.
- Si eliges otra pista de audio, el resultado será el de esa pista.
- Los carteles con efectos de posición (`\pos`, `\move`) en `.ass` siguen la corrección general, pero no se
  ajustan a la voz.
