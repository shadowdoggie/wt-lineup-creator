#!/usr/bin/env python3
"""
Precomputes four static tables the app ships with, all derived from each tank's
model file under
    aces.vromfs.bin_u/gamedata/units/tankmodels/<id>.blkx
(far too many files to fetch in the browser at runtime):

  data/mobility.json  — id -> horsepower-per-ton. The economy file (wpcost.blkx)
                        caps its `speed` field, so real mobility comes from the
                        physics block here.
  data/gunstats.json  — id -> {"v": muzzle velocity m/s, "c": bore mm,
                        "p": penetration mm at 1000m}. Drives the Sniper
                        playstyle. Penetration IS stored in the shell files
                        (armorpower block: ArmorPower0m / ArmorPower500m /
                        ArmorPower1000m arrays where [0] is penetration mm), so
                        we read the best AP shell's penetration at 1000m
                        directly — no DeMarre proxy needed. Reaching them needs
                        one extra hop per gun: tankmodel -> commonWeapons
                        cannon .blk -> shell definitions (weapon files are
                        cached since many vehicles share a gun).
  data/spaa.json      — id -> {"sam": 0/1, "radar": 0/1, "cal": mm} for SPAA
                        only. Lets the app rank anti-air by real capability
                        (a radar SAM launcher vs a WWII quad-MG) instead of
                        all-or-nothing. Read straight off the same tankmodel:
                        a surface-to-air missile launcher, a tracking-radar
                        sensor block, and the largest gun caliber.
  data/armor.json     — id -> {"h": hull_front_mm, "t": turret_front_mm,
                        "eff": effective_armor_rating, "stab": 0/1, "thermal":
                        0/1, "nv": 0/1, "rev": 0..1} for every tank. The raw
                        hull/turret numbers are the thickest steel plate on the
                        front of each, read from DamageParts — they replace the
                        Shop display values (which are missing for ~15% of
                        tanks, leaving the old UI with no armor figure for them).
                        The `eff` rating folds composite armor, ERA, and
                        spall-liners into a single KE-effective thickness so the
                        Armor playstyle can tell a T-90M (Relikt ERA + composite)
                        from a Maus (200mm RHA, nothing else). See
                        _armor_stats_from_model. `stab` is gun stabilization
                        presence, `thermal`/`nv` are thermal imaging / night
                        vision, `rev` is reverse speed as a fraction of forward
                        speed (from gear ratios).
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
ARMOR_OUT = os.path.join(os.path.dirname(os.path.abspath(MOBILITY_OUT)), "armor.json")

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
    best_pen = 0
    bore_m = 0.0
    for w in weps:
        if not (isinstance(w, dict) and "blk" in w):
            continue
        # Main gun only — machine guns and mortars (spigot bombs on the Matilda
        # Hedgehog) carry their own rounds that would pollute velocity, caliber,
        # and penetration.
        blk_l = w["blk"].lower()
        if "machinegun" in blk_l or "mortar" in blk_l:
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
                # Penetration estimate. War Thunder does NOT store a precomputed
                # penetration value — it computes penetration at runtime from the
                # shell's physics parameters. We estimate it from the two models
                # the game uses:
                #
                #  • APFSDS/APDS (modern): Lanz-Odermatt. The penetrator's
                #    workingLength (mm) × density ratio gives the penetration at
                #    ~point-blank. A DU penetrator (density 18600) vs RHA (7850)
                #    gives ~2.37× the working length. Tungsten (17600) ~2.24×.
                #    At 1000m the penetrator retains most of its speed (APFSDS
                #    loses little velocity), so the 1000m pen ≈ 0.9 × point-blank.
                #
                #  • APCBC/APCR (WWII): DeMarre. The hitpower block's HitPower0m[0]
                #    is a velocity-retention factor (1.0 at muzzle, ~0.9 at 1000m).
                #    Penetration scales with speed² × mass^0.71 × caliber^1.07,
                #    normalized so a reference shell (e.g. 88mm KwK36 APCBC at
                #    773 m/s, 10.2 kg) produces ~203mm at 0m (known in-game value).
                #    The DeMarre K constant is derived from this reference.
                dmg = b.get("damage", {})
                kin = dmg.get("kinetic", {}) if isinstance(dmg, dict) else {}
                lo_len = kin.get("lanzOdermattWorkingLength")
                lo_density = kin.get("lanzOdermattDensity")
                if isinstance(lo_len, (int, float)) and isinstance(lo_density, (int, float)) and lo_len > 0 and v:
                    # Lanz-Odermatt: penetration ≈ workingLength × (density / 7850)
                    # scaled by speed retention (APFSDS retains ~90% at 1000m).
                    density_ratio = lo_density / 7850.0
                    pen_0m = lo_len * density_ratio
                    # hitpower retention factor at 1000m (default 0.9 if absent)
                    hp = b.get("hitpower", {})
                    retention = 0.9
                    if isinstance(hp, dict):
                        hp1k = hp.get("HitPower1000m")
                        if isinstance(hp1k, list) and hp1k and isinstance(hp1k[0], (int, float)):
                            retention = hp1k[0]
                    best_pen = max(best_pen, round(pen_0m * retention))
                elif v and isinstance(b.get("mass"), (int, float)):
                    # DeMarre estimate for WWII shells. K is calibrated so the
                    # 88mm KwK36 APCBC (773 m/s, 10.2 kg, 88mm) ≈ 203mm at 0m,
                    # matching known in-game penetration.
                    mass = b["mass"]
                    cal_mm = cal * 1000
                    K_demarre = 0.00211  # calibrated vs 88mm KwK36 APCBC = 203mm
                    pen_0m = K_demarre * (mass ** 0.71) * (v ** 1.43) * (cal_mm ** 0.07)
                    # Scale to 1000m using hitpower retention
                    hp = b.get("hitpower", {})
                    retention = 0.9
                    if isinstance(hp, dict):
                        hp1k = hp.get("HitPower1000m")
                        if isinstance(hp1k, list) and hp1k and isinstance(hp1k[0], (int, float)):
                            retention = hp1k[0]
                    pen_1k = pen_0m * (retention ** 1.43)  # speed scales with retention, pen with speed^1.43
                    best_pen = max(best_pen, round(pen_1k))
    if best_vel:
        result = {"v": round(best_vel), "c": round(bore_m * 1000, 1)}
        if best_pen > 0:
            result["p"] = best_pen
        return result
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
    # A radar SPAA with no guns (cal=0) and no missile weapons is the FCS/radar
    # half of a split SAM system (e.g. NASAMS FCS, SAMP/T FCS, Iris-SLM FCS).
    # It's a real SAM system in-game — the launcher is the paired vehicle — so
    # treat it as SAM for scoring. This is a game-wide pattern, not a tank list.
    if radar and not sam and cal == 0:
        sam = True
    return {"sam": int(sam), "radar": int(radar), "cal": round(cal, 1)}


# --- Effective-armor extraction ------------------------------------------------
#
# The Armor playstyle used to score vehicles on raw steel thickness alone
# (Shop.armorThicknessHull/Turret), which is wrong at top BR: a T-90M (~90mm
# steel + Relikt ERA + composite) outranked nothing, while a Maus (200mm RHA,
# no ERA) won the slot. The shop values are also missing for ~15% of tanks,
# leaving the UI with a blank armor figure.
#
# This reads the DamageParts block of the tankmodel — the same file we already
# fetch for mobility/guns/SPAA — and derives three numbers per vehicle:
#
#   h   — raw steel on the hull front (thickest front-facing plate, mm)
#   t   — raw steel on the turret front (thickest front-facing plate, mm)
#   eff — an effective-armor rating (mm-equivalent) that adds composite arrays,
#         ERA tiles, and spall-liners on top of the raw steel
#
# The eff rating is intentionally a *ranking score*, not a precise penetration
# barrier: War Thunder's actual armor model is voxel-based and angle-dependent,
# so a single number can never be "correct". What we want is for the scorer to
# rank a T-90M above a T-72B3 above a T-34-85, which raw steel alone can't do.
#
# How eff is built (all from DamageParts):
#   • Start from max(h, t): the thickest raw-steel front is the base.
#   • Composite arrays (composite_armor_*_dm blocks): each contributes
#     armorThickness * genericArmorQuality (the KE multiplier the game assigns
#     to the array — 1.0 ≈ RHA-equivalent, 0.5 ≈ half-effective vs AP). We sum
#     the front-facing ones only. This captures NERA/composite inserts (Leopard
#     2A5 wedge, Abrams DU, Chally 2 Dorchester, T-series Fofa).
#   • ERA (era_* containers with ex_era_*_dm elements, plus blocks carrying
#     explosionArmorQuality): counted as a flat +15mm-equivalent per ERA element,
#     scaled by explosionArmorQuality when present. ERA in WT is binary (it
#     either defeats a shaped-charge jet or it doesn't), so a per-tile bonus
#     rewards vehicles with more coverage. Kontakt-1/5 and Relikt both register.
#   • Spall liner (hull_spall_liner with armour_aramide_fabric): +8mm flat. It
#     reduces behind-armor spall rather than stopping a round, so it's a small
#     nudge, not a multiplier.
#
# The composite and ERA terms are capped so a single huge array can't dominate
# (e.g. the 800mm Abrams turret composite is a LoS thickness, not 800mm of RHA).
# Caps: composite bonus ≤ 250mm, ERA bonus ≤ 80mm. This keeps the rating
# grounded in what a player actually feels in a match.
#
# What counts as "composite" vs "raw steel"? The game's `armorClass` field is
# the authoritative signal. Standard steel classes (RHA, CHA, aluminium,
# titanium, structural steel, etc.) are raw steel. Anything else — NERA,
# textolite, spaced_armor, special_armor, composite_armor classes — is a
# composite/special array. This works game-wide without hardcoding tank names:
# a Leo 2A5 wedge (class=leopard_2a5_turret_nera), a T-80U insert
# (class=t_80u_turret_composite_armor), and an Abrams special array
# (class=hull_side_special_armor) all register, while a Maus's pure RHA
# (class=RHA_tank) doesn't.

# DamageParts sub-blocks that hold front-facing steel (hull/turret front). The
# hull/turret containers themselves don't have an armorThickness, but their
# *_front_dm children do. We look for any child whose key contains "front".
_FRONT_RE = re.compile(r"front", re.I)
# Composite array blocks: keys matching composite_armor_* anywhere in DamageParts.
_COMP_RE = re.compile(r"composite_armor", re.I)
# ERA containers: keys starting with era_ (era_hull_front, era_turret_front, etc.)
# and Soviet-style relic_era_* and ex_armor_era_* blocks.
_ERA_RE = re.compile(r"^(era_|relict_era|ex_armor_era)", re.I)
# Spall liner: hull_spall_liner block.
_SPALL_RE = re.compile(r"spall_liner", re.I)

# armorClass values that mean "this is plain steel/aluminium, not composite".
# Anything NOT matching one of these prefixes is a composite/special array.
# This is a prefix check (startswith), so "RHA_tank_modern" matches "RHA".
_STEEL_CLASS_PREFIXES = (
    "RHA", "CHA", "alum", "titanium", "steel", "tank_structural",
    "tank_barrel", "tank_trak", "tank_steel", "optics", "armour_aramide",
    "nbc_liner", "CHA_tank", "abt", "aluminium",
)


def _is_steel_class(armor_class):
    """True if the armorClass string marks a block as plain steel/aluminium
    (not composite). None/empty counts as steel — many steel plates have no
    class at all, and we'd rather count them than miss real front armor."""
    if not isinstance(armor_class, str) or not armor_class:
        return True
    ac = armor_class.lower()
    return any(ac.startswith(p.lower()) for p in _STEEL_CLASS_PREFIXES)


def _iter_damage_parts(dp):
    """Yield (key, block) for every dict sub-block of DamageParts."""
    if not isinstance(dp, dict):
        return
    for k, v in dp.items():
        if isinstance(v, dict):
            yield k, v


def _armor_stats_from_model(model):
    """Effective-armor descriptor for a tank, all from its DamageParts block:
      h   — thickest front hull steel (mm)
      t   — thickest front turret steel (mm)
      eff — KE-effective armor rating (mm-equivalent), folding in composite
            arrays, ERA, and spall-liners on top of the raw steel base.
    Returns None only if the model has no DamageParts at all."""
    dp = model.get("DamageParts")
    if not isinstance(dp, dict):
        return None

    # 1) Raw front steel. Front-facing plates (superstructure_front_dm,
    #    body_front_dm, turret_XX_front_dm, etc.) can live in ANY DamageParts
    #    sub-block. We scan every sub-block's children for *_front_dm plates.
    #    A plate counts as raw steel UNLESS:
    #      a) Its own key matches composite_armor_* and its armorClass is
    #         non-standard (composite) — it's a composite insert.
    #      b) Its container's armorClass is non-standard (composite) — the
    #         whole container is a composite array (e.g. Leo 2A5's NERA wedge
    #         stored as body_front_dm inside a leopard_2a5_turret_nera container).
    #    This lets T-80U's superstructure_front_dm (50mm, container class=None)
    #    count as steel, while Leo 2A5's body_front_dm (400mm, container class=
    #    leopard_2a5_turret_nera) counts as composite.
    hull_mm = 0.0
    turret_mm = 0.0
    # 2) Composite bonus: accumulated alongside raw steel in the same pass.
    composite_bonus = 0.0
    n_composite = 0

    for key, blk in _iter_damage_parts(dp):
        if not isinstance(blk, dict):
            continue
        container_is_composite = not _is_steel_class(blk.get("armorClass"))
        for k2, v2 in blk.items():
            if not isinstance(v2, dict) or not _FRONT_RE.search(k2):
                continue
            t = v2.get("armorThickness")
            if not isinstance(t, (int, float)) or t <= 0:
                continue
            # Check if this specific plate is composite:
            # - key matches composite_armor_* AND has a non-steel class, OR
            # - the container itself is composite-classed
            plate_is_composite = False
            if _COMP_RE.search(k2):
                plate_class = v2.get("armorClass")
                if not _is_steel_class(plate_class):
                    plate_is_composite = True
                # Also check genericArmorQuality — its presence marks a real
                # composite array even when the class is empty (T-90M's arrays
                # sometimes have gq but no class).
                gq = v2.get("genericArmorQuality")
                if isinstance(gq, (int, float)) and gq > 0:
                    plate_is_composite = True
            if container_is_composite and not _COMP_RE.search(k2):
                # A non-composite-named plate inside a composite container
                # (Leo 2A5's body_front_dm inside hull_front_composite_armor
                # with class=leopard_2a5_turret_nera) — the plate IS composite.
                plate_is_composite = True

            k2l = k2.lower()
            if plate_is_composite:
                # Add to composite bonus.
                gq = v2.get("genericArmorQuality")
                if isinstance(gq, (int, float)) and gq > 0:
                    composite_bonus += t * gq
                else:
                    # No KE multiplier on record (T-80U's textolite insert,
                    # Leo 2A5's NERA wedge) — treat as RHA-equivalent (1.0).
                    composite_bonus += t
                n_composite += 1
            else:
                # Raw steel — attribute by the plate's own name.
                if "turret" in k2l or "mask" in k2l:
                    turret_mm = max(turret_mm, t)
                else:
                    hull_mm = max(hull_mm, t)

    # Also collect composite_armor_* blocks that are NOT front-facing (some
    # arrays don't use "front" in their key name but are still front armor).
    # These appear as children of composite containers or standalone blocks.
    for key, blk in _iter_damage_parts(dp):
        if not isinstance(blk, dict):
            continue
        for k2, v2 in blk.items() if isinstance(blk, dict) else []:
            if not isinstance(v2, dict) or not _COMP_RE.search(k2):
                continue
            # Skip if already counted as a front plate above.
            if _FRONT_RE.search(k2):
                continue
            t = v2.get("armorThickness")
            if not isinstance(t, (int, float)) or t <= 10.0:
                continue
            gq = v2.get("genericArmorQuality")
            plate_class = v2.get("armorClass")
            # Only count if it has gq OR a non-steel class — distinguishes real
            # composite arrays from structural steel named composite_armor_*.
            has_gq = isinstance(gq, (int, float)) and gq > 0
            has_comp_class = not _is_steel_class(plate_class)
            if has_gq:
                composite_bonus += t * gq
                n_composite += 1
            elif has_comp_class:
                composite_bonus += t
                n_composite += 1
        # Also handle composite containers at the top level directly.
        if _COMP_RE.search(key) and isinstance(blk, dict):
            t = blk.get("armorThickness")
            gq = blk.get("genericArmorQuality")
            if isinstance(t, (int, float)) and t > 10.0:
                if isinstance(gq, (int, float)) and gq > 0:
                    composite_bonus += t * gq
                    n_composite += 1
                elif not _is_steel_class(blk.get("armorClass")):
                    composite_bonus += t
                    n_composite += 1

    composite_bonus = min(composite_bonus, 250.0)

    # 3) ERA: count tiles and scale by explosionArmorQuality where present.
    #    Each ex_era_*_dm element is one ERA tile. Containers may carry an
    #    explosionArmorQuality (the ERA's HE-equivalent rating) which we use as
    #    a per-tile weight; when absent, default to 1.0 (a generic tile).
    era_bonus = 0.0
    n_era = 0
    for key, blk in _iter_damage_parts(dp):
        if not _ERA_RE.search(key):
            continue
        if not isinstance(blk, dict):
            continue
        # Container-level explosionArmorQuality applies to all its tiles.
        eq = blk.get("explosionArmorQuality")
        tile_weight = float(eq) if isinstance(eq, (int, float)) and eq > 0 else 1.0
        for k2, v2 in blk.items():
            if not isinstance(v2, dict):
                continue
            # ex_era_*_dm is the per-tile element; also count ex_armor_* blocks
            # that carry their own explosionArmorQuality.
            if "ex_era" in k2.lower() or "ex_armor_era" in k2.lower():
                n_era += 1
                era_bonus += 15.0 * tile_weight
            elif _ERA_RE.search(k2):
                # Nested era container (relict_era_hull_inner inside era_hull_*).
                eq2 = v2.get("explosionArmorQuality")
                tw2 = float(eq2) if isinstance(eq2, (int, float)) and eq2 > 0 else tile_weight
                for k3, v3 in v2.items():
                    if isinstance(v3, dict) and "ex_era" in k3.lower():
                        n_era += 1
                        era_bonus += 15.0 * tw2
    era_bonus = min(era_bonus, 80.0)

    # 4) Spall liner: aramide-fabric liner reduces behind-armor spalling. Small
    #    flat bonus — it's a survivability upgrade, not a penetration barrier.
    spall_bonus = 0.0
    for key, blk in _iter_damage_parts(dp):
        if _SPALL_RE.search(key) and isinstance(blk, dict):
            ac = blk.get("armorClass", "")
            if "aramide" in str(ac).lower() or "fabric" in str(ac).lower():
                spall_bonus = 8.0
                break

    base = max(hull_mm, turret_mm)
    if base <= 0 and composite_bonus <= 0 and era_bonus <= 0:
        # No armor data at all (e.g. open-topped M56). Return zeros so the UI
        # has something rather than null.
        return {"h": round(hull_mm, 1), "t": round(turret_mm, 1), "eff": 0.0}

    eff = base + composite_bonus + era_bonus + spall_bonus
    return {"h": round(hull_mm, 1), "t": round(turret_mm, 1), "eff": round(eff, 1)}


def _stabilized(model):
    """True if the tank has a gun stabilizer (horizontal or vertical). WWII tanks
    have no gunStabilizer block at all; modern tanks have hasHorizontal/hasVertical
    booleans. Stabilized tanks can shoot on the move — a major combat advantage."""
    common = model.get("commonWeapons") or {}
    weps = common.get("Weapon") if isinstance(common, dict) else common
    weps = weps if isinstance(weps, list) else [weps]
    for w in weps:
        if not isinstance(w, dict):
            continue
        gs = w.get("gunStabilizer")
        if isinstance(gs, dict) and (gs.get("hasHorizontal") or gs.get("hasVertical")):
            return True
    return False


def _night_vision(model):
    """Returns (thermal, nv): thermal=True if the tank has thermal imaging,
    nv=True if it has any night vision (IR or thermal). WWII tanks have no
    nightVision block at all. Modern tanks have thermals as a researchable
    modification (modifications.night_vision_system.effects.nightVision) with
    gunnerThermal/commanderViewThermal/driverThermal sub-blocks, and a
    hasNightVision flag on the turret weapon."""
    # Check the top-level nightVision block (some tanks have it stock)
    nv_block = model.get("nightVision")
    if isinstance(nv_block, dict) and nv_block:
        thermal, nv = False, False
        for k, v in nv_block.items():
            if not isinstance(v, dict):
                continue
            if "thermal" in k.lower():
                thermal = True
                nv = True
            elif "ir" in k.lower() or "night" in k.lower():
                nv = True
        if thermal or nv:
            return thermal, nv
    # Check the night_vision_system modification (the usual path for modern tanks)
    mods = model.get("modifications", {})
    if isinstance(mods, dict):
        nvs = mods.get("night_vision_system", {})
        if isinstance(nvs, dict):
            effects = nvs.get("effects", {})
            if isinstance(effects, dict):
                nv2 = effects.get("nightVision", {})
                if isinstance(nv2, dict):
                    thermal = False
                    nv = False
                    for k, v in nv2.items():
                        if not isinstance(v, dict):
                            continue
                        if "thermal" in k.lower():
                            thermal = True
                            nv = True
                        elif "ir" in k.lower() or "night" in k.lower():
                            nv = True
                    if thermal or nv:
                        return thermal, nv
    # Check hasNightVision flag on turret weapons (least specific signal)
    common = model.get("commonWeapons") or {}
    weps = common.get("Weapon") if isinstance(common, dict) else common
    weps = weps if isinstance(weps, list) else [weps]
    for w in weps:
        if not isinstance(w, dict):
            continue
        turret = w.get("turret", {})
        if isinstance(turret, dict) and turret.get("hasNightVision"):
            return False, True  # has NV but we don't know if it's thermal
    return False, False


def _reverse_ratio(model):
    """Reverse speed as a fraction of forward speed, derived from gear ratios.
    The physics block's gearRatios.ratio list contains negative entries (reverse
    gears). The least-negative ratio is the fastest reverse gear; the most-
    positive is the fastest forward gear. Their ratio approximates how fast the
    tank reverses relative to its top speed — critical for RB ridgeline peaking."""
    vp = model.get("VehiclePhys", {})
    ratios = vp.get("mechanics", {}).get("gearRatios", {}).get("ratio", [])
    if not isinstance(ratios, list):
        return 0.0
    rev = [r for r in ratios if isinstance(r, (int, float)) and r < 0]
    fwd = [r for r in ratios if isinstance(r, (int, float)) and r > 0]
    if not rev or not fwd:
        return 0.0
    best_rev = max(rev)  # least negative = fastest reverse
    best_fwd = max(fwd)  # most positive = fastest forward
    if best_fwd == 0:
        return 0.0
    return round(abs(best_rev) / abs(best_fwd), 2)


def fetch_vehicle(unit_id):
    """One tankmodel fetch yields hp/ton, gun stats, (SPAA) AA stats, and
    effective-armor stats. The model file is the single network hop for all
    four — armor just reads DamageParts, which is already in memory."""
    model = get_json_ci(f"gamedata/units/tankmodels/{unit_id}.blk")
    if model is None:
        return unit_id, None, None, None, None

    vp = model.get("VehiclePhys", {})
    hp = vp.get("engine", {}).get("horsePowers")
    mass_kg = vp.get("Mass", {}).get("TakeOff")
    hpt = round(hp / (mass_kg / 1000.0), 1) if hp and mass_kg else None

    gun = _gun_stats_from_model(model, _mods_by_id.get(unit_id, frozenset()))
    spaa = _spaa_stats_from_model(model) if unit_id in _spaa_ids else None
    armor = _armor_stats_from_model(model)
    if armor is not None:
        armor["stab"] = 1 if _stabilized(model) else 0
        thermal, nv = _night_vision(model)
        armor["thermal"] = 1 if thermal else 0
        armor["nv"] = 1 if nv else 0
        armor["rev"] = _reverse_ratio(model)
    return unit_id, hpt, gun, spaa, armor


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

    mobility, guns, spaa, armor = {}, {}, {}, {}
    mob_miss, gun_miss = [], []
    done = 0
    with cf.ThreadPoolExecutor(max_workers=24) as ex:
        for uid, hpt, gun, aa, ar in ex.map(fetch_vehicle, tanks):
            done += 1
            (mobility.__setitem__(uid, hpt) if hpt is not None else mob_miss.append(uid))
            (guns.__setitem__(uid, gun) if gun is not None else gun_miss.append(uid))
            if aa is not None:
                spaa[uid] = aa
            if ar is not None:
                armor[uid] = ar
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
    _guard_regression("armor", len(armor), ARMOR_OUT)

    _atomic_write(MOBILITY_OUT, dict(sorted(mobility.items())))
    _atomic_write(GUNSTATS_OUT, dict(sorted(guns.items())))
    _atomic_write(SPAA_OUT, dict(sorted(spaa.items())))
    _atomic_write(ARMOR_OUT, dict(sorted(armor.items())))

    print(f"\nWrote {len(mobility)} entries to {os.path.basename(MOBILITY_OUT)} "
          f"({len(mob_miss)} without physics data)")
    print(f"Wrote {len(guns)} entries to {os.path.basename(GUNSTATS_OUT)} "
          f"({len(gun_miss)} without an AP round — will use fallback)")
    sam_n = sum(v["sam"] for v in spaa.values())
    print(f"Wrote {len(spaa)}/{len(_spaa_ids)} SPAA entries to "
          f"{os.path.basename(SPAA_OUT)} ({sam_n} with SAMs)")
    era_n = sum(1 for v in armor.values() if v.get("eff", 0) > max(v.get("h", 0), v.get("t", 0)))
    print(f"Wrote {len(armor)} entries to {os.path.basename(ARMOR_OUT)} "
          f"({era_n} with ERA/composite beyond raw steel)")
    if gun_miss:
        print("No-AP vehicles:", ", ".join(gun_miss[:15]) + (" …" if len(gun_miss) > 15 else ""))


if __name__ == "__main__":
    main()
