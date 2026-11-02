"""Envoie la liste des Revenants au site PG Progression (onglet Revenants, partagé entre tous les joueurs)."""
import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

HEARTBEAT = 30  # renvoi même sans changement : le site sait que la liste est toujours suivie
FRESH = 15      # au-delà, la dernière liste lue est trop vieille pour être envoyée (jeu fermé, réduit…)
RETRY = 10      # pause après un échec d'envoi
HHMM = re.compile(r"(\d{2}):(\d{2})")
MMDD = re.compile(r"(\d{2})-(\d{2})")


def appeared_at(hhmm, mmdd=""):
    """Heure HH:MM et jour MM-DD lus dans le jeu (heure du PC) -> date ISO avec fuseau.

    Sans le jour : aujourd'hui, ou la veille si l'heure tombe dans le futur."""
    m = HHMM.fullmatch(hhmm or "")
    if not m:
        return None
    now = datetime.now()
    try:
        t = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        d = MMDD.fullmatch(mmdd or "")
        if d:
            t = t.replace(month=int(d.group(1)), day=int(d.group(2)))
            if t > now + timedelta(days=1):
                t = t.replace(year=t.year - 1)  # « Dec 31 » lu le 1er janvier
        elif t > now + timedelta(minutes=5):
            t -= timedelta(days=1)
    except ValueError:
        return None
    return t.astimezone().isoformat()  # heure locale naïve -> fuseau du PC (heure d'été comprise)


class SiteSync:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.payload = None     # dernière liste lue avec le panneau visible
        self.offered_at = 0
        self.sent = None        # dernière liste envoyée avec succès
        self.sent_at = 0
        self.status = {"ok": None, "error": "", "last": 0}

    def enabled(self):
        return bool(self.cfg.get("site_url") and self.cfg.get("site_key"))

    def offer(self, visible, revenants):
        """Appelé après chaque lecture du panneau ; panneau fermé = rien à partager."""
        with self.lock:
            self.payload = [dict(r) for r in revenants] if visible else None
            self.offered_at = time.time()
        self.wake.set()

    def reset(self):
        """Adresse ou clé modifiée : renvoyer tout de suite."""
        with self.lock:
            self.sent = None
            self.status = {"ok": None, "error": "", "last": 0}
        self.wake.set()

    def state(self):
        with self.lock:
            return dict(self.status, enabled=self.enabled())

    def loop(self):
        while True:
            self.wake.wait(timeout=5)
            self.wake.clear()
            with self.lock:
                payload = self.payload if time.time() - self.offered_at < FRESH else None
                due = payload is not None and (payload != self.sent or time.time() - self.sent_at >= HEARTBEAT)
            if not self.enabled() or not due:
                continue
            ok, err = self._post(payload)
            with self.lock:
                self.status.update(ok=ok, error=err)
                if ok:
                    self.status["last"] = self.sent_at = time.time()
                    self.sent = payload
            if not ok:
                time.sleep(RETRY)

    def _post(self, payload):
        body = {
            "panelVisible": True,
            "revenants": [dict(r, appearedAt=appeared_at(r.get("time"), r.get("date"))) for r in payload],
        }
        req = urllib.request.Request(
            self.cfg["site_url"].rstrip("/") + "/api/revenants/report",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-API-Key": self.cfg["site_key"],
                     "User-Agent": "PGCompanion"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10).read()
            return True, ""
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error") or f"erreur {e.code}"
            except ValueError:
                msg = f"erreur {e.code}"
            return False, msg
        except Exception as e:
            return False, f"site injoignable ({getattr(e, 'reason', e)})"
