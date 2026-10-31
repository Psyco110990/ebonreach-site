#!/usr/bin/env python3
"""Builds data/items.json for items.html from an EbonReach game database.

    python3 tools/build_item_data.py --defaults-file my.cnf --db magicbane

my.cnf is a MariaDB client file ([client] user=... password=...). Point it
at the live database or a restored backup; it only reads.

For every item a player can get (sold by a vendor, or dropped by a mob that
is actually spawned), it records the item's stats, which vendors sell it and
for how much, and which mobs drop it with the chance per kill.

Drop chances follow the server code (engine.gameManager.LootManager):
  - each LOOT entry in a mob's booty sets (its mob type's and its own) rolls
    1-100 and drops if roll < dropChance x NORMAL_DROP_RATE ("<", so a 4%
    entry at 1.0x is really 3%; computed as the code does it)
  - the treasure table then rolls 1-100 to pick an item list
  - the item list rolls between the mob's level-based min and max
    (TableRoll: min = 4.469 x level - 3.469, at least 70; max = 4.882 x level
    + 127, at most 319; level capped at 65)
  - where ranges overlap, the last matching row wins (rollTable)
  - ITEM entries drop if roll 1-100 <= dropChance
Hotzone bonus rolls and worn equipment drops are left out.
"""
import argparse
import datetime
import json
import math
import os
import subprocess
import sys
from collections import defaultdict

NORMAL_DROP_RATE = 1.5      # MB_NORMAL_DROP_RATE on live
MAX_SOURCES = 8             # mobs listed per item; the rest are counted


def q(args, sql):
    cmd = [args.mysql, f"--defaults-extra-file={args.defaults_file}", "-N", "-B", "-e", sql, args.db]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"query failed: {r.stderr.strip()}\n{sql}")
    return [[None if c == "NULL" else c for c in line.split("\t")] for line in r.stdout.splitlines()]


def table_roll_range(level):
    level = min(level, 65)
    hi = min(int(4.882 * level + 127.0), 319)
    lo = max(int(4.469 * level - 3.469), 70)
    return lo, hi


def last_match_counts(rows, lo, hi):
    """rows: [(minRoll, maxRoll, value)] in table order. Returns {value: count of
    rolls in lo..hi that pick it}, last matching row winning, as rollTable does."""
    counts = defaultdict(int)
    for roll in range(lo, hi + 1):
        pick = None
        for mn, mx, val in rows:
            if mn <= roll <= mx:
                pick = val
        if pick is not None:
            counts[pick] += 1
    return counts


def loot_chance(dc, rate):
    # rolls k in 1..100 with k < dc * rate
    return max(0, min(100, math.ceil(dc * rate) - 1)) / 100.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--defaults-file", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--mysql", default="mariadb")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "items.json"))
    ap.add_argument("--rate", type=float, default=NORMAL_DROP_RATE)
    args = ap.parse_args()

    items = {}
    for r in q(args, """SELECT ID, name, type, value, weight, skillRequired, percentRequired,
                               minDamage, maxDamage, damageType, speed, `range`, twoHanded,
                               defense, slashResist, crushResist, pierceResist, numCharges
                          FROM static_itembase"""):
        (iid, name, typ, value, weight, skill, pct, mind, maxd, dmgt, speed, rng, two,
         defense, sres, cres, pres, charges) = r
        items[int(iid)] = {
            "id": int(iid), "name": name, "type": typ, "value": int(value or 0),
            "weight": int(weight or 0), "skill": skill or "", "skillPct": int(pct or 0),
            "dmg": [int(float(mind or 0)), int(float(maxd or 0))], "dmgType": dmgt or "",
            "speed": float(speed or 0), "range": float(rng or 0), "twoHanded": two == "1",
            "defense": int(float(defense or 0)), "resist": [round(float(x or 0), 1) for x in (sres, cres, pres)],
            "charges": int(charges or 0),
        }

    # Vendors: NPC -> contract -> inventory set -> items, priced as buyFromNPC does
    # for non-city vendors: value x (1 + sellPercent). Vendors in player cities
    # are left out: their owners set stock and prices, and some are dev/test towns.
    vendors = defaultdict(list)
    for name, contract, zone, sellpct, iid in q(args, """
            SELECT n.npc_name, c.name, z.Name, n.npc_sellPercent, i.itembaseID
              FROM obj_npc n
              JOIN static_npc_contract c ON c.ID = n.npc_contractID
              JOIN static_npc_inventoryset i ON i.inventorySet = c.inventorySet
              JOIN object o ON o.UID = n.UID
              LEFT JOIN obj_zone z ON z.UID = o.parent
             WHERE c.inventorySet > 0
               AND COALESCE(z.isPlayerCity, 0) = 0"""):
        iid = int(iid)
        if iid not in items:
            continue
        price = int(items[iid]["value"] * (1 + float(sellpct or 0)))
        vendors[iid].append({"npc": name, "role": contract, "zone": zone or "", "price": price})

    # Loot tables.
    gen = defaultdict(list)       # genTable -> [(min, max, itemTableID)]
    for g, mn, mx, it in q(args, "SELECT genTable, minRoll, maxRoll, itemTableID FROM static_loot_gen"):
        gen[int(g)].append((int(mn), int(mx), int(it)))
    itab = defaultdict(list)      # itemTable -> [(min, max, itemBaseUUID)]
    for t, mn, mx, ib in q(args, "SELECT itemTable, minRoll, maxRoll, itemBaseUUID FROM static_loot_item"):
        itab[int(t)].append((int(mn), int(mx), int(ib)))
    booty = defaultdict(list)     # bootySet -> [(type, genTable, itemBase, dropChance)]
    for bs, typ, g, ib, dc in q(args, "SELECT bootySet, bootyType, genTable, itemBase, dropChance FROM static_loot_bootySet"):
        booty[int(bs)].append((typ, int(g or 0), int(ib or 0), float(dc or 0)))

    gen_pick = {g: last_match_counts(rows, 1, 100) for g, rows in gen.items()}

    # Spawned mobs, grouped by (mob type, own booty set, level): same odds.
    groups = defaultdict(lambda: {"zones": set(), "count": 0})
    for mbid, name, mlevel, blevel, mbooty, bbooty, contract, zone in q(args, """
            SELECT b.ID, b.name, m.mob_level, b.level, m.bootySet, b.bootySet, m.mob_contractID, z.Name
              FROM obj_mob m
              JOIN static_npc_mobbase b ON b.ID = m.mob_mobbaseID
              JOIN object o ON o.UID = m.UID
              LEFT JOIN obj_zone z ON z.UID = o.parent"""):
        level = int(mlevel or 0) if (contract and contract != "0") else int(blevel or 0)
        key = (name, level, int(bbooty or 0), int(mbooty or 0))
        groups[key]["count"] += 1
        if zone:
            groups[key]["zones"].add(zone)

    drops = defaultdict(list)
    range_cache = {}
    for (name, level, bbooty, mbooty), g in groups.items():
        lo, hi = table_roll_range(level)
        chance = defaultdict(float)
        for bs in (bbooty, mbooty):
            for typ, gtab, ib, dc in booty.get(bs, []) if bs else []:
                if typ == "LOOT" and gtab in gen_pick:
                    p_drop = loot_chance(dc, args.rate)
                    if p_drop <= 0:
                        continue
                    for itable, n in gen_pick[gtab].items():
                        key = (itable, lo, hi)
                        if key not in range_cache:
                            range_cache[key] = last_match_counts(itab.get(itable, []), lo, hi)
                        width = hi - lo + 1
                        for iid, m in range_cache[key].items():
                            chance[iid] += p_drop * (n / 100.0) * (m / width)
                elif typ == "ITEM" and ib:
                    chance[ib] += max(0, min(100, math.floor(dc))) / 100.0
        for iid, p in chance.items():
            if iid in items and p > 0:
                drops[iid].append({"mob": name, "level": level, "chance": p,
                                   "zones": sorted(g["zones"])[:4], "spawns": g["count"]})

    out = []
    for iid, it in items.items():
        v = vendors.get(iid, [])
        d = drops.get(iid, [])
        if not v and not d:
            continue
        # Merge the same mob name+level that differ only by booty set; keep best odds.
        best = {}
        for e in d:
            k = (e["mob"], e["level"])
            if k not in best or e["chance"] > best[k]["chance"]:
                best[k] = dict(e)
            else:
                best[k]["zones"] = sorted(set(best[k]["zones"]) | set(e["zones"]))[:4]
        d = sorted(best.values(), key=lambda e: -e["chance"])
        for e in d:
            e["chance"] = round(e["chance"] * 100, 3)  # percent per kill
        seen, vend = set(), []
        for e in sorted(v, key=lambda e: (e["price"], e["zone"], e["npc"])):
            k = (e["npc"], e["zone"])
            if k not in seen:
                seen.add(k)
                vend.append(e)
        rec = dict(it)
        rec["vendors"] = vend[:12]
        rec["vendorCount"] = len(vend)
        rec["drops"] = d[:MAX_SOURCES]
        rec["dropCount"] = len(d)
        out.append(rec)

    out.sort(key=lambda r: (r["name"].lower(), r["id"]))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"generated": datetime.date.today().isoformat(), "dropRate": args.rate, "items": out},
                  f, separators=(",", ":"), ensure_ascii=False)
    print(f"wrote {len(out)} items to {args.out} ({sum(1 for r in out if r['vendors'])} sold, "
          f"{sum(1 for r in out if r['drops'])} dropped)")


if __name__ == "__main__":
    main()
