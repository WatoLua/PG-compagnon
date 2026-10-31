"""Voyant de DroneHandler recopié dans l'appli : vert = clics actifs, rouge = désactivés.
Toucher le voyant dans l'appli envoie « / » (pavé numérique), la touche de pause de DroneHandler.
Si DroneHandler n'est pas lancé (ou pas installé), tout fonctionne comme s'il n'existait pas."""
import json
import os
import subprocess
import time

import capture
import game_input

DEFAULT_CONFIG = r"C:\Users\mathi\Desktop\informatique\python\drone-handler\config.json"
EXE_NAME = "dronehandler.exe"
SCRIPT_NAME = "drone_handler.py"
VK_DIVIDE = 0x6F  # « / » du pavé numérique : touche de pause de DroneHandler
NOT_FOUND = ""    # DroneHandler lancé mais sa config est introuvable : voyant illisible

_QUERY = ("Get-CimInstance Win32_Process -Filter \"Name='DroneHandler.exe' or Name like 'python%'\" | "
          "Select-Object Name, ExecutablePath, CommandLine | ConvertTo-Json -Compress")


def _processes():
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", _QUERY], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=15,
                             creationflags=0x08000000).stdout.strip()  # CREATE_NO_WINDOW
        procs = json.loads(out) if out else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return procs if isinstance(procs, list) else [procs]


def locate(override=None):
    """Cherche DroneHandler parmi les programmes lancés (DroneHandler.exe, ou py drone_handler.py).
    Renvoie le chemin de son config.json (rangé à côté du programme), NOT_FOUND s'il tourne mais que
    la config est introuvable, ou None s'il ne tourne pas : on fait alors comme s'il n'existait pas."""
    procs = _processes()
    if procs is None:  # impossible de lister les programmes : on se rabat sur la config connue
        path = override or DEFAULT_CONFIG
        return path if os.path.exists(path) else None
    found = False
    candidates = [override] if override else []
    for p in procs:
        name = (p.get("Name") or "").lower()
        cmd = p.get("CommandLine") or ""
        if name == EXE_NAME:
            found = True
            if p.get("ExecutablePath"):
                candidates.append(os.path.join(os.path.dirname(p["ExecutablePath"]), "config.json"))
        elif SCRIPT_NAME in cmd.lower():
            found = True
            for part in cmd.replace('"', " ").split():
                if part.lower().endswith(SCRIPT_NAME) and os.path.isabs(part):
                    candidates.append(os.path.join(os.path.dirname(part), "config.json"))
    if not found:
        return None
    candidates.append(DEFAULT_CONFIG)
    return next((c for c in candidates if c and os.path.exists(c)), NOT_FOUND)


def _positions(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            settings = json.load(f).get("settings", {})
    except (OSError, ValueError):
        return []
    ind = settings.get("indicator") or {}
    if ind.get("x") is None:
        return []
    off = settings.get("offset_9") or {"dx": 0, "dy": 0}
    # le voyant se décale en mode « 9 items »
    return [(ind["x"], ind["y"]), (ind["x"] + off.get("dx", 0), ind["y"] + off.get("dy", 0))]


def _color_at(x, y):
    img = capture._grab(x - 2, y - 2, 5, 5)
    px = list(img.getdata()) if hasattr(img, "getdata") else []
    r = sum(p[0] for p in px) / len(px)
    g = sum(p[1] for p in px) / len(px)
    b = sum(p[2] for p in px) / len(px)
    if g > 140 and r < 110 and b < 140:   # #22c55e
        return "on"
    if r > 170 and g < 110 and b < 110:   # #ef4444
        return "off"
    return None


def state(path):
    """'on', 'off', ou None si le voyant est introuvable (masqué, ou jeu pas au premier plan)."""
    for x, y in _positions(path) if path else []:
        s = _color_at(x, y)
        if s:
            return s
    return None


def toggle(path):
    """Bascule les clics de DroneHandler. Renvoie le nouvel état lu sur le voyant ('on', 'off' ou None)."""
    before = state(path)
    game_input.tap_vk(VK_DIVIDE, extended=True)
    for _ in range(10):  # le voyant est rafraîchi 5 fois par seconde
        time.sleep(0.1)
        s = state(path)
        if s and s != before:
            return s
    return state(path)


def require_paused(path):
    """Les flèches déclenchent les clics de DroneHandler : on ne les autorise que s'il est en pause.
    path : résultat de locate(). À appeler jeu au premier plan (le voyant n'est affiché qu'à ce moment).
    Ne bascule jamais rien. Renvoie (ok, message)."""
    if path is None:
        return True, "DroneHandler absent"
    if path == NOT_FOUND:
        return False, "DroneHandler lancé mais sa config.json est introuvable : impossible de lire son voyant"
    s = None
    for _ in range(8):  # le voyant est rafraîchi 5 fois par seconde
        s = state(path)
        if s:
            break
        time.sleep(0.1)
    if s == "off":
        return True, "DroneHandler en pause"
    if s == "on":
        return False, "DroneHandler est actif : mets-le en pause (voyant rouge) avant de changer de canal"
    return False, "voyant DroneHandler introuvable (masqué ?) : impossible de vérifier qu'il est en pause"
