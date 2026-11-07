#!/usr/bin/env python3
"""Builds data/planner.json for planner.html from an EbonReach game database.

    python3 tools/build_planner_data.py --defaults-file my.cnf --db magicbane

my.cnf is a MariaDB client file ([client] user=... password=...). Point it
at the live database or a restored backup; it only reads.

It exports the same rules the server uses (EbonReach repo, src/):
  - Races, base classes and which classes each race may take
    (static_rune_race, static_rune_baseclass, static_rune_racebaseclass).
  - Promotion classes and the race and base class tokens each one allows
    (static_rune_promotion, static_rune_promotionrunereq; checked in
    VendorDialogMsg).
  - Starting traits: runes a race AND a base class may take at creation
    (static_rune_racerune, static_rune_baseclassrune; 111111 = everyone),
    with cost, stat changes and minimum stats (static_rune_runebaseattribute,
    attribute IDs in MBServerStatics). Same subtype = pick one.
  - Disciplines: rune IDs 3001-3049 (ApplyRuneMsg.isDiscipline) with their
    race / base class / promotion allow-lists, prohibited runes and level
    requirement (static_rune_runereq types 1-4 and 6, dbRuneBaseHandler).

Stat points per level follow PlayerCharacter.calculateBaseStats.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
from collections import defaultdict

STATS = ("str", "dex", "con", "int", "spi")
EVERYONE = 111111
DISCIPLINE_IDS = range(3001, 3050)

# MBServerStatics.RUNE_*_ATTRIBUTE_ID
ATTR_COST = 0
ATTR_BASE = {1: "str", 2: "dex", 3: "con", 4: "int", 5: "spi"}
ATTR_MAX = {6: "str", 7: "dex", 8: "con", 9: "int", 10: "spi"}
ATTR_NEED = {11: "str", 12: "dex", 13: "con", 14: "int", 15: "spi"}


def q(args, sql):
    cmd = [args.mysql, f"--defaults-extra-file={args.defaults_file}", "-N", "-B", "-e", sql, args.db]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"query failed: {r.stderr.strip()}\n{sql}")
    return [[None if c == "NULL" else c for c in line.split("\t")] for line in r.stdout.splitlines()]


def num(v):
    f = float(v)
    return int(f) if f.is_integer() else round(f, 2)


def rune_effects(rows):
    """{runeID: {"cost": n, "stat": {s: [base, max]}, "need": {s: n}}} from runebaseattribute rows."""
    out = defaultdict(lambda: {"cost": 0, "stat": {}, "need": {}})
    for rune_id, attr, value in rows:
        rune_id, attr, value = int(rune_id), int(attr), int(value)
        e = out[rune_id]
        if attr == ATTR_COST:
            e["cost"] += value
        elif attr in ATTR_BASE:
            e["stat"].setdefault(ATTR_BASE[attr], [0, 0])[0] += value
        elif attr in ATTR_MAX:
            e["stat"].setdefault(ATTR_MAX[attr], [0, 0])[1] += value
        elif attr in ATTR_NEED:
            e["need"][ATTR_NEED[attr]] = value
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--defaults-file", required=True)
    ap.add_argument("--db", default="magicbane")
    ap.add_argument("--mysql", default="mariadb")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "data", "planner.json"))
    args = ap.parse_args()

    races = []
    race_by_token = {}
    for r in q(args, "SELECT ID, name, description, token, startingPoints, "
                     "strStart, dexStart, conStart, intStart, spiStart, "
                     "strMax, dexMax, conMax, intMax, spiMax FROM static_rune_race ORDER BY name"):
        race = {"id": int(r[0]), "name": r[1], "desc": r[2] or "", "points": int(r[4]),
                "start": dict(zip(STATS, map(int, r[5:10]))),
                "max": dict(zip(STATS, map(int, r[10:15]))), "classes": []}
        races.append(race)
        race_by_token[int(r[3])] = race["id"]

    classes = []
    class_by_token = {}
    for r in q(args, "SELECT ID, name, description, token, strMod, dexMod, conMod, intMod, spiMod "
                     "FROM static_rune_baseclass ORDER BY name"):
        classes.append({"id": int(r[0]), "name": r[1], "desc": r[2] or "",
                        "mods": dict(zip(STATS, map(int, r[4:9])))})
        class_by_token[int(r[3])] = int(r[0])

    race_index = {r["id"]: r for r in races}
    for race_id, class_id in q(args, "SELECT RaceID, BaseClassID FROM static_rune_racebaseclass"):
        if int(race_id) in race_index:
            race_index[int(race_id)]["classes"].append(int(class_id))
    for r in races:
        r["classes"].sort()

    promo_reqs = defaultdict(list)
    for promo_id, token in q(args, "SELECT promoID, runereqID FROM static_rune_promotionrunereq"):
        promo_reqs[int(promo_id)].append(int(token))
    promotions = []
    for r in q(args, "SELECT ID, name, description FROM static_rune_promotion ORDER BY name"):
        tokens = promo_reqs.get(int(r[0]), [])
        promotions.append({"id": int(r[0]), "name": r[1], "desc": r[2] or "",
                           "races": sorted(race_by_token[t] for t in tokens if t in race_by_token),
                           "classes": sorted(class_by_token[t] for t in tokens if t in class_by_token)})

    effects = rune_effects(q(args, "SELECT RuneBaseID, attributeID, modValue FROM static_rune_runebaseattribute"))
    runebase = {int(r[0]): {"name": r[1], "desc": r[2] or "", "subtype": int(r[3])}
                for r in q(args, "SELECT ID, name, description, SubType FROM static_rune_runebase")}

    # Starting traits: allowed for the race AND the base class (or everyone).
    race_runes = defaultdict(set)
    for rune_id, race_id in q(args, "SELECT RuneBaseID, RaceID FROM static_rune_racerune"):
        race_runes[int(rune_id)].add(int(race_id))
    class_runes = defaultdict(set)
    for rune_id, class_id in q(args, "SELECT RuneBaseID, BaseClassesID FROM static_rune_baseclassrune"):
        class_runes[int(rune_id)].add(int(class_id))

    all_races = {r["id"] for r in races}
    all_classes = {c["id"] for c in classes}
    traits = []
    for rune_id in sorted(set(race_runes) | set(class_runes)):
        if rune_id not in runebase or rune_id in race_index or rune_id in all_classes:
            continue
        rr = race_runes.get(rune_id, set())
        cr = class_runes.get(rune_id, set())
        ok_races = all_races if EVERYONE in rr else rr & all_races
        ok_classes = all_classes if EVERYONE in cr else cr & all_classes
        if not ok_races or not ok_classes:
            continue
        e = effects.get(rune_id, {"cost": 0, "stat": {}, "need": {}})
        rb = runebase[rune_id]
        traits.append({"id": rune_id, "name": rb["name"], "desc": rb["desc"], "sub": rb["subtype"],
                       "cost": e["cost"], "stat": e["stat"], "need": e["need"],
                       "races": None if ok_races == all_races else sorted(ok_races),
                       "classes": None if ok_classes == all_classes else sorted(ok_classes)})
    traits.sort(key=lambda t: t["name"])

    # Disciplines and their requirements.
    reqs = defaultdict(lambda: {"races": [], "classes": [], "promotions": [], "conflicts": [], "level": 1})
    for rune_id, rtype, required in q(args, "SELECT runeID, type, requiredRuneID FROM static_rune_runereq"):
        rune_id, rtype, required = int(rune_id), int(rtype), int(required)
        if rune_id not in DISCIPLINE_IDS:
            continue
        key = {1: "races", 2: "classes", 3: "promotions", 4: "conflicts"}.get(rtype)
        if key:
            reqs[rune_id][key].append(required)
        elif rtype == 6:
            reqs[rune_id]["level"] = required
    disciplines = []
    for rune_id in DISCIPLINE_IDS:
        if rune_id not in runebase:
            continue
        e = effects.get(rune_id, {"cost": 0, "stat": {}, "need": {}})
        rq = reqs[rune_id]
        disciplines.append({"id": rune_id, "name": runebase[rune_id]["name"], "desc": runebase[rune_id]["desc"],
                            "cost": e["cost"], "stat": e["stat"], "need": e["need"], "level": rq["level"],
                            "races": sorted(rq["races"]) or None, "classes": sorted(rq["classes"]) or None,
                            "promotions": sorted(rq["promotions"]) or None, "conflicts": sorted(rq["conflicts"])})
    disciplines.sort(key=lambda d: d["name"])

    data = {
        "generated": datetime.date.today().isoformat(),
        "source": "EbonReach game database",
        "sample": False,
        "rules": {"statFloor": -5, "maxLevel": 75, "promotionLevel": 10, "maxRunes": 13,
                  "disciplineSlots": [[1, 3], [70, 4]]},
        "races": races, "classes": classes, "promotions": promotions,
        "traits": traits, "disciplines": disciplines,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
    print(f"{len(races)} races, {len(classes)} classes, {len(promotions)} promotions, "
          f"{len(traits)} starting traits, {len(disciplines)} disciplines -> {os.path.normpath(args.out)}")


if __name__ == "__main__":
    main()
