"""Capture de zones de la fenêtre Pirate Galaxy (GDI, petites surfaces) et OCR (Windows.Media.Ocr)."""
import asyncio
import ctypes
import ctypes.wintypes as wt
import threading
import time

import mss
from PIL import Image, ImageChops, ImageFilter, ImageOps
import winocr

user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # coordonnées en pixels physiques
except Exception:
    user32.SetProcessDPIAware()

WINDOW_PREFIX = "PirateGalaxy"


def find_game_window():
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.startswith(WINDOW_PREFIX):
                found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def client_rect(hwnd):
    """Rectangle client de la fenêtre en coordonnées écran (left, top, width, height)."""
    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, r.right - r.left, r.bottom - r.top


_local = threading.local()  # une instance mss par thread (mss n'est pas partageable entre threads)

# Capturer une grande surface de l'écran du jeu (écran entier, fenêtre entière) fait décrocher la
# recopie audio « Écouter ce périphérique » ; de petites zones en GDI, non. On ne capture donc jamais
# plus que TILE pixels d'un coup.
TILE = (640, 520)


class CaptureError(Exception):
    """L'écran ne peut pas être copié pour l'instant (écran verrouillé, veille, fenêtre UAC…)."""


def _sct():
    if not hasattr(_local, "sct"):
        _local.sct = mss.MSS()
    return _local.sct


def _reset_sct():
    sct = _local.__dict__.pop("sct", None)
    if sct is not None:
        try:
            sct.close()
        except mss.ScreenShotError:
            pass  # contexte déjà invalide : rien à libérer de plus


def _grab(x, y, w, h):
    box = {"left": x, "top": y, "width": w, "height": h}
    try:
        shot = _sct().grab(box)
    except mss.ScreenShotError:
        # mss garde le contexte d'affichage de l'écran : après une veille, un verrouillage ou un changement de
        # résolution / de mode plein écran il n'est plus valable (BitBlt échoue) ; on en recrée un et on réessaie
        _reset_sct()
        try:
            shot = _sct().grab(box)
        except mss.ScreenShotError as e:
            _reset_sct()
            raise CaptureError(str(e)) from e
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def _grab_tiled(x, y, w, h, pause=0.0):
    """Capture une surface par petits morceaux recollés."""
    if w <= TILE[0] and h <= TILE[1]:
        return _grab(x, y, w, h)
    out = Image.new("RGB", (w, h))
    for ty in range(0, h, TILE[1]):
        for tx in range(0, w, TILE[0]):
            tw, th = min(TILE[0], w - tx), min(TILE[1], h - ty)
            out.paste(_grab(x + tx, y + ty, tw, th), (tx, ty))
            if pause:
                time.sleep(pause)
    return out


def grab_window(hwnd):
    """Image complète du jeu, par morceaux (seulement pour la page de réglage des zones)."""
    if user32.IsIconic(hwnd):
        return None
    x, y, w, h = client_rect(hwnd)
    if w < 100 or h < 100:
        return None
    return _grab_tiled(x, y, w, h, pause=0.05)


def grab_region(hwnd, region):
    """Capture uniquement une zone ([x0, y0, x1, y1] en fractions de la fenêtre) : bien plus léger qu'une capture complète."""
    if user32.IsIconic(hwnd):
        return None
    x, y, w, h = client_rect(hwnd)
    if w < 100 or h < 100:
        return None
    x0, y0, x1, y1 = region
    left, top = x + int(x0 * w), y + int(y0 * h)
    rw, rh = int(x1 * w) - int(x0 * w), int(y1 * h) - int(y0 * h)
    if rw < 10 or rh < 10:
        return None
    return _grab_tiled(left, top, rw, rh)


def signature(img, text_only=False):
    """Empreinte réduite d'une image pour savoir si elle a changé sans relancer l'OCR.
    text_only : ne garde que les pixels lumineux (le texte), pour ignorer le décor 3D qui bouge derrière le tchat."""
    if text_only:
        r, g, b = img.split()
        g = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v > 150 else 0)
    else:
        g = ImageOps.grayscale(img)
    return g.resize((max(1, img.width // 4), max(1, img.height // 4)), Image.BILINEAR).tobytes()


def changed(a, b, threshold=3.0):
    """Différence moyenne entre deux empreintes (0-255) au-dessus du seuil ?"""
    if a is None or b is None or len(a) != len(b):
        return True
    step = 3  # échantillonnage : largement suffisant et 3x plus rapide
    diff = sum(abs(a[i] - b[i]) for i in range(0, len(a), step))
    return diff / (len(a) / step) > threshold


def crop_region(img, region):
    """region = [x0, y0, x1, y1] en fractions de la fenêtre."""
    w, h = img.size
    x0, y0, x1, y1 = region
    return img.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))


def prepare_for_ocr(img, scale=3, mode="gray"):
    # Texte clair sur fond spatial : niveaux de gris, agrandissement, inversion -> texte sombre sur fond clair.
    # mode "max" = canal le plus lumineux : fait ressortir le texte coloré (rouge, orange).
    # mode "text" = ne garde que les traits fins et lumineux (le texte), pas le décor 3D derrière.
    if mode == "text":
        r, g, b = img.convert("RGB").split()
        m = ImageChops.lighter(ImageChops.lighter(r, g), b)
        # ouverture morphologique : efface les traits de 1-2 px, garde les zones claires du décor
        bg = m.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5))
        strokes = ImageChops.subtract(m, bg).point(lambda v: min(255, v * 255 // 60))
        g = ImageChops.multiply(strokes, m.point(lambda v: 255 if v > 100 else 0))
        g = g.resize((g.width * scale, g.height * scale), Image.LANCZOS)
        return ImageOps.invert(g).convert("RGBA")
    if mode == "max":
        r, g, b = img.convert("RGB").split()
        g = ImageChops.lighter(ImageChops.lighter(r, g), b)
    else:
        g = ImageOps.grayscale(img)
    g = g.resize((g.width * scale, g.height * scale), Image.LANCZOS)
    g = ImageOps.autocontrast(g, cutoff=1)
    return ImageOps.invert(g).convert("RGBA")


def _line(words, text=None):
    return {
        "text": text if text is not None else " ".join(w["text"] for w in words),
        "x": min(w["x"] for w in words),
        "y": min(w["y"] for w in words),
        "h": max(w["h"] for w in words),
        "words": words,
    }


def _rows(words):
    """Regroupe les mots par ligne d'après leur hauteur à l'écran (l'OCR fusionne parfois deux lignes voisines)."""
    if not words:
        return []
    hs = sorted(w["h"] for w in words)
    tol = hs[len(hs) // 2] * 0.5
    rows = []
    for w in sorted(words, key=lambda w: w["y"] + w["h"] / 2):
        cy = w["y"] + w["h"] / 2
        if rows and abs(cy - rows[-1]["cy"]) < tol:
            r = rows[-1]
            r["words"].append(w)
            r["cy"] = sum(v["y"] + v["h"] / 2 for v in r["words"]) / len(r["words"])
        else:
            rows.append({"cy": cy, "words": [w]})
    return [_line(sorted(r["words"], key=lambda w: w["x"])) for r in rows]


_LANGS = {}


def _ocr_lang(lang):
    """Le pack OCR français n'est pas installé partout : on prend sinon une langue OCR disponible sur ce PC."""
    if lang not in _LANGS:
        if winocr.OcrEngine.is_language_supported(winocr.Language(lang)):
            _LANGS[lang] = lang
        else:
            tags = [l.language_tag for l in winocr.OcrEngine.available_recognizer_languages]
            # une langue à alphabet latin de préférence (les pseudos et le tchat sont en lettres latines)
            latin = [t for t in tags if t.split("-")[0] in ("fr", "en", "de", "es", "it", "pt", "nl")]
            _LANGS[lang] = (latin or tags or [lang])[0]
            print(f"OCR : langue {lang} absente de Windows, utilisation de {_LANGS[lang]}")
    return _LANGS[lang]


def ocr_lines(img, scale=3, lang="fr-FR", mode="gray", split_rows=False):
    """Renvoie une liste de lignes : {text, x, y, h, words:[{text,x,y,w,h}]} en px de l'image d'origine.
    split_rows : lignes recalculées d'après la position des mots plutôt que celles de l'OCR."""
    prepared = prepare_for_ocr(img, scale, mode)
    result = asyncio.run(winocr.to_coroutine(winocr.recognize_pil(prepared, _ocr_lang(lang))))
    lines, all_words = [], []
    for line in result.lines:
        words = []
        for wd in line.words:
            b = wd.bounding_rect
            words.append({"text": wd.text, "x": b.x / scale, "y": b.y / scale,
                          "w": b.width / scale, "h": b.height / scale})
        if words:
            lines.append(_line(words, line.text))
            all_words += words
    if split_rows:
        lines = _rows(all_words)
    lines.sort(key=lambda l: (round(l["y"]), l["x"]))
    return lines
