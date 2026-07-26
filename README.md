# Shiny War – PokeMMO Team-Tracker

Tools für unser PokeMMO-Shiny-War-Team: ein Discord-Bot, der Hunts & gefundene
Shinys erfasst, plus eine Web-App, die daraus Leaderboard, Horden-Übersicht,
Spezies-Suche und Tierlist macht.

## Inhalt

| Datei | Zweck |
|-------|-------|
| `shiny-war.html` | Die Web-App (einfach im Browser öffnen). Leaderboard, Horden, Suche, Tierlist. |
| `hordes.json` | Horden-Daten zum Import in die App (Admin → ⬆ Import). |
| `bot.py` | Discord-Bot: `/hunt`, `/caught`, `/allhunts` … schreibt `hunts.json`. |
| `fetch_pokemon_data.py` | Lädt einmalig `pokemon_data.json` (DE/EN-Namen + Sprites) von der PokéAPI. |
| `pokemon_data.json` | Namens-/Bilddatenbank (wird von `fetch_pokemon_data.py` erzeugt). |
| `requirements.txt` | Python-Abhängigkeiten für den Bot. |
| `.env.example` | Vorlage für den Bot-Token (echte `.env` bleibt lokal). |

## Web-App

`shiny-war.html` im Browser öffnen. Admin-Modus (Bearbeiten/Upload) durch
`#nimda` am Ende der URL, z. B. `…/shiny-war.html#nimda`. Ohne das nur Lesen.

## Bot einrichten

```bash
pip install -r requirements.txt
cp .env.example .env                   # dann echten DISCORD_TOKEN eintragen
python fetch_pokemon_data.py           # einmalig: pokemon_data.json erzeugen
python bot.py
```

## Wichtig

- Die echte `.env` mit dem Bot-Token wird **nicht** eingecheckt (`.gitignore`).
- `hunts.json` (Live-Daten) ist ebenfalls in `.gitignore` – der Bot pflegt sie.
