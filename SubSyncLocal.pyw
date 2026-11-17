# Lanzador sin consola: doble clic aqui (o via SubSync.bat / el acceso directo)
import os, sys, subprocess, importlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def _asegurar(modulo, paquete):
    """instala en silencio (sin ventana de consola) lo que falte, solo la primera vez"""
    try:
        importlib.import_module(modulo); return
    except ImportError:
        pass
    try:
        subprocess.run([sys.executable, '-m', 'pip', 'install', paquete], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=0x08000000, timeout=300)
        importlib.invalidate_caches()
    except Exception:
        pass

_asegurar('tkinterdnd2', 'tkinterdnd2')
import subsync
sys.exit(subsync.main(['--gui']))
