#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SubSync Local - sincroniza subtitulos (SRT/ASS) contra el AUDIO real de un video, en local.

Metodo (sin transcripcion ni internet):
  1. ffmpeg extrae el audio (canal central si es 5.1) y se calcula, cada 10 ms, donde hay voz.
  2. Busqueda global de desfase + cambio de velocidad (25<->23.976<->24 fps, etc.), hasta +-5 min.
  3. Deteccion de saltos/cortes y derivas por tramos (Viterbi), en dos niveles (grueso y fino).
  4. Ajuste de inicio/fin de cada linea al arranque/final real de la voz.
Solo necesita: Python 3.8+, numpy y ffmpeg/ffprobe (en el PATH o junto a este script).
"""
import os, sys, re, math, shutil, subprocess, json, time, argparse, threading, queue
try:
    import numpy as np
except ImportError:
    def _instalar_numpy():
        """intenta 'pip install numpy' con ESTE mismo Python y reinicia el programa"""
        try:
            import tkinter as _tk
            from tkinter import messagebox as _mb
            _r = _tk.Tk(); _r.withdraw()
            _ok = _mb.askyesno("SubSync Local", "A este Python le falta numpy:\n%s\n\n¿Instalarlo ahora? (necesita internet, tarda unos segundos)" % sys.executable)
            if not _ok:
                _r.destroy(); return
            _p = subprocess.run([sys.executable, '-m', 'pip', 'install', 'numpy'], capture_output=True, text=True,
                                creationflags=(0x08000000 if os.name == 'nt' else 0))
            if _p.returncode == 0:
                _mb.showinfo("SubSync Local", "numpy instalado. Se reinicia el programa."); _r.destroy()
                os.execv(sys.executable, [sys.executable] + sys.argv)
            else:
                _mb.showerror("SubSync Local", "No se pudo instalar numpy:\n\n" + (_p.stderr or _p.stdout)[-700:]
                              + "\n\nAbre una consola y ejecuta:\n\"%s\" -m pip install numpy" % sys.executable); _r.destroy()
        except Exception:
            print("Falta numpy. Ejecuta:  \"%s\" -m pip install numpy" % sys.executable)
    _instalar_numpy()
    sys.exit(1)

SUB_EXT = ('.srt', '.ass', '.ssa')
MEDIA_EXT = ('.mkv', '.mka', '.mp4', '.m4v', '.avi', '.mov', '.webm', '.ts', '.m2ts', '.mp3', '.flac',
             '.aac', '.ac3', '.eac3', '.opus', '.ogg', '.wav', '.wmv', '.m4a')
FR = 0.01  # 10 ms
RATIOS = [1.0, 24/23.976, 23.976/24, 25/23.976, 23.976/25, 25/24, 24/25, 30/29.97, 29.97/30]

# ----------------------------------------------------------------------------- subtitulos
def read_text(path):
    raw = open(path, 'rb').read()
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return raw.decode('utf-16'), 'utf-16'
    for enc in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            pass
    return raw.decode('latin-1', 'replace'), 'latin-1'

def _ms(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, '0')[:3]) / 1000.0

def fmt_srt(t):
    ms = int(round(max(0.0, t) * 1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)

def fmt_ass(t):
    cs = int(round(max(0.0, t) * 100)); h, cs = divmod(cs, 360000); m, cs = divmod(cs, 6000); s, cs = divmod(cs, 100)
    return "%d:%02d:%02d.%02d" % (h, m, s, cs)

def parse_ass_time(t):
    h, m, s = t.strip().split(':'); return int(h) * 3600 + int(m) * 60 + float(s)

# cue = dict(s, e, text, sign, line)
def load_srt(path):
    text, enc = read_text(path); text = text.replace('\r\n', '\n').replace('\r', '\n')
    rx = re.compile(r'(\d+):(\d\d):(\d\d)[,.](\d{1,3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{1,3})[^\n]*\n(.*?)(?:\n\s*\n|\Z)', re.S)
    cues = []
    for m in rx.finditer(text):
        g = m.groups()
        cues.append(dict(s=_ms(*g[0:4]), e=_ms(*g[4:8]), text=g[8].strip('\n'), sign=False, line=None))
    return cues, dict(kind='srt', enc=enc)

def load_ass(path):
    text, enc = read_text(path); text = text.replace('\r\n', '\n')
    lines = text.split('\n'); fmt = None; cues = []; in_ev = False
    for li, l in enumerate(lines):
        if l.strip().lower().startswith('[events]'): in_ev = True; continue
        if in_ev and l.lower().startswith('format:'):
            fmt = [x.strip().lower() for x in l.split(':', 1)[1].split(',')]; continue
        if in_ev and l.startswith('Dialogue:'):
            if fmt is None: fmt = ['layer', 'start', 'end', 'style', 'name', 'marginl', 'marginr', 'marginv', 'effect', 'text']
            p = l[len('Dialogue:'):].lstrip().split(',', len(fmt) - 1)
            if len(p) < len(fmt): continue
            d = dict(zip(fmt, p)); raw = d['text']
            sign = ('\\pos' in raw) or ('\\move' in raw) or ('\\p1' in raw)
            t = re.sub(r'\{[^}]*\}', '', raw).replace('\\N', '\n').replace('\\n', '\n').strip()
            cues.append(dict(s=parse_ass_time(d['start']), e=parse_ass_time(d['end']), text=t, sign=sign, line=li))
    cues.sort(key=lambda c: c['s'])
    return cues, dict(kind='ass', enc=enc, lines=lines, fmt=fmt)

def load_subs(path):
    return load_ass(path) if path.lower().endswith(('.ass', '.ssa')) else load_srt(path)

def write_srt(path, cues):
    out = []
    n = 0
    for c in cues:
        if not c['text'].strip(): continue
        n += 1; out.append("%d\n%s --> %s\n%s\n" % (n, fmt_srt(c['s']), fmt_srt(c['e']), c['text']))
    data = '\n'.join(out).replace('\n', '\r\n')
    with open(path, 'wb') as f: f.write(b'\xef\xbb\xbf' + data.encode('utf-8'))

def write_ass_like(path, cues, meta):
    lines = list(meta['lines']); fmt = meta['fmt']; si = fmt.index('start'); ei = fmt.index('end')
    new = []
    for c in cues:
        if c.get('line') is None: new.append(c); continue
        li = c['line']; p = lines[li][len('Dialogue:'):].lstrip().split(',', len(fmt) - 1)
        p[si] = fmt_ass(c['s']); p[ei] = fmt_ass(c['e'])
        lines[li] = 'Dialogue: ' + ','.join(p)
    if new:   # lineas anadidas: se clona una linea de dialogo normal (mismo estilo) y se cambia tiempo y texto
        ti = fmt.index('text'); tpl = None
        for c in cues:
            if c.get('line') is not None and not c.get('sign'): tpl = c['line']; break
        if tpl is None: tpl = next((c['line'] for c in cues if c.get('line') is not None), None)
        if tpl is not None:
            base = lines[tpl][len('Dialogue:'):].lstrip().split(',', len(fmt) - 1)
            last = max([c['line'] for c in cues if c.get('line') is not None])
            ins = []
            for c in new:
                p = list(base); p[si] = fmt_ass(c['s']); p[ei] = fmt_ass(c['e']); p[ti] = srt_to_ass_text(c['text'])
                ins.append('Dialogue: ' + ','.join(p))
            lines[last + 1:last + 1] = ins
    with open(path, 'wb') as f: f.write(b'\xef\xbb\xbf' + '\r\n'.join(lines).encode('utf-8'))

def srt_to_ass_text(t):
    """texto de SRT -> texto de ASS: saltos de linea y etiquetas <i> <b> <u> <font color>"""
    t = t.replace('\r', '').replace('\n', '\\N')
    for tag, on, off in (('i', '{\\i1}', '{\\i0}'), ('b', '{\\b1}', '{\\b0}'), ('u', '{\\u1}', '{\\u0}')):
        t = re.sub(r'<%s>' % tag, lambda m, on=on: on, t, flags=re.I)
        t = re.sub(r'</%s>' % tag, lambda m, off=off: off, t, flags=re.I)
    def col(m):
        h = m.group(1).lstrip('#')
        if len(h) == 6: return '{\\c&H%s%s%s&}' % (h[4:6], h[2:4], h[0:2])
        return ''
    t = re.sub(r'<font[^>]*?color\s*=\s*["\']?(#?[0-9a-fA-F]{6})["\']?[^>]*>', col, t, flags=re.I)
    t = re.sub(r'</font>', lambda m: '{\\c}', t, flags=re.I)
    t = re.sub(r'<[^>]+>', '', t)
    return t

def write_ass_new(path, cues):
    """ASS nuevo a partir de un SRT, con tu estilo Default (HP Simplified 85 negrita, contorno azul oscuro 3.5, sin sombra, margen vertical 97)"""
    head = ["[Script Info]", "; Generado por SubSync Local", "ScriptType: v4.00+", "PlayResX: 1920", "PlayResY: 1080",
            "WrapStyle: 0", "ScaledBorderAndShadow: yes", "YCbCr Matrix: None", "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            "Style: Default,HP Simplified,85,&H00FFFFFF,&H000000FF,&H00380301,&H00000000,-1,0,0,0,100,100,0,0,1,3.5,0,2,40,40,97,0", "",
            "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    ev = ["Dialogue: 0,%s,%s,Default,,0,0,0,,%s" % (fmt_ass(c['s']), fmt_ass(c['e']), srt_to_ass_text(c['text']))
          for c in cues if c['text'].strip()]
    with open(path, 'wb') as f: f.write(b'\xef\xbb\xbf' + '\r\n'.join(head + ev + ['']).encode('utf-8'))

# ----------------------------------------------------------------------------- audio
def _no_window():
    return dict(creationflags=0x08000000) if os.name == 'nt' else {}

# ---- cancelar: el boton "Parar" activa CANCEL; los procesos externos (ffmpeg, Whisper) se matan y el trabajo se corta
class Cancelled(BaseException):      # BaseException a proposito: los "except Exception" del programa no la tragan
    pass
CANCEL = threading.Event()
_PROCS = set(); _PROCS_LOCK = threading.Lock()

def check_cancel():
    if CANCEL.is_set(): raise Cancelled()

def kill_all():
    with _PROCS_LOCK: ps = list(_PROCS)
    for pr in ps:
        try: pr.kill()
        except Exception: pass

def run_cancellable(cmd, capture_output=True, timeout=None, **kw):
    # como subprocess.run(..., capture_output=True), pero se puede cortar con CANCEL
    check_cancel()
    pr = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    with _PROCS_LOCK: _PROCS.add(pr)
    t0 = time.time()
    try:
        while True:
            try:
                out, err = pr.communicate(timeout=0.25); break
            except subprocess.TimeoutExpired:
                if CANCEL.is_set():
                    pr.kill(); pr.communicate(); raise Cancelled()
                if timeout is not None and time.time() - t0 > timeout:
                    pr.kill(); pr.communicate(); raise subprocess.TimeoutExpired(cmd, timeout)
    finally:
        with _PROCS_LOCK: _PROCS.discard(pr)
    return subprocess.CompletedProcess(cmd, pr.returncode, out, err)

_CONFIG_NAMES = ('subsync_config.json',)
_TOOL_CACHE = {}

def _config_paths():
    here = os.path.dirname(os.path.abspath(__file__))
    ps = [os.path.join(here, 'subsync_config.json')]
    appdata = os.environ.get('APPDATA')
    if appdata: ps.append(os.path.join(appdata, 'SubSync', 'subsync_config.json'))
    return ps

def load_config():
    for p in _config_paths():
        try:
            with open(p, 'r', encoding='utf-8') as f: return json.load(f)
        except Exception:
            pass
    return {}

def save_config(cfg):
    for p in _config_paths():
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'w', encoding='utf-8') as f: json.dump(cfg, f, indent=1)
            return p
        except Exception:
            continue
    return None

def _windows_registry_path_dirs():
    """PATH actual segun el registro de Windows (usuario + sistema). Un proceso hereda el PATH del que lo lanza
    y no ve los cambios hechos despues; el registro si tiene siempre el valor actual."""
    if os.name != 'nt': return []
    dirs = []
    try:
        import winreg
        for hive, sub in ((winreg.HKEY_CURRENT_USER, 'Environment'),
                          (winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment')):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    val, _ = winreg.QueryValueEx(k, 'Path')
                    dirs += [os.path.expandvars(x.strip().strip('"')) for x in str(val).split(';') if x.strip()]
            except OSError:
                pass
    except Exception:
        pass
    return dirs

def _common_windows_dirs():
    if os.name != 'nt': return []
    import glob
    env = os.environ.get
    ds = [r'C:\ffmpeg\bin', r'C:\ffmpeg', r'C:\Program Files\ffmpeg\bin', r'C:\Program Files (x86)\ffmpeg\bin',
          r'C:\ProgramData\chocolatey\bin']
    up = env('USERPROFILE')
    if up: ds += [os.path.join(up, 'scoop', 'shims'), os.path.join(up, 'ffmpeg', 'bin')]
    la = env('LOCALAPPDATA')
    if la:
        ds.append(os.path.join(la, 'Microsoft', 'WinGet', 'Links'))
        for pat in (os.path.join(la, 'Microsoft', 'WinGet', 'Packages', '*FFmpeg*', '**', 'bin'),
                    os.path.join(la, 'Microsoft', 'WinGet', 'Packages', '*ffmpeg*', '**', 'bin')):
            try: ds += glob.glob(pat, recursive=True)
            except Exception: pass
    return ds

def _candidate_dirs():
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = load_config(); out = []
    d = cfg.get('ffmpeg_dir')
    if d: out += [d, os.path.join(d, 'bin')]
    out.append(here)
    out += [x for x in os.environ.get('PATH', '').split(os.pathsep) if x]
    out += _windows_registry_path_dirs()
    out += _common_windows_dirs()
    seen = set(); res = []
    for x in out:
        k = os.path.normcase(os.path.abspath(x))
        if k not in seen: seen.add(k); res.append(x)
    return res

def find_tool(name):
    if name in _TOOL_CACHE and os.path.isfile(_TOOL_CACHE[name]): return _TOOL_CACHE[name]
    exe = name + ('.exe' if os.name == 'nt' else '')
    for d in _candidate_dirs():
        p = os.path.join(d, exe)
        if os.path.isfile(p):
            _TOOL_CACHE[name] = p; return p
    raise RuntimeError("No encuentro %s. Instala ffmpeg, o indica su carpeta (en la ventana: boton 'Buscar ffmpeg...'; "
                       "en linea de comandos: --ffmpeg-dir CARPETA), o copia %s junto a subsync.py." % (name, exe))

def set_ffmpeg_dir(path):
    """acepta la carpeta que contiene ffmpeg/ffprobe, o su carpeta padre (con subcarpeta bin)"""
    exe = 'ffprobe' + ('.exe' if os.name == 'nt' else '')
    for d in (path, os.path.join(path, 'bin')):
        if os.path.isfile(os.path.join(d, exe)):
            cfg = load_config(); cfg['ffmpeg_dir'] = d; save_config(cfg); _TOOL_CACHE.clear(); return d
    raise RuntimeError("En esa carpeta no hay %s." % exe)

def probe_audio(path):
    r = run_cancellable([find_tool('ffprobe'), '-v', 'error', '-print_format', 'json', '-show_streams', '-select_streams', 'a', path],
                       capture_output=True, **_no_window())
    if r.returncode != 0: raise RuntimeError("ffprobe no puede leer %s: %s" % (path, r.stderr.decode('utf-8', 'replace')[:300]))
    st = json.loads(r.stdout.decode('utf-8', 'replace')).get('streams', [])
    out = []
    for n, s in enumerate(st):
        out.append(dict(n=n, index=s.get('index'), codec=s.get('codec_name'), ch=int(s.get('channels') or 2),
                        lang=(s.get('tags') or {}).get('language', ''), title=(s.get('tags') or {}).get('title', '')))
    return out

def choose_stream(streams, lang='jpn', track=None):
    if not streams: raise RuntimeError("El archivo no tiene pistas de audio.")
    if track is not None:
        for s in streams:
            if s['n'] == track: return s
        raise RuntimeError("No existe la pista de audio %d (hay %d)." % (track, len(streams)))
    codes = {'jpn': ('jpn', 'ja', 'jap'), 'spa': ('spa', 'es'), 'eng': ('eng', 'en')}
    prefer = [lang.lower()] if lang else []
    if 'eng' not in prefer: prefer.append('eng')      # si no hay pista del idioma preferido (japones), la inglesa
    for pl in prefer:
        l2 = codes.get(pl, (pl,))
        for s in streams:
            if s['lang'].lower() in l2: return s
    return streams[0]

def extract_db(path, stream, mode='center'):
    """devuelve dB por trama de 10 ms. mode: center | mix"""
    if mode == 'center' and stream['ch'] >= 3: pan = 'pan=mono|c0=c2,'
    else: pan = ''
    af = pan + 'highpass=f=120,lowpass=f=4000'
    cmd = [find_tool('ffmpeg'), '-nostdin', '-v', 'error', '-i', path, '-map', '0:a:%d' % stream['n'], '-af', af,
           '-ac', '1', '-ar', '8000', '-f', 's16le', 'pipe:1']
    r = run_cancellable(cmd, capture_output=True, **_no_window())
    if r.returncode != 0 or len(r.stdout) < 16000:
        raise RuntimeError("ffmpeg no pudo extraer el audio: " + r.stderr.decode('utf-8', 'replace')[:300])
    x = np.frombuffer(r.stdout, dtype='<i2').astype(np.float32)
    n = len(x) // 80; x = x[:n * 80].reshape(n, 80)
    rms = np.sqrt((x ** 2).mean(1) + 1e-3)
    return 20 * np.log10(rms + 1e-3)

def speech_activity(db):
    ds = np.convolve(db, np.ones(7) / 7, mode='same')
    lo = np.percentile(ds, 15)
    B = 1000; nb = len(ds) // B + 1
    fl = np.array([np.percentile(ds[i * B:(i + 1) * B], 20) if len(ds[i * B:(i + 1) * B]) else lo for i in range(nb)])
    floor = np.interp(np.arange(len(ds)), (np.arange(nb) + 0.5) * B, fl)
    thr = np.maximum(floor + 9.0, lo + 6.0)
    a = 1 / (1 + np.exp(-(ds - thr) / 2.0))
    a = np.maximum(a, np.convolve(a, np.ones(13) / 13, mode='same'))
    return np.clip(a, 0, 1)

def packet_pts(path, n):
    """tiempos (pts) de los paquetes de la pista de audio n, segun el contenedor"""
    r = run_cancellable([find_tool('ffprobe'), '-v', 'error', '-select_streams', 'a:%d' % n, '-show_entries', 'packet=pts_time',
                        '-of', 'csv=p=0', path], capture_output=True, **_no_window())
    vals = []
    for l in r.stdout.decode('utf-8', 'replace').split('\n'):
        l = l.strip().split(',')[0]
        if l and l != 'N/A':
            try: vals.append(float(l))
            except ValueError: pass
    return np.array(sorted(vals))

def to_container_timeline(a, pts, log, force=False):
    """Pasa la actividad de voz (tiempo decodificado, contiguo) a la linea de tiempo del contenedor (la que usa el
    reproductor para sincronizar video, audio y subtitulos). Si los pts del audio estan estirados (p.ej. 0.1 % por
    24 -> 23.976 fps), el audio decodificado en bruto NO coincide con el video; esto lo corrige."""
    n = len(pts)
    if n < 100: return a
    ddec = len(a) * FR; pk = ddec / n
    nom = np.arange(n) * pk
    stretch = (pts[-1] - pts[0]) - nom[-1]
    if abs(stretch) < 0.05 and not force:
        log("  tiempos del audio coherentes con el contenedor (estiramiento %+.2f s): sin correccion" % stretch); return a
    T = np.arange(0, pts[-1] + pk, FR)
    wt = np.interp(T, pts, nom + 0.0)
    idx = np.clip(np.round(wt / FR).astype(np.int64), 0, len(a) - 1)
    log("  audio estirado en el contenedor: %+.2f s al final (%.3f %%); sincronizo contra la linea de tiempo del video" % (stretch, 100 * stretch / ddec))
    return a[idx]

def speech_segments(a, thr=0.5, min_len=0.12, merge_gap=0.18):
    m = a > thr
    d = np.diff(np.concatenate([[0], m.astype(np.int8), [0]]))
    st = np.where(d == 1)[0]; en = np.where(d == -1)[0]
    mg = []
    for s, e in zip(st * FR, en * FR):
        if mg and s - mg[-1][1] < merge_gap: mg[-1][1] = e
        else: mg.append([s, e])
    r = np.array([x for x in mg if x[1] - x[0] >= min_len])
    return r if len(r) else np.zeros((0, 2))

# ----------------------------------------------------------------------------- alineacion
def cum(a): return np.concatenate([[0.0], np.cumsum(a)])

def seg_int(ca, t0, t1):
    n = len(ca) - 1
    i0 = np.clip(np.round(np.asarray(t0) / FR).astype(np.int64), 0, n)
    i1 = np.clip(np.round(np.asarray(t1) / FR).astype(np.int64), 0, n)
    return (ca[i1] - ca[i0]) * FR

def _cue_score(i, S, E, ca, offs, mu=0.45, mug=0.30, we=0.7, margin=0.4):
    N = len(S); s = S[i]; e = E[i]
    pe = E[i - 1] if i > 0 else -9; ns = S[i + 1] if i + 1 < N else 1e9
    m0 = min(margin, max(0.0, (s - pe) / 2)); m1 = min(margin, max(0.0, (ns - e) / 2))
    t0 = s + offs; t1 = e + offs
    v = seg_int(ca, t0, t1) - mu * (e - s)
    if m0 > 0: v = v + we * (mug * m0 - seg_int(ca, t0 - m0, t0))
    if m1 > 0: v = v + we * (mug * m1 - seg_int(ca, t1, t1 + m1))
    return v

def cue_scores(S, E, ca, offs):
    sc = np.zeros((len(S), len(offs)), dtype=np.float32)
    for i in range(len(S)): sc[i] = _cue_score(i, S, E, ca, offs)
    return sc

def total_score(S, E, ca, offs):
    tot = np.zeros(len(offs))
    for i in range(len(S)): tot += _cue_score(i, S, E, ca, offs)
    return tot

def _dt_l1(g, sl):
    K = len(g); ar = np.arange(K)
    h = g - sl * ar; cm = np.minimum.accumulate(h); fa = np.maximum.accumulate(np.where(h == cm, ar, 0)); f = cm + sl * ar
    hb = g + sl * ar; cmb = np.minimum.accumulate(hb[::-1])[::-1]
    ba = np.minimum.accumulate(np.where(hb == cmb, ar, K)[::-1])[::-1]; b = cmb - sl * ar
    use_f = f <= b
    return np.where(use_f, f, b), np.where(use_f, fa, ba)

def viterbi(sc, step, lam, jump):
    N, K = sc.shape; cost = -sc.astype(np.float64); sl = lam * step
    dp = cost[0].copy(); ptr = np.zeros((N, K), dtype=np.int32)
    for i in range(1, N):
        best, barg = _dt_l1(dp, sl)
        gm = dp.min(); ga = int(dp.argmin()); uj = (gm + jump) < best
        best = np.where(uj, gm + jump, best); barg = np.where(uj, ga, barg)
        ptr[i] = barg.astype(np.int32); dp = best + cost[i]
    path = np.zeros(N, dtype=np.int32); path[-1] = int(dp.argmin())
    for i in range(N - 1, 0, -1): path[i - 1] = ptr[i][path[i]]
    return path, -float(dp.min())

def warp_arrays(S, E, rho, d0):
    return S * (1 + rho) + d0, E * (1 + rho) + d0

def fit_global(S, E, a, log):
    ca = cum(a)
    offsA = np.arange(-300, 300.01, 0.1)
    res = []
    for r in RATIOS:
        s2, e2 = warp_arrays(S, E, r - 1, 0.0)
        tot = total_score(s2, e2, ca, offsA); k = int(tot.argmax()); res.append((float(tot[k]), r - 1, float(offsA[k]), tot))
    res.sort(key=lambda x: -x[0])
    best = res[0]
    # significancia: pico principal frente al mejor pico alejado >3 s
    tot = best[3]; k = int(tot.argmax()); far = np.abs(offsA - offsA[k]) > 3.0
    second = float(tot[far].max()) if far.any() else 0.0
    peak_ratio = best[0] / max(second, 1e-6) if best[0] > 0 else 0.0
    rho0, d0 = best[1], best[2]
    log("  busqueda amplia: velocidad x%.5f, desfase %+.1f s (pico/segundo pico = %.1f)" % (1 + rho0, d0, peak_ratio))
    # fino 1
    bestf = (-1e18, rho0, d0)
    offs = np.arange(d0 - 1.5, d0 + 1.5 + 1e-9, 0.02)
    for rho in np.arange(rho0 - 0.0008, rho0 + 0.0008 + 1e-12, 0.00004):
        s2, e2 = warp_arrays(S, E, rho, 0.0); tot = total_score(s2, e2, ca, offs); k = int(tot.argmax())
        if tot[k] > bestf[0]: bestf = (float(tot[k]), rho, float(offs[k]))
    _, rho1, d1 = bestf
    offs = np.arange(d1 - 0.1, d1 + 0.1 + 1e-9, 0.01)
    for rho in np.arange(rho1 - 0.00004, rho1 + 0.00004 + 1e-12, 0.00001):
        s2, e2 = warp_arrays(S, E, rho, 0.0); tot = total_score(s2, e2, ca, offs); k = int(tot.argmax())
        if tot[k] > bestf[0]: bestf = (float(tot[k]), rho, float(offs[k]))
    return bestf[1], bestf[2], peak_ratio

def piecewise_offsets(S, E, a, log, allow_cuts=True):
    """devuelve desfase por cue (nivel grueso para cortes + nivel fino para derivas)"""
    ca = cum(a); N = len(S); off = np.zeros(N)
    if allow_cuts and N >= 30:
        step = 0.1; offs = np.arange(-40, 40.01, step)
        sc = cue_scores(S, E, ca, offs)
        path, _ = viterbi(sc, step, lam=1.0, jump=8.0)
        k0 = int(np.argmin(np.abs(offs)))
        gain = float(sum(sc[i, path[i]] for i in range(N)) - sum(sc[i, k0] for i in range(N)))
        o1 = offs[path]
        nseg = 1 + int(np.sum(np.abs(np.diff(o1)) > 2.0))
        if gain > 12.0 and nseg > 1:
            off = o1.copy(); log("  detectados %d tramos con saltos/cortes (mejora %.0f)" % (nseg, gain))
    S1 = S + off; E1 = E + off
    step = 0.02; offs = np.arange(-2.5, 2.5 + step / 2, step)
    sc = cue_scores(S1, E1, ca, offs)
    path, _ = viterbi(sc, step, lam=1.0, jump=6.0)
    return off + offs[path]

def coverage(S, E, a):
    ca = cum((a > 0.5).astype(np.float64)); L = np.maximum(E - S, 1e-3)
    return seg_int(ca, S, E) / L

def snap(S, E, text_len, sign, a, pre=0.04, post=0.12, cov_min=0.25):
    S = S.copy(); E = E.copy(); N = len(S)
    segs0 = speech_segments(a); segs1 = speech_segments(a, merge_gap=0.35)
    if len(segs0) == 0: return S, E, 0
    cov = coverage(S, E, a); snapped = 0
    def find(segs, t):
        k = np.searchsorted(segs[:, 0], t, side='right') - 1
        return (k >= 0 and segs[k, 1] >= t), k
    for i in range(N):
        if sign[i] or cov[i] < cov_min: continue
        inside, k = find(segs0, S[i])
        if inside:
            if S[i] - segs0[k, 0] <= 0.6: S[i] = segs0[k, 0] - pre; snapped += 1
        elif k + 1 < len(segs0) and segs0[k + 1, 0] - S[i] <= 0.5:
            S[i] = segs0[k + 1, 0] - pre; snapped += 1
    for i in range(N):
        if sign[i] or cov[i] < cov_min: continue
        e = E[i]; ne = None; inside, k = find(segs1, e)
        if inside:
            if segs1[k, 1] - e <= 0.9: ne = segs1[k, 1] + post
        else:
            if k >= 0 and e - segs1[k, 1] <= 0.6: ne = segs1[k, 1] + post
            elif k + 1 < len(segs1) and segs1[k + 1, 0] - e <= 0.3: ne = segs1[k + 1, 1] + post
        if ne is None: continue
        if i + 1 < N: ne = min(ne, S[i + 1] - 0.03)
        if ne - S[i] >= 0.4: E[i] = ne
    for i in range(N):
        if i > 0 and S[i] < E[i - 1] + 0.02:
            if E[i - 1] - 0.02 - S[i - 1] >= 0.4: E[i - 1] = S[i] - 0.02
            else: S[i] = E[i - 1] + 0.02
        if E[i] - S[i] < 0.3: E[i] = S[i] + max(0.3, min(0.7, 0.06 * text_len[i]))
    return S, E, snapped

def local_offsets(S, E, sign, a, win=150, R=3.0):
    ca = cum(a); res = []; T = E.max()
    for w0 in range(0, int(T), win):
        idx = [i for i in range(len(S)) if w0 <= S[i] < w0 + win and not sign[i]]
        if len(idx) < 8: continue
        offs = np.arange(-R, R + 1e-9, 0.02); s = S[idx]; e = E[idx]
        tot = np.zeros(len(offs))
        for j in range(len(idx)):
            t0 = s[j] + offs; t1 = e[j] + offs
            tot += seg_int(ca, t0, t1) - 0.45 * (e[j] - s[j])
        res.append(float(offs[int(tot.argmax())]))
    return res

# ----------------------------------------------------------------------------- pipeline
def align_cues(cues, a, log, do_snap=True, allow_cuts=True):
    cues = sorted(cues, key=lambda c: c['s'])
    S = np.array([c['s'] for c in cues]); E = np.array([c['e'] for c in cues])
    sign = np.array([c['sign'] for c in cues]); tl = np.array([len(c['text']) for c in cues])
    use = ~sign
    if use.sum() < 15: raise RuntimeError("Muy pocas lineas de dialogo (%d) para sincronizar de forma fiable." % use.sum())
    rho, d0, peak = fit_global(S[use], E[use], a, log)
    S1, E1 = warp_arrays(S, E, rho, d0)
    log("  ajuste global: velocidad x%.5f (%+.3f %%), desfase %+.2f s" % (1 + rho, rho * 100, d0))
    cov0 = float(np.median(coverage(S, E, a)[use]))
    off = piecewise_offsets(S1[use] if False else S1, E1, a, log, allow_cuts)
    # los carteles/efectos siguen el desfase del cue de dialogo mas cercano (no se ajustan a la voz)
    S2 = S1 + off; E2 = E1 + off
    covg = float(np.median(coverage(S2, E2, a)[use]))
    nsn = 0
    if do_snap: S3, E3, nsn = snap(S2, E2, tl, sign, a)
    else: S3, E3 = S2, E2
    covf = float(np.median(coverage(S3, E3, a)[use]))
    after = local_offsets(S3, E3, sign, a)
    for i, c in enumerate(cues): c['s'] = float(S3[i]); c['e'] = float(E3[i])
    rep = dict(rho=float(rho), d0=float(d0), peak_ratio=float(peak), cov_orig=cov0, cov_final=covf, snapped=int(nsn),
               n=len(cues), residual_max=float(max([abs(x) for x in after])) if after else None,
               jump_segments=int(1 + np.sum(np.abs(np.diff(off)) > 2.0)) if len(off) else 1)
    warn = []
    if covf < 0.45: warn.append("Poca coincidencia con la voz (%.0f %%). Revisa la pista de audio (--lang / --track) o prueba --mix." % (covf * 100))
    if peak < 1.3 and rep['jump_segments'] == 1: warn.append("El desfase global no es claro (pico/segundo = %.1f). Resultado poco fiable." % peak)
    if rep['residual_max'] is not None and rep['residual_max'] > 0.3: warn.append("Quedan tramos con desfase > 0.3 s: revisalos.")
    rep['warnings'] = warn
    return cues, rep

def sync_one(media, sub, outdir, opts, log):
    t0 = time.time(); P = opts.get('prog') or (lambda *x: None)
    P(0.02, 'Leyendo subtítulo y audio')
    log("Subtitulo: %s" % os.path.basename(sub)); log("Audio:     %s" % os.path.basename(media))
    cues, meta = load_subs(sub)
    if not cues: raise RuntimeError("No he podido leer lineas del subtitulo.")
    streams = probe_audio(media); st = choose_stream(streams, opts.get('lang', 'jpn'), opts.get('track'))
    log("  pista de audio %d (%s%s, %d canales)" % (st['n'], st['lang'] or '?', ' - ' + st['title'] if st['title'] else '', st['ch']))
    mode = 'mix' if opts.get('mix') else 'center'
    P(0.05, 'Extrayendo audio')
    db = extract_db(media, st, mode)
    P(0.30, 'Detectando voz')
    if mode == 'center' and st['ch'] >= 3 and np.percentile(db, 95) < 25:
        log("  canal central casi vacio; uso mezcla completa"); db = extract_db(media, st, 'mix')
    a = speech_activity(db)
    if opts.get('timeline', 'container') == 'container':
        a = to_container_timeline(a, packet_pts(media, st['n']), log)
    P(0.34, 'Alineando subtítulo')
    cues, rep = align_cues(cues, a, log, opts.get('snap', True), opts.get('cuts', True))
    P(0.50, 'Alineado')
    os.makedirs(outdir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(media))[0]
    try:
        import fillgaps
        cues = fillgaps.process(sys.modules[__name__], media, st, mode, a, cues, rep, opts, log, outdir, stem)
    except ImportError:
        pass
    P(0.97, 'Guardando archivos')
    outs = []
    fmt = (opts.get('fmt') or 'auto').lower()
    is_ass = meta['kind'] == 'ass'
    want_ass = fmt == 'ass' or (fmt == 'auto' and is_ass)
    want_srt = fmt == 'srt' or (fmt == 'auto')
    if want_ass:
        p = os.path.join(outdir, stem + '.ass')
        if is_ass: write_ass_like(p, cues, meta)
        else: write_ass_new(p, cues)
        outs.append(p)
    if want_srt:
        p = os.path.join(outdir, stem + '.srt'); write_srt(p, [c for c in cues]); outs.append(p)
    log("  cobertura de voz %.0f %% -> %.0f %% | bordes ajustados: %d/%d | desfase residual max %.2f s | %.0f s" % (
        rep['cov_orig'] * 100, rep['cov_final'] * 100, rep['snapped'], rep['n'], rep['residual_max'] or 0, time.time() - t0))
    for w in rep['warnings']: log("  AVISO: " + w)
    for p in outs: log("  -> " + p)
    rep['outputs'] = outs
    P(1.0, 'Episodio terminado')
    return rep

# ----------------------------------------------------------------------------- emparejado
AUDIO_ONLY = ('.mka', '.mp3', '.flac', '.aac', '.ac3', '.eac3', '.opus', '.ogg', '.wav', '.m4a')

def episode_key(path):
    """(temporada, episodio). La temporada sale del nombre (04x02, S04E02) o, si no, de las carpetas ('Temporada 4')."""
    n = os.path.splitext(os.path.basename(path))[0]
    season = ep = None
    m = re.search(r'[sS](\d{1,2})[eE](\d{1,3})', n)
    if m: season, ep = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r'(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)', n)
        if m: season, ep = int(m.group(1)), int(m.group(2))
        else:
            m = re.search(r'(?<![0-9A-Za-z])(\d{1,3})(?![0-9A-Za-z])', n)
            if m: ep = int(m.group(1))
    if ep is not None and season is None:
        parts = os.path.normpath(os.path.dirname(os.path.abspath(path))).split(os.sep)
        for part in reversed(parts[-4:]):
            mm = re.search(r'(?:temporada|season|saison|staffel)[\s._-]*0*(\d{1,2})(?!\d)', part, re.I)
            if mm: season = int(mm.group(1)); break
    return season, ep

def looks_cjk(path):
    try:
        t = open(path, 'rb').read(6000).decode('utf-8', 'ignore')
    except Exception:
        return False
    letters = [c for c in t if c.isalpha()]
    if len(letters) < 20: return False
    cjk = sum(1 for c in letters if '぀' <= c <= 'ヿ' or '一' <= c <= '鿿' or '가' <= c <= '힯')
    return cjk / len(letters) > 0.3

def plan_pairs(medias, subs):
    """Empareja videos/audios con subtitulos por temporada+episodio.
    Devuelve dict(pairs=[(media, sub, notas)], no_sub=[media], no_media=[sub], ignored=[(media, motivo)])"""
    medias = sorted(set(medias)); subs = sorted(set(subs)); ignored = []
    # varios archivos del mismo episodio (p.ej. .mkv y su .mka): se usa uno solo (prefiero el de solo audio, mas ligero)
    groups = {}
    for m in medias:
        k = episode_key(m); groups.setdefault(k if k[1] is not None else ('?', m), []).append(m)
    chosen = []
    for k, lst in groups.items():
        lst = sorted(lst, key=lambda x: (0 if x.lower().endswith(AUDIO_ONLY) else 1, x))
        chosen.append(lst[0])
        for o in lst[1:]: ignored.append((o, "mismo episodio que " + os.path.basename(lst[0])))
    pairs = []; used = set(); no_sub = []
    cjk_cache = {}
    for m in sorted(chosen):
        km = episode_key(m); cand = []
        for s_ in subs:
            if s_ in used: continue
            ks = episode_key(s_)
            if km[1] is None or ks[1] is None or km[1] != ks[1]: continue
            if km[0] is not None and ks[0] is not None and km[0] != ks[0]: continue
            cand.append(s_)
        if not cand: no_sub.append(m); continue
        def rank(x):
            if x not in cjk_cache: cjk_cache[x] = looks_cjk(x)
            same_season = 0 if (km[0] is not None and episode_key(x)[0] == km[0]) else 1
            return (same_season, 1 if cjk_cache[x] else 0, x)
        cand.sort(key=rank)
        notes = ""
        if len(cand) > 1: notes = "hay %d subtitulos candidatos; uso este (otros: %s)" % (len(cand), ', '.join(os.path.basename(c) for c in cand[1:3]))
        pairs.append((m, cand[0], notes)); used.add(cand[0])
    return dict(pairs=pairs, no_sub=no_sub, no_media=[s_ for s_ in subs if s_ not in used], ignored=ignored)

def pair_files(medias, subs):
    p = plan_pairs(medias, subs)
    return [(m, s_) for m, s_, _ in p['pairs']], p['no_sub'], p['no_media']

def collect(paths, recursive=False):
    medias, subs = [], []
    def add(f):
        e = os.path.splitext(f)[1].lower()
        if e in SUB_EXT: subs.append(f)
        elif e in MEDIA_EXT: medias.append(f)
    for p in paths:
        if os.path.isdir(p):
            if recursive:
                for r, ds, fs in os.walk(p):
                    ds[:] = [d for d in ds if d.lower() != 'sincronizados']
                    for f in fs: add(os.path.join(r, f))
            else:
                for f in sorted(os.listdir(p)):
                    fp = os.path.join(p, f)
                    if os.path.isfile(fp): add(fp)
        elif os.path.isfile(p): add(p)
    return medias, subs

def run_jobs(paths, outdir, opts, log, recursive=False):
    _log0 = log
    def log(m): check_cancel(); _log0(m)
    medias, subs = collect(paths, recursive)
    if not medias or not subs:
        raise RuntimeError("Necesito al menos un video/audio y un subtitulo (encontrados: %d video/audio, %d subtitulos)." % (len(medias), len(subs)))
    plan = plan_pairs(medias, subs)
    log("Parejas encontradas: %d" % len(plan['pairs']))
    for m, s_, note in plan['pairs']:
        log("  %s  <->  %s%s" % (os.path.basename(m), os.path.basename(s_), ("   [" + note + "]") if note else ""))
    for m, why in plan['ignored']: log("  ignorado: %s (%s)" % (os.path.basename(m), why))
    for m in plan['no_sub']: log("  SIN subtitulo: " + os.path.basename(m))
    for s_ in plan['no_media']: log("  subtitulo sin video: " + os.path.basename(s_))
    reports = []; n = len(plan['pairs'])
    for i, (m, s_, _) in enumerate(plan['pairs'], 1):
        log("\n[%d/%d]" % (i, n))
        od = outdir or os.path.join(os.path.dirname(os.path.abspath(s_)), 'sincronizados')
        o2 = dict(opts); pcb = opts.get('progress_cb')
        if pcb:
            def _prog(fr, txt='', i=i):
                check_cancel(); pcb((i - 1 + min(1.0, max(0.0, fr))) / n, ("[%d/%d] %s" % (i, n, txt)) if n > 1 else txt)
            o2['prog'] = _prog
        try: reports.append((m, sync_one(m, s_, od, o2, log)))
        except Exception as ex: log("  ERROR: %s" % ex)
    return reports

# ----------------------------------------------------------------------------- GUI
LANG_NAMES = {'jpn': 'japones', 'ja': 'japones', 'eng': 'ingles', 'en': 'ingles', 'spa': 'espanol', 'es': 'espanol',
              'fre': 'frances', 'fra': 'frances', 'ger': 'aleman', 'deu': 'aleman', 'ita': 'italiano', 'por': 'portugues',
              'kor': 'coreano', 'chi': 'chino', 'zho': 'chino', 'rus': 'ruso', 'und': 'sin idioma', '': 'sin idioma'}
AUTO_LABEL = "Automática (japonesa; si no hay, inglesa)"
FMT_CHOICES = {"Igual que el original (si es ASS, también SRT)": 'auto', "Solo SRT": 'srt', "Solo ASS (editable en Aegisub / Subtitle Edit)": 'ass'}

def track_label(s):
    ch = {1: 'mono', 2: 'estereo', 6: '5.1', 8: '7.1'}.get(s['ch'], '%d canales' % s['ch'])
    lang = LANG_NAMES.get(s['lang'].lower(), s['lang'])
    t = "Pista %d  |  %s  |  %s  |  %s" % (s['n'] + 1, lang, s['codec'] or '?', ch)
    if s['title']: t += "  |  " + s['title']
    return t

def apply_dark(root):
    from tkinter import ttk
    bg, panel, field, fg, muted, accent, accent2, border = '#1e1f22', '#2b2d31', '#383a40', '#e3e5e8', '#9aa0a6', '#5865f2', '#6d78f5', '#3f4147'
    root.configure(bg=bg)
    st = ttk.Style(root)
    try: st.theme_use('clam')
    except Exception: pass
    st.configure('.', background=bg, foreground=fg, fieldbackground=field, bordercolor=border, lightcolor=bg, darkcolor=bg,
                 troughcolor=panel, focuscolor=bg, insertcolor=fg, selectbackground=accent, selectforeground='#ffffff')
    st.configure('TFrame', background=bg); st.configure('TLabel', background=bg, foreground=fg)
    st.configure('Muted.TLabel', background=bg, foreground=muted)
    st.configure('TEntry', fieldbackground=field, foreground=fg, insertcolor=fg, bordercolor=border)
    st.configure('TButton', background=panel, foreground=fg, bordercolor=border, padding=(10, 5))
    st.map('TButton', background=[('active', accent2), ('pressed', accent), ('disabled', panel)], foreground=[('disabled', muted)])
    st.configure('Accent.TButton', background=accent, foreground='#ffffff', padding=(18, 8), font=('Segoe UI', 10, 'bold'))
    st.map('Accent.TButton', background=[('active', accent2), ('pressed', accent), ('disabled', panel)], foreground=[('disabled', muted)])
    st.configure('Stop.TButton', background='#9c4a4e', foreground='#ffffff', padding=(18, 8), font=('Segoe UI', 10, 'bold'))
    st.map('Stop.TButton', background=[('active', '#b0585d'), ('pressed', '#9c4a4e'), ('disabled', panel)], foreground=[('disabled', muted)])
    st.configure('TCheckbutton', background=bg, foreground=fg, indicatorcolor=field, indicatorbackground=field)
    st.map('TCheckbutton', background=[('active', bg)], indicatorcolor=[('selected', accent), ('pressed', accent2)])
    st.configure('TCombobox', fieldbackground=field, background=panel, foreground=fg, arrowcolor=fg, bordercolor=border,
                 selectbackground=field, selectforeground=fg)
    st.map('TCombobox', fieldbackground=[('readonly', field), ('disabled', panel)], foreground=[('readonly', fg), ('disabled', muted)],
           background=[('active', accent2), ('readonly', panel)], arrowcolor=[('disabled', muted)])
    st.configure('Horizontal.TProgressbar', background=accent, troughcolor=panel, bordercolor=border, lightcolor=accent, darkcolor=accent)
    st.configure('Vertical.TScrollbar', background=panel, troughcolor=bg, bordercolor=bg, arrowcolor=fg)
    st.configure('TNotebook', background=bg, borderwidth=0, tabmargins=(0, 4, 0, 0))
    st.configure('TNotebook.Tab', background=panel, foreground=muted, padding=(14, 6), borderwidth=0)
    st.map('TNotebook.Tab', background=[('selected', field), ('active', field)], foreground=[('selected', fg), ('active', fg)])
    root.option_add('*TCombobox*Listbox.background', field); root.option_add('*TCombobox*Listbox.foreground', fg)
    root.option_add('*TCombobox*Listbox.selectBackground', accent); root.option_add('*TCombobox*Listbox.selectForeground', '#ffffff')
    root.option_add('*TCombobox*Listbox.font', ('Segoe UI', 9))
    try:  # barra de titulo oscura en Windows 10/11
        import ctypes
        root.update()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        for attr in (20, 19):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ctypes.c_int(1)), 4)
    except Exception:
        pass
    return dict(bg=bg, panel=panel, field=field, fg=fg, muted=muted, accent=accent)

def flow_columns(width, item_w, n):
    """cuantas columnas de opciones caben en 'width' pixeles"""
    return max(1, min(n, int(width // item_w)))

def summarize_paths(lst):
    if not lst: return ""
    if len(lst) == 1: return lst[0]
    return "%d elementos seleccionados" % len(lst)

def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog
    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES
        root = TkinterDnD.Tk(); dnd = True
    except Exception:
        root = tk.Tk(); dnd = False
    root.title("SubSync Local"); root.minsize(360, 380)
    # recordar tamano y posicion de la ventana entre sesiones
    def restore_geometry():
        geo, zoomed = "780x720", False
        try:
            w = load_config().get('window') or {}
            m = re.match(r'^(\d+)x(\d+)([+-])(-?\d+)([+-])(-?\d+)$', str(w.get('geometry', '')))
            if m:
                sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
                gw, gh = max(360, min(int(m.group(1)), sw)), max(380, min(int(m.group(2)), sh))
                gx, gy = int(m.group(4)), int(m.group(6))
                if m.group(3) == '-': gx = sw - gw - gx    # "-N" en Tk = N pixeles desde el borde DERECHO
                if m.group(5) == '-': gy = sh - gh - gy
                gx = min(max(gx, -gw + 120), sw - 120); gy = min(max(gy, 0), sh - 80)   # que quede a la vista
                geo = "%dx%d+%d+%d" % (gw, gh, gx, gy); zoomed = bool(w.get('zoomed'))
        except Exception:
            pass
        root.geometry(geo)
        if zoomed:
            try: root.state('zoomed')
            except Exception: pass
    def abs_geometry():
        """tamano y posicion SIEMPRE absolutos desde la izquierda/arriba (Tk puede devolver '-8' = desde el borde derecho)"""
        g = root.geometry(); m = re.match(r'^(\d+)x(\d+)([+-])(-?\d+)([+-])(-?\d+)$', g)
        if not m: return g
        gw, gh = int(m.group(1)), int(m.group(2)); gx, gy = int(m.group(4)), int(m.group(6))
        if m.group(3) == '-': gx = root.winfo_screenwidth() - gw - gx
        if m.group(5) == '-': gy = root.winfo_screenheight() - gh - gy
        return "%dx%d+%d+%d" % (gw, gh, gx, gy)
    def save_geometry():
        try:
            zoomed = (root.state() == 'zoomed')
            if zoomed: root.state('normal'); root.update_idletasks()
            cfg = load_config(); cfg['window'] = dict(geometry=abs_geometry(), zoomed=zoomed); cfg['out_dir'] = v_out.get().strip(); save_config(cfg)
        except Exception:
            pass
    restore_geometry()
    def on_close():
        CANCEL.set(); kill_all()          # que no se queden ffmpeg o Whisper huerfanos al cerrar con la cruz
        save_geometry(); root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)
    col = apply_dark(root)
    v_media = tk.StringVar(); v_sub = tk.StringVar(); v_out = tk.StringVar(); v_lang = tk.StringVar(value='jpn')
    v_track = tk.StringVar(value=AUTO_LABEL); v_fmt = tk.StringVar(value=list(FMT_CHOICES)[0])
    v_snap = tk.BooleanVar(value=True); v_cuts = tk.BooleanVar(value=True); v_mix = tk.BooleanVar(value=False)
    v_rec = tk.BooleanVar(value=False); v_tl = tk.BooleanVar(value=True)
    try: v_out.set(str(load_config().get('out_dir') or ''))
    except Exception: pass
    try:
        import fillgaps as FG
        fcfg = FG.get_cfg(sys.modules[__name__]); has_fg = True
    except Exception:
        FG = None; fcfg = {}; has_fg = False
    v_fill = tk.BooleanVar(value=bool(fcfg.get('enabled', True))); v_fauto = tk.BooleanVar(value=bool(fcfg.get('auto', False)))
    def tr_choices():
        return FG.translator_choices(fcfg.get('ollama_model', '')) if has_fg else {}
    _tr_inv = {v: k for k, v in tr_choices().items()}
    v_ftr = tk.StringVar(value=_tr_inv.get(fcfg.get('translator', 'local'), next(iter(_tr_inv), '')))
    _ki = {v: k for k, v in (FG.KIND_CHOICES.items() if has_fg else [])}
    v_fkind = tk.StringVar(value=_ki.get(fcfg.get('content', 'auto'), list(FG.KIND_CHOICES)[0] if has_fg else ''))
    q = queue.Queue()
    state = dict(tracks={}, probing=None, w=0, cols=None, media=[], subs=[], guard=False, ptimer=None)
    # contenido en un lienzo con desplazamiento: si la ventana es pequena aparece una barra lateral y nada queda tapado;
    # si es grande, el registro se estira y las opciones se recolocan solas
    outer = ttk.Frame(root); outer.pack(fill='both', expand=True)
    cv_s = tk.Canvas(outer, bg=col['bg'], highlightthickness=0, bd=0)
    vsb = ttk.Scrollbar(outer, orient='vertical', command=cv_s.yview); cv_s.configure(yscrollcommand=vsb.set)
    cv_s.pack(side='left', fill='both', expand=True)
    f = ttk.Frame(cv_s, padding=12); win_id = cv_s.create_window((0, 0), window=f, anchor='nw')
    sc = dict(on=False, last=None); capped = set()
    def fit_canvas(_e=None):
        try:
            cw, ch, rh = cv_s.winfo_width(), cv_s.winfo_height(), f.winfo_reqheight()
        except Exception:
            return
        if cw < 20 or ch < 20: return
        need = rh > ch; hh = max(ch, rh)
        if sc['last'] != (cw, hh):
            sc['last'] = (cw, hh)
            cv_s.itemconfigure(win_id, width=cw, height=hh); cv_s.configure(scrollregion=(0, 0, cw, hh))
        if need != sc['on']:
            sc['on'] = need
            if need: vsb.pack(side='right', fill='y')
            else: vsb.pack_forget(); cv_s.yview_moveto(0)
    cv_s.bind('<Configure>', fit_canvas); f.bind('<Configure>', fit_canvas, add='+')
    def wheel(ev):
        if not sc['on']: return
        wcls = ev.widget.winfo_class() if hasattr(ev.widget, 'winfo_class') else ''
        if wcls in ('Listbox', 'Treeview'): return   # (el combobox NO: la rueda sobre él desplaza la ventana, no cambia la opción)
        if wcls == 'Text' and ev.widget in capped: return
        cv_s.yview_scroll(int(-1 * (ev.delta / 120)), 'units')
    def combo_wheel(ev):
        # sustituye el comportamiento de serie del desplegable (cambiar de opcion con la rueda): aqui solo desplaza la ventana
        try:
            if ev.widget.winfo_toplevel() is root: wheel(ev)
        except Exception:
            pass
        return 'break'
    for _seq in ('<MouseWheel>', '<Shift-MouseWheel>', '<Button-4>', '<Button-5>'):
        try: root.bind_class('TCombobox', _seq, combo_wheel if _seq == '<MouseWheel>' else (lambda e: 'break'))
        except Exception: pass
    root.bind_all('<MouseWheel>', wheel)
    f.columnconfigure(0, weight=1)
    wrap_labels = []; counter = [0]
    def nxt():
        counter[0] += 1; return counter[0] - 1
    def hint(text):
        h = ttk.Label(f, text=text, style='Muted.TLabel', justify='left'); h.grid(row=nxt(), column=0, sticky='w')
        wrap_labels.append(h); return h

    # ---- casillas propias: marca blanca sobre fondo de color, visibles en cualquier version de Tk
    def make_check(parent, text, var):
        fr = tk.Frame(parent, bg=col['bg'], cursor='hand2')
        cv = tk.Canvas(fr, width=20, height=20, bg=col['bg'], highlightthickness=0, bd=0)
        cv.pack(side='left')
        lb = tk.Label(fr, text=text, bg=col['bg'], fg=col['fg'], font=('Segoe UI', 9), anchor='w')
        lb.pack(side='left', padx=(6, 0))
        def draw(*_):
            cv.delete('all'); on = bool(var.get())
            cv.create_rectangle(2, 2, 18, 18, outline=(col['accent'] if on else '#7d818a'), fill=(col['accent'] if on else col['field']), width=1)
            if on: cv.create_line(5, 10, 9, 14, 15, 6, fill='#ffffff', width=2, capstyle='round', joinstyle='round')
        def toggle(_e=None): var.set(not var.get())
        for w in (cv, lb, fr): w.bind('<Button-1>', toggle)
        var.trace_add('write', draw); draw()
        return fr

    # ---- seleccion multiple de archivos / carpetas
    def set_list(which, lst):
        state[which] = list(lst); state['guard'] = True
        (v_media if which == 'media' else v_sub).set(summarize_paths(lst)); state['guard'] = False
        schedule_pairs()
    def on_entry(which, var):
        def cb(*_):
            if state['guard']: return
            p = var.get().strip().strip('"')
            state[which] = [p] if p else []
            schedule_pairs()
        return cb
    def pick_files(which, types):
        ps = filedialog.askopenfilenames(filetypes=types)
        if ps: set_list(which, list(ps))
    def pick_dir_for(which):
        p = filedialog.askdirectory()
        if p: set_list(which, [p])
    def pick_out():
        p = filedialog.askdirectory()
        if p: v_out.set(p)
    def field(label, var, btns):
        ttk.Label(f, text=label).grid(row=nxt(), column=0, sticky='w', pady=(8, 2))
        fr = ttk.Frame(f); fr.grid(row=nxt(), column=0, sticky='ew'); fr.columnconfigure(0, weight=1)
        e = ttk.Entry(fr, textvariable=var, width=8); e.grid(row=0, column=0, sticky='ew')
        for i, (t, cmd) in enumerate(btns):
            ttk.Button(fr, text=t, command=cmd).grid(row=0, column=i + 1, padx=(6, 0))
        return e

    media_types = [("Vídeo/audio", "*.mkv *.mka *.mp4 *.avi *.mov *.webm *.ts *.flac *.ac3 *.eac3 *.mp3 *.wav"), ("Todos", "*.*")]
    sub_types = [("Subtítulos", "*.srt *.ass *.ssa"), ("Todos", "*.*")]
    e1 = field("Vídeos / audios (uno o varios)", v_media, [("Archivos…", lambda: pick_files('media', media_types)), ("Carpeta…", lambda: pick_dir_for('media'))])
    e2 = field("Subtítulos (uno o varios)", v_sub, [("Archivos…", lambda: pick_files('subs', sub_types)), ("Carpeta…", lambda: pick_dir_for('subs'))])
    v_media.trace_add('write', on_entry('media', v_media)); v_sub.trace_add('write', on_entry('subs', v_sub))
    field("Carpeta de salida", v_out, [("Elegir…", pick_out)])
    ttk.Label(f, text="Formato de salida").grid(row=nxt(), column=0, sticky='w', pady=(8, 2))
    ttk.Combobox(f, textvariable=v_fmt, state='readonly', values=list(FMT_CHOICES), width=10).grid(row=nxt(), column=0, sticky='ew')
    hint("Para un lote: elige las carpetas (o varios archivos) y se emparejan solos por número de episodio. "
         "Salida vacía = carpeta 'sincronizados' junto a cada subtítulo. Los originales no se modifican.")

    ttk.Label(f, text="Pista de audio").grid(row=nxt(), column=0, sticky='w', pady=(8, 2))
    cb = ttk.Combobox(f, textvariable=v_track, state='readonly', values=[AUTO_LABEL], width=10)
    cb.grid(row=nxt(), column=0, sticky='ew')
    hint("Por defecto se usa la pista japonesa y, si no hay, la inglesa; si necesitas otra, elígela aquí. Con varios vídeos, la elegida se aplica a todos.")

    opt = ttk.Frame(f); opt.grid(row=nxt(), column=0, sticky='ew', pady=(10, 0))
    opt_widgets = [make_check(opt, "Tiempos del MKV (recomendado)", v_tl),
                   make_check(opt, "Ajustar bordes a la voz", v_snap),
                   make_check(opt, "Detectar saltos/cortes", v_cuts),
                   make_check(opt, "Mezcla completa (no solo canal central)", v_mix),
                   make_check(opt, "Incluir subcarpetas", v_rec)]
    for i, w in enumerate(opt_widgets): w.grid(row=i, column=0, sticky='w', pady=2)
    v_rec.trace_add('write', lambda *_: schedule_pairs())

    if has_fg:
        ffr = ttk.LabelFrame(f, text="Frases que faltan (Whisper solo donde hay voz sin subtítulo)", padding=8)
        ffr.grid(row=nxt(), column=0, sticky='ew', pady=(10, 0)); ffr.columnconfigure(0, weight=1)
        make_check(ffr, "Buscar frases que faltan tras sincronizar", v_fill).grid(row=0, column=0, sticky='w', pady=2)
        make_check(ffr, "Añadirlas automáticamente, sin preguntar", v_fauto).grid(row=1, column=0, sticky='w', pady=2)
        v_franges = tk.StringVar()
        ttk.Label(ffr, text="Traducir con").grid(row=2, column=0, sticky='w', pady=(6, 2))
        cb_tr = ttk.Combobox(ffr, textvariable=v_ftr, state='readonly', values=list(tr_choices()), width=10)
        cb_tr.grid(row=3, column=0, sticky='ew')
        def refresh_tr_labels(code=None):
            code = code or tr_choices().get(v_ftr.get(), 'local')
            ch = tr_choices(); cb_tr.configure(values=list(ch)); v_ftr.set(next((k for k, v in ch.items() if v == code), next(iter(ch))))
        ttk.Label(ffr, text="Tipo de contenido (para el prompt y la traducción)").grid(row=5, column=0, sticky='w', pady=(6, 2))
        ttk.Combobox(ffr, textvariable=v_fkind, state='readonly', values=list(FG.KIND_CHOICES), width=10).grid(row=6, column=0, sticky='ew')
        def open_settings():
            code = tr_choices().get(v_ftr.get(), 'local')
            FG.settings_dialog(root, sys.modules[__name__], fcfg, col)
            refresh_tr_labels(code)
        ttk.Label(ffr, text="Revisar también estos tramos a mano (opcional)").grid(row=7, column=0, sticky='w', pady=(8, 2))
        ttk.Entry(ffr, textvariable=v_franges).grid(row=8, column=0, sticky='ew')
        lb_rg = ttk.Label(ffr, text="Ej.: 12:53-13:05, 20:10 (un solo minuto = ese momento y los 20 s siguientes). Whisper escucha justo ahí aunque "
                          "el detector no oiga voz por el ruido. Solo donde no haya ya subtítulo. Se aplica a todos los episodios de la tanda.",
                          style='Muted.TLabel', justify='left')
        lb_rg.grid(row=9, column=0, sticky='w', pady=(2, 0)); wrap_labels.append(lb_rg)
        ttk.Button(ffr, text="Ajustes (Whisper, Ollama)…", command=open_settings).grid(row=10, column=0, sticky='w', pady=(8, 0))

    fr_ff = ttk.Frame(f); fr_ff.grid(row=nxt(), column=0, sticky='ew', pady=(10, 0)); fr_ff.columnconfigure(0, weight=1)
    lb_ff = ttk.Label(fr_ff, text="", style='Muted.TLabel', justify='left'); lb_ff.grid(row=0, column=0, sticky='w')
    wrap_labels.append(lb_ff)
    def refresh_ffmpeg():
        try:
            p1 = find_tool('ffmpeg'); find_tool('ffprobe')
            lb_ff.configure(text="ffmpeg: encontrado (%s)" % os.path.dirname(p1)); return True
        except Exception:
            lb_ff.configure(text="ffmpeg: NO encontrado. Pulsa 'Buscar ffmpeg…' y elige la carpeta donde está (la que contiene ffmpeg.exe y ffprobe.exe)."); return False
    def pick_ffmpeg():
        p = filedialog.askdirectory(title="Carpeta de ffmpeg (la que contiene ffmpeg.exe y ffprobe.exe)")
        if not p: return
        try: q.put("ffmpeg configurado: %s" % set_ffmpeg_dir(p))
        except Exception as ex: q.put("ERROR: %s" % ex)
        refresh_ffmpeg(); state['probing'] = None; refresh_tracks()
    ttk.Button(fr_ff, text="Buscar ffmpeg…", command=pick_ffmpeg).grid(row=0, column=1, padx=(8, 0))

    btn = ttk.Button(f, text="Sincronizar", style='Accent.TButton'); btn.grid(row=nxt(), column=0, sticky='ew', pady=(12, 6))
    # el progreso va junto al registro (debajo del borde que se arrastra), para verlo siempre aunque el registro se amplie
    bottom = ttk.Frame(f); bottom.grid(row=nxt(), column=0, sticky='ew', pady=(0, 6)); bottom.columnconfigure(0, weight=1)
    pfr = ttk.Frame(bottom); pfr.pack(fill='x', pady=(0, 6)); pfr.columnconfigure(0, weight=1)
    pb = ttk.Progressbar(pfr, mode='determinate', maximum=100, value=0); pb.grid(row=0, column=0, sticky='ew')
    v_pct = tk.StringVar(value="0 %"); ttk.Label(pfr, textvariable=v_pct, width=7, anchor='e').grid(row=0, column=1, padx=(8, 0))
    v_ptxt = tk.StringVar(value="Baja con la rueda del ratón para ver el registro"); ttk.Label(pfr, textvariable=v_ptxt, style='Muted.TLabel').grid(row=1, column=0, columnspan=2, sticky='w')

    # ---- pestañas: Emparejado / Registro
    nb = ttk.Notebook(bottom); nb.pack(fill='x')
    def make_text(parent):
        parent.rowconfigure(0, weight=1); parent.columnconfigure(0, weight=1)
        t = tk.Text(parent, height=8, width=10, wrap='word', bg=col['panel'], fg=col['fg'], insertbackground=col['fg'], relief='flat',
                    highlightthickness=1, highlightbackground='#3f4147', highlightcolor=col['accent'], font=('Consolas', 10),
                    selectbackground=col['accent'], padx=8, pady=6)
        t.grid(row=0, column=0, sticky='nsew')
        return t
    import tkinter.font as tkfont
    MIN_LINES, MAX_LINES = 8, 1500
    def autosize(t):
        """el hueco del texto se ajusta a lo que hay escrito (la rueda mueve toda la ventana); solo con un registro enorme se queda en un tope y se desplaza por dentro"""
        try:
            if t.winfo_ismapped() and t.winfo_width() > 60:
                r = t.count('1.0', 'end', 'displaylines'); n = int(r[0]) if r else 1
            else:   # pestana oculta: estimo con el ancho del bloque de pestanas
                cw = max(1, tkfont.Font(font=t.cget('font')).measure('0'))
                cols = max(20, (nb.winfo_width() - 40) // cw - 2)
                n = sum(max(1, -(-len(l) // cols)) for l in t.get('1.0', 'end-1c').split('\n'))
            h = max(MIN_LINES, min(MAX_LINES, n + 1))
            if h >= MAX_LINES: capped.add(t)
            else: capped.discard(t)
            if int(t.cget('height')) != h: t.configure(height=h)
        except Exception:
            pass
    lg = dict(pending=False, follow=False)
    def _grow():
        lg['pending'] = False
        autosize(txt); autosize(ptxt)
        if lg['follow']:
            try:
                root.update_idletasks(); fit_canvas(); cv_s.yview_moveto(1)
            except Exception:
                pass
    def grow_log():
        """llamar tras escribir en el registro: lo agranda y, si ya estabas abajo del todo, sigue las lineas nuevas"""
        if lg['pending']: return
        lg['follow'] = (not sc['on']) or cv_s.yview()[1] >= 0.99
        lg['pending'] = True; root.after_idle(_grow)
    tab_pairs = ttk.Frame(nb); tab_log = ttk.Frame(nb)
    nb.add(tab_pairs, text="Emparejado"); nb.add(tab_log, text="Registro")
    ptxt = make_text(tab_pairs); txt = make_text(tab_log)
    ptxt.tag_configure('ok', foreground='#e3e5e8'); ptxt.tag_configure('dim', foreground='#9aa0a6')
    ptxt.tag_configure('warn', foreground='#f0b232'); ptxt.tag_configure('bad', foreground='#f87171'); ptxt.tag_configure('head', foreground='#7c87f8')
    if not dnd:
        txt.insert('end', "Consejo: para poder arrastrar archivos a esta ventana instala 'tkinterdnd2' (pip install tkinterdnd2).\n"
                          "Sin eso, usa los botones o suelta los archivos sobre SubSync_soltar_aqui.bat.\n\n")

    def show_pairs():
        state['ptimer'] = None
        paths = state['media'] + state['subs']
        ptxt.configure(state='normal'); ptxt.delete('1.0', 'end')
        if not paths:
            ptxt.insert('end', "Añade vídeos/audios y subtítulos (archivos sueltos o carpetas). Aquí verás cómo se emparejan antes de sincronizar.\n", 'dim')
        else:
            try:
                medias, subs = collect(paths, v_rec.get()); plan = plan_pairs(medias, subs)
                ptxt.insert('end', "%d parejas  |  %d vídeos sin subtítulo  |  %d subtítulos sin usar\n\n" % (len(plan['pairs']), len(plan['no_sub']), len(plan['no_media'])), 'head')
                for m, s_, note in plan['pairs']:
                    ptxt.insert('end', "✓ %s\n" % os.path.basename(m), 'ok'); ptxt.insert('end', "    ↔ %s\n" % os.path.basename(s_), 'dim')
                    if note: ptxt.insert('end', "    ! %s\n" % note, 'warn')
                for m in plan['no_sub']: ptxt.insert('end', "✗ SIN subtítulo: %s\n" % os.path.basename(m), 'bad')
                for m, why in plan['ignored']: ptxt.insert('end', "· ignorado: %s (%s)\n" % (os.path.basename(m), why), 'dim')
            except Exception as ex:
                ptxt.insert('end', "No he podido leer las rutas: %s\n" % ex, 'bad')
        ptxt.configure(state='disabled'); autosize(ptxt)
    def schedule_pairs(*_):
        if state['ptimer']:
            try: root.after_cancel(state['ptimer'])
            except Exception: pass
        state['ptimer'] = root.after(350, show_pairs)
        refresh_tracks_later()

    # ---- adaptacion al tamano de la ventana
    def on_resize(_ev=None):
        try: w = int(f.winfo_width())
        except Exception: return
        if abs(w - state['w']) < 4: return
        state['w'] = w
        for l in wrap_labels: l.configure(wraplength=max(120, w - 40))
        cols = flow_columns(w - 30, 250, len(opt_widgets))
        if cols != state['cols']:
            state['cols'] = cols
            for i, wd in enumerate(opt_widgets): wd.grid(row=i // cols, column=i % cols, sticky='w', padx=(0, 14), pady=2)
    f.bind('<Configure>', on_resize)
    def relayout(_e=None):
        if lg['pending']: return
        lg['follow'] = False; lg['pending'] = True; root.after_idle(_grow)
    nb.bind('<Configure>', relayout, add='+')

    # ---- pistas de audio (del primer video)
    def first_media():
        medias, _ = collect(state['media'] + state['subs'], v_rec.get())
        if not medias: return None, False
        return sorted(medias)[0], len(medias) == 1
    def probe_worker(path, single):
        try: q.put(('tracks', path, single, probe_audio(path)))
        except Exception as ex: q.put(('tracks_err', str(ex)))
    def refresh_tracks(*_):
        path, single = first_media()
        if not path or path == state['probing']: return
        state['probing'] = path
        threading.Thread(target=probe_worker, args=(path, single), daemon=True).start()
    timer = {'id': None}
    def refresh_tracks_later():
        if timer['id']:
            try: root.after_cancel(timer['id'])
            except Exception: pass
        timer['id'] = root.after(500, refresh_tracks)

    def on_drop(ev):
        medias, subs = [], []
        for p in root.tk.splitlist(ev.data):
            if os.path.isdir(p):
                mm, ss = collect([p], False); medias += mm; subs += ss
            elif p.lower().endswith(SUB_EXT): subs.append(p)
            else: medias.append(p)
        if medias: set_list('media', medias)
        if subs: set_list('subs', subs)
    if dnd:
        for w in (e1, e2, ptxt, txt, f):
            w.drop_target_register(DND_FILES); w.dnd_bind('<<Drop>>', on_drop)

    def log(m): q.put(m)
    def worker(opts, paths, out, rec):
        try:
            run_jobs(paths, out, opts, log, rec); log("\nTerminado.")
        except Cancelled:
            log("\nCancelado. Lo que ya estaba terminado antes se conserva.")
        except Exception as ex:
            log("ERROR: %s" % ex)
        q.put(None)
    def set_running(on):
        if on: btn.configure(text="Parar", style='Stop.TButton', command=stop)
        else: btn.configure(text="Sincronizar", style='Accent.TButton', command=start)
        btn.state(['!disabled'])
    def stop():
        CANCEL.set(); btn.configure(text="Parando…"); btn.state(['disabled']); v_ptxt.set("Parando…")
        txt.insert('end', "\nParando el proceso…\n"); grow_log()
    def start():
        if not refresh_ffmpeg():
            nb.select(tab_log); txt.insert('end', "No encuentro ffmpeg. Pulsa 'Buscar ffmpeg…' y elige su carpeta.\n"); grow_log(); return
        paths = state['media'] + state['subs']
        if not paths:
            nb.select(tab_log); txt.insert('end', "Elige primero los vídeos/audios y los subtítulos.\n"); grow_log(); return
        opts = dict(lang=v_lang.get().strip() or None, track=state['tracks'].get(v_track.get()), snap=v_snap.get(),
                    cuts=v_cuts.get(), mix=v_mix.get(), timeline='container' if v_tl.get() else 'audio',
                    fmt=FMT_CHOICES.get(v_fmt.get(), 'auto'))
        if has_fg:
            opts['fill_cfg'] = dict(enabled=v_fill.get(), auto=v_fauto.get(), translator=tr_choices().get(v_ftr.get(), 'local'), content=FG.KIND_CHOICES.get(v_fkind.get(), 'auto'),
                                    whisper_exe=fcfg.get('whisper_exe') or None, whisper_model=fcfg.get('whisper_model') or None,
                                    adapter=fcfg.get('adapter'), ollama_host=fcfg.get('ollama_host'), ollama_model=fcfg.get('ollama_model'),
                                    es_variant=fcfg.get('es_variant'), min_gap=fcfg.get('min_gap'))
            fcfg.update({k: v for k, v in opts['fill_cfg'].items() if v is not None})
            try: FG.save_cfg(sys.modules[__name__], fcfg)
            except Exception: pass
            def review_cb(items, ctx):
                rq = queue.Queue(); q.put(('review', items, ctx, rq)); return rq.get()
            opts['review_cb'] = review_cb
            try: opts['fill_ranges'] = FG.parse_ranges(v_franges.get())
            except ValueError as ex:
                nb.select(tab_log); txt.insert('end', "Tramos a revisar a mano: %s\n" % ex); grow_log(); return
            if opts['fill_ranges'] and not v_fill.get():
                nb.select(tab_log); txt.insert('end', "Has escrito tramos a revisar, pero «Buscar frases que faltan» está desactivado: actívalo para usarlos.\n"); grow_log(); return
        opts['progress_cb'] = lambda fr, t='': q.put(('prog', fr, t))
        try:
            c_ = load_config(); c_['out_dir'] = v_out.get().strip(); save_config(c_)
        except Exception: pass
        pb.configure(value=0); v_pct.set("0 %"); v_ptxt.set("Empezando…")
        CANCEL.clear(); set_running(True)
        nb.select(tab_log)
        threading.Thread(target=worker, args=(opts, paths, v_out.get().strip() or None, v_rec.get()), daemon=True).start()
    def poll():
        try:
            while True:
                m = q.get_nowait()
                if m is None:
                    set_running(False)
                    if CANCEL.is_set():
                        pb.configure(value=0); v_pct.set("0 %"); v_ptxt.set("Cancelado")
                    else:
                        if pb['value'] >= 95: pb.configure(value=100); v_pct.set("100 %")
                        v_ptxt.set("Terminado")
                    root.title("SubSync Local")
                elif isinstance(m, tuple) and m[0] == 'prog':
                    pct = int(round(max(0.0, min(1.0, m[1])) * 100)); pb.configure(value=pct); v_pct.set("%d %%" % pct); v_ptxt.set(m[2]); root.title("SubSync Local - %d %%" % pct)
                elif isinstance(m, tuple) and m[0] == 'tracks':
                    _, path, single, streams = m
                    labels = [track_label(x) for x in streams]
                    state['tracks'] = {lab: x['n'] for lab, x in zip(labels, streams)}
                    cb.configure(values=[AUTO_LABEL] + labels)
                    if single and streams:
                        v_track.set(track_label(choose_stream(streams, v_lang.get().strip() or None)))
                    else:
                        v_track.set(AUTO_LABEL)
                    txt.insert('end', "Pistas de audio en %s: %d\n" % (os.path.basename(path), len(streams))); grow_log()
                elif isinstance(m, tuple) and m[0] == 'review':
                    _, items, ctx, rq = m
                    try: rq.put(FG.review_dialog(root, items, ctx, col))
                    except Exception as ex:
                        txt.insert('end', "Error en la ventana de revisión: %s\n" % ex); rq.put([])
                elif isinstance(m, tuple) and m[0] == 'tracks_err':
                    txt.insert('end', "No he podido leer las pistas de audio: %s\n" % m[1]); grow_log()
                else:
                    txt.insert('end', m + '\n'); grow_log()
        except queue.Empty:
            pass
        root.after(150, poll)
    btn.configure(command=start)
    refresh_ffmpeg(); on_resize(); show_pairs(); poll(); root.mainloop()

# ----------------------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="Sincroniza subtitulos con el audio de un video (local).")
    ap.add_argument('paths', nargs='*', help="video/audio, subtitulo, y/o carpetas (en cualquier orden)")
    ap.add_argument('-o', '--out', help="carpeta de salida (por defecto: 'sincronizados' junto al subtitulo)")
    ap.add_argument('--lang', default='jpn', help="idioma de la pista de audio a usar (por defecto jpn)")
    ap.add_argument('--track', type=int, help="numero de pista de audio (1 = primera); anula --lang")
    ap.add_argument('--no-snap', action='store_true', help="no ajustar inicio/fin a la voz")
    ap.add_argument('--no-cuts', action='store_true', help="no buscar saltos/cortes")
    ap.add_argument('--timeline', choices=['container', 'audio'], default='container',
                    help="container (por defecto): tiempos del MKV, como los reproduce el reproductor; audio: audio decodificado en bruto")
    ap.add_argument('--format', choices=['auto', 'srt', 'ass'], default='auto',
                    help="formato de salida: auto (igual que el original; si es .ass genera tambien .srt), srt o ass")
    ap.add_argument('--mix', action='store_true', help="usar mezcla completa en vez del canal central")
    ap.add_argument('-r', '--recursive', action='store_true', help="buscar tambien en subcarpetas")
    ap.add_argument('--ffmpeg-dir', help="carpeta donde estan ffmpeg y ffprobe (se recuerda para la proxima vez)")
    ap.add_argument('--no-fill', action='store_true', help="no buscar frases que faltan")
    ap.add_argument('--fill-auto', action='store_true', help="anadir las frases que faltan sin preguntar (traduce con Ollama)")
    ap.add_argument('--translator', choices=['local', 'ia', 'both'], default=None, help="como traducir las frases anadidas")
    ap.add_argument('--gui', action='store_true', help="abrir la ventana")
    ap.add_argument('--list-tracks', action='store_true', help="listar las pistas de audio del video y salir")
    args = ap.parse_args(argv)
    if args.ffmpeg_dir:
        try: print('ffmpeg:', set_ffmpeg_dir(args.ffmpeg_dir))
        except Exception as ex: print('ERROR:', ex); return 2
    if args.list_tracks:
        for p in args.paths:
            if os.path.isfile(p) and p.lower().endswith(MEDIA_EXT):
                print(os.path.basename(p))
                for s in probe_audio(p): print("   --track %d : %s | %s | %d canales %s" % (s['n'] + 1, s['lang'] or '?', s['codec'], s['ch'], s['title']))
        return 0
    if args.gui or not args.paths:
        if os.name == 'nt' and os.environ.get('SUBSYNC_HIDE'):
            try:   # lanzado desde el .bat sin pythonw: oculta la consola negra
                import ctypes
                hw = ctypes.windll.kernel32.GetConsoleWindow()
                if hw: ctypes.windll.user32.ShowWindow(hw, 0)
            except Exception:
                pass
        try:
            run_gui()
        except Exception:
            import traceback
            tb = traceback.format_exc()
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'subsync_error.log'), 'w', encoding='utf-8') as fh: fh.write(tb)
            except Exception:
                pass
            try:
                import tkinter as _tk
                from tkinter import messagebox as _mb
                _r = _tk.Tk(); _r.withdraw(); _mb.showerror("SubSync Local", "Error en la ventana:\n\n" + tb[-900:]); _r.destroy()
            except Exception:
                print(tb)
            return 1
        return 0
    opts = dict(lang=args.lang, track=(args.track - 1 if args.track else None), snap=not args.no_snap, cuts=not args.no_cuts, mix=args.mix, timeline=args.timeline, fmt=args.format)
    try:
        import fillgaps
        opts['fill_cfg'] = dict(enabled=not args.no_fill, auto=args.fill_auto, translator=args.translator)
        opts['review_cb'] = fillgaps.cli_review
    except ImportError:
        pass
    try:
        reps = run_jobs(args.paths, args.out, opts, lambda m: print(m, flush=True), args.recursive)
    except Exception as ex:
        print("ERROR:", ex); return 2
    return 0

if __name__ == '__main__':
    sys.exit(main())
