@echo off
rem Construit dist\PGCompanion.exe : un seul fichier a envoyer (Python, bibliotheques et pages web dedans).
rem Au premier lancement, l'exe cree a cote de lui sa config.json (nouvelle cle d'acces) et chat_log.jsonl.
rem Build local = version 0.0.0, sans mise a jour automatique ; les versions publiees sont construites par la CI (tag vX.Y.Z).
title PG Companion - build
cd /d "%~dp0"

echo Installation des dependances...
py -m pip install --quiet --disable-pip-version-check -r requirements-build.txt || goto :error

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo Construction de l'executable...
rem winrt : modules de l'OCR de Windows, charges dynamiquement par winocr
py -m PyInstaller server.py --name PGCompanion --onefile --console --noconfirm --clean ^
  --add-data "static;static" ^
  --collect-all winrt --collect-all winocr --hidden-import qrcode ^
  --exclude-module tkinter --exclude-module numpy --exclude-module cv2 --exclude-module dxcam ^
  || goto :error

rmdir /s /q build
del /q PGCompanion.spec
echo.
echo Termine : %~dp0dist\PGCompanion.exe
pause
exit /b 0

:error
echo.
echo Echec du build.
pause
exit /b 1
