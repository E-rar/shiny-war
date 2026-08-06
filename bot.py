"""
Shiny Hunt Discord Bot
======================
Ein Discord-Bot, mit dem Server-Mitglieder eintragen können, welches
Pokémon sie gerade shiny hunten. Fängt jemand an, ein Pokémon zu hunten,
das schon jemand anderes huntet, bekommt er/sie eine Warnung mit den
Namen der bereits huntenden Personen.

Setup:
1. pip install -r requirements.txt
2. Bot-Token in der Datei ".env" hinterlegen (siehe .env.example)
3. python bot.py
"""

import asyncio
import base64
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATA_FILE = Path(__file__).parent / "hunts.json"        # Klartext – kommt ins Repo (öffentliches Board)

# Optionaler Link zum Board (für /zugang). Z. B. https://<user>.github.io/<repo>/
BOARD_URL = os.getenv("BOARD_URL", "")

# ID des Text-Channels, in dem der Bot ausschließlich reagieren soll.
# 0 = Einschränkung deaktiviert (Bot reagiert überall).
ALLOWED_CHANNEL_ID = int(os.getenv("ALLOWED_CHANNEL_ID", "0"))

# ID deines Discord-Servers. Wenn gesetzt, werden Slash-Commands SOFORT dort
# registriert (statt global, was bis zu ~1 Stunde dauern kann). Stark empfohlen.
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

# Automatisches Pushen der hunts.json ins Git-Repo (für die gehostete Web-App auf GitHub Pages).
# GIT_AUTO_PUSH=0 schaltet es ab. GIT_PUSH_DELAY = Sekunden Wartezeit nach der letzten Änderung.
REPO_DIR = Path(__file__).parent
GIT_AUTO_PUSH = os.getenv("GIT_AUTO_PUSH", "1") != "0"
GIT_PUSH_DELAY = int(os.getenv("GIT_PUSH_DELAY", "300"))  # Standard: 5 Minuten


class RestrictedTree(app_commands.CommandTree):
    """CommandTree, die Slash-Commands außerhalb des erlaubten Channels blockiert."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if ALLOWED_CHANNEL_ID == 0 or interaction.channel_id == ALLOWED_CHANNEL_ID:
            return True

        await interaction.response.send_message(
            f"❌ Dieser Befehl funktioniert nur in <#{ALLOWED_CHANNEL_ID}>.",
            ephemeral=True,
        )
        return False


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents, tree_cls=RestrictedTree)


# ---------------------------------------------------------------------------
# Persistenz: einfache JSON-Datei
# Struktur:
# {
#   "active": { "pikachu": [user_id, user_id, ...], "glumanda": [user_id] },
#   "caught": [ {"pokemon": "pikachu", "user_id": 123, "date": "2026-07-16"}, ... ],
#   "users":  { "123": "Erik", "456": "Enzo" }   # user_id -> Anzeigename (für die Web-App)
# }
# ---------------------------------------------------------------------------

def load_data() -> dict:
    if not DATA_FILE.exists():
        return {"active": {}, "caught": [], "users": {}}

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Migration: alte Dateien hatten nur { "pokemon": [user_id, ...] } ohne "active"/"caught"
    if "active" not in data and "caught" not in data:
        data = {"active": data, "caught": []}

    data.setdefault("active", {})
    data.setdefault("caught", [])
    data.setdefault("users", {})
    return data


def encrypt_json(obj: dict, password: str) -> dict:
    """AES-GCM 256, Schlüssel via PBKDF2-SHA256 (200k). Format exakt wie die Web-App:
    {"v":1,"salt":<b64>,"iv":<b64>,"ct":<b64>} mit ct = ciphertext+GCM-Tag."""
    salt = os.urandom(16)
    iv = os.urandom(12)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200000)
    key = kdf.derive(password.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode("utf-8"), None)
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return {"v": 1, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


def save_data(data: dict) -> None:
    # Öffentliches Board (Klartext): hunts.json wird direkt ins Repo committet.
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Debounced Git-Push: bündelt viele Einträge zu EINEM Push.
# Nach jeder Änderung wird ein Timer (GIT_PUSH_DELAY) gestartet; kommt vorher
# eine weitere Änderung, wird der Timer zurückgesetzt. Erst wenn es GIT_PUSH_DELAY
# Sekunden ruhig war, wird hunts.json committet und gepusht.
# ---------------------------------------------------------------------------

_push_task = None


def _run_git_push() -> None:
    """Blockierender Git-Teil (läuft in einem Thread, damit der Bot nicht hängt)."""
    try:
        # Klartext-Daten ins Repo (öffentliches Board).
        subprocess.run(["git", "add", "hunts.json"], cwd=REPO_DIR, check=True)
        # Nur committen, wenn es tatsächlich Änderungen gibt.
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO_DIR).returncode == 0:
            has_local = False
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            subprocess.run(["git", "commit", "-m", f"Update hunts.json ({stamp})"], cwd=REPO_DIR, check=True)
            has_local = True
        # Erst Remote-Änderungen einbauen (verhindert 'non-fast-forward' -> Push wird sonst abgelehnt),
        # dann pushen. So bleibt der Zwei-Wege-Betrieb (du pushst Code, Bot pusht Daten) reibungslos.
        subprocess.run(["git", "pull", "--rebase", "--autostash"], cwd=REPO_DIR)
        # Auch pushen, wenn keine neue lokale Änderung war, aber noch unveröffentlichte Commits anstehen.
        ahead = subprocess.run(["git", "rev-list", "--count", "@{u}..HEAD"], cwd=REPO_DIR,
                               capture_output=True, text=True).stdout.strip()
        if has_local or (ahead and ahead != "0"):
            subprocess.run(["git", "push"], cwd=REPO_DIR, check=True)
            print("✅ hunts.json ins Repo gepusht.")
    except Exception as e:
        print(f"⚠️  Git-Push fehlgeschlagen: {e}")


async def _push_after_delay() -> None:
    try:
        await asyncio.sleep(GIT_PUSH_DELAY)
    except asyncio.CancelledError:
        return  # Es kam eine neue Änderung -> Timer wurde zurückgesetzt.
    await asyncio.to_thread(_run_git_push)


def schedule_git_push() -> None:
    """Setzt den Push-Timer (neu). Nach jeder schreibenden Aktion aufrufen."""
    if not GIT_AUTO_PUSH:
        return
    global _push_task
    if _push_task and not _push_task.done():
        _push_task.cancel()
    _push_task = asyncio.create_task(_push_after_delay())


def record_user(data: dict, interaction: discord.Interaction) -> None:
    """
    Merkt sich den aktuellen Discord-Anzeigenamen des Nutzers.
    Wird bei jeder schreibenden Interaktion aktualisiert, damit die
    Web-App (Leaderboard etc.) automatisch die richtigen Namen zeigt.
    Bevorzugt den Server-Nickname (display_name), fällt sonst auf den
    globalen Namen / Usernamen zurück.
    """
    user = interaction.user
    name = getattr(user, "display_name", None) or getattr(user, "global_name", None) or user.name
    data.setdefault("users", {})[str(user.id)] = name


async def backfill_users():
    """Ergänzt Namen für alle Hunter/Fänger, die noch nicht in 'users' stehen
    (z.B. Alt-Hunts). Holt den Discord-Namen per fetch_user (braucht keine
    privilegierten Intents). Gibt (aufgelöst, gesamt fehlend) zurück."""
    data = load_data()
    ids = set()
    for arr in data.get("active", {}).values():
        for x in arr or []:
            try:
                ids.add(int(x))
            except (TypeError, ValueError):
                pass
    for c in data.get("caught", []):
        try:
            ids.add(int(c.get("user_id")))
        except (TypeError, ValueError):
            pass

    users = data.setdefault("users", {})
    missing = [uid for uid in ids if str(uid) not in users]
    resolved = 0
    for uid in missing:
        try:
            u = await bot.fetch_user(uid)
            users[str(uid)] = getattr(u, "global_name", None) or u.name
            resolved += 1
        except Exception as e:
            print(f"⚠️  Name für User {uid} nicht auflösbar: {e}")
    # Immer neu speichern & veröffentlichen (Git committet nur bei echter Änderung).
    # So werden auch manuelle Edits an hunts.json (z.B. gelöschte Fänge) verschlüsselt gepusht.
    save_data(data)
    schedule_git_push()
    if resolved:
        print(f"✅ {resolved} fehlende Namen ergänzt.")
    return resolved, len(missing)


def normalize(name: str) -> str:
    """Vereinheitlicht Pokémon-Namen, damit 'Pikachu' == 'pikachu' == ' pikachu '."""
    return name.strip().lower()


# ---------------------------------------------------------------------------
# Pokémon-Datenbank (aus fetch_pokemon_data.py, lokal von der PokéAPI geladen)
# Ermöglicht Namensabgleich (DE/EN) + Sprite-Bilder.
# Struktur je Eintrag: { "id": 4, "name_de": "Glumanda", "name_en": "charmander", "sprite": "https://..." }
# ---------------------------------------------------------------------------

POKEMON_DATA_FILE = Path(__file__).parent / "pokemon_data.json"


def load_pokemon_data() -> dict:
    if not POKEMON_DATA_FILE.exists():
        print(
            "ℹ️  Keine pokemon_data.json gefunden – Namensabgleich & Bilder sind "
            "deaktiviert. Führe 'python fetch_pokemon_data.py' aus, um sie zu aktivieren."
        )
        return {}
    with open(POKEMON_DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


POKEMON_DATA = load_pokemon_data()


def resolve_pokemon(name: str, strict: bool = False):
    """
    Gleicht einen eingegebenen Namen gegen die PokéAPI-Daten ab.

    Rückgabe: (key, display_name, sprite_url) oder None, wenn strict=True
    und der Name nicht in der Datenbank gefunden wurde.

    key: normalisierter Schlüssel, unter dem in hunts.json gespeichert wird
    display_name: hübsch formatierter Name für die Anzeige
    sprite_url: Bild-URL, falls bekannt (sonst None)
    """
    info = POKEMON_DATA.get(normalize(name))
    if info:
        return info["name_de"].lower(), info["name_de"], info["sprite"]

    if strict and POKEMON_DATA:
        return None

    # Kein Abgleich möglich (keine Datenbank vorhanden oder Fallback erlaubt).
    key = normalize(name)
    return key, name.strip().title(), None


def pretty_name(key: str) -> str:
    """Schöner Anzeigename für einen gespeicherten Schlüssel, bevorzugt aus der PokéAPI-DB."""
    info = POKEMON_DATA.get(key)
    return info["name_de"] if info else key.capitalize()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)          # alle Befehle auf den Server kopieren
            synced = await bot.tree.sync(guild=guild)      # sofort verfügbar
            where = f"Server {GUILD_ID}"
        else:
            synced = await bot.tree.sync()                 # global (kann bis ~1 Std. dauern)
            where = "global"
        print(f"Eingeloggt als {bot.user} – {len(synced)} Slash-Commands synchronisiert ({where}).")
    except Exception as e:
        print(f"Fehler beim Synchronisieren der Commands: {e}")
    try:
        resolved, missing = await backfill_users()
        print(f"Namens-Backfill: {resolved}/{missing} fehlende Namen ergänzt.")
    except Exception as e:
        print(f"Fehler beim Namens-Backfill: {e}")


# ---------------------------------------------------------------------------
# Slash-Commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="hunt", description="Trage ein Pokémon ein, das du shiny huntest.")
@app_commands.describe(pokemon="Name des Pokémon, z.B. Glumanda")
async def hunt(interaction: discord.Interaction, pokemon: str):
    resolved = resolve_pokemon(pokemon, strict=True)
    if resolved is None:
        await interaction.response.send_message(
            f"❓ Ich kenne kein Pokémon namens **{pokemon}**. Bitte prüfe die Schreibweise "
            "(Deutscher oder englischer Name funktioniert).",
            ephemeral=True,
        )
        return
    key, display_name, sprite = resolved

    data = load_data()
    active = data["active"]
    hunters = active.get(key, [])

    already_hunting = interaction.user.id in hunters

    if already_hunting:
        await interaction.response.send_message(
            f"Du huntest **{display_name}** bereits – kein Grund zur Sorge! 🎯",
            ephemeral=True,
        )
        return

    warning_text = ""
    if hunters:
        mentions = ", ".join(f"<@{uid}>" for uid in hunters)
        warning_text = f"\n\n⚠️ **Achtung:** {mentions} huntet/hunten **{display_name}** bereits auch schon!"

    hunters.append(interaction.user.id)
    active[key] = hunters
    record_user(data, interaction)
    save_data(data)
    schedule_git_push()

    embed = discord.Embed(
        description=f"✅ {interaction.user.mention} huntet jetzt **{display_name}**!{warning_text}",
        color=discord.Color.gold(),
    )
    if sprite:
        embed.set_thumbnail(url=sprite)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="unhunt", description="Entferne ein Pokémon von deiner Hunt-Liste.")
@app_commands.describe(pokemon="Name des Pokémon, z.B. Glumanda")
async def unhunt(interaction: discord.Interaction, pokemon: str):
    key, display_name, _ = resolve_pokemon(pokemon)

    data = load_data()
    active = data["active"]
    hunters = active.get(key, [])

    if interaction.user.id not in hunters:
        await interaction.response.send_message(
            f"Du huntest **{display_name}** aktuell gar nicht.", ephemeral=True
        )
        return

    hunters.remove(interaction.user.id)
    if hunters:
        active[key] = hunters
    else:
        active.pop(key, None)
    record_user(data, interaction)
    save_data(data)
    schedule_git_push()

    await interaction.response.send_message(
        f"🛑 {interaction.user.mention} huntet **{display_name}** nicht mehr."
    )


@bot.tree.command(name="myhunts", description="Zeigt, welche Pokémon du gerade huntest.")
async def myhunts(interaction: discord.Interaction):
    data = load_data()
    mine = [key for key, hunters in data["active"].items() if interaction.user.id in hunters]

    if not mine:
        await interaction.response.send_message(
            "Du huntest aktuell kein Pokémon.", ephemeral=True
        )
        return

    liste = "\n".join(f"• {pretty_name(key)}" for key in mine)
    await interaction.response.send_message(
        f"🎯 Du huntest gerade:\n{liste}", ephemeral=True
    )


@bot.tree.command(name="hunters", description="Zeigt, wer ein bestimmtes Pokémon huntet.")
@app_commands.describe(pokemon="Name des Pokémon, z.B. Glumanda")
async def hunters_cmd(interaction: discord.Interaction, pokemon: str):
    key, display_name, sprite = resolve_pokemon(pokemon)

    data = load_data()
    hunters = data["active"].get(key, [])

    if not hunters:
        await interaction.response.send_message(
            f"Aktuell huntet niemand **{display_name}**.", ephemeral=True
        )
        return

    mentions = ", ".join(f"<@{uid}>" for uid in hunters)
    embed = discord.Embed(
        description=f"**{display_name}** wird gehuntet von: {mentions}",
        color=discord.Color.blue(),
    )
    if sprite:
        embed.set_thumbnail(url=sprite)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="allhunts", description="Zeigt eine Übersicht aller aktiven Shiny-Hunts.")
async def allhunts(interaction: discord.Interaction):
    data = load_data()
    active = data["active"]

    if not active:
        await interaction.response.send_message("Aktuell huntet niemand irgendetwas.")
        return

    lines = []
    for key, hunters in sorted(active.items()):
        mentions = ", ".join(f"<@{uid}>" for uid in hunters)
        lines.append(f"**{pretty_name(key)}** – {mentions}")

    await interaction.response.send_message("🔎 **Aktive Shiny-Hunts:**\n" + "\n".join(lines))


@bot.tree.command(name="caught", description="Markiere ein Pokémon als shiny gefunden!")
@app_commands.describe(pokemon="Name des Pokémon, das du gefunden hast, z.B. Glumanda")
async def caught(interaction: discord.Interaction, pokemon: str):
    resolved = resolve_pokemon(pokemon, strict=True)
    if resolved is None:
        await interaction.response.send_message(
            f"❓ Ich kenne kein Pokémon namens **{pokemon}**. Bitte prüfe die Schreibweise "
            "(Deutscher oder englischer Name funktioniert).",
            ephemeral=True,
        )
        return
    key, display_name, sprite = resolved

    data = load_data()

    # Falls das Pokémon noch als aktiver Hunt eingetragen ist, dort entfernen.
    hunters = data["active"].get(key, [])
    if interaction.user.id in hunters:
        hunters.remove(interaction.user.id)
        if hunters:
            data["active"][key] = hunters
        else:
            data["active"].pop(key, None)

    data["caught"].append(
        {
            "pokemon": key,
            "user_id": interaction.user.id,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    )
    record_user(data, interaction)
    save_data(data)
    schedule_git_push()

    embed = discord.Embed(
        description=f"🎉✨ Glückwunsch {interaction.user.mention}! Du hast ein shiny **{display_name}** gefunden!",
        color=discord.Color.purple(),
    )
    if sprite:
        embed.set_thumbnail(url=sprite)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="caughtlist", description="Zeigt alle bisher gefundenen Shinys und wer sie gefunden hat.")
async def caughtlist(interaction: discord.Interaction):
    data = load_data()
    caught_list = data["caught"]

    if not caught_list:
        await interaction.response.send_message(
            "Es wurde bisher noch kein Shiny gefunden.", ephemeral=True
        )
        return

    sorted_entries = sorted(caught_list, key=lambda e: e["date"], reverse=True)
    lines = [
        f"✨ **{pretty_name(entry['pokemon'])}** – <@{entry['user_id']}> ({entry['date']})"
        for entry in sorted_entries
    ]

    text = "🏆 **Gefundene Shinys:**\n" + "\n".join(lines)
    if len(text) > 1900:
        text = text[:1900] + "\n… (Liste gekürzt, es gibt noch mehr Einträge)"

    await interaction.response.send_message(text)


@bot.tree.command(name="zugang", description="Link zum Bober Board.")
async def zugang(interaction: discord.Interaction):
    msg = "🦫 **Bober Board**\nOffen für alle – einfach öffnen, kein Passwort nötig."
    if BOARD_URL:
        msg += f"\n{BOARD_URL}"
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="syncnamen", description="Ergänzt fehlende Spieler-Namen fürs Leaderboard (Admin/Backfill).")
async def syncnamen(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    resolved, missing = await backfill_users()
    await interaction.followup.send(
        f"🔄 Namen aktualisiert: {resolved} von {missing} fehlenden ergänzt.", ephemeral=True
    )


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "Kein Bot-Token gefunden! Lege eine .env Datei mit DISCORD_TOKEN=... an."
        )
    bot.run(TOKEN)
