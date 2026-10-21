# nemo-actions-pdf-unprotect

*[English version](README.md)*

Fügt im Dateimanager Nemo (Linux Mint / Cinnamon) beim Rechtsklick auf PDF-Dateien den Eintrag **PDF-Passwort entfernen** hinzu.
Du gibst das Passwort einmal ein, und eine ungeschützte Kopie wird neben dem Original abgelegt:

```
Rechnung.pdf  →  Rechnung_unprotected.pdf
```

![Passwort-Dialog](docs/dialog-de.png)

## Funktionen

- Funktioniert mit einer oder mehreren markierten PDFs. Ein Passwort, das gepasst hat, wird bei den nächsten Dateien automatisch probiert – mehrere Kontoauszüge mit demselben Passwort brauchen also nur eine Eingabe.
- Falsches Passwort: Der Dialog fragt erneut; **Überspringen** lässt die Datei unverändert.
- PDFs, die nur ein Besitzer-Passwort haben (Druck-/Kopiersperre), werden ohne Nachfrage entsperrt.
- Überschreibt nie etwas: Gibt es `…_unprotected.pdf` schon, wird `…_unprotected-2.pdf` verwendet. Das Original bleibt unangetastet.
- Das Passwort wird nie auf der Kommandozeile übergeben und taucht daher nicht in der Prozessliste auf.
- Oberfläche auf Englisch und Deutsch, je nach Systemsprache.

## Voraussetzungen

Unter Linux Mint 22 bereits vorinstalliert:

- `python3-pikepdf` (PDF-Verarbeitung, basiert auf qpdf)
- `python3-gi` mit GTK 3 (Passwort-Dialog)
- `libnotify-bin` (Erfolgsmeldung, optional)
- `gettext` (nur für den Installer, um die Übersetzungen zu bauen)

```bash
sudo apt install python3-pikepdf python3-gi gettext
```

## Installation

```bash
./install.sh
```

Das kopiert die Aktion nach `~/.local/share/nemo/actions/` und die Übersetzungen nach `~/.local/share/locale/`.
Nemo erkennt den neuen Menüeintrag sofort; falls nicht, `nemo -q` ausführen und Nemo neu öffnen.

Deinstallieren:

```bash
./install.sh --uninstall
```

## Übersetzungen

Alle sichtbaren Texte liegen in `po/`. Nach Textänderungen im Code die Kataloge aktualisieren:

```bash
po/update-pot.sh
```

Für eine neue Sprache `po/<sprache>.po` aus `po/pdf-password-remover.pot` erzeugen (z. B. mit `msginit`) und den Sprachcode in `po/LINGUAS` eintragen.

## Danksagung

Die Grundidee des Scripts stammt von Alfcx. Danke dafür!

## Lizenz

[MIT](LICENSE)
