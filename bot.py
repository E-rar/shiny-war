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
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DATA_FILE = Path(__file__).parent / "hunts.json"

# ID des Text-Channels, in dem der Bot ausschließlich reagieren soll.
# 0 = Einschränkung deaktiviert (Bot reagiert überall).
ALLOWED_CHANNEL_ID = int(os.getenv("ALLOWED_CHANNEL_ID", "0"))

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


def save_data(data: dict) -> None:
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
        subprocess.run(["git", "add", "hunts.json"], cwd=REPO_DIR, check=True)
        # Nur committen, wenn es tatsächlich Änderungen gibt.
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO_DIR).returncode == 0:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        subprocess.run(["git", "commit", "-m", f"Update hunts.json ({stamp})"], cwd=REPO_DIR, check=True)
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
        synced = await bot.tree.sync()
        print(f"Eingeloggt als {bot.user} – {len(synced)} Slash-Commands synchronisiert.")
    except Exception as e:
        print(f"Fehler beim Synchronisieren der Commands: {e}")


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


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "Kein Bot-Token gefunden! Lege eine .env Datei mit DISCORD_TOKEN=... an."
        )
    bot.run(TOKEN)
