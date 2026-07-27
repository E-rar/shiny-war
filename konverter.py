"""
konverter.py — PokeMMO-Encounterdaten  ->  hordes.json (für die Shiny-War-App)
=============================================================================

Wandelt den (kommenden) PokeMMO-Daten-Export in unsere hordes.json um.

Erwartetes Eingabeformat (Forum-Preview, Stand vor Release – kann sich noch
leicht ändern): ein JSON-ARRAY von Pokémon-Objekten, jedes mit einem
"locations"-Array. Ein Location-Eintrag sieht so aus:

    {
      "form": 27, "type": "Cave",
      "region_id": 0, "region_name": "Kanto", "location": "ALTERING CAVE",
      "min_level": 53, "max_level": 54, "season": "Any",
      "rarity_flags": 0,
      "rarity_morning": "1%", "rarity_day": "1%", "rarity_night": "1%"
    }

Aufruf:
    python konverter.py pokemon_export.json            # -> hordes.json
    python konverter.py pokemon_export.json out.json

Danach in der App (Admin) "⬆ Import" -> hordes.json, oder direkt ins Repo
committen (die App lädt es automatisch).

⚠️  OFFENE PUNKTE (beim echten Release kurz prüfen/anpassen):
  - RARITY_FLAGS: Bedeutung der Bits (Lure? Safari?) ist noch unklar -> unten
    in flags_to_extras() anpassen.
  - HORDE-TYP: Wie heißt der "type" für Horden genau ("Horde", "Horde 5" …)
    und woher kommt die Größe (3/5)? -> map_type() anpassen.
  - Season-Namen (Spring/Summer/Autumn/Winter vs. Frühling…) -> SEASON_MAP.
"""

import argparse
import json
import re
import sys
from collections import OrderedDict

# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------

# Saison-Namen aus dem Export -> unsere Keys. "Any"/"All" = alle vier Saisons.
SEASON_KEYS = ["fruehling", "sommer", "herbst", "winter"]
SEASON_MAP = {
    "spring": ["fruehling"],
    "summer": ["sommer"],
    "autumn": ["herbst"], "fall": ["herbst"],
    "winter": ["winter"],
    "any": SEASON_KEYS, "all": SEASON_KEYS, "": SEASON_KEYS,
}


def map_season(season):
    return SEASON_MAP.get(str(season or "").strip().lower(), SEASON_KEYS)


# type -> (method, habitat, note-Zusatz)
#   method: "horde5" | "horde3" | "angeln" | "single"
#   habitat: "gras" | "dunkelgras" | "wasser" | "hoehle" | "" (keiner)
def map_type(type_str):
    t = str(type_str or "").strip().lower()
    if "horde" in t:
        size = "horde3" if "3" in t else "horde5"
        # Untergrund bei Horden aus dem Kontext oft Gras – ggf. anpassen:
        return size, "gras", ""
    if "rod" in t or "fish" in t or "angel" in t:  # Old/Good/Super Rod
        return "angeln", "wasser", type_str
    if "dark" in t and "grass" in t:
        return "single", "dunkelgras", ""
    if "grass" in t:
        return "single", "gras", ""
    if "water" in t or "surf" in t:
        return "single", "wasser", ""
    if "cave" in t:
        return "single", "hoehle", ""
    if "rock" in t:
        return "single", "hoehle", "Rock Smash"
    if "headbutt" in t:
        return "single", "", "Headbutt"
    # Unbekannter Typ -> Single ohne Untergrund, Typ als Notiz
    return "single", "", type_str


# rarity_flags -> Zusatz-Eigenschaften.  ⚠️ Bit-Bedeutung noch unklar!
# Sobald bekannt: passende Bits hier eintragen.
FLAG_LURE = 0     # TODO: echtes Bit für "Lure" eintragen (z.B. 1)
FLAG_SAFARI = 0   # TODO: echtes Bit für "Safari" (falls relevant)


def flags_to_extras(flags):
    try:
        f = int(flags or 0)
    except (TypeError, ValueError):
        f = 0
    lure = bool(FLAG_LURE and (f & FLAG_LURE))
    return {"lure": lure}


def rarity_to_pct(val):
    """'1%' -> 1 ; '12 %' -> 12 ; '-'/None -> 0"""
    if val is None:
        return 0
    m = re.search(r"(\d+(?:\.\d+)?)", str(val))
    return round(float(m.group(1))) if m else 0


def loc_pct(loc):
    """Repräsentativer %-Wert eines Location-Eintrags = max über Morning/Day/Night."""
    return max(
        rarity_to_pct(loc.get("rarity_morning")),
        rarity_to_pct(loc.get("rarity_day")),
        rarity_to_pct(loc.get("rarity_night")),
    )


def loc_tod(loc):
    """Tageszeit ableiten. Unser Modell: 'immer' | 'morgen' | 'tag' | 'nacht'.
    (Die App zeigt Tag-Mons auch morgens; darum ist 'tag' die breitere Wahl.)"""
    m = rarity_to_pct(loc.get("rarity_morning")) > 0
    d = rarity_to_pct(loc.get("rarity_day")) > 0
    n = rarity_to_pct(loc.get("rarity_night")) > 0
    if m and d and n:
        return "immer"
    if n and not d and not m:
        return "nacht"
    if m and not d and not n:
        return "morgen"
    if d and not n:
        return "tag"
    return "immer"


def combine_tod(a, b):
    if a is None or a == b:
        return b
    return "immer"  # verschiedene Zeiten -> nicht einschränken


# ---------------------------------------------------------------------------
# Kernlogik
# ---------------------------------------------------------------------------

def convert(pokemon_list):
    # Bucket-Key = (Region, Ort, Methode). Innerhalb: Members je Species.
    spots = OrderedDict()

    for mon in pokemon_list:
        name = (mon.get("name") or "").strip()
        if not name:
            continue
        for loc in mon.get("locations", []) or []:
            method, hab, note_extra = map_type(loc.get("type"))
            region = loc.get("region_name") or loc.get("region") or ""
            place = loc.get("location") or ""
            key = (region, place, method)

            spot = spots.get(key)
            if spot is None:
                spot = {"loc": place, "region": region, "method": method,
                        "hab": hab, "level": "", "note": "", "members": OrderedDict(),
                        "notes": set()}
                spots[key] = spot

            mem = spot["members"].get(name)
            if mem is None:
                mem = {"name": name, "s": {}, "tod": None, "lure": False, "levels": set()}
                spot["members"][name] = mem

            pct = loc_pct(loc)
            for s in map_season(loc.get("season")):
                mem["s"][s] = max(mem["s"].get(s, 0), pct)

            mem["tod"] = combine_tod(mem["tod"], loc_tod(loc))

            extras = flags_to_extras(loc.get("rarity_flags"))
            if extras["lure"]:
                mem["lure"] = True

            lo, hi = loc.get("min_level"), loc.get("max_level")
            if lo is not None and hi is not None:
                mem["levels"].add((lo, hi))
            if note_extra:
                spot["notes"].add(note_extra)

    # In hordes.json-Format gießen
    hordes = []
    for spot in spots.values():
        members = []
        all_levels = set()
        for mem in spot["members"].values():
            s = {k: int(mem["s"].get(k, 0)) for k in SEASON_KEYS}
            members.append({
                "name": mem["name"],
                "s": s,
                "tod": mem["tod"] or "immer",
                "lure": bool(mem["lure"]),
            })
            all_levels |= mem["levels"]
        if not members:
            continue
        # Notiz: Level-Bereich + Zusatzinfos (Rod/Rock Smash/Headbutt …)
        note_parts = list(spot["notes"])
        if all_levels:
            lo = min(l for l, _ in all_levels)
            hi = max(h for _, h in all_levels)
            note_parts.insert(0, f"Lv {lo}-{hi}" if lo != hi else f"Lv {lo}")
        hordes.append({
            "loc": spot["loc"],
            "region": spot["region"],
            "method": spot["method"],
            "hab": spot["hab"],
            "note": " · ".join(note_parts),
            "members": members,
        })

    return {"hordes": hordes}


def main():
    ap = argparse.ArgumentParser(description="PokeMMO-Encounterdaten -> hordes.json")
    ap.add_argument("input", help="Pfad zum PokeMMO-Daten-Export (JSON-Array)")
    ap.add_argument("output", nargs="?", default="hordes.json", help="Ziel (Standard: hordes.json)")
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Root ist ein Array; falls es ein Objekt mit z.B. {"pokemon": [...]} ist, abfangen:
    if isinstance(data, dict):
        data = data.get("pokemon") or data.get("results") or list(data.values())

    result = convert(data)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    n_spots = len(result["hordes"])
    n_mem = sum(len(h["members"]) for h in result["hordes"])
    print(f"✅ {n_spots} Spots / {n_mem} Einträge -> {args.output}")
    if FLAG_LURE == 0:
        print("ℹ️  Hinweis: RARITY_FLAGS-Bits (Lure/Safari) sind noch nicht gesetzt "
              "(FLAG_LURE=0). Beim Release kurz anpassen.")


if __name__ == "__main__":
    sys.exit(main())
