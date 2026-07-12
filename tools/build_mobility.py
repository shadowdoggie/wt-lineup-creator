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
                        "p": penetration mm at 1000m}. `p` is ONLY written when
                        the shell has an ArmorPower1000m table (the value the
                        game client uses). No Lanz-Odermatt/DeMarre estimates.
  data/spaa.json      — id -> {"sam": 0/1, "radar": 0/1, "cal": mm} for SPAA
                        only. Read straight off the tankmodel: SAM launcher,
                        tracking-radar sensor, largest gun caliber.
  data/armor.json     — id -> {"h": hull_front_mm, "t": turret_front_mm,
                        "era": 0/1, "comp": 0/1, "stab": 0/1, "thermal": 0/1,
                        "nv": 0/1, "rev": 0..1}. All factual from the model:
                        thickest front steel plates, whether ERA tiles /
                        composite arrays exist, stabilizer, optics, reverse
                        ratio. No synthetic "effective mm" rating.
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

# Kinetic armour-piercing shell family, incl. semi-AP: bulletTypes are single
# tokens like "apcbc_tank", "apfsds_long_tank", "sapcbc_tank". Match the ap/sap
# prefix at the token start so we catch AP and SAP (the KV-2's 152 mm sapcbc is
# its real anti-tank round) while excluding "shrapnel"/"smoke"/"he"/"heat" and a
# stray "napalm" — the old bare "ap" substring wrongly matched shrapnel & napalm.
AP_RE = re.compile(r"^s?ap", re.I)

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


def _armor_power_at(armorpower, distance_m):
    """Read RHA penetration (mm) from a shell's armorpower table at distance_m.
    Keys look like ArmorPower1000m: [pen_mm, distance_m]. Returns None if absent."""
    if not isinstance(armorpower, dict):
        return None
    # Exact key first (common case).
    exact = armorpower.get(f"ArmorPower{int(distance_m)}m")
    if isinstance(exact, list) and exact and isinstance(exact[0], (int, float)):
        return float(exact[0])
    # Otherwise pick the nearest tabulated distance.
    best, best_dist = None, None
    for key, val in armorpower.items():
        if not (isinstance(key, str) and key.startswith("ArmorPower") and key.endswith("m")):
            continue
        if not (isinstance(val, list) and val and isinstance(val[0], (int, float))):
            continue
        try:
            d = float(key[len("ArmorPower"):-1])
        except ValueError:
            d = val[1] if len(val) > 1 and isinstance(val[1], (int, float)) else None
        if d is None:
            continue
        if best_dist is None or abs(d - distance_m) < abs(best_dist - distance_m):
            best, best_dist = float(val[0]), d
    return best


def _shell_pen_1000m(bullet, speed, cal_m):
    """Factual RHA pen (mm) from the shell's armorpower table only.

    Prefer ArmorPower1000m (what the client usually shows at range). If that
    row is missing but other ArmorPower distances exist, use the nearest
    tabulated value (still a game-authored number — never LO/DeMarre).
    """
    ap = bullet.get("armorpower") or bullet.get("armorPower")
    table = _armor_power_at(ap, 1000.0)
    if table is not None and table > 10:
        # HEAT stock shells sometimes ship a near-zero kinetic armorpower table
        # (e.g. ArmorPower0m: [5.0, 10.0]); ignore those junk rows.
        return round(table)
    return None


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
                # Penetration at 1000m. Prefer the shell's own armorpower table
                # (ArmorPower1000m[0] is RHA mm) — this is what the game uses for
                # display and is correct for 3BM22-class shells (~400mm, not 1000+).
                # Only fall back to physics estimates when the table is absent
                # (some long-rod APFSDS store Lanz-Odermatt params instead).
                pen_1k = _shell_pen_1000m(b, v, cal)
                if pen_1k is not None:
                    best_pen = max(best_pen, pen_1k)
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

    # 3) ERA presence only (factual). We do NOT invent mm-equivalent bonuses.
    n_era = 0
    for key, blk in _iter_damage_parts(dp):
        if not _ERA_RE.search(key):
            continue
        if not isinstance(blk, dict):
            continue
        for k2, v2 in blk.items():
            if not isinstance(v2, dict):
                continue
            if "ex_era" in k2.lower() or "ex_armor_era" in k2.lower():
                n_era += 1
            elif _ERA_RE.search(k2):
                for k3, v3 in v2.items():
                    if isinstance(v3, dict) and "ex_era" in k3.lower():
                        n_era += 1

    # Factual armor descriptor: steel thicknesses + boolean protection features.
    # No synthetic "eff" mm — that was a ranking invention, not a game value.
    return {
        "h": round(hull_mm, 1),
        "t": round(turret_mm, 1),
        "era": 1 if n_era > 0 else 0,
        "comp": 1 if n_composite > 0 else 0,
    }


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


# Device tokens that mark an IR / image-intensifier night-vision optic. Matched
# per-token (see _key_tokens) so a camelCase key like "driverIr" hits on "ir"
# while "airFilter"/"mirror"/"viewingDirection" don't false-positive.
_NV_TOKENS = {"ir", "nv", "nvd", "night", "nightvision", "infrared"}


def _key_tokens(k):
    """Split a datamine key on camelCase and snake_case boundaries into a set of
    lowercase tokens. "commanderViewThermal" -> {commander, view, thermal};
    "driverIr" -> {driver, ir}."""
    parts = re.split(r"[_\s]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", k)
    return {p.lower() for p in parts if p}


def _classify_optic_key(k):
    """(thermal, nv) contribution of a single nightVision sub-block key.
    A 'thermal' token implies night vision too; an IR/NV token is night vision
    only."""
    toks = _key_tokens(k)
    if "thermal" in toks:
        return True, True
    if toks & _NV_TOKENS:
        return False, True
    return False, False


def _night_vision(model):
    """Returns (thermal, nv): thermal=True if the tank has thermal imaging,
    nv=True if it has any night vision (IR or thermal). WWII tanks have no
    nightVision data at all. Signals live in up to three places and a tank often
    has several at once (e.g. stock driverIr in the top-level `nightVision`
    block PLUS gunner/commander thermals in the night_vision_system
    modification), so we OR across ALL of them rather than returning at the first
    hit — otherwise a stock driver-IR device masks the far more important gunner
    thermal and the tank is reported as NV-only."""
    thermal = False
    nv = False

    def scan(block):
        nonlocal thermal, nv
        if not isinstance(block, dict):
            return
        for k, v in block.items():
            if not isinstance(v, dict):
                continue
            t, n = _classify_optic_key(k)
            thermal = thermal or t
            nv = nv or n

    # 1) Top-level nightVision block (stock devices — often just driver IR).
    scan(model.get("nightVision"))
    # 2) Every modification's effects.nightVision. Thermals are researchable and
    #    live under varying mod names — "night_vision_system" (Leo 2A5, Abrams)
    #    or an upgrade like "night_vision_system_upgrade_nv_to_thermal" (T-80U) —
    #    so scan them all rather than one hard-coded key.
    mods = model.get("modifications")
    if isinstance(mods, dict):
        for mod_name, mod in mods.items():
            if not isinstance(mod, dict):
                continue
            effects = mod.get("effects")
            if isinstance(effects, dict):
                scan(effects.get("nightVision"))
            # Robustness for future patches: a modification whose *name* calls
            # out thermal is a thermal-sight upgrade, even if Gaijin restructures
            # where the sight parameters live (the "..._nv_to_thermal" pattern).
            if "thermal" in _key_tokens(mod_name):
                thermal = True
                nv = True
    # 3) hasNightVision flag on turret weapons — least specific (NV, not thermal).
    if not (thermal or nv):
        common = model.get("commonWeapons") or {}
        weps = common.get("Weapon") if isinstance(common, dict) else common
        weps = weps if isinstance(weps, list) else [weps]
        for w in weps:
            if not isinstance(w, dict):
                continue
            turret = w.get("turret", {})
            if isinstance(turret, dict) and turret.get("hasNightVision"):
                nv = True
                break
    return thermal, nv


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


def _guard_field_regression(label, field, new_count, path):
    """Like _guard_regression, but guards the count of tanks carrying a boolean
    capability (thermal / nv / stab) rather than the total row count. This is the
    net that catches a *silent* detection break: if a future War Thunder patch
    moves or renames where thermals live, the entry count stays ~1230 (so the
    row-count guard passes) while the thermal field quietly collapses to zero.
    A >50% drop against the last good file fails the run loud instead, so the
    cron keeps the previous correct data and logs the failure."""
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        return  # no prior file to compare against — first run
    old_count = sum(1 for v in old.values() if isinstance(v, dict) and v.get(field))
    if old_count >= 20 and new_count < 0.5 * old_count:
        raise SystemExit(
            f"{label}: only {new_count} tanks with '{field}' vs {old_count} last "
            f"run (>50% drop) — refusing to overwrite {path}. A datamine change "
            f"likely broke {field} detection; inspect before rebuilding."
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
    # Capability-field guards: catch a silent break in thermal/NV/stab detection
    # (the row count would still look fine). Must run before the armor file is
    # overwritten so they compare against the last good run.
    thermal_n = sum(1 for v in armor.values() if v.get("thermal"))
    nv_n = sum(1 for v in armor.values() if v.get("nv"))
    stab_n = sum(1 for v in armor.values() if v.get("stab"))
    _guard_field_regression("armor/thermal", "thermal", thermal_n, ARMOR_OUT)
    _guard_field_regression("armor/nv", "nv", nv_n, ARMOR_OUT)
    _guard_field_regression("armor/stab", "stab", stab_n, ARMOR_OUT)

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
    era_n = sum(1 for v in armor.values() if v.get("era"))
    comp_n = sum(1 for v in armor.values() if v.get("comp"))
    pen_n = sum(1 for v in guns.values() if isinstance(v, dict) and v.get("p"))
    print(f"Wrote {len(armor)} entries to {os.path.basename(ARMOR_OUT)} "
          f"({era_n} with ERA, {comp_n} with composite; "
          f"{thermal_n} thermal, {nv_n} night-vision, {stab_n} stabilized)")
    print(f"Gun pen from ArmorPower tables: {pen_n}/{len(guns)} (others have vel/cal only)")
    if gun_miss:
        print("No-AP vehicles:", ", ".join(gun_miss[:15]) + (" …" if len(gun_miss) > 15 else ""))


if __name__ == "__main__":
    main()
