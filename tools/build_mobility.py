#!/usr/bin/env python3
"""
Builds data/mobility.json: a compact map of tank id -> horsepower-per-ton.

The economy file (wpcost.blkx) caps its `speed` field, so real mobility has to
come from each vehicle's physics file under
    aces.vromfs.bin_u/gamedata/units/tankmodels/<id>.blkx
which is far too many files to fetch in the browser at runtime. This script
pulls them once (concurrently) and writes a small static table the app ships
with. Re-run it after a major patch that reworks engines/weights:

    python tools/build_mobility.py

hp/ton changes very rarely, so this snapshot can lag the live BR data safely.
"""
import concurrent.futures as cf
import gzip
import json
import os
import urllib.request

RAW = "https://raw.githubusercontent.com/gszabi99/War-Thunder-Datamine/master/"
TANKMODELS = "aces.vromfs.bin_u/gamedata/units/tankmodels/"
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "mobility.json")


def get(url):
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data


def get_json(url):
    return json.loads(get(url))


def fetch_mobility(unit_id):
    # Physics filenames usually match the unit id, sometimes only in lowercase.
    for candidate in (unit_id, unit_id.lower()):
        try:
            d = get_json(f"{RAW}{TANKMODELS}{candidate}.blkx")
        except Exception:
            continue
        vp = d.get("VehiclePhys", {})
        hp = vp.get("engine", {}).get("horsePowers")
        mass_kg = vp.get("Mass", {}).get("TakeOff")
        if hp and mass_kg:
            return unit_id, round(hp / (mass_kg / 1000.0), 1)
    return unit_id, None


def main():
    print("Loading vehicle lists…")
    wpcost = get_json(RAW + "char.vromfs.bin_u/config/wpcost.blkx")
    unittags = get_json(RAW + "char.vromfs.bin_u/config/unittags.blkx")

    tanks = [
        k for k, v in wpcost.items()
        if isinstance(v, dict) and unittags.get(k, {}).get("type") == "tank"
    ]
    print(f"{len(tanks)} tanks — fetching physics files…")

    result, misses = {}, []
    done = 0
    with cf.ThreadPoolExecutor(max_workers=24) as ex:
        for uid, hpt in ex.map(fetch_mobility, tanks):
            done += 1
            if hpt is not None:
                result[uid] = hpt
            else:
                misses.append(uid)
            if done % 200 == 0:
                print(f"  {done}/{len(tanks)}")

    result = dict(sorted(result.items()))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, separators=(",", ":"), sort_keys=True)

    print(f"\nWrote {len(result)} entries to data/mobility.json "
          f"({len(misses)} without physics data, will use fallback)")
    if misses:
        print("Misses:", ", ".join(misses[:15]) + (" …" if len(misses) > 15 else ""))


if __name__ == "__main__":
    main()
