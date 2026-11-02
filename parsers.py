"""Transforme les lignes OCR en messages de tchat et en liste de Revenants, et détecte les changements."""
import difflib
import re
import time

from PIL import ImageChops

from capture import crop_region, ocr_lines


def similar(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


# --------------------------------------------------------------------------- tchat

CHAT_RE = re.compile(r"^[\[\(\{]\s*([^\]\)\}]{2,30}?)\s*[\]\)\}]\s*(.*)$")
NAME_RE = re.compile(r"^([^\s:;]{2,25})[:;]\s?(.*)$")
LEADING_JUNK = " 	'`.,·•�-_~*"
# canaux déjà vus : servent à découper une ligne dont le crochet fermant a été mal lu
KNOWN_CHANNELS = {"escouade", "Global", "Commerce", "Clan"}
MISREAD_CLOSE_RE = re.compile(r"^(.{2,30}?)[lI1|ï!]{1,2}[\.,]?\s+(\S{2,25}[:;].*)$")


def parse_chat_line(text):
    """'[escouade] youyou: salut' -> {channel, author, text}. Sans crochet en tête -> suite du message précédent."""
    text = text.strip().lstrip(LEADING_JUNK)
    if not text or text[0] not in "[({":
        return None
    m = CHAT_RE.match(text)
    if m:
        channel, rest = m.group(1).strip(), m.group(2).strip()
        KNOWN_CHANNELS.add(channel)
    else:
        channel, rest = "", text[1:].strip()
        for ch in sorted(KNOWN_CHANNELS, key=len, reverse=True):
            if len(rest) > len(ch) and similar(rest[:len(ch)], ch) >= 0.85:
                channel, rest = ch, rest[len(ch) + 1:].strip()  # +1 : le crochet mal lu
                break
        else:
            # ']' souvent lu 'l' ou 'I' : "[Orbite d'Erebosl pseudo: texte"
            m = MISREAD_CLOSE_RE.match(rest)
            if m:
                channel, rest = m.group(1).strip(), m.group(2)
    nm = NAME_RE.match(rest)
    if nm:
        return {"channel": channel, "author": nm.group(1), "text": nm.group(2)}
    return {"channel": channel, "author": "", "text": rest}


FADED = 190  # luminosité du texte en dessous de laquelle le message est en train de s'effacer
TASKBAR_RE = re.compile(r"\s*[QO©]?\s*Rechercher\s*$")  # barre des tâches Windows par-dessus le jeu


def _brightness(img, l):
    """Luminosité du texte d'une ligne (97e centile du canal le plus clair sur la ligne)."""
    x1 = max(w["x"] + w["w"] for w in l["words"])
    box = img.crop((int(l["x"]), int(l["y"]), int(x1) + 1, int(l["y"] + l["h"]) + 1)).convert("RGB")
    r, g, b = box.split()
    px = sorted(ImageChops.lighter(ImageChops.lighter(r, g), b).getdata())
    return px[int(len(px) * 0.97)] if px else 0


def chat_ocr(img):
    """Lignes du tchat : seuls les traits fins et clairs sont gardés (mode "text"), puis les mots sont
    regroupés par ligne d'après leur position (l'OCR fusionne parfois deux lignes)."""
    lines = ocr_lines(img, scale=3, mode="text", split_rows=True)
    for l in lines:
        l["bright"] = _brightness(img, l)
    return lines


def _split_glued(t):
    """Coupe une ligne là où commence un autre message ('[canal]' au milieu, crochet ouvrant souvent mal lu)."""
    words = t.split(" ")
    cuts = []
    for i, w in enumerate(words[1:], 1):
        core = w.lstrip("[({LlI|").rstrip("])}lIj|")
        if len(core) >= 4 and w[-1:] in "])}lIj|" and any(similar(core, ch) >= 0.8 for ch in KNOWN_CHANNELS):
            cuts.append(i)
    parts, prev = [], 0
    for i in cuts:
        parts.append(" ".join(words[prev:i]))
        prev = i
    parts.append(" ".join(words[prev:]))
    # le crochet ouvrant du morceau coupé a pu être lu 'L' ou 'l' : on le remet
    return [parts[0]] + ["[" + q.lstrip("[({LlI|") if q[:1] not in "[({" else q for q in parts[1:]]


def chat_messages_from_ocr(lines):
    msgs = []
    prev = None
    for l in lines:
        t = TASKBAR_RE.sub("", l["text"]).strip()
        if len(t) < 2 or l.get("bright", 255) < FADED:
            continue
        p = parse_chat_line(t)
        if p:
            for piece in _split_glued(t):
                q = parse_chat_line(piece)
                if q:
                    q["raw"] = piece
                    msgs.append(q)
            prev = l
        elif msgs and prev and 4 < l["x"] < 40 and 0 < l["y"] - prev["y"] < prev["h"] * 2.2:
            # ligne repliée, juste sous le message et alignée à gauche : on la rattache au message précédent
            msgs[-1]["text"] += " " + t
            msgs[-1]["raw"] += " " + t
            prev = l
        # sinon : bruit du décor lu comme du texte
    return msgs


BRACKETS = str.maketrans("({)}", "[[]]")


class ChatTracker:
    """Repère les nouveaux messages entre deux scans (le tchat défile et s'efface avec le temps)."""

    def __init__(self, history_size=300):
        self.history = []          # messages confirmés
        self.next_id = 1
        self.pending = []          # lignes vues une seule fois (attente de confirmation)
        self.history_size = history_size

    @staticmethod
    def _norm(m):
        # Sans le canal : « [escouade] youyou abattu ! » et « [escouade] MN-Nova abattu ! » se ressemblaient
        # à plus de 80 % à cause du canal commun, et le suivi les confondait.
        body = (m.get("author", "") + " " + m.get("text", "")).strip()
        return (body or m["raw"]).translate(BRACKETS).lower()

    def _match(self, a, b, threshold=0.8):
        return similar(self._norm(a), self._norm(b)) >= threshold

    def update(self, current):
        # Une ligne doit être vue sur 2 scans consécutifs pour éviter les lectures ratées.
        stable = [m for m in current if any(self._match(m, p) for p in self.pending)]
        self.pending = current
        if not stable:
            return []

        recent = self.history[-40:]
        # Plus grand k tel que les k premières lignes visibles = les k dernières de l'historique.
        k = 0
        for cand in range(min(len(stable), len(recent)), 0, -1):
            if all(self._match(stable[i], recent[len(recent) - cand + i]) for i in range(cand)):
                k = cand
                break
        new = stable[k:]
        if k == 0:
            # Pas d'alignement (tchat effacé puis nouveau message) : on ignore ce qui est déjà connu.
            new = [m for m in new if not any(self._match(m, h, 0.7) for h in recent[-15:])]

        added = []
        for m in new:
            msg = {"id": self.next_id, "ts": time.time(), "channel": m["channel"],
                   "author": m["author"], "text": m["text"], "raw": m["raw"]}
            self.next_id += 1
            self.history.append(msg)
            added.append(msg)
        self.history = self.history[-self.history_size:]
        return added


# --------------------------------------------------------------------------- revenants

REV_NAME_RE = re.compile(r"^([A-Z0-9]{2})[\-\.\s_~]+([A-Za-z0-9]{3,})")
# date d'apparition affichée par le jeu : « Oct 31 · 04:15 » (mois en anglais)
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
DATE_RE = re.compile(r"\b([A-Za-z]{3})[a-z]*\.?\s*(\d{1,2})\b")
# l'heure termine la ligne : on la cherche à la fin pour ne pas prendre le jour (« Nov 1, 04:15 » -> 04:15)
TIME_END_RE = re.compile(r"(\d{1,2})\s*[:;.,·•'’]\s*(\d{2})\D*$")
TIME_NO_COLON_RE = re.compile(r"(\d{2})\s+(\d{2})\D*$")  # « : » non lu par l'OCR
DIGIT_FIX = str.maketrans("OoQlI|", "000111")  # lettres lues à la place des chiffres (O4:15, 2l:32)

# colonnes du panneau Revenants (fractions de la largeur du panneau)
COL_LEFT = (0.14, 0.46)
COL_RIGHT = (0.46, 1.0)
COL_TIME = (0.80, 1.0)


def read_when(text):
    """'Nov 1 · 04:15' -> ('04:15', '11-01') ; chaîne vide pour ce qui n'a pas pu être lu."""
    day = ""
    m = DATE_RE.search(text)
    if m and m.group(1).lower() in MONTHS and 1 <= int(m.group(2)) <= 31:
        day = f"{MONTHS.index(m.group(1).lower()) + 1:02d}-{int(m.group(2)):02d}"
        text = text[m.end():]
    text = text.translate(DIGIT_FIX)
    for rx in (TIME_END_RE, TIME_NO_COLON_RE):
        m = rx.search(text)
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            return f"{int(m.group(1)):02d}:{m.group(2)}", day
    return "", day


def row_when(lines, y, row_h):
    """Date de l'entrée dont le nom est à la hauteur y : l'OCR coupe parfois « Nov 1 » et « 04:15 »
    en deux lignes, on recolle tout ce qui est sur la ligne du nom, de gauche à droite."""
    on_row = sorted((l for l in lines if abs(l["y"] - y) < row_h * 0.35), key=lambda l: l["x"])
    return read_when(" ".join(l["text"] for l in on_row))


def fix_name(prefix, rest):
    prefix = prefix.upper().replace("0", "O").replace("5", "S")
    if any(c.isalpha() for c in rest):
        # les noms sont alphabétiques : chiffres = lettres mal lues (Ge1torfi -> Geltorfi)
        rest = rest.replace("1", "l").replace("0", "o").replace("5", "s")
    return f"{prefix}-{rest}"


def has_revenants_title(panel_img):
    """Le panneau peut afficher un autre onglet (missions, cibles…) : on vérifie le titre REVENANTS."""
    w, h = panel_img.size
    top = panel_img.crop((0, 0, w, max(20, int(h * 0.12))))
    for l in ocr_lines(top, scale=3):
        for word in l["text"].split():
            if similar(word, "REVENANTS") >= 0.75:
                return True
    return False


def _sub(img, col):
    w, h = img.size
    return img.crop((int(col[0] * w), 0, int(col[1] * w), h))


# barre de vie (ligne 'Statut') : de ~55 % à ~96 % de la largeur du panneau
BAR_X0 = 0.54
BAR_X1 = 0.952


def _is_bar_red(px):
    r, g, b = px[:3]
    return r > 150 and g < 100 and b < 90


def health_bar(img, y0, y1):
    """Vie du Revenant (0..1) d'après la barre rouge dans la bande y0..y1 ; 0 si pas de barre (combat pas commencé)."""
    w, h = img.size
    x0, x1 = int(BAR_X0 * w), int(BAR_X1 * w)
    y0, y1 = max(0, int(y0)), min(h, int(y1))
    if y1 <= y0:
        return 0.0
    px = img.convert("RGB").load()
    best_left, best_right, best_count = None, None, 0
    for y in range(y0, y1):
        reds = [x for x in range(x0, x1) if _is_bar_red(px[x, y])]
        if len(reds) > best_count:
            best_count, best_left, best_right = len(reds), reds[0], reds[-1]
    if best_count < max(3, (x1 - x0) * 0.01):
        return 0.0
    # la barre commence juste après 'Statut' et finit au bord droit du panneau
    start = min(best_left, int(0.552 * w))
    return max(0.01, min(1.0, (best_right - start + 1) / (x1 - start)))


def parse_revenants(panel_img):
    """Renvoie (panel_visible, [revenants])."""
    if not has_revenants_title(panel_img):
        return False, []
    left = ocr_lines(_sub(panel_img, COL_LEFT), scale=3)
    # 2e lecture des noms à une autre échelle : l'OCR en rate parfois un
    left_img = _sub(panel_img, COL_LEFT)
    for extra in (ocr_lines(left_img, scale=3, mode="max"), ocr_lines(left_img, scale=2)):
        for l in extra:
            if REV_NAME_RE.match(l["text"].strip()) and not any(abs(l["y"] - o["y"]) < 6 for o in left):
                left.append(l)
    left.sort(key=lambda l: l["y"])
    right = ocr_lines(_sub(panel_img, COL_RIGHT), scale=3)
    times = ocr_lines(_sub(panel_img, COL_TIME), scale=4)
    times_retry = []  # 2e lecture de la colonne de la date, faite seulement si une heure manque

    all_text = " ".join(l["text"] for l in left + right).lower()
    if not left or ("statut" not in all_text and "type" not in all_text and "planete" not in all_text
                    and "plan" not in all_text):
        return False, []

    # fin du panneau
    stop_y = None
    for l in left + right:
        if "cacher" in l["text"].lower() or "mission de chasse" in l["text"].lower():
            stop_y = l["y"] if stop_y is None else min(stop_y, l["y"])

    heads = []
    for l in left:
        if stop_y is not None and l["y"] >= stop_y - 2:
            continue
        m = REV_NAME_RE.match(l["text"].strip().lstrip("!△▲⚠ "))
        if m:
            heads.append((l["y"], fix_name(m.group(1), m.group(2)), l))
    if not heads:
        return True, []

    row_h = heads[1][0] - heads[0][0] if len(heads) > 1 else 50
    entries = []
    for i, (y, name, _) in enumerate(heads):
        y_end = heads[i + 1][0] if i + 1 < len(heads) else min(y + row_h, stop_y or 1e9)
        y_from = y - row_h * 0.3

        def in_row(l):
            return y_from <= l["y"] < y_end - row_h * 0.15

        lft = [l["text"].strip() for l in left if in_row(l) and l["y"] > y + 3]
        rgt = [l for l in right if in_row(l) and l["y"] > y + 3]  # la ligne du nom porte la date
        when, day = row_when(times, y, row_h)
        if not when:
            # la colonne de droite lit aussi la date sur la ligne du nom, à une autre échelle
            when, day2 = row_when(right, y, row_h)
            day = day or day2
        if not when:
            if not times_retry:
                times_retry = ocr_lines(_sub(panel_img, COL_TIME), scale=3, mode="max")
            when, day2 = row_when(times_retry, y, row_h)
            day = day or day2

        info, status = "", ""
        status_line = None
        for l in rgt:
            t = l["text"].strip()
            if t.lower().startswith("stat") or t.lower().startswith("st"):
                status = t
                status_line = l
            elif not info:
                info = t
        location = lft[0] if lft else ""
        planet = lft[1] if len(lft) > 1 else ""
        if planet.lower().startswith("plan"):
            planet = ""  # libellé vide 'Planète' = planète inconnue
        if status.lower().startswith("stat") and len(status) <= 8:
            status = ""

        # la barre est sur la dernière ligne de l'entrée ; on cherche dans toute l'entrée sauf la ligne du nom
        bar = health_bar(panel_img, y + row_h * 0.3, y_end - 1)

        entries.append({
            "name": name,
            "danger": name.startswith("XC"),
            "time": when,
            "date": day,
            "system": location,
            "planet": planet,
            "info": info,
            "status": status,
            "bar": round(bar, 2),
        })
    return True, entries


class RevenantTracker:
    """Garde l'état stable des Revenants et produit des évènements quand ça bouge."""

    FIELDS = (("system", "système"), ("planet", "planète"), ("info", "infos"), ("status", "statut"))

    def __init__(self):
        self.state = {}        # nom -> entrée confirmée
        self.candidates = {}   # nom -> (entrée, nb de scans)
        self.missing = {}      # nom -> nb de scans absents
        self.pending = {}      # (nom, champ) -> valeur vue une fois, en attente de confirmation
        self.hp_notified = {}  # nom -> dernière vie annoncée
        self.visible = False
        self.primed = False    # la 1re liste lue au démarrage ne génère pas d'alerte "nouveau"

    def _key(self, e, taken):
        """Revenant déjà suivi qui correspond à cette lecture (nom proche malgré l'OCR), sinon son nom.

        Deux noms proches avec des heures d'apparition différentes sont deux Revenants (SC-Neman / SC-Nela),
        et une même lecture ne peut pas désigner deux fois le même Revenant (`taken`)."""
        name = e["name"]
        if name not in taken and (name in self.state or name in self.candidates):
            return name
        best, best_score = name, 0.8
        for k in list(self.state) + list(self.candidates):
            if k in taken:
                continue
            score = similar(k, name)
            if score < best_score:
                continue
            old = self.state.get(k) or self.candidates[k][0]
            if old.get("time") and e["time"] and old["time"] != e["time"]:
                continue
            best, best_score = k, score
        return best

    def _confirmed(self, k, field, value, same):
        """Un changement doit être lu sur 2 scans consécutifs (évite les fausses alertes de l'OCR)."""
        prev = self.pending.get((k, field))
        self.pending[(k, field)] = value
        return prev is not None and same(prev, value)

    @staticmethod
    def _where(e):
        return " / ".join(x for x in (e.get("system"), e.get("planet")) if x) or "?"

    def _changes(self, k, old, e):
        events = []
        moved, other = [], []
        for field, label in self.FIELDS:
            if not e[field]:
                e[field] = old[field]  # lecture vide : on garde l'ancienne valeur
                self.pending.pop((k, field), None)
                continue
            if similar(e[field], old[field] or "") >= 0.75:
                e[field] = old[field]  # même valeur (petites variations d'OCR)
                self.pending.pop((k, field), None)
                continue
            if not self._confirmed(k, field, e[field], lambda a, b: similar(a, b) >= 0.75):
                e[field] = old[field]
                continue
            self.pending.pop((k, field), None)
            if field == "info":
                continue  # le jeu fait défiler plusieurs butins sur cette ligne : pas d'alerte
            (moved if field in ("system", "planet") else other).append((label, e[field]))
        if moved:
            events.append({"type": "move", "name": k,
                           "text": f"📍 {k} s'est déplacé : {self._where(old)} → {self._where(e)}"})
        if other:
            txt = ", ".join(f"{label} : {new}" for label, new in other)
            events.append({"type": "change", "name": k, "text": f"{k} — {txt}"})

        # vie : apparition de la barre = combat commencé, puis paliers de 10 %
        hp_old, hp = old["bar"], e["bar"]

        def same_state(a, b):
            return (a > 0) == (b > 0) and abs(a - b) < 0.06

        if (hp_old > 0) != (hp > 0) or abs(hp - hp_old) >= 0.03:
            if self._confirmed(k, "bar", hp, same_state):
                self.pending.pop((k, "bar"), None)
                last = self.hp_notified.get(k, 0.0)
                if hp > 0 and hp_old == 0:
                    events.append({"type": "fight", "name": k,
                                   "text": f"⚔ Combat commencé sur {k} ({self._where(e)}) — vie {round(hp * 100)} %"})
                    self.hp_notified[k] = hp
                elif hp == 0 and hp_old > 0:
                    events.append({"type": "hp", "name": k, "text": f"{k} : barre de vie disparue"})
                    self.hp_notified.pop(k, None)
                elif abs(hp - last) >= 0.10:
                    arrow = "▼" if hp < last else "▲"
                    events.append({"type": "hp", "name": k, "text": f"{arrow} {k} : vie {round(hp * 100)} %"})
                    self.hp_notified[k] = hp
            else:
                e["bar"] = hp_old
        else:
            self.pending.pop((k, "bar"), None)
        return events

    def update(self, visible, entries):
        events = []
        self.visible = visible
        if not visible:
            return events
        seen = set()
        # noms lus à l'identique d'abord : une lecture approximative ne peut pas leur prendre leur place
        for e in sorted(entries, key=lambda e: e["name"] not in self.state):
            k = self._key(e, seen)
            seen.add(k)
            self.missing.pop(k, None)
            _, n = self.candidates.get(k, (None, 0))
            self.candidates[k] = (e, n + 1)
            if n + 1 < 2:
                continue
            old = self.state.get(k)
            e = dict(e, name=k)
            if old is None:
                self.state[k] = e
                if e["bar"] > 0:
                    self.hp_notified[k] = e["bar"]
                if not self.primed:
                    continue
                fight = f" — en combat, vie {round(e['bar'] * 100)} %" if e["bar"] > 0 else ""
                events.append({"type": "new", "name": k,
                               "text": f"Nouveau Revenant {k} — {self._where(e)} {e['time']}{fight}".strip()})
                continue
            events += self._changes(k, old, e)
            self.state[k] = e

        for k in list(self.state):
            if k in seen:
                continue
            self.missing[k] = self.missing.get(k, 0) + 1
            if self.missing[k] >= 6:
                old = self.state.pop(k)
                self.candidates.pop(k, None)
                self.missing.pop(k, None)
                self.hp_notified.pop(k, None)
                for key in [x for x in self.pending if x[0] == k]:
                    del self.pending[key]
                txt = f"💀 {k} a disparu (vaincu ?)" if old["bar"] > 0 else f"{k} a disparu de la liste"
                events.append({"type": "gone", "name": k, "text": txt})
        if self.state:
            self.primed = True
        for k in list(self.candidates):
            if k not in seen and k not in self.state:
                del self.candidates[k]
        return events

    def snapshot(self):
        return sorted(self.state.values(), key=lambda e: e.get("time", ""))


def scan_regions(img, cfg):
    chat_img = crop_region(img, cfg["regions"]["chat"])
    rev_img = crop_region(img, cfg["regions"]["revenants"])
    return chat_img, rev_img


def _channel_words(lines):
    words = [w["text"].strip(".,:;'\"()[]|") for l in lines for w in l["words"]]
    # chiffres lus à la place de lettres (Reggi0, A11iance)
    words = [w.translate(str.maketrans("01", "ol")) if any(c.isalpha() for c in w) else w for w in words]
    # les icônes à gauche du nom sont parfois lues comme des lettres isolées ('uZ', 'O')
    return [w for w in words if len(w) >= 3 and sum(c.isalpha() for c in w) >= len(w) - 1]


def read_channel(img):
    """Nom du canal affiché dans le sélecteur sous le tchat ('Flotte', 'Escouade'…)."""
    if img is None:
        return ""
    # fond texturé derrière le nom : d'abord la lecture qui ne garde que le texte, sinon la lecture simple
    for mode in ("text", "gray"):
        words = _channel_words(ocr_lines(img, scale=4, mode=mode))
        if words:
            return " ".join(words)
    return ""
