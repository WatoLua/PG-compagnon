# PG Companion

Suit le tchat et le panneau Revenants de Pirate Galaxy (lecture de l'écran par l'OCR de Windows) et les affiche
sur ton téléphone, sur le même Wi-Fi. Peut aussi partager les Revenants sur le site
[PG Progression](https://pgp.watolua.fr), onglet Revenants.

## Installation

1. Télécharge [PGCompanion.exe](https://github.com/WatoLua/PG-compagnon/releases/latest/download/PGCompanion.exe)
   et mets-le dans un dossier à lui (il y crée sa `config.json` et l'historique du tchat).
2. Lance-le. Windows peut afficher « Windows a protégé votre ordinateur » (l'exe n'est pas signé) :
   *Informations complémentaires* → *Exécuter quand même*.
3. Scanne le QR code affiché avec ton téléphone, ou ouvre l'adresse indiquée.
4. Réglage des zones de lecture : `http://localhost:8765/calibrate?k=…` (adresse affichée au lancement).

## Mises à jour

À chaque lancement, PG Companion regarde s'il existe une version plus récente sur ce dépôt ; si oui, il la
télécharge, vérifie son empreinte SHA-256, remplace l'exe et se relance. La config et l'historique sont gardés.
Pour désactiver : `"auto_update": false` dans `config.json`.

## Développement

```bat
start.bat     :: lance depuis les sources (Python 3.13)
build.bat     :: construit dist\PGCompanion.exe (version 0.0.0, sans mise à jour automatique)
py -m pytest  :: tests
```

## Publier une version

```bash
git tag v1.2.3
git push origin v1.2.3
```

La CI (GitHub Actions, Windows) lance les tests, construit l'exe avec ce numéro de version et crée la release
avec `PGCompanion.exe` et `PGCompanion.exe.sha256`. Les joueurs l'installent à leur prochain démarrage.
