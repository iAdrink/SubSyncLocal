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

**Para sincronizar (lo imprescindible):** Windows, Python 3.8+ (con `tkinter`), `numpy` y `ffmpeg`/`ffprobe`.

**Opcional:** `tkinterdnd2` (arrastrar archivos a la ventana) y, para la detección de frases que faltan,
**Whisper** (el de Subtitle Edit) y un traductor (**Ollama** local o una IA externa por copiar/pegar).

> Whisper Const-me necesita Windows de 64 bits, una GPU compatible con Direct3D 11 y una CPU con AVX y F16C
> (prácticamente cualquier PC desde 2012). Funciona con GPUs AMD, NVIDIA e Intel.

## Instalación completa, paso a paso (con todos los extras)

### 1. Lo básico (obligatorio)

1. **Python 3.8 o superior**: [python.org/downloads](https://www.python.org/downloads/). Marca *Add Python to PATH*.
2. **Este programa**: botón verde *Code → Download ZIP* (o `git clone https://github.com/iAdrink/SubSyncLocal.git`) y descomprímelo donde quieras.
3. **Librerías**: `pip install numpy tkinterdnd2`. Solo `numpy` es imprescindible; si falta, el programa ofrece instalarlo solo, y el lanzador `SubSyncLocal.pyw` instala `tkinterdnd2` la primera vez.
4. **ffmpeg y ffprobe**: en una consola, `winget install --id=Gyan.FFmpeg -e`
   (o descarga las builds de [gyan.dev](https://www.gyan.dev/ffmpeg/builds/)). No hace falta tocar el PATH: el programa lo busca solo.

Con esto ya puedes sincronizar. Los pasos siguientes son solo para la detección de frases que faltan.

### 2. Whisper, para transcribir los tramos sin subtítulo (opcional)

Este programa usa un `main.exe` de Whisper + un modelo `.bin` (formato ggml). Hay dos caminos según tu tarjeta gráfica.

#### Opción A: AMD (la probada por el autor)

> **Probado con una AMD Radeon RX 7800 XT de 16 GB.** El autor tiene AMD, así que este es el camino que usa y que
> sabe que funciona: **Whisper Const-me** (el que instala Subtitle Edit) con un modelo ggml.
> Const-me usa Direct3D 11, por lo que no depende de CUDA y va bien en tarjetas AMD.

1. Instala [Subtitle Edit](https://github.com/SubtitleEdit/subtitleedit/releases).
2. Dentro de Subtitle Edit: **Video → Speech to text (Whisper)…**, elige el motor **Const-me** y un modelo.
   Subtitle Edit descarga motor y modelo automáticamente la primera vez que lo usas.
3. Listo: SubSync Local lo encuentra solo en `%APPDATA%\Subtitle Edit\SpeechToText\Const-me\`
   (`main.exe` y `Models\*.bin`; usa el modelo `.bin` más grande que haya).

Si ya tienes `main.exe` y un modelo en otra carpeta, indícalos en el botón **Ajustes…** (incluye una prueba).
El motor original está en [Const-me/Whisper](https://github.com/Const-me/Whisper) (licencia MPL-2.0).

#### Opción B: NVIDIA

- **Lo más sencillo: usa también la Opción A.** Const-me funciona igual en tarjetas NVIDIA (Direct3D 11), así que
  puedes seguir exactamente los mismos pasos. Es la combinación con la que se ha desarrollado el programa, aunque
  el autor solo la ha probado con AMD.
- **Alternativa (sin probar): [whisper.cpp](https://github.com/ggml-org/whisper.cpp) con CUDA.** Es más rápido en
  muchas tarjetas NVIDIA. Este programa llama al ejecutable con las opciones `-m`, `-l`, `-osrt` y `--prompt`, que
  también existen en la línea de comandos de whisper.cpp, así que en principio debería servir. Ten en cuenta que
  en versiones recientes el ejecutable se llama `whisper-cli.exe` y no `main.exe`; indícalo en **Ajustes…**.
  Usa el botón de prueba de **Ajustes…** para comprobar que funciona antes de lanzar un episodio.
  Si lo pruebas, cuéntame el resultado abriendo un *issue*.

#### Modelos de Whisper (formato ggml)

Se descargan de Hugging Face, del repositorio [`ggerganov/whisper.cpp`](https://huggingface.co/ggerganov/whisper.cpp):

| Modelo | Tamaño aprox. | Descarga directa |
|---|---|---|
| `ggml-medium.bin` (el que recomienda Whisper Const-me) | ~1,5 GB | [enlace](https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.bin) |
| `ggml-large-v3-turbo.bin` | ~1,6 GB | [enlace](https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin) |
| `ggml-large-v3.bin` (el más preciso) | ~3,1 GB | [enlace](https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin) |

Guarda el `.bin` en la carpeta `Models` de arriba o elígelo en **Ajustes…**. En la página de Hugging Face figura el
SHA de cada archivo, por si quieres comprobar la descarga. Los modelos cuantizados (`q5_0`, `q8_0`) no están probados
con este programa; usa los de la tabla. Con una GPU de 16 GB como la del autor cabe cualquiera de ellos; con menos
memoria de vídeo, empieza por `medium`.

### 3. Traducción de las frases detectadas (opcional)

- **Local con [Ollama](https://ollama.com/download)**: instálalo y ejecuta `ollama pull qwen3:8b`
  (~5 GB, licencia Apache 2.0). Puedes elegir otro modelo en **Ajustes…**, con un botón para actualizar la lista.
- **Sin Ollama**: el traductor «IA externa» te da un prompt completo para copiar y pegar en la IA que prefieras.

## ¿Por qué no se incluyen aquí los modelos?

- **Tamaño.** GitHub bloquea los archivos de más de 100 MiB en un repositorio normal; con Git LFS el máximo por
  archivo es de 2 GB en cuentas gratuitas, y los *assets* de una *release* tienen un tope de 2 GiB. `ggml-large-v3.bin`
  ronda los 3,1 GB, así que no cabe en ninguna de las tres vías.
- **Licencias.** Los pesos de Whisper de OpenAI y las conversiones a ggml de whisper.cpp son MIT, y Qwen3 es Apache 2.0,
  de modo que redistribuirlos es posible conservando sus avisos de licencia. Aun así, lo más limpio es descargarlos de
  la fuente original: siempre están al día, se pueden verificar y este repositorio no tiene que cargar con binarios
  de terceros. Esto no es asesoramiento legal.

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
