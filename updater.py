"""Mise à jour automatique de l'exe au démarrage, depuis les releases GitHub du dépôt public.

L'exe en cours d'exécution ne peut pas être supprimé sous Windows, mais il peut être renommé :
on le renomme en .old, on met le nouveau à sa place, on relance, et le .old est effacé au lancement suivant.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request

from version import VERSION

REPO = "WatoLua/PG-compagnon"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET = "PGCompanion.exe"
TIMEOUT = 20


def parse_version(v):
    """'v1.2.3' -> (1, 2, 3) ; None si ce n'est pas une version."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def _open(url, accept="application/octet-stream"):
    req = urllib.request.Request(url, headers={"User-Agent": f"PGCompanion/{VERSION}", "Accept": accept})
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass  # absent, ou encore verrouillé par l'ancienne version qui se ferme : ce sera pour le prochain lancement


def _download(url, dest, expected_sha256):
    h = hashlib.sha256()
    with _open(url) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 16):
            h.update(chunk)
            f.write(chunk)
    if h.hexdigest() != expected_sha256:
        os.remove(dest)
        raise ValueError("fichier téléchargé corrompu")


def update_and_restart():
    """Installe la dernière version si elle est plus récente, puis relance l'exe. Ne fait rien depuis les sources."""
    if not getattr(sys, "frozen", False):
        return
    exe = sys.executable
    _remove(exe + ".old")
    current = parse_version(VERSION)
    if not current or current == (0, 0, 0):
        return
    try:
        with _open(LATEST, "application/vnd.github+json") as r:
            release = json.load(r)
        latest = parse_version(release.get("tag_name"))
        if not latest or latest <= current:
            return
        assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
        if ASSET not in assets or ASSET + ".sha256" not in assets:
            return
        print(f"Mise à jour de PG Companion : {VERSION} -> {release['tag_name'].lstrip('v')}…")
        with _open(assets[ASSET + ".sha256"]) as r:
            expected = r.read().decode().split()[0].lower()
        _download(assets[ASSET], exe + ".new", expected)
        os.replace(exe, exe + ".old")
        try:
            os.replace(exe + ".new", exe)
        except OSError:
            os.replace(exe + ".old", exe)
            raise
    except (OSError, ValueError, KeyError, IndexError) as e:
        _remove(exe + ".new")
        print(f"Mise à jour impossible ({e}) : on continue avec la version {VERSION}.")
        return
    # même console ; le nouvel exe doit extraire ses propres fichiers au lieu de réutiliser ceux de celui-ci
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    subprocess.Popen([exe] + sys.argv[1:], env=env, cwd=os.path.dirname(exe))
    os._exit(0)
