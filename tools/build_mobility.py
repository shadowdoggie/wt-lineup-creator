#!/usr/bin/env python3
"""
Precomputes three static tables the app ships with, all derived from each tank's
model file under
    aces.vromfs.bin_u/gamedata/units/tankmodels/<id>.blkx
(far too many files to fetch in the browser at runtime):

  data/mobility.json  — id -> horsepower-per-ton. The economy file (wpcost.blkx)
                        caps its `speed` field, so real mobility comes from the
                        physics block here.
  data/gunstats.json  — id -> {"v": muzzle velocity m/s, "c": bore mm}. Drives
                        the Sniper playstyle. Penetration is NOT stored in the
                        game files (it's computed at runtime via DeMarre), so we
                        use the two stored numbers that actually define a
                        long-range gun: the fastest AP shell's velocity (flat
                        trajectory) and the gun's bore caliber. Reaching them
                        needs one extra hop per gun: tankmodel -> commonWeapons
                        cannon .blk -> shell definitions (weapon files are cached
                        since many vehicles share a gun).
  data/spaa.json      — id -> {"sam": 0/1, "radar": 0/1, "cal": mm} for SPAA
                        only. Lets the app rank anti-air by real capability
                        (a radar SAM launcher vs a WWII quad-MG) instead of
                        all-or-nothing. Read straight off the same tankmodel:
                        a surface-to-air missile launcher, a tracking-radar
                        sensor block, and the largest gun caliber.
        (Aircraft/heli firepower needs no precompute — wpcost pre-aggregates
        ordnance mass and ATGM presence per weapon preset, so the browser scores
        CAS directly.)

Run after a major patch that reworks engines/weights/guns:

    python tools/build_mobility.py

These stats change rarely, so the snapshot can lag the live BR data safely.
The daily VPS cron (tools/vps-mobility.cron) re-runs this into the web root.
"""
import concurrent.futures as cf
import gzip
import json
import os
import re
import threading
import urllib.request

RAW = "https://raw.githubusercontent.com/gszabi99/War-Thunder-Datamine/master/"
ACES = RAW + "aces.vromfs.bin_u/"
TANKMODELS = ACES + "gamedata/units/tankmodels/"
# mobility.json defaults to the repo's data/ dir; the daily VPS cron overrides
# this with MOBILITY_OUT to write straight into the served web root. gunstats
# is written next to it.
MOBILITY_OUT = os.environ.get(
    "MOBILITY_OUT",
    os.path.join(os.path.dirname(__file__), "..", "data", "mobility.json"),
)
GUNSTATS_OUT = os.path.join(os.path.dirname(os.path.abspath(MOBILITY_OUT)), "gunstats.json")
SPAA_OUT = os.path.join(os.path.dirname(os.path.abspath(MOBILITY_OUT)), "spaa.json")

# Kinetic armour-piercing shell family (apbc / apcbc / aphebc / apcr / apds /
# apfsds). Excludes heat / he / smoke / shrapnel — chemical and filler rounds
# aren't what a sniping gun is judged on.
AP_RE = re.compile(r"ap", re.I)

# Gun caliber embedded in a weapon .blk filename, e.g. "23mm_2A7_user_cannon"
# or the underscore-decimal "37mm" / "12_7mm". Reading it off the name avoids
# opening the file — handy for autocannons, whose belt-fed shells don't appear
# in the top-level shell slots _shell_slots looks at.
CAL_RE = re.compile(r"(?:^|[_/])(\d+(?:_\d+)?)mm")

# Weapon .blk files are shared across many vehicles (dozens of Shermans use the
# same 75mm), so cache them to roughly halve the extra fetches.
_weapon_cache = {}
_weapon_lock = threading.Lock()

# Per-vehicle set of researchable modification names, from wpcost. A shared
# cannon .blk lists every shell the gun model can ever fire; a given vehicle
# can only equip its stock round plus the shells matching one of its mods.
_mods_by_id = {}

# Which tank ids are SPAA (from unittags). Only these get an spaa.json entry.
_spaa_ids = set()


def get(url):
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data


def get_json(url):
    return json.loads(get(url))


def get_json_ci(path_no_ext):
    """Fetch a .blkx whose reference casing may not match the mirror (which is
    largely lowercased). Tries the given casing, then all-lowercase."""
    for candidate in (path_no_ext, path_no_ext.lower()):
        try:
            return get_json(ACES + candidate + "x")
        except Exception:
            continue
    return None


def _weapon_file(blk_ref):
    with _weapon_lock:
        if blk_ref in _weapon_cache:
            return _weapon_cache[blk_ref]
    wf = get_json_ci(blk_ref)  # blk_ref is relative to aces.vromfs.bin_u
    with _weapon_lock:
        _weapon_cache[blk_ref] = wf
    return wf


def _shell_slots(weapon_file):
    """Selectable shells in a weapon .blk: the stock `bullet` (always available)
    plus each named block holding a `bullet` (a researchable round). Returns
    (mod_name_or_None, bullet_dict) pairs. Deliberately only one level deep, so
    APDS/APFSDS sabot sub-projectile cores nested inside a shell aren't mistaken
    for separate rounds."""
    slots = []
    stock = weapon_file.get("bullet")
    if isinstance(stock, dict):
        slots.append((None, stock))
    for key, val in weapon_file.items():
        if isinstance(val, dict) and isinstance(val.get("bullet"), dict):
            slots.append((key, val["bullet"]))
    return slots


def _gun_stats_from_model(model, mods):
    common = model.get("commonWeapons") or {}
    weps = common.get("Weapon") if isinstance(common, dict) else common
    weps = weps if isinstance(weps, list) else [weps]

    best_vel = 0
    bore_m = 0.0
    for w in weps:
        if not (isinstance(w, dict) and "blk" in w):
            continue
        # Main gun only — machine guns carry their own AP rounds that would
        # pollute both velocity and caliber.
        if "machinegun" in w["blk"].lower():
            continue
        wf = _weapon_file(w["blk"])
        if not wf:
            continue
        for name, b in _shell_slots(wf):
            # A named round needs the matching vehicle modification; the shared
            # cannon catalog also lists shells this vehicle can't equip.
            if name is not None and name not in mods:
                continue
            cal = b.get("caliber")
            # >=20mm keeps this to real cannon rounds (drops stray MG/sabot
            # artifacts). Autocannon (belt-fed) shells aren't in top-level slots,
            # so IFVs fall through to no-gun — correct: they aren't snipers.
            if not (isinstance(cal, (int, float)) and cal >= 0.020):
                continue
            bore_m = max(bore_m, cal)  # bore = largest caliber the vehicle fields
            if AP_RE.search(str(b.get("bulletType", ""))):
                v = b.get("speed")
                if isinstance(v, (int, float)):
                    best_vel = max(best_vel, v)  # fastest AP round = flattest shooter
    if best_vel:
        return {"v": round(best_vel), "c": round(bore_m * 1000, 1)}
    return None  # missile / HE-only / autocannon vehicles have no AP round


def _spaa_stats_from_model(model):
    """Anti-air firepower descriptor for an SPAA, all from its tankmodel:
      sam   — carries a surface-to-air missile launcher
      radar — has a tracking-radar sensor block
      cal   — largest gun caliber (mm); missile-launcher diameters excluded
    A radar SAM launcher and a towed quad-MG both tag as `type_spaa`; this is
    what tells them apart."""
    common = model.get("commonWeapons") or {}
    weps = common.get("Weapon") if isinstance(common, dict) else common
    weps = weps if isinstance(weps, list) else [weps]

    sam = False
    cal = 0.0
    for w in weps:
        if not (isinstance(w, dict) and isinstance(w.get("blk"), str)):
            continue
        blk = w["blk"].lower()
        if "rocket_launcher" in blk or "missile" in blk:
            sam = True  # its "caliber" is the missile diameter — don't count it
            continue
        if "dummy" in blk or "machinegun" in blk:
            continue  # MGs aren't the AA gun that defines the vehicle
        m = CAL_RE.search(blk.rsplit("/", 1)[-1])
        if not m:
            continue
        bore = float(m.group(1).replace("_", "."))
        # Many SAMs (Roland, Chaparral, ADATS, NASAMS, HQ-11…) are modeled as a
        # `_cannon` whose "caliber" is the missile's body diameter. Real AA guns
        # in the game top out at 80mm; every SPAA weapon of 85mm+ is a missile,
        # so treat those as a launcher, not a gun.
        if bore >= 85:
            sam = True
        else:
            cal = max(cal, bore)
    radar = bool(model.get("sensors"))
    return {"sam": int(sam), "radar": int(radar), "cal": round(cal, 1)}


def fetch_vehicle(unit_id):
    """One tankmodel fetch yields hp/ton, gun stats, and (for SPAA) AA stats."""
    model = get_json_ci(f"gamedata/units/tankmodels/{unit_id}.blk")
    if model is None:
        return unit_id, None, None, None

    vp = model.get("VehiclePhys", {})
    hp = vp.get("engine", {}).get("horsePowers")
    mass_kg = vp.get("Mass", {}).get("TakeOff")
    hpt = round(hp / (mass_kg / 1000.0), 1) if hp and mass_kg else None

    gun = _gun_stats_from_model(model, _mods_by_id.get(unit_id, frozenset()))
    spaa = _spaa_stats_from_model(model) if unit_id in _spaa_ids else None
    return unit_id, hpt, gun, spaa


def _guard_regression(label, new_count, path):
    """Refuse to overwrite an existing table if the fresh run resolved far fewer
    entries than the file already on disk — a >50% drop almost always means a
    datamine field/structure got renamed, not that half the vehicles vanished."""
    try:
        with open(path, encoding="utf-8") as f:
            old_count = len(json.load(f))
    except (OSError, ValueError):
        return  # no prior file to compare against — first run
    if old_count and new_count < 0.5 * old_count:
        raise SystemExit(
            f"{label}: only {new_count} entries vs {old_count} last run "
            f"(>50% drop) — refusing to overwrite {path}. The datamine format "
            f"likely changed; inspect before rebuilding."
        )


def _atomic_write(path, obj):
    out_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(out_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, path)  # atomic: a visitor mid-cron never sees a half file


def main():
    print("Loading vehicle lists…")
    wpcost = get_json(RAW + "char.vromfs.bin_u/config/wpcost.blkx")
    unittags = get_json(RAW + "char.vromfs.bin_u/config/unittags.blkx")

    tanks = [
        k for k, v in wpcost.items()
        if isinstance(v, dict) and unittags.get(k, {}).get("type") == "tank"
    ]
    # Which shells each vehicle can actually equip (see _gun_stats_from_model).
    _mods_by_id.update(
        {k: frozenset(wpcost[k].get("modifications", {}) or {}) for k in tanks}
    )
    _spaa_ids.update(
        k for k in tanks if unittags.get(k, {}).get("tags", {}).get("type_spaa")
    )
    print(f"{len(tanks)} tanks ({len(_spaa_ids)} SPAA) — fetching model + gun files…")

    mobility, guns, spaa = {}, {}, {}
    mob_miss, gun_miss = [], []
    done = 0
    with cf.ThreadPoolExecutor(max_workers=24) as ex:
        for uid, hpt, gun, aa in ex.map(fetch_vehicle, tanks):
            done += 1
            (mobility.__setitem__(uid, hpt) if hpt is not None else mob_miss.append(uid))
            (guns.__setitem__(uid, gun) if gun is not None else gun_miss.append(uid))
            if aa is not None:
                spaa[uid] = aa
            if done % 200 == 0:
                print(f"  {done}/{len(tanks)}")

    # Refuse to clobber good files if a structure change or network hiccup left
    # us with far less than last time (the daily cron writes into the live web
    # root). Mobility is guarded against the tank count; guns and SPAA are
    # partly-legit misses (missile/HE-only vehicles), so they're guarded against
    # the PREVIOUS run's counts — a sudden collapse means the schema moved.
    if len(mobility) < 0.5 * len(tanks):
        raise SystemExit(
            f"Only {len(mobility)}/{len(tanks)} tanks resolved — refusing to "
            f"overwrite {MOBILITY_OUT}. Likely a network issue; leaving old files."
        )
    _guard_regression("gunstats", len(guns), GUNSTATS_OUT)
    _guard_regression("spaa", len(spaa), SPAA_OUT)

    _atomic_write(MOBILITY_OUT, dict(sorted(mobility.items())))
    _atomic_write(GUNSTATS_OUT, dict(sorted(guns.items())))
    _atomic_write(SPAA_OUT, dict(sorted(spaa.items())))

    print(f"\nWrote {len(mobility)} entries to {os.path.basename(MOBILITY_OUT)} "
          f"({len(mob_miss)} without physics data)")
    print(f"Wrote {len(guns)} entries to {os.path.basename(GUNSTATS_OUT)} "
          f"({len(gun_miss)} without an AP round — will use fallback)")
    sam_n = sum(v["sam"] for v in spaa.values())
    print(f"Wrote {len(spaa)}/{len(_spaa_ids)} SPAA entries to "
          f"{os.path.basename(SPAA_OUT)} ({sam_n} with SAMs)")
    if gun_miss:
        print("No-AP vehicles:", ", ".join(gun_miss[:15]) + (" …" if len(gun_miss) > 15 else ""))


if __name__ == "__main__":
    main()
