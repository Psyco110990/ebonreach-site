#!/usr/bin/env python3
"""Builds maps/vorringia-zones.json for map.html from the EbonReach repo's map data.

    python3 tools/build_map_data.py /path/to/EbonReach/Docs/mapdata

Inputs (EbonReach repo, Docs/mapdata):
  vorringia_zone_origins.csv  zone centres in world units (from obj_zone, absolute)
  all_zonedata.csv            level bands and lore, extracted from the client
  base_Vorringia.png          the map image (copied to maps/vorringia.jpg)

The image spans the whole world frame, 131072 x 98304 world units, so
pixel = (X / 128, (98304 - |Z|) / 192) at 1024x512. Checked by plotting all 42
zones: every one lands on its island.
"""
import csv, json, os, sys
from PIL import Image

src = sys.argv[1] if len(sys.argv) > 1 else "../EbonReach/Docs/mapdata"
here = os.path.dirname(os.path.abspath(__file__))
out_dir = os.path.join(here, "..", "maps")
os.makedirs(out_dir, exist_ok=True)

info = {}
with open(os.path.join(src, "all_zonedata.csv"), encoding="utf-8") as f:
    for r in csv.DictReader(f):
        if r["Map"] == "Vorringia":
            info[r["Zone"].strip().lower()] = r

zones, missing = [], []
with open(os.path.join(src, "vorringia_zone_origins.csv"), encoding="utf-8") as f:
    for r in csv.DictReader(f):
        if r["zone"] == "Vorringia":        # the continent root sits in open sea
            continue
        z = {"name": r["zone"], "x": int(r["absX"]), "z": -int(r["absZ"])}
        i = info.get(r["zone"].strip().lower())
        if i:
            # level bands are text in the client data, e.g. "20", "60+"
            lo, hi = i["MinLevel"].strip(), i["MaxLevel"].strip()
            z["levels"] = f"{lo}–{hi}" if hi else f"{lo}+"
            z["min"] = int("".join(c for c in i["MinLevel"] if c.isdigit()) or 0)
            z["lore"] = i["Lore"].strip()
        else:
            missing.append(r["zone"])
        zones.append(z)

data = {"world": {"width": 131072, "height": 98304},
        "image": "maps/vorringia.jpg", "zones": sorted(zones, key=lambda z: z["name"])}
with open(os.path.join(out_dir, "vorringia-zones.json"), "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=1)

Image.open(os.path.join(src, "base_Vorringia.png")).convert("RGB").save(
    os.path.join(out_dir, "vorringia.jpg"), quality=88, optimize=True)

print(f"{len(zones)} zones written; no level/lore for: {', '.join(missing) or 'none'}")
