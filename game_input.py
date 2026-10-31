"""Écrit un message dans le tchat du jeu en simulant le clavier (SendInput) : Entrée, [↑/↓ pour le canal], Ctrl+V, Entrée."""
import ctypes
import ctypes.wintypes as wt
import time

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
VK_RETURN = 0x0D
VK_UP = 0x26
VK_DOWN = 0x28
KEYEVENTF_EXTENDEDKEY = 0x0001
VK_CONTROL = 0x11
VK_V = 0x56
SW_RESTORE = 9

ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _U(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _U)]


def _send(inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _vk(vk, up=False, extended=False):
    # Le jeu lit le clavier par scancode (touche physique) : un code virtuel seul est mal interprété
    # (flèches ignorées, Retour arrière qui tape des lettres). On envoie donc le scancode.
    scan = user32.MapVirtualKeyW(vk, 0)
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if extended else 0)
    return INPUT(INPUT_KEYBOARD, _U(ki=KEYBDINPUT(0, scan, flags, 0, 0)))


def tap_vk(vk, extended=False, delay=0.05):
    """Frappe par code virtuel (et non par scancode), pour les programmes qui lisent le code virtuel."""
    scan = user32.MapVirtualKeyW(vk, 0)
    ext = KEYEVENTF_EXTENDEDKEY if extended else 0
    _send([INPUT(INPUT_KEYBOARD, _U(ki=KEYBDINPUT(vk, scan, ext, 0, 0)))])
    time.sleep(delay)
    _send([INPUT(INPUT_KEYBOARD, _U(ki=KEYBDINPUT(vk, scan, ext | KEYEVENTF_KEYUP, 0, 0)))])


def press(vk, delay=0.05, extended=False):
    # les flèches sont des touches "étendues" : sans ce drapeau, Windows les envoie comme le pavé numérique
    _send([_vk(vk, extended=extended)])
    time.sleep(delay)
    _send([_vk(vk, up=True, extended=extended)])
    time.sleep(delay)


def _set_clipboard(text):
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    kernel32.GlobalAlloc.restype = wt.HGLOBAL
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
    user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
    data = (text + "\0").encode("utf-16-le")
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    user32.EmptyClipboard()
    user32.SetClipboardData(CF_UNICODETEXT, h)
    user32.CloseClipboard()


def _bring_to_front(hwnd):
    # Windows bloque SetForegroundWindow depuis un process en arrière-plan. Pas d'astuce Alt : seul, Alt ouvre
    # le menu système de la fenêtre du jeu. On s'attache plutôt à la file d'entrée de la fenêtre au premier plan.
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    me = kernel32.GetCurrentThreadId()
    attached = bool(fg_thread and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(me, fg_thread, False)


def focus_window(hwnd):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    if user32.GetForegroundWindow() == hwnd:
        return True
    _bring_to_front(hwnd)
    for _ in range(20):
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.05)
    return False


def _same_channel(a, b):
    import difflib
    return bool(a and b) and difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() >= 0.75


def _open_input(hwnd, cfg, before_enter=None):
    """Met le jeu au premier plan et ouvre la saisie du tchat.
    before_enter() est appelé jeu au premier plan, saisie encore fermée ; il renvoie (ok, message).
    Renvoie (fenêtre qui avait le focus, message d'erreur ou None)."""
    previous = user32.GetForegroundWindow() or 0
    if not focus_window(hwnd):
        return previous, "impossible de mettre le jeu au premier plan"
    time.sleep(cfg.get("focus_delay", 0.25))
    if before_enter:
        ok, msg = before_enter()
        if not ok:
            return previous, msg
    if cfg.get("open_with_enter", True):
        press(VK_RETURN)
        time.sleep(0.15)
    return previous, None


def _restore(hwnd, previous, cfg):
    if cfg.get("restore_focus", True) and previous and previous != hwnd:
        time.sleep(0.1)
        _bring_to_front(previous)


def _read_stable(read_channel, tries=5):
    """Le sélecteur met un instant à se redessiner : on attend deux lectures identiques de suite."""
    last = read_channel()
    for _ in range(tries):
        time.sleep(0.15)
        cur = read_channel()
        if _same_channel(cur, last):
            return cur
        last = cur
    return last


def _step(direction, read_channel):
    press(direction, extended=True)
    time.sleep(0.25)
    return _read_stable(read_channel)


def _goto(target, read_channel, order, max_steps=12):
    """Saisie ouverte : va jusqu'au canal voulu. Le sens le plus court d'après l'ordre connu,
    puis l'autre sens si on bute sur le bout de la liste."""
    cur = _read_stable(read_channel)
    if _same_channel(cur, target):
        return True
    idx = lambda name: next((i for i, c in enumerate(order) if _same_channel(c, name)), None)
    i, j = idx(cur), idx(target)
    first = VK_DOWN
    if i is not None and j is not None and order:
        down = (j - i) % len(order)
        first = VK_DOWN if down <= len(order) - down else VK_UP
    for direction in (first, VK_UP if first == VK_DOWN else VK_DOWN):
        prev = cur
        for _ in range(max_steps):
            cur = _step(direction, read_channel)
            if _same_channel(cur, target):
                return True
            if _same_channel(cur, prev):
                break  # bout de la liste dans ce sens
            prev = cur
    return False


def _walk(direction, read_channel, start, max_steps=12):
    """Fait défiler les canaux. S'arrête au retour au canal de départ (la liste reboucle)
    ou quand le canal ne change plus (bout de la liste). Renvoie (canaux vus, a_rebouclé)."""
    seen = [start] if start else []
    for _ in range(max_steps):
        cur = _step(direction, read_channel)
        if not cur:
            continue
        if seen and _same_channel(cur, seen[-1]):
            return seen, False
        if any(_same_channel(cur, c) for c in seen):
            return seen, True  # déjà vu : on a fait le tour
        seen.append(cur)
    return seen, False


def discover_channels(hwnd, cfg, read_channel, before_arrows=None):
    """Ouvre la saisie et fait défiler les canaux avec ↓ jusqu'à revenir au départ, puis y retourne."""
    previous, err = _open_input(hwnd, cfg, before_enter=before_arrows)
    if err:
        _restore(hwnd, previous, cfg)
        return False, err, []
    start = _read_stable(read_channel)
    order, cyclic = _walk(VK_DOWN, read_channel, start)
    if not cyclic and len(order) > 0:
        # bout de la liste atteint vers le bas : on remonte pour voir les canaux au-dessus du départ
        up, _ = _walk(VK_UP, read_channel, order[-1], max_steps=len(order) + 12)
        order = up[::-1]
    if start:
        _goto(start, read_channel, order)
    _restore(hwnd, previous, cfg)
    if not order:
        return False, "sélecteur de canal illisible", []
    return True, "canaux détectés", order


def send_chat(hwnd, text, cfg, channel=None, read_channel=None, order=(), before_arrows=None):
    """Entrée (ouvre la saisie), ↑/↓ si un autre canal est demandé, colle le texte (Ctrl+V), Entrée (envoie),
    puis rend la main à la fenêtre d'avant. Renvoie (ok, message).
    before_arrows() est appelé (saisie fermée) seulement s'il faut changer de canal ; il renvoie (ok, message)."""
    text = text.replace(chr(13), " ").replace(chr(10), " ").strip()  # une seule ligne dans le tchat
    if not text:
        return False, "message vide"
    # le sélecteur de canal est visible même saisie fermée : on sait d'avance s'il faudra les flèches
    switch = bool(channel and read_channel and not _same_channel(read_channel(), channel))
    previous, err = _open_input(hwnd, cfg, before_enter=before_arrows if switch else None)
    if err:
        _restore(hwnd, previous, cfg)
        return False, err
    if switch and not _goto(channel, read_channel, list(order)):
        _restore(hwnd, previous, cfg)
        return False, f"canal « {channel} » introuvable"
    _set_clipboard(text)
    _send([_vk(VK_CONTROL)])
    press(VK_V)
    _send([_vk(VK_CONTROL, up=True)])
    time.sleep(0.1)
    press(VK_RETURN)
    _restore(hwnd, previous, cfg)
    return True, "envoyé"
