"""PG Companion : suit le tchat et les Revenants de Pirate Galaxy et les sert sur le téléphone (Wi-Fi local)."""
import io
import json
import os
import secrets
import socket
import sys
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import capture
import drone_guard
import game_input
import parsers
import site_sync
import updater
from version import VERSION

if getattr(sys, "frozen", False):
    # exécutable (build.py) : pages web embarquées dans l'exe, config et historique à côté de l'exe
    BASE = os.path.dirname(sys.executable)
    STATIC = os.path.join(sys._MEIPASS, "static")
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
    STATIC = os.path.join(BASE, "static")
CONFIG_PATH = os.path.join(BASE, "config.json")
CHAT_LOG = os.path.join(BASE, "chat_log.jsonl")

DEFAULT_CONFIG = {
    "port": 8765,
    "token": "",
    "scan_interval": 1.5,      # tchat (secondes)
    "revenant_interval": 3.0,  # panneau Revenants (secondes)
    "regions": {
        # fractions de la fenêtre du jeu : [x0, y0, x1, y1]
        "chat": [0.0, 0.73, 0.25, 0.928],
        "revenants": [0.795, 0.04, 1.0, 0.40],
        "channel": [0.443, 0.922, 0.476, 0.945],  # sélecteur de canal sous le tchat
    },
    "channels": [],                  # ordre des canaux (flèche ↓), appris par « Détecter »
    "input": {
        "open_with_enter": True,     # Entrée ouvre la saisie du tchat
        "restore_focus": True,
        "focus_delay": 0.25,
    },
    "ntfy_topic": "",                # optionnel : sujet ntfy.sh pour des notifications même écran verrouillé
    "site_url": "https://pgp.watolua.fr",  # site PG Progression qui partage les Revenants entre joueurs
    "site_key": "",                  # optionnel : clé API générée sur l'onglet Revenants du site (vide = pas d'envoi)
    "auto_update": True,             # installe la dernière version publiée au démarrage (exe uniquement)
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    if not cfg["token"]:
        cfg["token"] = secrets.token_urlsafe(8)
    save_config(cfg)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


class Companion:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()       # protège l'état partagé
        self.game_lock = threading.Lock()  # une seule action à la fois sur la fenêtre du jeu
        self.chat = parsers.ChatTracker()
        self.revs = parsers.RevenantTracker()
        self.site = site_sync.SiteSync(cfg)
        self.events = []
        self.next_event = 1
        self.status = {"game": False, "revenants_panel": False, "last_scan": 0, "error": "", "channel": "", "drone": None}
        self.last_chat_img = None
        self.last_rev_img = None
        self._chat_sig = self._rev_sig = self._chan_sig = None
        self._chat_cache = self._rev_cache = None
        self._drone_seen = 0     # dernière recherche de DroneHandler
        self._drone_cfg = None   # sa config.json (None : pas lancé, NOT_FOUND : lancé mais config introuvable)
        self._load_chat_log()

    def _load_chat_log(self):
        if not os.path.exists(CHAT_LOG):
            return
        with open(CHAT_LOG, encoding="utf-8") as f:
            for line in f.readlines()[-300:]:
                try:
                    m = json.loads(line)
                    self.chat.history.append(m)
                    if m.get("channel"):
                        parsers.KNOWN_CHANNELS.add(m["channel"])
                except ValueError:
                    pass
        if self.chat.history:
            self.chat.next_id = self.chat.history[-1]["id"] + 1

    def add_event(self, ev):
        ev = dict(ev, id=self.next_event, ts=time.time())
        self.next_event += 1
        self.events.append(ev)
        self.events = self.events[-200:]
        topic = self.cfg.get("ntfy_topic")
        if topic:
            threading.Thread(target=push_ntfy, args=(topic, ev), daemon=True).start()

    def _scan_chat(self, hwnd):
        img = capture.grab_region(hwnd, self.cfg["regions"]["chat"])
        if img is None:
            return None
        sig = capture.signature(img, text_only=True)
        if capture.changed(sig, self._chat_sig, threshold=1.0) or self._chat_cache is None:
            self._chat_cache = parsers.chat_messages_from_ocr(parsers.chat_ocr(img))
            self._chat_sig = sig
        with self.lock:
            self.last_chat_img = img
        return self._chat_cache

    def _read_channel(self, hwnd):
        return parsers.read_channel(capture.grab_region(hwnd, self.cfg["regions"]["channel"]))

    def _scan_channel(self, hwnd):
        img = capture.grab_region(hwnd, self.cfg["regions"]["channel"])
        if img is None:
            return
        sig = capture.signature(img)
        if capture.changed(sig, self._chan_sig, threshold=2.0):
            self._chan_sig = sig
            name = parsers.read_channel(img)
            if name:
                with self.lock:
                    self.status["channel"] = name

    def _drone_path(self, refresh=False):
        """config.json du DroneHandler lancé, retrouvée à côté du programme (toutes les 10 s au plus)."""
        if refresh or time.time() - self._drone_seen > 10:
            self._drone_seen = time.time()
            self._drone_cfg = drone_guard.locate(self.cfg.get("drone_handler_config"))
        return self._drone_cfg

    def _scan_drone(self):
        """Recopie le voyant DroneHandler. Il n'est affiché que jeu au premier plan : sinon on garde le dernier état."""
        path = self._drone_path()
        s = drone_guard.state(path)
        with self.lock:
            if path is None:
                self.status["drone"] = "absent"  # pas de DroneHandler : l'appli fonctionne comme s'il n'existait pas
            elif s:
                self.status["drone"] = s
            elif self.status["drone"] == "absent":
                self.status["drone"] = None  # lancé, voyant pas encore lu

    def toggle_drone(self):
        path = self._drone_path(refresh=True)
        if path is None:
            return False, "DroneHandler n'est pas lancé", None
        with self.game_lock:
            s = drone_guard.toggle(path)
        with self.lock:
            if s is None and self.status["drone"]:  # voyant invisible : on suppose que la bascule a marché
                s = "off" if self.status["drone"] == "on" else "on"
            self.status["drone"] = s
        return True, "ok", s

    def _scan_revenants(self, hwnd):
        img = capture.grab_region(hwnd, self.cfg["regions"]["revenants"])
        if img is None:
            return None
        sig = capture.signature(img)
        if capture.changed(sig, self._rev_sig, threshold=1.5) or self._rev_cache is None:
            self._rev_cache = parsers.parse_revenants(img)
            self._rev_sig = sig
        with self.lock:
            self.last_rev_img = img
        return self._rev_cache

    def scan_once(self, with_revenants):
        hwnd = capture.find_game_window()
        if not hwnd:
            with self.lock:
                self.status.update(game=False, error="fenêtre du jeu introuvable")
            return
        with self.game_lock:
            current = self._scan_chat(hwnd)
            self._scan_channel(hwnd)
            revs = self._scan_revenants(hwnd) if with_revenants else None
        self._scan_drone()
        if current is None:
            with self.lock:
                self.status.update(game=True, error="jeu réduit : impossible de lire l'écran")
            return
        with self.lock:
            added = self.chat.update(current)
            if added:
                with open(CHAT_LOG, "a", encoding="utf-8") as f:
                    for m in added:
                        f.write(json.dumps(m, ensure_ascii=False) + "\n")
            if revs is not None:
                visible, entries = revs
                for ev in self.revs.update(visible, entries):
                    self.add_event(ev)
                self.status["revenants_panel"] = visible
                self.site.offer(visible, self.revs.snapshot())
            self.status.update(game=True, last_scan=time.time(), error="")

    def loop(self):
        n = 0
        every = max(1, round(self.cfg["revenant_interval"] / self.cfg["scan_interval"]))
        capture_failing = False
        while True:
            t0 = time.time()
            try:
                self.scan_once(with_revenants=(n % every == 0))
                if capture_failing:
                    print("Capture de l'écran rétablie.")
                    capture_failing = False
            except capture.CaptureError as e:
                # écran verrouillé, veille… : passager, un seul message tant que ça dure
                if not capture_failing:
                    print(f"Capture de l'écran impossible pour l'instant ({e}), nouvel essai en continu.")
                    capture_failing = True
                with self.lock:
                    self.status["error"] = "capture de l'écran impossible (écran verrouillé ?)"
            except Exception as e:
                traceback.print_exc()
                with self.lock:
                    self.status["error"] = f"erreur de scan : {e}"
            n += 1
            time.sleep(max(0.2, self.cfg["scan_interval"] - (time.time() - t0)))

    def state(self, chat_after, event_after):
        with self.lock:
            return {
                "status": self.status,
                "chat": [m for m in self.chat.history if m["id"] > chat_after][-200:],
                "events": [e for e in self.events if e["id"] > event_after],
                "revenants": self.revs.snapshot(),
                "site": self.site.state(),
            }

    def _drone_paused(self):
        """Avant les flèches : DroneHandler doit être en pause (on ne le bascule pas à la place de l'utilisateur)."""
        ok, msg = drone_guard.require_paused(self._drone_path(refresh=True))
        if not ok:
            s = drone_guard.state(self._drone_path())
            if s:
                with self.lock:
                    self.status["drone"] = s
        return ok, msg

    def _drone_blocks(self):
        with self.lock:
            return self.status.get("drone") == "on"

    def send(self, text, channel=None):
        hwnd = capture.find_game_window()
        if not hwnd:
            return False, "jeu non lancé"
        with self.game_lock:
            ok, msg = game_input.send_chat(hwnd, text, self.cfg["input"], channel=channel,
                                           read_channel=lambda: self._read_channel(hwnd),
                                           order=self.cfg["channels"], before_arrows=self._drone_paused)
            self._chan_sig = None  # relire le canal affiché au prochain scan
        return ok, msg

    def discover_channels(self):
        hwnd = capture.find_game_window()
        if not hwnd:
            return False, "jeu non lancé", []
        if self._drone_blocks():  # inutile de prendre la main sur le jeu pour rien
            return False, "DroneHandler est actif : mets-le en pause (voyant rouge) avant la détection", []
        with self.game_lock:
            ok, msg, order = game_input.discover_channels(hwnd, self.cfg["input"], lambda: self._read_channel(hwnd),
                                                          before_arrows=self._drone_paused)
            if ok and order:
                self.cfg["channels"] = order
                save_config(self.cfg)
            self._chan_sig = None
        return ok, msg, order

    def channels(self):
        with self.lock:
            current = self.status.get("channel", "")
        return {"current": current, "known": self.cfg["channels"]}

    def snapshot_jpeg(self, which):
        if which == "full":
            hwnd = capture.find_game_window()
            try:
                img = capture.grab_window(hwnd) if hwnd else None
            except capture.CaptureError:
                img = None
            if img is not None:
                img.thumbnail((1280, 1280))
        else:
            with self.lock:
                img = self.last_chat_img if which == "chat" else self.last_rev_img
        if img is None:
            return None
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        return buf.getvalue()


def push_ntfy(topic, ev):
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=ev["text"].encode("utf-8"),
                                     headers={"Title": "Revenants", "Tags": "skull"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print("ntfy:", e)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # aucun paquet envoyé : Windows choisit son réseau principal (ici le mesh en Ethernet)
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def other_ips(main_ip):
    """Les autres adresses du PC (le PC peut être sur deux réseaux : Ethernet + Wi-Fi)."""
    try:
        ips = {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        return []
    return sorted(ip for ip in ips if ip != main_ip and not ip.startswith(("127.", "169.254.")))


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            if os.environ.get("PGC_HTTP_LOG"):  # diagnostic : journal des requêtes
                print(self.client_address[0], fmt % args, flush=True)

        def _authorized(self, qs):
            key = qs.get("k", [""])[0] or self.headers.get("X-Key", "")
            return secrets.compare_digest(key, app.cfg["token"])

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _file(self, name, ctype):
            with open(os.path.join(STATIC, name), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if u.path in ("/", "/index.html"):
                return self._file("index.html", "text/html; charset=utf-8")
            if u.path == "/calibrate":
                return self._file("calibrate.html", "text/html; charset=utf-8")
            if u.path == "/manifest.json":
                return self._file("manifest.json", "application/manifest+json")
            if not self._authorized(qs):
                return self._json({"error": "clé invalide"}, 403)
            if u.path == "/api/state":
                return self._json(app.state(int(qs.get("chat_after", ["0"])[0]),
                                            int(qs.get("event_after", ["0"])[0])))
            if u.path == "/api/config":
                return self._json({"version": VERSION, "regions": app.cfg["regions"], "input": app.cfg["input"],
                                   "site": {"url": app.cfg["site_url"], "key_set": bool(app.cfg["site_key"])}})
            if u.path == "/api/channels":
                return self._json(app.channels())
            if u.path.startswith("/api/snap/"):
                which = u.path.rsplit("/", 1)[-1].replace(".jpg", "")
                data = app.snapshot_jpeg(which)
                if data is None:
                    return self._json({"error": "pas encore de capture"}, 404)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self._json({"error": "introuvable"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if not self._authorized(qs):
                return self._json({"error": "clé invalide"}, 403)
            n = int(self.headers.get("Content-Length", "0"))
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json({"error": "JSON invalide"}, 400)
            if u.path == "/api/send":
                text = str(data.get("text", ""))[:250]
                ok, msg = app.send(text, channel=str(data.get("channel") or "").strip() or None)
                return self._json({"ok": ok, "message": msg}, 200 if ok else 500)
            if u.path == "/api/channels/discover":
                ok, msg, order = app.discover_channels()
                return self._json({"ok": ok, "message": msg, "known": order}, 200 if ok else 500)
            if u.path == "/api/drone/toggle":
                ok, msg, s = app.toggle_drone()
                return self._json({"ok": ok, "message": msg, "drone": s}, 200 if ok else 500)
            if u.path == "/api/config":
                for key in ("chat", "revenants", "channel"):
                    r = data.get("regions", {}).get(key)
                    if r and len(r) == 4 and all(0 <= float(v) <= 1 for v in r):
                        app.cfg["regions"][key] = [round(float(v), 4) for v in r]
                for key, val in data.get("input", {}).items():
                    if key in app.cfg["input"]:
                        app.cfg["input"][key] = val
                site = data.get("site")
                if isinstance(site, dict):
                    url = str(site.get("url") or "").strip().rstrip("/")
                    if url and not url.startswith(("http://", "https://")):
                        return self._json({"error": "l'adresse du site doit commencer par https://"}, 400)
                    app.cfg["site_url"] = url
                    if str(site.get("key") or "").strip():  # champ vide : on garde la clé enregistrée
                        app.cfg["site_key"] = str(site["key"]).strip()
                    app.site.reset()
                save_config(app.cfg)
                return self._json({"ok": True})
            self._json({"error": "introuvable"}, 404)

    return Handler


def lower_priority():
    """Priorité basse : le jeu et l'audio passent toujours avant l'OCR."""
    import ctypes
    BELOW_NORMAL_PRIORITY_CLASS = 0x4000
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = ctypes.c_void_p  # pseudo-handle -1 : tronqué en 32 bits sans ça
    k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    if not k32.SetPriorityClass(k32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS):
        print("impossible de baisser la priorité")


def main():
    lower_priority()
    cfg = load_config()
    if cfg["auto_update"]:
        updater.update_and_restart()  # relance le nouvel exe s'il y a une mise à jour
    app = Companion(cfg)
    url = f"http://{lan_ip()}:{cfg['port']}/?k={cfg['token']}"
    print("=" * 60)
    print(f" PG Companion {VERSION} lancé")
    print(" Sur ton téléphone (même Wi-Fi), ouvre :")
    print("   " + url)
    for ip in other_ips(lan_ip()):
        print(f"   ou http://{ip}:{cfg['port']}/?k={cfg['token']}  (selon le réseau du téléphone)")
    print(" Réglage des zones (sur le PC) :")
    print(f"   http://localhost:{cfg['port']}/calibrate?k={cfg['token']}")
    if app.site.enabled():
        print(" Revenants partagés sur : " + cfg["site_url"])
    print("=" * 60)
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except (ImportError, UnicodeEncodeError):  # sortie redirigée : pas de QR code, ce n'est pas grave
        pass
    # Sous Windows, SO_REUSEADDR laisse une 2e copie s'installer sur le même port sans erreur :
    # on le désactive pour qu'un 2e lancement échoue clairement.
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        server = ThreadingHTTPServer(("0.0.0.0", cfg["port"]), make_handler(app))
    except OSError:
        print(f"Le port {cfg['port']} est déjà utilisé : PG Companion tourne sûrement déjà dans une autre fenêtre.")
        return
    threading.Thread(target=app.loop, daemon=True).start()
    threading.Thread(target=app.site.loop, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        traceback.print_exc()
    if getattr(sys, "frozen", False):
        input("Appuie sur Entrée pour fermer…")  # l'exe ouvre sa propre fenêtre : qu'elle ne disparaisse pas
