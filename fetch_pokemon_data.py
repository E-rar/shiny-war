"""
Lädt einmalig alle Pokémon-Daten (deutsche & englische Namen, Sprite-Bild,
Pokédex-Nummer) von der PokéAPI (https://pokeapi.co) und speichert sie lokal
in pokemon_data.json. Der Bot nutzt diese Datei, um Eingaben abzugleichen
und Bilder anzuzeigen, ohne bei jedem Befehl die PokéAPI anzufragen.

Das dauert je nach Internetverbindung ein paar Minuten (ca. 1300 Pokémon),
muss aber nur einmal ausgeführt werden. Bei neuen Pokémon-Generationen
kannst du es einfach erneut laufen lassen, um die Daten zu aktualisieren.

Aufruf: python fetch_pokemon_data.py
"""

import asyncio
import json
from pathlib import Path

import aiohttp

OUTPUT_FILE = Path(__file__).parent / "pokemon_data.json"
SPECIES_LIST_URL = "https://pokeapi.co/api/v2/pokemon-species?limit=2000"
CONCURRENCY = 20  # Gleichzeitige Anfragen, um die PokéAPI nicht zu überlasten


async def fetch_json(session: aiohttp.ClientSession, url: str) -> dict:
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.json()


async def fetch_species(session, sem, entry, results):
    async with sem:
        try:
            species = await fetch_json(session, entry["url"])
        except Exception as e:
            print(f"⚠️  Fehler bei {entry['name']}: {e}")
            return

        dex_id = species["id"]
        en_name = entry["name"]

        de_name = en_name
        for name_entry in species.get("names", []):
            if name_entry["language"]["name"] == "de":
                de_name = name_entry["name"]
                break

        sprite_url = (
            "https://raw.githubusercontent.com/PokeAPI/sprites/master/"
            f"sprites/pokemon/other/official-artwork/{dex_id}.png"
        )

        record = {
            "id": dex_id,
            "name_de": de_name,
            "name_en": en_name,
            "sprite": sprite_url,
        }

        # Sowohl unter deutschem als auch englischem Namen auffindbar machen.
        results[de_name.lower()] = record
        results.setdefault(en_name.lower(), record)

        print(f"[{dex_id:>4}] {de_name} / {en_name}")


async def main():
    results: dict = {}
    async with aiohttp.ClientSession() as session:
        species_list = await fetch_json(session, SPECIES_LIST_URL)
        sem = asyncio.Semaphore(CONCURRENCY)
        tasks = [
            fetch_species(session, sem, entry, results)
            for entry in species_list["results"]
        ]
        await asyncio.gather(*tasks)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Fertig! {len(results)} Einträge in {OUTPUT_FILE.name} gespeichert.")


if __name__ == "__main__":
    asyncio.run(main())
