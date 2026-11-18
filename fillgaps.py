# -*- coding: utf-8 -*-
"""
SubSync Local - modulo "frases que faltan".

Despues de sincronizar, busca tramos donde HAY VOZ en el audio y NO hay ninguna linea de subtitulo.
Solo en esos tramos (nunca en todo el episodio) pasa Whisper (el main.exe de Whisper Const-me que ya usa
Subtitle Edit, con su mismo modelo) para saber que se dice. Si Whisper no saca texto, era musica o ruido y se ignora.
Las frases que de verdad faltan se traducen (modelo local via Ollama y/o una IA externa por copiar-pegar)
y se anaden al subtitulo, preguntando antes (ventana de revision) o en automatico.

Todo en local. Este modulo se usa desde subsync.py; recibe el propio modulo subsync como parametro "S".
"""
import os, re, sys, json, time, bisect, subprocess, tempfile, shutil, urllib.request, urllib.error
import numpy as np

# ----------------------------------------------------------------------------- configuracion
LANG_CODE = {'jpn': 'ja', 'ja': 'ja', 'jap': 'ja', 'eng': 'en', 'en': 'en', 'spa': 'es', 'es': 'es', 'kor': 'ko', 'ko': 'ko',
             'chi': 'zh', 'zho': 'zh', 'zh': 'zh', 'fre': 'fr', 'fra': 'fr', 'fr': 'fr', 'ger': 'de', 'deu': 'de', 'de': 'de',
             'ita': 'it', 'it': 'it', 'por': 'pt', 'pt': 'pt', 'rus': 'ru', 'ru': 'ru'}
LANG_NAME = {'ja': 'japonés', 'en': 'inglés', 'es': 'español', 'ko': 'coreano', 'zh': 'chino', 'fr': 'francés', 'de': 'alemán',
             'it': 'italiano', 'pt': 'portugués', 'ru': 'ruso', 'auto': 'idioma original'}
TRANSLATOR_CHOICES = {"Modelo local (Ollama)": 'local', "IA externa (copiar y pegar)": 'ia', "Local + IA externa (comparar)": 'both'}

def translator_choices(model=''):
    """etiquetas del desplegable 'Traducir con', con el nombre del modelo local que esta elegido"""
    m = (model or '').strip() or 'Ollama'
    return {"Modelo local (%s)" % m: 'local', "IA externa (copiar y pegar)": 'ia', "Local (%s) + IA externa (comparar)" % m: 'both'}

DEFAULTS = dict(
    enabled=True,            # buscar frases que faltan
    auto=False,              # aplicar sin preguntar
    translator='local',      # local | ia | both
    whisper_exe='', whisper_model='', adapter='',
    ollama_host='http://localhost:11434', ollama_model='qwen3:8b',
    es_variant='español de España', min_gap=0.8, content='auto',
    classify=True,           # el modelo local confirma si lo detectado es voz real o sonido/ruido
)

def default_cfg():
    return dict(DEFAULTS)

def get_cfg(S, opts=None):
    cfg = default_cfg()
    try:
        cfg.update({k: v for k, v in (S.load_config().get('fill') or {}).items() if k in DEFAULTS})
    except Exception:
        pass
    if opts and isinstance(opts.get('fill_cfg'), dict):
        cfg.update({k: v for k, v in opts['fill_cfg'].items() if v is not None})
    if not cfg['whisper_exe'] or not cfg['whisper_model']:
        exe, model = detect_whisper()
        cfg['whisper_exe'] = cfg['whisper_exe'] or exe
        cfg['whisper_model'] = cfg['whisper_model'] or model
    return cfg

def save_cfg(S, cfg):
    c = S.load_config(); c['fill'] = {k: cfg.get(k) for k in DEFAULTS}; S.save_config(c)

def detect_whisper():
    """busca el Whisper Const-me que ya tiene instalado Subtitle Edit (main.exe + modelo .bin mas grande)"""
    bases = []
    for env in ('APPDATA', 'LOCALAPPDATA'):
        v = os.environ.get(env)
        if v: bases.append(os.path.join(v, 'Subtitle Edit', 'SpeechToText', 'Const-me'))
    bases.append(os.path.join(os.path.expanduser('~'), 'AppData', 'Roaming', 'Subtitle Edit', 'SpeechToText', 'Const-me'))
    for b in bases:
        exe = os.path.join(b, 'main.exe')
        if os.path.isfile(exe):
            models = []
            md = os.path.join(b, 'Models')
            if os.path.isdir(md):
                models = [os.path.join(md, f) for f in os.listdir(md) if f.lower().endswith('.bin')]
            models.sort(key=lambda p: os.path.getsize(p), reverse=True)
            return exe, (models[0] if models else '')
    return '', ''

def check_whisper(cfg):
    if not cfg.get('whisper_exe') or not os.path.isfile(cfg['whisper_exe']):
        return False, "no encuentro Whisper (main.exe de Subtitle Edit). Indícalo en 'Ajustes…'"
    if not cfg.get('whisper_model') or not os.path.isfile(cfg['whisper_model']):
        return False, "no encuentro el modelo de Whisper (.bin). Indícalo en 'Ajustes…'"
    return True, ''

# ----------------------------------------------------------------------------- 1. buscar tramos con voz y sin subtitulo
def _clean(t):
    t = re.sub(r'\{[^}]*\}', '', t); t = re.sub(r'<[^>]+>', '', t)
    return re.sub(r'\s+', ' ', t.replace('\\N', ' ').replace('\\n', ' ')).strip()

def find_gaps(S, cues, a, min_gap=0.8, pad=0.25, merge=1.2):
    """tramos de voz (actividad > 0.5) que no tienen ninguna linea de subtitulo encima (ni dialogo ni cartel/letra).
    Dos trozos solo se unen si entre ellos hay silencio (nunca se salta una linea que ya existe)."""
    n = len(a); cov = np.zeros(n + 1, dtype=bool)
    for c in cues:
        i0 = max(0, int(round((c['s'] - pad) / S.FR))); i1 = min(n, int(round((c['e'] + pad) / S.FR)) + 1)
        if i1 > i0: cov[i0:i1] = True
    unc = ((a > 0.5) & ~cov[:n]).astype(np.float32)
    runs = S.speech_segments(unc, 0.5, 0.2, 0.3)
    merged = []
    for s, e in runs:
        if merged and s - merged[-1][1] < merge:
            i0 = int(round(merged[-1][1] / S.FR)); i1 = int(round(s / S.FR))
            if not cov[i0:i1].any():
                merged[-1][1] = float(e); continue
        merged.append([float(s), float(e)])
    out = []
    for s, e in merged:
        voiced = float(unc[int(round(s / S.FR)):int(round(e / S.FR))].sum()) * S.FR
        if voiced >= min_gap: out.append(dict(gs=s, ge=e, voiced=voiced))
    return out

def _parse_t(s):
    s = s.strip(); parts = s.split(':')
    if not 1 <= len(parts) <= 3 or not all(re.fullmatch(r'\d+', x.strip()) for x in parts):
        raise ValueError("no entiendo «%s» (escribe minutos y segundos, por ejemplo 12:53)" % s)
    v = 0
    for x in parts: v = v * 60 + int(x)
    return float(v)

def parse_ranges(text, single_span=20.0, max_len=300.0):
    # "12:53-13:05, 20:10" -> [(773.0, 785.0), (1210.0, 1230.0)]. Un solo minuto = ese momento y los 20 s siguientes.
    out = []
    for chunk in re.split(r'[;,\n]+', text or ''):
        chunk = chunk.strip()
        if not chunk: continue
        bits = re.split(r'\s*(?:-|–|—|\s+a\s+|\s+hasta\s+)\s*', chunk, maxsplit=1)
        t0 = _parse_t(bits[0]); t1 = _parse_t(bits[1]) if len(bits) > 1 and bits[1].strip() else t0 + single_span
        if t1 <= t0: raise ValueError("el tramo «%s» termina antes de empezar" % chunk)
        if t1 - t0 > max_len: raise ValueError("el tramo «%s» es demasiado largo (máximo %d min)" % (chunk, int(max_len // 60)))
        out.append((t0, t1))
    return out

def forced_gaps(S, cues, a, ranges, pad=0.1, min_len=0.8):
    # tramos pedidos a mano: dentro de cada uno se queda lo que NO tiene ya linea de subtitulo (no se mira si el detector oye voz o no)
    n = len(a); cov = np.zeros(n + 1, dtype=bool)
    for c in cues:
        i0 = max(0, int(round((c['s'] - pad) / S.FR))); i1 = min(n, int(round((c['e'] + pad) / S.FR)) + 1)
        if i1 > i0: cov[i0:i1] = True
    out = []
    for t0, t1 in ranges:
        i0 = max(0, int(round(t0 / S.FR))); i1 = min(n, int(round(t1 / S.FR)))
        unc = ~cov[i0:i1]; m = len(unc); k = 0
        while k < m:
            if unc[k]:
                j = k
                while j < m and unc[j]: j += 1
                if (j - k) * S.FR >= min_len: out.append(dict(gs=(i0 + k) * S.FR, ge=(i0 + j) * S.FR, voiced=(j - k) * S.FR, forced=True))
                k = j
            else: k += 1
    return out

def merge_gaps(g1, g2):
    out = []
    for g in sorted([dict(x) for x in list(g1) + list(g2)], key=lambda x: x['gs']):
        if out and g['gs'] <= out[-1]['ge'] + 0.3:
            o = out[-1]; o['ge'] = max(o['ge'], g['ge']); o['voiced'] = o.get('voiced', 0) + g.get('voiced', 0)
            o['forced'] = bool(o.get('forced') or g.get('forced'))
        else: out.append(g)
    return out

# ----------------------------------------------------------------------------- 2. Whisper solo en esos tramos
def cut_clip(S, media, stream, mode, t0, dur, out_wav):
    pan = 'pan=mono|c0=c2,' if (mode == 'center' and stream['ch'] >= 3) else ''
    cmd = [S.find_tool('ffmpeg'), '-nostdin', '-v', 'error', '-y', '-ss', '%.3f' % t0, '-t', '%.3f' % dur, '-i', media,
           '-map', '0:a:%d' % stream['n'], '-af', pan + 'aresample=16000', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', out_wav]
    r = S.run_cancellable(cmd, capture_output=True, **S._no_window())
    if r.returncode != 0 or not os.path.isfile(out_wav):
        raise RuntimeError("ffmpeg no pudo cortar el trozo: " + r.stderr.decode('utf-8', 'replace')[:200])

_TS = r'(\d+):(\d\d):(\d\d)[.,](\d{1,3})'
def _tsec(g):
    return int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3].ljust(3, '0')[:3]) / 1000.0

def parse_whisper_stdout(text):
    segs = []
    for m in re.finditer(r'\[\s*' + _TS + r'\s*-->\s*' + _TS + r'\s*\]\s*(.*)', text):
        g = m.groups(); segs.append((_tsec(g[0:4]), _tsec(g[4:8]), g[8].strip()))
    return segs

def _decode(b):
    for enc in ('utf-8', 'cp1252'):
        try: return b.decode(enc)
        except Exception: pass
    return b.decode('utf-8', 'replace')

def run_whisper(S, cfg, wavs, lang, log):
    """Un solo main.exe por tandas (el modelo se carga una vez por tanda). Devuelve {wav: [(ini, fin, texto)]} en segundos del clip."""
    exe, model = cfg['whisper_exe'], cfg['whisper_model']
    base = [exe, '-m', model, '-l', lang or 'auto', '-osrt']
    if cfg.get('adapter'): base += ['-gpu', cfg['adapter']]
    if cfg.get('whisper_prompt'): base += ['--prompt', cfg['whisper_prompt']]
    out = {}
    def srt_for(w):
        for cand in (w + '.srt', os.path.splitext(w)[0] + '.srt'):
            if os.path.isfile(cand):
                try:
                    cs, _ = S.load_srt(cand); return [(c['s'], c['e'], c['text'].replace('\n', ' ').strip()) for c in cs]
                except Exception:
                    return None
        return None
    def call(files):
        p = S.run_cancellable(base + files, capture_output=True, cwd=os.path.dirname(exe), timeout=3600, **S._no_window())
        return p.returncode, _decode(p.stdout), _decode(p.stderr)
    CH = 25
    for k in range(0, len(wavs), CH):
        chunk = wavs[k:k + CH]
        rc, so, se = call(chunk)
        miss = [w for w in chunk if srt_for(w) is None]
        if rc != 0 and len(miss) == len(chunk):
            raise RuntimeError("Whisper terminó con error (%s): %s" % (rc, (se or so).strip()[-300:]))
        for w in chunk:
            r = srt_for(w)
            if r is not None: out[w] = r
        if miss and len(chunk) > 1:   # no encuentro los .srt: repito de uno en uno y leo la salida en pantalla
            for w in miss:
                rc, so, se = call([w]); r = srt_for(w)
                out[w] = r if r is not None else parse_whisper_stdout(so)
        elif miss:
            out[miss[0]] = parse_whisper_stdout(so)
    return out

_HALLU = ('ご視聴', 'チャンネル登録', 'ご覧いただき', 'Thanks for watching', 'Thank you for watching', 'Subtitles by', 'Amara.org')
def keep_text(t):
    t = t.strip()
    if not t: return False
    if any(h.lower() in t.lower() for h in _HALLU): return False
    core = re.sub(r'[\s\W_ー〜～…・、。!?！？♪♬♫「」『』（）()\[\]【】-]+', '', t)
    if len(core) < 2: return False
    if re.fullmatch(r'[\[(（【]?(音楽|拍手|笑|Music|music|Applause|MUSIC)[\])）】]?', t): return False
    return True

# Whisper marca los ruidos entre parentesis: (爆発音), (悲鳴), (♪ BGM), [Music]... Eso NO es una frase.
_SND_WRAP = re.compile(r'^\s*[\[(（【〔<＜]\s*[^\[\]()（）【】〔〕<>＜＞]*[\])）】〕>＞]\s*$')
_SND_BARE = re.compile(r'^[\s♪♬♫～〜・]*(BGM|SE|音楽|効果音|拍手|笑い声?|歓声|足音|銃声|爆発音?|悲鳴|music|applause|laughter|silence)[\s♪♬♫～〜・]*$', re.I)
_TAGS = re.compile(r'[\(（][^\)）]{1,8}[\)）]')       # etiquetas de hablante: (男) (女) (男性)...
_GRUNT = re.compile(r'^[んンうウあアぁァぅゥはハふフくクぐグっッー\s…・、。,.!?！？〜～♪]+$')   # quejidos, jadeos y gritos de esfuerzo sin palabras
_SCREAM = re.compile(r'^(?:ひ[ぃいー]+|ひゃ[あー]*|きゃ[あー]*|ぎゃ[あー]*|うわ[あー]*|わ[あー]+|ぎえ[えー]*)[ー…!！?？っッ\s]*$')   # chillidos de susto/dolor sin palabras
def is_sound(t):
    t = (t or '').strip()
    if _SND_WRAP.match(t) or _SND_BARE.match(t): return True
    bare = _TAGS.sub('', t).strip()
    if bare != t and not bare: return True                 # solo etiquetas de hablante
    if bare and len(bare) <= 24 and _SCREAM.match(bare): return True            # ひぃー! きゃー! うわー!
    if bare and len(bare) <= 24 and _GRUNT.match(bare) and re.search(r'[ぁ-んァ-ヶ]', bare): return True   # (男)んんんっ! (男)はぁっ!
    return False

def transcribe_gaps(S, cfg, media, stream, mode, gaps, cues, lang, tmp, log):
    st = sorted(c['s'] for c in cues); en = sorted(c['e'] for c in cues)
    jobs = []
    for i, g in enumerate(gaps):
        # relleno adaptativo: que el clip no se meta en lineas vecinas que ya existen
        j = bisect.bisect_right(en, g['gs'] + 0.01) - 1
        prev_end = en[j] if j >= 0 else -9e9
        k = bisect.bisect_left(st, g['ge'] - 0.01)
        next_start = st[k] if k < len(st) else 9e9
        pb = max(0.1, min(0.7, g['gs'] - prev_end - 0.05)); pa = max(0.1, min(0.7, next_start - g['ge'] - 0.05))
        t0 = max(0.0, g['gs'] - pb); dur = (g['ge'] - g['gs']) + pb + pa
        w = os.path.join(tmp, 'gap_%03d.wav' % i)
        cut_clip(S, media, stream, mode, t0, dur, w)
        jobs.append((g, t0, w))
    t1 = time.time()
    res = run_whisper(S, cfg, [w for _, _, w in jobs], lang, log)
    log("  Whisper: %d trozos en %.0f s" % (len(jobs), time.time() - t1))
    items = []
    for g, t0, w in jobs:
        segs = [(t0 + s, t0 + e, t) for s, e, t in res.get(w, []) if keep_text(t)]
        # solo lo que cae dentro del tramo (descarta voz de lineas vecinas que se haya colado)
        segs = [x for x in segs if g['gs'] - 0.3 <= (x[0] + x[1]) / 2 <= g['ge'] + 0.3]
        # bucles tipicos de alucinacion: el mismo texto muchas veces
        cnt = {}
        for x in segs: cnt[x[2]] = cnt.get(x[2], 0) + 1
        segs = [x for x in segs if cnt[x[2]] < (8 if g.get('forced') else 3)]   # en un tramo pedido a mano se toleran mas repeticiones (rezos, cantos)
        if not segs: continue
        if len(segs) == 1 and (segs[0][1] - segs[0][0]) >= 0.6 * (g['ge'] - g['gs']):
            ts = [(g['gs'] - 0.04, g['ge'] + 0.12, segs[0][2])]
        else:
            ts = []
            for s, e, t in segs:
                ts.append((max(s, g['gs'] - 0.1), min(max(e, s + 0.8), g['ge'] + 0.25), t))
            ts[0] = (max(g['gs'] - 0.04, 0.0), ts[0][1], ts[0][2])
        for s, e, t in ts:
            items.append(dict(s=float(max(0.0, s)), e=float(e), jp=t, wav=w, gs=g['gs'], ge=g['ge'], sound=is_sound(t), forced=bool(g.get('forced'))))
    items.sort(key=lambda x: x['s'])
    for n, it in enumerate(items, 1): it['n'] = n
    return items

def add_context(items, cues, nb=3, na=2):
    dl = sorted([c for c in cues if _clean(c['text'])], key=lambda c: c['s'])
    starts = [c['s'] for c in dl]
    for it in items:
        k = bisect.bisect_left(starts, it['s'])
        it['before'] = [_clean(c['text']) for c in dl[max(0, k - nb):k]]
        it['after'] = [_clean(c['text']) for c in dl[k:k + na]]

# ----------------------------------------------------------------------------- 3. traduccion
def _mmss(t):
    t = int(t); return "%d:%02d:%02d" % (t // 3600, (t % 3600) // 60, t % 60) if t >= 3600 else "%d:%02d" % (t // 60, t % 60)

KIND_CHOICES = {"Automático (japonés = anime, otro idioma = serie/película)": 'auto',
                "Anime / animación": 'anime', "Serie o película (imagen real)": 'live'}
KINDS = {
    'anime': dict(name="anime y series animadas", ctx="Es un anime (o serie animada).",
                  style=("con el lenguaje natural y coloquial típico de los subtítulos de anime y series animadas "
                         "(interjecciones, tono de cada personaje, registro dramático o cómico según la escena). "
                         "Conserva las interjecciones cortas (¡Eh!, ¡Ah!, ¡Vaya!).")),
    'live': dict(name="series y películas de imagen real", ctx="Es una serie o película de imagen real (no es animación).",
                 style=("con el español natural y creíble que hablaría una persona real en esa situación, sin exageraciones "
                        "de estilo anime. Respeta el registro de cada personaje (formal, callejero, técnico, profesional) y mantén "
                        "los insultos y palabrotas del original con su misma fuerza, y NUNCA añadas ninguno que no esté en el original. Evita calcos del idioma original; "
                        "los diálogos deben sonar a conversación real."))}

def resolve_kind(cfg, lang):
    k = (cfg.get('content') or 'auto')
    if k in KINDS: return k
    return 'anime' if lang == 'ja' else 'live'

def _rules(cfg, src, kind='anime'):
    head = "Traduce al %s, " % cfg.get('es_variant', 'español')
    return (head + KINDS.get(kind, KINDS['anime'])['style'] + " "
            "Mantén los nombres propios (personajes, lugares, términos de la serie) exactamente como aparecen en los subtítulos de contexto; "
            "si un nombre no aparece en el contexto, usa su forma habitual en español. "
            "Usa el castellano de España con sus modismos y expresiones coloquiales de aquí (vale, tío, venga, qué fuerte, ni de coña, "
            "estar hecho polvo...), tuteo y 'vosotros' cuando toque, y evita el vocabulario latinoamericano (ordenador y no computadora, "
            "coche y no carro, coger y no agarrar, hablar o charlar y no platicar, etc.). No fuerces los modismos: úsalos solo donde suenen naturales. "
            "Cada traducción debe leerse rápido en pantalla: máximo 2 líneas de unos 42 caracteres. "
            "No traduzcas palabra por palabra: busca el equivalente natural. "
            "No añadas palabrotas, insultos ni énfasis que no estén en el original. No añadas explicaciones, notas ni comillas.")

def build_external_prompt(items, ctx):
    """prompt COMPLETO para pegar tal cual en el chat de cualquier IA (Claude, Gemini, ChatGPT...)"""
    cfg = ctx['cfg']; src = LANG_NAME.get(ctx.get('lang', 'ja'), 'japonés'); n = len(items)
    kind = ctx.get('kind') or resolve_kind(cfg, ctx.get('lang', 'ja')); K = KINDS[kind]
    L = []
    L.append("Eres un traductor profesional de subtítulos de %s (%s → %s). Te voy a pasar frases que se OYEN en el "
             "episodio pero que FALTAN en el subtítulo en español que ya existe, y necesito que las traduzcas para añadirlas." % (K['name'], src, cfg.get('es_variant', 'español')))
    L.append("")
    L.append("CONTEXTO")
    L.append("- Episodio: %s. %s" % (ctx.get('episode', ''), K['ctx']))
    L.append("- Audio original en %s. Las frases están transcritas automáticamente con Whisper, así que puede haber algún error de transcripción: "
             "usa el contexto de la escena para entender lo que se dice." % src)
    L.append("- Debajo de cada frase te pongo las líneas del subtítulo en español que van justo antes y después. Sirven para que veas el tono de la "
             "escena y para que escribas los NOMBRES de personajes y lugares igual que en ese subtítulo.")
    L.append("")
    L.append("REGLAS")
    L.append("1. " + _rules(cfg, src, kind))
    L.append("2. Si la frase en %s parece ruido o un error de transcripción sin sentido (no es una frase real), responde exactamente: [IGNORAR]" % src)
    L.append("3. Si una traducción necesita dos líneas de subtítulo, sepáralas con \" / \".")
    L.append("4. FORMATO DE RESPUESTA: solo la lista, una línea por frase, con el MISMO número que te doy, así:")
    L.append("1. traducción de la frase 1")
    L.append("2. traducción de la frase 2")
    L.append("No escribas nada más (ni saludos, ni explicaciones, ni el japonés).")
    L.append("")
    L.append("FRASES A TRADUCIR (%d)" % n)
    for i, it in enumerate(items, 1):
        L.append("")
        L.append("[%d] minuto %s" % (i, _mmss(it['s'])))
        for b in it.get('before', []): L.append("   subtítulo anterior: %s" % b)
        L.append("   FRASE EN %s: %s" % (src.upper(), it['jp']))
        for b in it.get('after', []): L.append("   subtítulo siguiente: %s" % b)
    return "\n".join(L) + "\n"

def parse_external_response(text, n):
    """lee la respuesta de la IA: lineas '1. texto'. Devuelve {numero: texto | None (=[IGNORAR])}"""
    res = {}; cur = None
    for raw in text.replace('\r', '').split('\n'):
        line = raw.strip()
        if not line or line.startswith('```'): continue
        line = re.sub(r'^[*_>\s]+', '', line); line = line.replace('**', '')
        m = re.match(r'^[\[(]?(\d{1,3})[\])\.:\-–—]+\s*(.*)$', line)
        if m and 1 <= int(m.group(1)) <= n:
            cur = int(m.group(1)); body = m.group(2).strip().strip('"“”«»')
            res[cur] = body
        elif cur is not None and not re.match(r'^[\[(]?\d{1,3}[\])\.:]', line):
            res[cur] = (res[cur] + ' / ' + line.strip('"“”«»')) if res[cur] else line.strip('"“”«»')
    out = {}
    for k, v in res.items():
        if re.search(r'\[?\s*IGNORAR\s*\]?', v, re.I) and len(v) < 16: out[k] = None
        else: out[k] = re.sub(r'\s*/\s*', '\n', v).strip()
    return out

def _http_json(url, payload=None, timeout=180):
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # localhost: sin proxies
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))

def _ollama_chat(cfg, sysmsg, user, num_predict=200):
    payload = dict(model=cfg['ollama_model'], stream=False, think=False, keep_alive='1m',
                   messages=[dict(role='system', content=sysmsg), dict(role='user', content=user)],
                   options=dict(temperature=0.0, num_predict=num_predict))
    try:
        r = _http_json(cfg['ollama_host'].rstrip('/') + '/api/chat', payload)
    except urllib.error.HTTPError as ex:
        if ex.code != 400: raise
        payload.pop('think', None)
        r = _http_json(cfg['ollama_host'].rstrip('/') + '/api/chat', payload)
    return re.sub(r'<think>.*?</think>', '', (r.get('message') or {}).get('content', ''), flags=re.S).strip()

def llm_classify(cfg, it, lang, episode=''):
    # Pregunta cerrada al modelo local: devuelve 'frase', 'sonido' o 'ruido'. Ante la duda, 'frase' (asi nunca se esconde una frase real por error).
    src = LANG_NAME.get(lang, 'japonés')
    sysmsg = ("Eres un revisor de subtítulos. Te paso un texto que Whisper ha sacado del audio de un episodio (idioma: %s). "
              "Responde con UNA sola palabra:\n"
              "FRASE = alguien dice palabras de verdad en voz alta (diálogo, narración, o una interjección corta con significado como 'えっ', 'おうよ', 'なに?').\n"
              "SONIDO = descripción o anotación de un sonido, onomatopeya, música, efecto, golpe, explosión, grito, risa, respiración o ruido; "
              "también quejidos, jadeos, chillidos y gritos de susto, dolor o esfuerzo SIN palabras (んっ, うっ, はぁっ, ああっ, ひぃー, きゃー, うわー). "
              "Ejemplos: (爆発音), ドン!, ガシャーン, (♪ BGM), (悲鳴), (男)んんんっ!, (男)はぁっ!.\n"
              "Las etiquetas entre paréntesis como (男) o (女) solo indican quién habla: ignóralas para decidir.\n"
              "RUIDO = alucinación de Whisper: texto sin sentido, una repetición absurda o una frase de fin de vídeo ('gracias por ver', 'suscríbete').\n"
              "Si dudas entre FRASE y otra cosa, responde FRASE. No escribas nada más que la palabra." % src)
    u = []
    if episode: u.append("Episodio: %s" % episode)
    if it.get('before'): u.append("Subtítulos en español justo antes:\n" + "\n".join("- " + x for x in it['before'][-2:]))
    if it.get('after'): u.append("Subtítulos en español justo después:\n" + "\n".join("- " + x for x in it['after'][:1]))
    u.append("Texto de Whisper a clasificar:\n%s" % it['jp'])
    ans = _ollama_chat(cfg, sysmsg, "\n\n".join(u), num_predict=12).upper()
    if 'SONIDO' in ans: return 'sonido'
    if 'RUIDO' in ans: return 'ruido'
    return 'frase'

def ollama_translate(cfg, it, lang, episode='', kind=None):
    src = LANG_NAME.get(lang, 'japonés'); kind = kind or resolve_kind(cfg, lang)
    sysmsg = "Eres un traductor profesional de subtítulos de %s (%s → español). %s Responde SOLO con la traducción." % (KINDS[kind]['name'], src, _rules(cfg, src, kind))
    u = []
    if episode: u.append("Episodio: %s" % episode)
    if it.get('before'): u.append("Subtítulos en español justo antes:\n" + "\n".join("- " + x for x in it['before']))
    if it.get('after'): u.append("Subtítulos en español justo después:\n" + "\n".join("- " + x for x in it['after']))
    u.append("Frase en %s a traducir (puede tener algún error de transcripción):\n%s" % (src, it['jp']))
    u.append("Si es ruido sin sentido, responde exactamente [IGNORAR]." + (" /no_think" if 'qwen3' in cfg['ollama_model'].lower() else ""))
    payload = dict(model=cfg['ollama_model'], stream=False, think=False, keep_alive='1m',
                   messages=[dict(role='system', content=sysmsg), dict(role='user', content="\n\n".join(u))],
                   options=dict(temperature=0.2, num_predict=200))
    try:
        r = _http_json(cfg['ollama_host'].rstrip('/') + '/api/chat', payload)
    except urllib.error.HTTPError as ex:
        if ex.code != 400: raise
        payload.pop('think', None)      # modelos sin modo "pensar" (Gemma, Llama...): reintento sin esa opcion
        r = _http_json(cfg['ollama_host'].rstrip('/') + '/api/chat', payload)
    txt = (r.get('message') or {}).get('content', '')
    txt = re.sub(r'<think>.*?</think>', '', txt, flags=re.S).strip().strip('"“”«»')
    if re.fullmatch(r'\[?\s*IGNORAR\s*\]?', txt, re.I): return None
    return re.sub(r'\n{2,}', '\n', txt).strip()

# ----------------------------------------------------------------------------- 4. colocar las lineas nuevas
def place_cues(accepted, cues):
    """pone los tiempos finales de cada linea nueva sin pisar las existentes"""
    ex = sorted((c['s'], c['e']) for c in cues)
    starts = [x[0] for x in ex]
    new = []
    for it in sorted(accepted, key=lambda x: x['s']):
        s, e = it['s'], max(it['e'], it['s'] + 0.8)
        k = bisect.bisect_left(starts, s)
        if k < len(ex) and e > ex[k][0] - 0.03: e = max(s + 0.4, ex[k][0] - 0.03)
        if k > 0 and s < ex[k - 1][1] + 0.02 and ex[k - 1][1] + 0.02 < e - 0.3: s = ex[k - 1][1] + 0.02
        new.append(dict(s=float(s), e=float(e), text=it['es'], sign=False, line=None, new=True))
    return new

# ----------------------------------------------------------------------------- 5. informe
def write_report(path, episode, items, applied_ids, note=''):
    L = ["Frases detectadas en el audio que faltaban en el subtítulo", "Episodio: %s" % episode, ""]
    if note: L += [note, ""]
    for it in items:
        mark = "AÑADIDA" if it['n'] in applied_ids else "no añadida"
        L.append("[%d] %s  (%s)%s" % (it['n'], _mmss(it['s']) + "  %.1f-%.1f s" % (it['s'], it['e']), mark, "  [sonido/onomatopeya]" if it.get('sound') else ""))
        L.append("    JP: %s" % it['jp'])
        if it.get('es_local'): L.append("    ES (local): %s" % it['es_local'].replace('\n', ' / '))
        if it.get('es_ext'): L.append("    ES (IA externa): %s" % it['es_ext'].replace('\n', ' / '))
        if it.get('es') and it['n'] in applied_ids: L.append("    ES (final): %s" % it['es'].replace('\n', ' / '))
        L.append("")
    with open(path, 'wb') as f: f.write(b'\xef\xbb\xbf' + "\r\n".join(L).encode('utf-8'))

# ----------------------------------------------------------------------------- 6. orquestador
def process(S, media, stream, mode, a, cues, rep, opts, log, outdir, stem):
    """Devuelve la lista de lineas (con las nuevas anadidas si procede). No toca nada si no faltan frases."""
    cfg = get_cfg(S, opts)
    if not cfg['enabled']: return cues
    if rep['cov_final'] < 0.45 or rep['warnings']:
        log("  frases que faltan: no las busco porque la sincronización no es fiable (revisa los avisos de arriba)"); return cues
    t0 = time.time(); P = opts.get('prog') or (lambda *x: None)
    P(0.52, 'Buscando voz sin subtítulo')
    gaps = find_gaps(S, cues, a, cfg['min_gap'])
    ranges = opts.get('fill_ranges') or []
    if ranges:
        fg_ = forced_gaps(S, cues, a, ranges)
        log("  tramos que has pedido revisar a mano: %s" % ", ".join("%s-%s" % (_mmss(x), _mmss(y)) for x, y in ranges))
        if not fg_: log("     (en esos tramos ya hay subtítulo por todas partes, no hay nada que buscar)")
        else: log("     parte sin subtítulo que se revisa: %s" % ", ".join("%s-%s" % (_mmss(g['gs']), _mmss(g['ge'])) for g in fg_))
        gaps = merge_gaps(gaps, fg_)
    if not gaps:
        P(0.95, 'Sin frases que faltar'); log("  frases que faltan: ninguna (no hay voz sin subtítulo). Whisper no hace falta."); rep['fill'] = dict(gaps=0, found=0, added=0); return cues
    log("  %d tramos con voz y sin subtítulo: compruebo solo esos con Whisper" % len(gaps))
    ok, why = check_whisper(cfg)
    if not ok:
        log("  AVISO: " + why)
        for g in gaps[:20]: log("     voz sin subtítulo en %s - %s" % (_mmss(g['gs']), _mmss(g['ge'])))
        return cues
    lang = LANG_CODE.get((stream.get('lang') or '').lower()) or LANG_CODE.get((opts.get('lang') or '').lower()) or 'auto'
    tmp = tempfile.mkdtemp(prefix='subsync_')
    try:
        P(0.55, 'Whisper: escuchando %d trozos' % len(gaps))
        try:
            items = transcribe_gaps(S, cfg, media, stream, mode, gaps, cues, lang, tmp, log)
        except Exception as ex:
            log("  ERROR con Whisper: %s" % ex); return cues
        if not items:
            log("  Whisper no oyó frases en esos tramos (era música o ruido): no falta nada."); rep['fill'] = dict(gaps=len(gaps), found=0, added=0); return cues
        P(0.70, 'Frases detectadas: %d' % len(items))
        add_context(items, cues)
        parent = os.path.basename(os.path.dirname(os.path.abspath(media)))
        episode = "%s (carpeta: %s)" % (stem, parent)
        mode_tr = cfg['translator']; kind = resolve_kind(cfg, lang)
        log("  tipo de contenido: %s" % KINDS[kind]['name'])
        if cfg.get('classify', True) and mode_tr in ('local', 'both'):
            cand = [it for it in items if not it.get('sound') and not it.get('forced')]   # lo de un tramo pedido a mano no se pone en duda
            if cand:
                t1 = time.time(); ch = 0; ok_cl = True
                for k, it in enumerate(cand):
                    P(0.68 + 0.02 * k / max(1, len(cand)), 'Comprobando voz o sonido %d/%d' % (k + 1, len(cand)))
                    try: r = llm_classify(cfg, it, lang, episode)
                    except Exception as ex:
                        ok_cl = False
                        log("  AVISO: no pude comprobar con el modelo local si son frases o sonidos (%s); las dejo como frases." % ex); break
                    if r != 'frase': it['sound'] = True; it['sound_why'] = r; ch += 1
                if ok_cl: log("  comprobación con el modelo local: %d de %d eran sonido/ruido (%.0f s)" % (ch, len(cand), time.time() - t1))
        real = [it for it in items if not it.get('sound')]; nsnd = len(items) - len(real)
        log("  >> FALTAN %d FRASES:" % len(real))
        for it in real: log("     [%d] %s  %s" % (it['n'], _mmss(it['s']), it['jp']))
        if nsnd: log("  (además hay %d sonidos/onomatopeyas/ruidos, que no son frases: los oculto y no los traduzco; en la ventana puedes verlos)" % nsnd)
        if mode_tr in ('local', 'both') or (cfg['auto'] and mode_tr == 'ia'):
            t1 = time.time(); fails = 0
            for k, it in enumerate(items):
                if it.get('sound'): it['es_local'] = None; continue      # los sonidos no se traducen
                P(0.70 + 0.25 * k / max(1, len(items)), 'Traduciendo %d/%d' % (k + 1, len(items)))
                try: it['es_local'] = ollama_translate(cfg, it, lang, episode, kind)
                except Exception as ex:
                    fails += 1; it['es_local'] = None
                    if fails == 1: log("  AVISO: el modelo local no responde (%s). ¿Está Ollama abierto y el modelo '%s' descargado?" % (ex, cfg['ollama_model']))
                    break
            if any(it.get('es_local') for it in items): log("  traducción local: %.0f s" % (time.time() - t1))
        ctx = dict(cfg=cfg, lang=lang, episode=episode, mode=mode_tr, stem=stem, kind=kind)
        applied = []; ctx['many'] = len(real) > 40
        if ctx['many']:
            log("  AVISO: faltan MUCHAS frases (%d). Lo normal es que sean pocas: es probable que el subtítulo no sea de esta versión del episodio o que le falten bloques enteros." % len(real))
        P(0.95, 'Esperando tu revisión' if not cfg['auto'] else 'Añadiendo frases')
        if cfg['auto']:
            for it in items:
                it['es'] = it.get('es_local') or it.get('es_ext'); it['apply'] = bool(it['es']) and not it.get('sound')
            applied = items
        else:
            if mode_tr in ('ia', 'both'):
                try:
                    pp = os.path.join(outdir, stem + '_prompt_IA.txt'); os.makedirs(outdir, exist_ok=True)
                    with open(pp, 'wb') as f: f.write(b'\xef\xbb\xbf' + build_external_prompt(real, ctx).encode('utf-8'))
                    log("  prompt para la IA externa guardado en: " + pp)
                except Exception: pass
            cb = opts.get('review_cb')
            if cb:
                log("  esperando tu revisión…")
                try: applied = cb(items, ctx) or []
                except Exception as ex: log("  ERROR en la ventana de revisión: %s" % ex); applied = []
            else:
                log("  (sin ventana de revisión: no añado nada; mira el informe y repite con --fill-auto o usa la ventana)")
        acc = [it for it in applied if it.get('apply') and (it.get('es') or '').strip()]
        os.makedirs(outdir, exist_ok=True)
        write_report(os.path.join(outdir, stem + '_frases_detectadas.txt'), episode, items, set(i['n'] for i in acc))
        log("  informe: " + os.path.join(outdir, stem + '_frases_detectadas.txt'))
        if acc:
            new = place_cues(acc, cues)
            cues = sorted(cues + new, key=lambda c: c['s'])
            log("  añadidas %d frases al subtítulo (de %d detectadas)" % (len(new), len(items)))
        else:
            log("  no se añadió ninguna frase")
        rep['fill'] = dict(gaps=len(gaps), found=len(real), added=len(acc))
        return cues
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

# ----------------------------------------------------------------------------- 7. prueba de que todo esta a mano
def selftest(S, cfg, log):
    ok, why = check_whisper(cfg)
    if ok:
        log("Whisper: OK\n   %s\n   modelo: %s (%.1f GB)" % (cfg['whisper_exe'], os.path.basename(cfg['whisper_model']), os.path.getsize(cfg['whisper_model']) / 1e9))
        try:
            p = subprocess.run([cfg['whisper_exe'], '-la'], capture_output=True, timeout=60, cwd=os.path.dirname(cfg['whisper_exe']), **S._no_window())
            t = _decode(p.stdout).strip()
            log("   adaptadores gráficos que ve Whisper:\n      " + t.replace('\n', '\n      ') if t else "   (no pude listar adaptadores)")
        except Exception as ex:
            log("   no pude ejecutar main.exe: %s" % ex)
    else:
        log("Whisper: " + why)
    try:
        r = _http_json(cfg['ollama_host'].rstrip('/') + '/api/tags', None, 10)
        names = [m.get('name', '') for m in r.get('models', [])]
        has = any(n == cfg['ollama_model'] or n.split(':')[0] == cfg['ollama_model'] for n in names)
        log("Ollama: responde. Modelos: %s" % (', '.join(names) or '(ninguno)'))
        if not has: log("   falta el modelo '%s'. Descárgalo con:  ollama pull %s" % (cfg['ollama_model'], cfg['ollama_model']))
    except Exception as ex:
        log("Ollama: no responde en %s (%s). Es opcional: sin él puedes usar la IA externa por copiar y pegar." % (cfg['ollama_host'], ex))

# ----------------------------------------------------------------------------- 8. revision en terminal (linea de comandos)
def cli_review(items, ctx):
    print("\nFrases que faltan en %s:" % ctx.get('stem', ''))
    nsnd = sum(1 for it in items if it.get('sound'))
    if nsnd: print("  (%d sonidos/onomatopeyas ocultos; no son frases)" % nsnd)
    for it in items:
        if it.get('sound'): continue
        print("  [%d] %s  JP: %s" % (it['n'], _mmss(it['s']), it['jp']))
        print("        ES: %s" % ((it.get('es_local') or '(sin traducción)').replace('\n', ' / ')))
    if not sys.stdin or not sys.stdin.isatty(): return []
    r = input("¿Cuáles añado? (números separados por comas, 'todas', o Enter para ninguna): ").strip().lower()
    if not r: return []
    sel = set(it['n'] for it in items) if r.startswith('t') else set(int(x) for x in re.findall(r'\d+', r))
    out = []
    for it in items:
        if it['n'] in sel:
            it['es'] = it.get('es_local') or it.get('es_ext') or ''; it['apply'] = bool(it['es']); out.append(it)
    return out

# ----------------------------------------------------------------------------- 9. ventanas (tkinter)
def _box(col, text, buttons, parent=None, title="SubSync Local"):
    # Ventana de aviso con el tema oscuro del programa (las de Windows no se pueden tematizar).
    # buttons: ((texto, valor), ...). El primero es el principal (Intro); Esc o la cruz devuelven el valor del ultimo.
    import tkinter as tk
    from tkinter import ttk
    d = tk.Toplevel(parent); d.title(title); d.configure(bg=col['bg']); d.resizable(False, False)
    res = {'v': buttons[-1][1]}
    try:
        if parent is not None: d.transient(parent)
    except Exception: pass
    f = ttk.Frame(d, padding=18); f.pack(fill='both', expand=True)
    ttk.Label(f, text=text, justify='left', wraplength=480, font=('Segoe UI', 10)).pack(anchor='w')
    bt = ttk.Frame(f); bt.pack(fill='x', pady=(18, 0))
    def pick(v): res['v'] = v; d.destroy()
    first = None
    for k, (lab, val) in enumerate(buttons):
        b = ttk.Button(bt, text=lab, command=lambda v=val: pick(v), style='Accent.TButton' if k == 0 else 'TButton')
        b.pack(side='right', padx=(8, 0))
        if k == 0: first = b
    d.bind('<Return>', lambda e: pick(buttons[0][1])); d.bind('<Escape>', lambda e: pick(buttons[-1][1]))
    d.protocol("WM_DELETE_WINDOW", lambda: pick(buttons[-1][1]))
    d.update_idletasks()
    try:
        px, py, pw, ph = parent.winfo_rootx(), parent.winfo_rooty(), parent.winfo_width(), parent.winfo_height()
        x = px + (pw - d.winfo_reqwidth()) // 2; y = py + (ph - d.winfo_reqheight()) // 2
        d.geometry("+%d+%d" % (max(0, x), max(0, y)))
    except Exception: pass
    try: d.grab_set()
    except Exception: pass
    try: first.focus_set()
    except Exception: pass
    d.wait_window(d)
    return res['v']

def _info(col, text, parent=None):
    _box(col, text, (("Aceptar", True),), parent)

def _ask(col, text, parent=None, yes="Sí", no="No"):
    return bool(_box(col, text, ((yes, True), (no, False)), parent))

def review_dialog(parent, items, ctx, col):
    """Ventana de revision. Devuelve la lista de items (con apply y es) o [] si cancelas."""
    import tkinter as tk
    from tkinter import ttk, messagebox
    mode = ctx.get('mode', 'local')
    for it in items:
        it['apply'] = bool(it.get('es_local')) if mode != 'ia' else False
        it['es'] = it.get('es_local') or ''
        if it.get('sound'): it['apply'] = False
    real_n = sum(1 for it in items if not it.get('sound')); snd_n = len(items) - real_n
    start_show = False
    if real_n == 0 and items:
        if not _ask(col, "Whisper no ha encontrado frases reales que falten: solo %d sonidos u onomatopeyas "
                    "(explosiones, gritos, música...).\n\n¿Quieres verlos por si alguno es una frase de verdad?" % snd_n, parent=parent,
                    yes="Verlos", no="No, no hace falta"):
            return []
        start_show = True
    st = ttk.Style(parent)
    st.configure('Rev.Treeview', background=col['panel'], fieldbackground=col['panel'], foreground=col['fg'], rowheight=26,
                 borderwidth=0, font=('Segoe UI', 10))
    st.map('Rev.Treeview', background=[('selected', col['accent'])], foreground=[('selected', '#ffffff')])
    st.configure('Rev.Treeview.Heading', background=col['field'], foreground=col['fg'], relief='flat', font=('Segoe UI', 10, 'bold'), padding=(6, 5))
    st.map('Rev.Treeview.Heading', background=[('active', col['field'])])
    dlg = tk.Toplevel(parent); dlg.title("SubSync Local - frases que faltan"); dlg.configure(bg=col['bg'])
    sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
    dlg.geometry("%dx%d" % (min(1150, sw - 80), min(760, sh - 100))); dlg.minsize(760, 520)
    try: dlg.transient(parent)
    except Exception: pass
    try: dlg.grab_set()
    except Exception: pass
    res = {'out': []}
    top = ttk.Frame(dlg, padding=10); top.pack(fill='both', expand=True)
    head = ttk.Label(top, text="Faltan %d frases en %s. Marca las que quieres añadir (clic en la casilla o barra espaciadora), escúchalas "
                     "y corrige la traducción si hace falta. Al terminar pulsa «Añadir las marcadas»." % (real_n, ctx.get('stem', '')), justify='left')
    head.pack(side='top', anchor='w', fill='x')
    top.bind('<Configure>', lambda e: head.configure(wraplength=max(300, e.width - 30)))
    if ctx.get('many'):
        tk.Label(top, text="⚠ Son muchas frases. Lo normal es que falten pocas: el subtítulo puede no ser de esta versión del episodio "
                 "o faltarle bloques enteros. Revisa bien antes de añadir.", bg=col['bg'], fg='#f0b232', justify='left', anchor='w',
                 wraplength=1000, font=('Segoe UI', 10, 'bold')).pack(side='top', anchor='w', fill='x', pady=(4, 0))
    show_snd = tk.BooleanVar(value=start_show)
    if snd_n:
        sf = ttk.Frame(top); sf.pack(side='top', anchor='w', fill='x', pady=(6, 0))
        ttk.Checkbutton(sf, text="Whisper también detectó %d sonidos/onomatopeyas (no son frases, por eso están ocultos). Mostrarlos por si acaso se ha confundido" % snd_n,
                        variable=show_snd, command=lambda: rebuild()).pack(side='left')
    # ---- zonas inferiores (se empaquetan primero para que SIEMPRE se vean los botones)
    act = ttk.Frame(top); act.pack(side='bottom', fill='x', pady=(10, 0))
    prm = ttk.Frame(top); prm.pack(side='bottom', fill='x', pady=(8, 0))
    tools = ttk.Frame(top); tools.pack(side='bottom', fill='x', pady=(2, 0))
    ed = ttk.Frame(top); ed.pack(side='bottom', fill='x', pady=(8, 0))
    fr = ttk.Frame(top); fr.pack(side='top', fill='both', expand=True, pady=(8, 0))
    tree = ttk.Treeview(fr, columns=('ok', 't', 'jp', 'es'), show='headings', selectmode='browse', style='Rev.Treeview')
    for c, t, w, stc in (('ok', '✓', 46, False), ('t', 'Minuto', 84, False), ('jp', 'Original (lo que se oye)', 420, True), ('es', 'Traducción que se añadirá', 520, True)):
        tree.heading(c, text=t); tree.column(c, width=w, minwidth=w if not stc else 120, stretch=stc, anchor='center' if c in ('ok', 't') else 'w')
    sb = ttk.Scrollbar(fr, orient='vertical', command=tree.yview); tree.configure(yscrollcommand=sb.set)
    tree.pack(side='left', fill='both', expand=True); sb.pack(side='right', fill='y')
    cnt = tk.StringVar(); msg = tk.StringVar()
    def visible():
        return [i for i, it in enumerate(items) if show_snd.get() or not it.get('sound')]
    def upd_count():
        v = visible()
        cnt.set("%d marcadas de %d" % (sum(1 for i in v if items[i]['apply'] and (items[i]['es'] or '').strip()), len(v)))
    tree.tag_configure('on', background='#1f3a2a', foreground='#e6ffee')
    tree.tag_configure('off', background=col['panel'], foreground='#8a8f98')
    tree.tag_configure('snd', background=col['panel'], foreground='#c9a24b')
    def refresh(i):
        it = items[i]
        if not tree.exists(str(i)): return
        es_txt = (it['es'] or '').replace('\n', ' / ')
        if not es_txt and it.get('sound'): es_txt = '(sonido, no se traduce)'
        tree.item(str(i), values=('✔' if it['apply'] else '☐', _mmss(it['s']), it['jp'], es_txt),
                  tags=('on' if it['apply'] else ('snd' if it.get('sound') else 'off'),)); upd_count()
    def rebuild():
        keep = state.get('sel')
        for r in tree.get_children(): tree.delete(r)
        for i in visible():
            tree.insert('', 'end', iid=str(i), values=('', '', '', '')); refresh(i)
        vs = visible()
        if keep is not None and keep in vs: tree.selection_set(str(keep))
        elif vs: tree.selection_set(str(vs[0]))
        else: state['sel'] = None; on_select()
        upd_count()
    state = {'sel': None, 'loading': False, 'prompt_items': None}
    ttk.Label(ed, text="Original detectado por Whisper (puedes seleccionarlo y copiarlo con Ctrl+C):").pack(anchor='w')
    obox = tk.Text(ed, height=2, wrap='word', bg=col['panel'], fg=col['fg'], insertbackground=col['fg'], relief='flat',
                   highlightthickness=1, highlightbackground='#3f4147', highlightcolor=col['accent'], font=('Segoe UI', 11), padx=6, pady=4)
    obox.pack(fill='x', pady=(2, 8))
    def _ro(e):       # solo lectura, pero se puede seleccionar y copiar
        if (e.state & 0x4) and e.keysym.lower() in ('c', 'insert'): return None
        if e.keysym in ('Left', 'Right', 'Up', 'Down', 'Home', 'End', 'Prior', 'Next', 'Shift_L', 'Shift_R', 'Control_L', 'Control_R'): return None
        return 'break'
    obox.bind('<Key>', _ro)
    obox.bind('<Control-a>', lambda e: (obox.tag_add('sel', '1.0', 'end-1c'), 'break')[1])
    ttk.Label(ed, text="Traducción de la frase seleccionada (editable; ' / ' = salto de línea):").pack(anchor='w')
    box = tk.Text(ed, height=3, wrap='word', bg=col['panel'], fg=col['fg'], insertbackground=col['fg'], relief='flat',
                  highlightthickness=1, highlightbackground='#3f4147', highlightcolor=col['accent'], font=('Segoe UI', 11), padx=6, pady=4)
    box.pack(fill='x', pady=(2, 0))
    def sel_index():
        s_ = tree.selection(); return int(s_[0]) if s_ else None
    def on_select(_e=None):
        i = sel_index(); state['sel'] = i
        obox.delete('1.0', 'end')
        if i is None:
            state['loading'] = True; box.delete('1.0', 'end'); state['loading'] = False; return
        obox.insert('1.0', items[i]['jp'])
        state['loading'] = True; box.delete('1.0', 'end'); box.insert('1.0', (items[i]['es'] or '').replace('\n', ' / ')); state['loading'] = False
    def on_edit(_e=None):
        i = state['sel']
        if i is None or state['loading']: return
        items[i]['es'] = box.get('1.0', 'end').strip().replace(' / ', '\n')
        if items[i]['es'] and not items[i]['apply']: items[i]['apply'] = True
        refresh(i)
    def toggle(i):
        items[i]['apply'] = not items[i]['apply']; refresh(i)
    def on_click(e):
        if tree.identify_region(e.x, e.y) != 'cell': return
        row = tree.identify_row(e.y)
        if row and tree.identify_column(e.x) == '#1': toggle(int(row))
    def on_space(_e=None):
        i = sel_index()
        if i is not None: toggle(i)
        return 'break'
    tree.bind('<<TreeviewSelect>>', on_select); tree.bind('<Button-1>', on_click, add='+'); tree.bind('<space>', on_space); box.bind('<KeyRelease>', on_edit)
    def listen():
        i = state['sel']
        if i is None: return
        try:
            if os.name == 'nt': os.startfile(items[i]['wav'])
            else: subprocess.Popen(['xdg-open', items[i]['wav']])
        except Exception as ex: _info(col, "No pude reproducir el trozo: %s" % ex, parent=dlg)
    def use(which):
        i = state['sel']
        if i is None: return
        v = items[i].get(which)
        if not v: _info(col, "Esa traducción no está disponible para esta frase.", parent=dlg); return
        same = (items[i]['es'] == v)
        items[i]['es'] = v; items[i]['apply'] = True; refresh(i); on_select()
        msg.set("Frase %d: ya tenía esa traducción (marcada)." % (i + 1) if same else "Frase %d: traducción %s puesta y marcada." % (i + 1, "del modelo local" if which == 'es_local' else "de la IA"))
    def copy_orig():
        i = state['sel']
        if i is None: return 'break'
        dlg.clipboard_clear(); dlg.clipboard_append(items[i]['jp']); dlg.update(); msg.set("Original copiado."); return 'break'
    def copy_all():
        txt = "\n".join("%s  %s" % (_mmss(items[i]['s']), items[i]['jp']) for i in visible())
        dlg.clipboard_clear(); dlg.clipboard_append(txt); dlg.update(); msg.set("Copiados %d originales." % len(visible()))
    tree.bind('<Control-c>', lambda e: copy_orig())
    def mark(v):
        for i in visible():
            it = items[i]
            it['apply'] = v and bool((it['es'] or '').strip()); refresh(i)
        tree.update_idletasks(); msg.set("Todas marcadas." if v else "Todas desmarcadas.")
    ttk.Button(tools, text="▶ Escuchar", command=listen).pack(side='left')
    ttk.Button(tools, text="Copiar original", command=copy_orig).pack(side='left', padx=(6, 0))
    ttk.Button(tools, text="Copiar todos los originales", command=copy_all).pack(side='left', padx=(6, 0))
    ttk.Button(tools, text="Volver a la traducción local", command=lambda: use('es_local')).pack(side='left', padx=(6, 0))
    ttk.Button(tools, text="Usar la traducción de la IA", command=lambda: use('es_ext')).pack(side='left', padx=(6, 0))
    ttk.Button(tools, text="Marcar todas", command=lambda: mark(True)).pack(side='right')
    ttk.Button(tools, text="Desmarcar todas", command=lambda: mark(False)).pack(side='right', padx=(0, 6))
    # ---- prompt para IA externa
    kv = tk.StringVar(value=next((k for k, v in KIND_CHOICES.items() if v == ctx.get('kind')), list(KIND_CHOICES)[1]))
    def on_kind(_e=None):
        ctx['kind'] = KIND_CHOICES.get(kv.get(), ctx.get('kind'))
    def copy_prompt():
        try:
            vis = [items[i] for i in visible()]; state['prompt_items'] = vis
            txt = build_external_prompt(vis, ctx)
            dlg.clipboard_clear(); dlg.clipboard_append(txt); dlg.update()
            _info(col, "Prompt copiado al portapapeles.\n\nPégalo tal cual en el chat de la IA (Claude, Gemini, ChatGPT…). "
                                "Cuando te conteste, copia su respuesta y pulsa 'Pegar respuesta de la IA'.", parent=dlg)
        except Exception as ex: _info(col, str(ex), parent=dlg)
    def paste_answer():
        d2 = tk.Toplevel(dlg); d2.title("Respuesta de la IA"); d2.configure(bg=col['bg']); d2.geometry("760x460"); d2.transient(dlg)
        try: d2.grab_set()
        except Exception: pass
        bt = ttk.Frame(d2); bt.pack(side='bottom', fill='x', padx=10, pady=8)
        ttk.Label(d2, text="Pega aquí la respuesta de la IA (la lista numerada):").pack(side='top', anchor='w', padx=10, pady=(10, 4))
        t = tk.Text(d2, wrap='word', bg=col['panel'], fg=col['fg'], insertbackground=col['fg'], relief='flat', font=('Segoe UI', 10), padx=6, pady=4)
        t.pack(side='top', fill='both', expand=True, padx=10)
        try: t.insert('1.0', d2.clipboard_get())
        except Exception: pass
        def ok():
            vis = state.get('prompt_items') or [items[i] for i in visible()]
            got = parse_external_response(t.get('1.0', 'end'), len(vis))
            if not got:
                _info(col, "No he encontrado líneas numeradas ('1. texto'). Revisa lo que has pegado.", parent=d2); return
            n = 0
            for k, v in got.items():
                it = vis[k - 1]
                if v is None: it['es_ext'] = ''; it['apply'] = False; continue
                it['es_ext'] = v; n += 1
                if mode == 'ia' or not it.get('es_local') or not it['es']:
                    it['es'] = v; it['apply'] = True
            for i in range(len(items)): refresh(i)
            on_select(); d2.destroy()
            _info(col, "Leídas %d traducciones de la IA.%s" % (n, "\nCuando hay dos versiones, selecciona la frase y elige cuál usar." if mode == 'both' else ""), parent=dlg)
        ttk.Button(bt, text="Cancelar", command=d2.destroy).pack(side='right')
        ttk.Button(bt, text="Aceptar", style='Accent.TButton', command=ok).pack(side='right', padx=(0, 8))
    ttk.Label(prm, text="Prompt para:").pack(side='left', padx=(0, 4))
    kc = ttk.Combobox(prm, textvariable=kv, state='readonly', values=[k for k in KIND_CHOICES if KIND_CHOICES[k] != 'auto'], width=28)
    kc.pack(side='left', padx=(0, 8)); kc.bind('<<ComboboxSelected>>', on_kind)
    ttk.Button(prm, text="1) Copiar prompt para la IA", command=copy_prompt).pack(side='left')
    ttk.Button(prm, text="2) Pegar respuesta de la IA…", command=paste_answer).pack(side='left', padx=(6, 0))
    # ---- botones finales (siempre visibles)
    def apply_():
        res['out'] = [it for it in items if it['apply'] and (it['es'] or '').strip()]; dlg.destroy()
    def cancel():
        res['out'] = []; dlg.destroy()
    ttk.Label(act, textvariable=cnt, style='Muted.TLabel').pack(side='left')
    ttk.Label(act, textvariable=msg, style='Muted.TLabel').pack(side='left', padx=(14, 0))
    ttk.Button(act, text="Cancelar (no añadir nada)", command=cancel).pack(side='right')
    ttk.Button(act, text="Añadir las marcadas", style='Accent.TButton', command=apply_).pack(side='right', padx=(0, 8))
    dlg.protocol("WM_DELETE_WINDOW", cancel)
    rebuild()
    if tree.selection(): tree.focus(tree.selection()[0])
    on_select()
    parent.wait_window(dlg)
    return res['out']

def settings_dialog(parent, S, cfg, col):
    """Ajustes de Whisper y traduccion. Guarda en subsync_config.json y devuelve la config."""
    import tkinter as tk
    from tkinter import ttk, filedialog
    dlg = tk.Toplevel(parent); dlg.title("Ajustes - frases que faltan"); dlg.configure(bg=col['bg']); dlg.geometry("760x560"); dlg.transient(parent)
    try: dlg.grab_set()
    except Exception: pass
    f = ttk.Frame(dlg, padding=12); f.pack(fill='both', expand=True); f.columnconfigure(1, weight=1)
    vars_ = {k: tk.StringVar(value=str(cfg.get(k, ''))) for k in ('whisper_exe', 'whisper_model', 'adapter', 'ollama_host', 'ollama_model', 'es_variant', 'min_gap')}
    rows = [("Whisper (main.exe)", 'whisper_exe', 'exe'), ("Modelo de Whisper (.bin)", 'whisper_model', 'bin'),
            ("Adaptador gráfico (vacío = por defecto)", 'adapter', None), ("Servidor Ollama", 'ollama_host', None),
            ("Modelo de Ollama", 'ollama_model', None), ("Español de destino", 'es_variant', None),
            ("Tramo mínimo con voz sin subtítulo (s)", 'min_gap', None)]
    for r, (lab, key, kind) in enumerate(rows):
        ttk.Label(f, text=lab).grid(row=r, column=0, sticky='w', pady=4, padx=(0, 8))
        if key == 'ollama_model':
            cbm = ttk.Combobox(f, textvariable=vars_[key]); cbm.grid(row=r, column=1, sticky='ew', pady=4)
            def load_models(cbm=cbm):
                try:
                    host = vars_['ollama_host'].get().strip().rstrip('/') or DEFAULTS['ollama_host']
                    names = sorted(m.get('name', '') for m in _http_json(host + '/api/tags', None, 4).get('models', []) if m.get('name'))
                    cbm['values'] = names
                    out.delete('1.0', 'end'); out.insert('end', "Modelos encontrados en Ollama (%d): elige uno en el desplegable.\n" % len(names))
                except Exception as ex:
                    out.delete('1.0', 'end'); out.insert('end', "No pude leer la lista de modelos (¿está Ollama abierto?): %s\nPuedes escribir el nombre a mano.\n" % ex)
            ttk.Button(f, text="Actualizar lista", command=load_models).grid(row=r, column=2, padx=(6, 0))
            models_loader = load_models
            continue
        ttk.Entry(f, textvariable=vars_[key]).grid(row=r, column=1, sticky='ew', pady=4)
        if kind:
            def pick(key=key, kind=kind):
                p = filedialog.askopenfilename(parent=dlg, filetypes=[("Whisper main.exe", "*.exe")] if kind == 'exe' else [("Modelo ggml", "*.bin")])
                if p: vars_[key].set(p)
            ttk.Button(f, text="Buscar…", command=pick).grid(row=r, column=2, padx=(6, 0))
    out = tk.Text(f, height=12, wrap='word', bg=col['panel'], fg=col['fg'], relief='flat', font=('Consolas', 9), padx=6, pady=4)
    out.grid(row=len(rows) + 1, column=0, columnspan=3, sticky='nsew', pady=(10, 0)); f.rowconfigure(len(rows) + 1, weight=1)
    try: models_loader()
    except Exception: pass
    v_cls = tk.BooleanVar(value=bool(cfg.get('classify', True)))
    ttk.Checkbutton(f, text="Comprobar con el modelo local si lo detectado es voz real o un sonido/ruido (más fiable; añade ~1 s por línea dudosa)",
                    variable=v_cls).grid(row=len(rows) + 2, column=0, columnspan=3, sticky='w', pady=(8, 0))
    def collect_cfg():
        c = dict(cfg); c['classify'] = bool(v_cls.get())
        for k, v in vars_.items(): c[k] = v.get().strip()
        try: c['min_gap'] = float(str(c['min_gap']).replace(',', '.'))
        except Exception: c['min_gap'] = 0.8
        return c
    def test():
        import threading
        c = collect_cfg(); out.delete('1.0', 'end'); out.insert('end', "Probando…\n")
        def work():
            lines = []
            try: selftest(S, c, lines.append)
            except Exception as ex: lines.append("Error: %s" % ex)
            dlg.after(0, lambda: (out.delete('1.0', 'end'), out.insert('end', "\n".join(lines))))
        threading.Thread(target=work, daemon=True).start()
    def save():
        c = collect_cfg(); cfg.update(c); save_cfg(S, cfg); dlg.destroy()
    bt = ttk.Frame(f); bt.grid(row=len(rows), column=0, columnspan=3, sticky='ew', pady=(10, 0))
    ttk.Button(bt, text="Probar Whisper y Ollama", command=test).pack(side='left')
    ttk.Button(bt, text="Cancelar", command=dlg.destroy).pack(side='right')
    ttk.Button(bt, text="Guardar", style='Accent.TButton', command=save).pack(side='right', padx=(0, 8))
    parent.wait_window(dlg)
    return cfg
