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
                        "p": penetration mm at 1000m, "ps": "table"|"est"}.
                        Prefer ArmorPower tables (ps=table). If absent, a
                        physics estimate (ps=est) so modern APFSDS still ranks;
                        the UI must label estimates so they are never shown as
                        exact client pen.
  data/spaa.json      — id -> {"sam": 0/1, "radar": 0/1, "cal": mm} for SPAA
                        only. Read straight off the tankmodel: SAM launcher,
                        tracking-radar sensor, largest gun caliber.
  data/armor.json     — id -> {"h": hull_front_mm, "t": turret_front_mm,
                        "eff": ranking-only protection rating (mm-equiv),
                        "era": 0/1, "comp": 0/1, "al": 0/1 autoloader,
                        "stab": 0/1, "thermal": 0/1, "nv": 0/1, "rev": 0..1}.
                        h/t are factual thickest front steel plates (shown on
                        cards); eff folds composite arrays + ERA coverage into
                        a score the UI uses ONLY to rank, never displays as mm.
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

# Gaijin's own quoted armor triples from unittags Shop
# (armorThicknessHull/Turret = [front, side, back] mm) — the sanity anchor for
# the model scan. Some damage models contain internal gun-mount blocks with
# huge armorThickness values that are NOT armor (the M56 Scorpion — a
# near-unarmored TD whose shop quote is turret 5mm — models a 228mm
# `turret_04_front_dm` recoil-mechanism block; plate-name scanning read it as
# a 228mm turret and crowned it a "best armor" pick). A steel plate that
# wildly exceeds the quote is such an artifact and is skipped; see _steel_cap.
# id -> {"hull": front_mm|None, "turret": front_mm|None, "side": side_mm|None}
_shop_armor = {}


def _steel_cap(quote):
    """Max believable STEEL plate (mm) given the shop-quoted value for that
    zone. 4× tolerates legitimately-thicker-than-quote plates (T-72 cast
    cheeks ~400mm vs a 125mm quote); the +60 / 60 floors keep small real
    plates on near-zero-quote vehicles. Composite arrays are never capped —
    their LoS thickness legitimately dwarfs the quote (Leopard 2 hulls quote
    ~45mm steel but carry 600mm+ arrays), and they're already bounded by
    _COMP_CAP. None (no Shop quote at all) = no cap."""
    if quote is None:
        return None
    return max(quote * 4, quote + 60, 60)

# Heavy-tank ids (from unittags), for the post-build angling-data audit: a
# heavy with no measured hull side is exactly the gap that hid the Churchill
# bug, so every one of them is listed loudly after each build.
_heavy_ids = set()

# Sloped-or-already-strong fronts: heavies whose FRONT is already effective
# head-on (well-sloped glacis, pike nose, or plain immune), so angling only
# trades a working plate for a weaker side. Front slope is NOT in the datamine
# (it lives in the 3D collision mesh), so this knowledge list is the one
# hand-curated piece of the angling advisor — kept HERE, next to the data
# build, as the single source of truth (the site reads the baked `ang` flag,
# never its own list). A false "don't angle" is mildly suboptimal; a false
# "angle" is a death sentence — so when in doubt a tank belongs on this list.
_NO_ANGLE_RE = [
    re.compile(r"(^|_)is_[1-7]\w*"),   # IS-1…IS-7 — sloped/pike Soviet heavies
    re.compile(r"(^|_)t_10"),          # T-10 family — IS-lineage pike nose
    re.compile(r"amx_50|amx50"),       # AMX-50 — sloped nose
    re.compile(r"object"),             # Soviet "Object" heavies (248/279/770…)
    re.compile(r"wz_?111"),            # Chinese WZ-111 — IS-3-style pike
    re.compile(r"sherman_jumbo|m4a3e2"),  # Jumbo — 47deg glacis, keep square
    re.compile(r"(^|_)t14(_|$)"),      # US T14 — sloped glacis
    re.compile(r"(^|_)t26e"),          # T26E5/Super Pershing — sloped glacis
    re.compile(r"(^|_)t(29|30|32|34)"),  # US T29/30/32/34 (+captured T34s) —
                                         # sloped glacis, hull-down turrets
    re.compile(r"m6a2e1"),             # M6A2E1 — front already immune
    re.compile(r"arl_44"),             # ARL-44 — 120mm well-sloped UFP
]

# Frontal slope correction for the eff rating, by design family. The damage
# model stores plate THICKNESS but not orientation (slope lives in the 3D
# collision mesh), so raw plates systematically underrate sloped designs: an
# IS-2's 120mm-at-60° glacis (~240mm LoS) reads as 120 while a T34's flat
# 305mm mantlet reads as 305 — which made the USA sweep every WWII "best
# armor" bracket. Like the angling list above, this is hand-curated ground
# truth (multipliers ≈ 1/cos of the documented glacis/turret slope,
# conservative), kept HERE next to the canon guards. First matching regex
# wins; (hull_mult, turret_mult) apply to the STEEL paths only — composite
# arrays already model LoS thickness and are never multiplied.
_SLOPE_EFF = [
    # --- casemates / TDs first (before broader family patterns) ---
    (re.compile(r"jagdpanther"),               1.7, 1.7),   # 80mm @ 55°
    (re.compile(r"jagdtiger"),                 1.3, 1.05),  # 150 @ 50 hull, 250 @ 15 case
    (re.compile(r"ferdinand"),                 1.0, 1.0),   # 200mm flat
    # --- USSR / China (sloped & pike school) ---
    (re.compile(r"(^|_)t_34_85"),              1.6, 1.1),
    (re.compile(r"(^|_)t_34"),                 1.8, 1.1),   # 45mm @ 60°
    (re.compile(r"kv_85|kv_122"),              1.2, 1.2),
    (re.compile(r"kv_1"),                      1.25, 1.15),
    (re.compile(r"(^|_)is_1"),                 1.5, 1.2),
    (re.compile(r"(^|_)is_2"),                 1.9, 1.2),   # 120 @ 60° glacis
    (re.compile(r"(^|_)is_3|wz_111"),          2.6, 1.35),  # pike nose 110 compound
    (re.compile(r"(^|_)is_4"),                 1.9, 1.35),
    (re.compile(r"t_10a|t_10b|t_10m"),         1.9, 1.3),
    (re.compile(r"(^|_)t_44"),                 1.6, 1.15),
    (re.compile(r"t_54_|t_55|type_59|type_69"), 1.9, 1.25), # 100 @ 60°
    (re.compile(r"(^|_)t_62"),                 1.9, 1.25),
    # --- Germany ---
    (re.compile(r"tiger_ii|kungstiger"),       1.55, 1.0),  # 150 @ 50° glacis
    (re.compile(r"panther"),                   1.8, 1.0),   # 80-85 @ 55° glacis
    (re.compile(r"maus|pzkpfw_e_100"),         1.55, 1.15), # 200 @ 55°
    # --- USA ---
    (re.compile(r"m4a3e2"),                    1.5, 1.1),   # Jumbo 47° glacis + tranny
    (re.compile(r"(^|_)t26e5"),                1.4, 1.15),
    (re.compile(r"(^|_)t2[69]e?|(^|_)t3[024]"), 1.5, 1.0),  # T29/30/32/34 sloped hulls, flat mantlets
    (re.compile(r"(^|_)m4[678]|(^|_)m60"),     1.4, 1.1),
    # --- Britain / France ---
    (re.compile(r"centurion|shot_kal|caernarvon"), 1.6, 1.1),
    (re.compile(r"conqueror"),                 1.3, 1.0),
    (re.compile(r"arl_44"),                    1.5, 1.0),
    (re.compile(r"amx_m4|amx_50"),             1.3, 1.0),
]


def _slope_mults(uid):
    uid_l = uid.lower()
    for rx, hm, tm in _SLOPE_EFF:
        if rx.search(uid_l):
            return hm, tm
    return 1.0, 1.0


# Hull sides thinner than this can't survive being turned toward the enemy.
_ANGLE_SIDE_MIN = 55.0


def _angle_ok(uid, hs, comp):
    """The baked should-angle eligibility flag: heavy tank, thick measured
    hull sides, no composite (angling died with APFSDS), and a front that is
    NOT already sloped/strong. The site applies its own BR/mode gates on top;
    this flag is the only place the flat-front judgement lives."""
    if uid not in _heavy_ids or comp:
        return False
    if hs < _ANGLE_SIDE_MIN:
        return False
    uid_l = uid.lower()
    return not any(rx.search(uid_l) for rx in _NO_ANGLE_RE)


# Ground-truth armor values, checked after every build and FATAL on mismatch.
# These are famous, hand-verified plates that the game has kept stable for
# years — if extraction ever misfiles them again (the Churchill's
# `hull_turret_rha` container sent its 95mm sides to the turret), the build
# aborts instead of shipping silently-wrong angling advice. Ranges, not exact
# values, so a minor Gaijin rebalance doesn't false-alarm.
_CANON_HS = {
    # id: (min_mm, max_mm, should_angle) — thickest hull-side plate `hs` range
    # plus the expected baked angling verdict. The verdict column IS the
    # feature's spec: poster-children anglers must stay 1, famous
    # sloped/thin/modern cases must stay 0.
    "germ_pzkpfw_VI_ausf_e_tiger":     (60, 100, 1),   # Tiger I ~80 — angle
    "germ_pzkpfw_VI_ausf_b_tiger_IIh": (60, 100, 1),   # Tiger II ~80 — angle
    "uk_a_22f_mk_7_churchill_1944":    (80, 110, 1),   # Churchill VII ~95 — angle
    "uk_a_22_mk_1_churchill_1941":     (55, 100, 1),   # Churchill I ~63.5 — angle
    "ussr_kv_1_zis_5":                 (60, 90, 1),    # KV-1 ~75 — angle
    "germ_pzkpfw_V_ausf_d_panther":    (30, 54, 0),    # Panther ~40 — thin sides
    "ussr_is_2_1944":                  (80, 100, 0),   # IS-2 ~90 — sloped front
    "ussr_t_34_85_zis_53":             (35, 54, 0),    # T-34-85 ~45 — thin+sloped
    "us_m4a3e2_sherman_jumbo":         (60, 90, 0),    # Jumbo — 47° glacis
    "us_t32":                          (60, 90, 0),    # T32 — sloped glacis
    "ussr_t_80u":                      (60, 100, 0),   # T-80U — composite/top tier
}

# Ground-truth EFF ranges, same fatal-on-mismatch idea as _CANON_HS but for the
# ranking score: the M56 Scorpion is the poster child for gun-mount artifact
# plates (228mm "turret_04" recoil block on a ~5mm vehicle) — if its eff ever
# climbs back into real-armor territory, the artifact filter has regressed.
_CANON_EFF = {
    # id: (min_eff, max_eff) — post-slope-correction expected ranges, anchored
    # to well-documented effective frontal protection.
    "us_m56_scorpion":                 (0, 60),     # ~unarmored TD
    "us_t26e5":                        (150, 260),  # 152mm @ 46° hull
    "germ_pzkpfw_VI_ausf_e_tiger":     (80, 150),   # Tiger I ~100mm flat
    "ussr_is_2_1944":                  (170, 260),  # 120mm @ 60° glacis ≈ 240
    "germ_pzkpfw_VI_ausf_b_tiger_IIh": (200, 270),  # 150mm @ 50° glacis ≈ 233
    "ussr_is_3":                       (280, 420),  # pike nose / 250 cast turret
    "us_t34":                          (260, 360),  # flat ~280mm mantlet
    "jp_type_90":                      (300, 550),  # unquoted; LoS "steel" must route to comp
    "germ_leopard_2a7v":               (380, 560),  # comp arrays + spall liner
}


def _guard_canon_hs(armor):
    """Abort the build if any canonical hull-side value or baked angling
    verdict disagrees with ground truth — wrong data here turns into wrong
    (possibly fatal) angling advice on the site, so fail loud here and on
    the VPS cron rather than ship it."""
    errors = []
    for uid, (lo, hi, want_ang) in _CANON_HS.items():
        row = armor.get(uid)
        if row is None:
            errors.append(f"{uid}: missing from armor build entirely")
            continue
        hs = row.get("hs", 0)
        if not (lo <= hs <= hi):
            errors.append(f"{uid}: hs={hs} outside known-truth range [{lo}, {hi}]")
        if row.get("ang", 0) != want_ang:
            errors.append(f"{uid}: ang={row.get('ang')} expected {want_ang}")
    for uid, (lo, hi) in _CANON_EFF.items():
        row = armor.get(uid)
        if row is None:
            errors.append(f"{uid}: missing from armor build entirely")
            continue
        eff = row.get("eff", 0)
        if not (lo <= eff <= hi):
            errors.append(f"{uid}: eff={eff} outside known-truth range [{lo}, {hi}]")
    if errors:
        raise SystemExit(
            "FATAL: canonical angling-armor check failed — refusing to write "
            "armor.json:\n  " + "\n  ".join(errors)
        )


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


def _hitpower_retention(bullet, distance_m=1000.0, default=0.9):
    hp = bullet.get("hitpower") or bullet.get("hitPower") or {}
    if not isinstance(hp, dict):
        return default
    exact = hp.get(f"HitPower{int(distance_m)}m")
    if isinstance(exact, list) and exact and isinstance(exact[0], (int, float)):
        return float(exact[0])
    return default


def _shell_pen_1000m(bullet, speed, cal_m):
    """1000m RHA pen (mm) and provenance for one AP shell.

    Returns (pen_mm, source) where source is:
      "table" — ArmorPower* from the shell file (game-authored, preferred)
      "est"   — physics estimate only when no table exists (LO / DeMarre)
      None    — cannot determine

    Prefer tables so display stays trustworthy; fall back to estimates so
    modern long-rod shells without ArmorPower still rank sensibly. The UI
    labels estimates so they are never confused with exact client values.
    """
    ap = bullet.get("armorpower") or bullet.get("armorPower")
    table = _armor_power_at(ap, 1000.0)
    if table is not None and table > 10:
        # Ignore near-zero HEAT junk tables (e.g. ArmorPower0m: [5.0, 10.0]).
        return round(table), "table"

    dmg = bullet.get("damage", {})
    kin = dmg.get("kinetic", {}) if isinstance(dmg, dict) else {}
    lo_len = kin.get("lanzOdermattWorkingLength")
    lo_density = kin.get("lanzOdermattDensity")

    # Long-rod APFSDS: calibrated LO-style estimate (not client display pen).
    if isinstance(lo_len, (int, float)) and isinstance(lo_density, (int, float)) and lo_len > 0:
        density_ratio = max(lo_density, 1.0) / 7850.0
        pen_0m = lo_len * (density_ratio ** 0.5) * 0.55
        retention = _hitpower_retention(bullet, 1000.0, 0.9)
        return round(pen_0m * (0.85 + 0.15 * retention)), "est"

    # Sabot rounds (APDS/APFSDS) without LO data: no estimate. Their pen physics
    # isn't DeMarre-shaped — running them through the full-caliber formula gave
    # wild values (a 57mm autocannon dart is not a solid shot).
    if "apds" in str(bullet.get("bulletType", "")).lower():
        return None, None

    # Full-caliber solid shot: DeMarre, calibrated to 88mm KwK36 ≈ 203mm @ 0m.
    # Proper DeMarre divides by caliber^1.07. (An earlier version multiplied by
    # caliber^0.07 instead, which inflated big guns ~50% — the IS-7's 130mm came
    # out at 517mm vs its real ~300 — and starved small calibers.)
    if speed and isinstance(bullet.get("mass"), (int, float)) and cal_m:
        mass = bullet["mass"]
        cal_mm = cal_m * 1000
        pen_0m = 0.3476 * (mass ** 0.71) * (speed ** 1.43) / (cal_mm ** 1.07)
        retention = _hitpower_retention(bullet, 1000.0, 0.9)
        return round(pen_0m * (retention ** 1.43)), "est"
    return None, None


def _gun_stats_from_model(model, mods):
    common = model.get("commonWeapons") or {}
    weps = common.get("Weapon") if isinstance(common, dict) else common
    weps = weps if isinstance(weps, list) else [weps]

    best_vel = 0
    # Track table vs estimate separately. If the vehicle has ANY ArmorPower
    # shell, use the best table value (never let a wild LO estimate override
    # a real client table). Only use estimates when no table exists at all.
    best_table = 0
    best_est = 0
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
                pen_1k, src = _shell_pen_1000m(b, v, cal)
                if pen_1k is None:
                    continue
                if src == "table":
                    best_table = max(best_table, pen_1k)
                elif src == "est":
                    best_est = max(best_est, pen_1k)
    if best_vel:
        result = {"v": round(best_vel), "c": round(bore_m * 1000, 1)}
        if best_table > 0:
            result["p"] = best_table
            result["ps"] = "table"
        elif best_est > 0:
            result["p"] = best_est
            result["ps"] = "est"
        return result
    return None  # missile / HE-only / autocannon vehicles have no AP round


_sensor_cache = {}
_sensor_lock = threading.Lock()


def _sensor_file(blk_ref):
    """Fetch and cache a sensor .blk (relative to aces.vromfs.bin_u)."""
    # Sensor paths in the model end with .blk; get_json_ci appends "x" to form
    # the .blkx URL the mirror serves, trying original then lowercase casing.
    with _sensor_lock:
        if blk_ref in _sensor_cache:
            return _sensor_cache[blk_ref]
    sf = get_json_ci(blk_ref)
    with _sensor_lock:
        _sensor_cache[blk_ref] = sf
    return sf


def _radar_stats_from_sensors(model):
    """Classify the SPAA's sensor suite from its sensor .blk files.
    Returns (radar, radar_search, radar_range) where:
      radar        — has a real radar sensor (not just an IR optic)
      radar_search — has a search radar (can find targets autonomously)
      radar_range  — max radar range in metres (0 if no radar)
    The game models IR optics and ranging radars under the same 'sensors' block,
    so checking model.get('sensors') alone is a false positive (Chaparral's
    'radar' is actually an IR optic). We fetch each sensor .blk and inspect its
    transivers: a 'search' key means search radar, 'track'/'radarTrack' is a
    tracking radar, 'visibilityType: infraRed' means it's NOT a radar at all."""
    sensors = model.get("sensors")
    if not isinstance(sensors, dict):
        return False, False, 0
    sensor_list = sensors.get("sensor")
    if not isinstance(sensor_list, list):
        sensor_list = [sensor_list] if sensor_list else []

    has_radar = False
    has_search = False
    max_range = 0.0
    for s in sensor_list:
        if not isinstance(s, dict) or not isinstance(s.get("blk"), str):
            continue
        sf = _sensor_file(s["blk"])
        if not isinstance(sf, dict):
            continue
        trans = sf.get("transivers")
        if not isinstance(trans, dict):
            continue
        for tname, tval in trans.items():
            if not isinstance(tval, dict):
                continue
            # IR optics are NOT radars — they're passive infrared trackers.
            if tval.get("visibilityType") == "infraRed":
                continue
            has_radar = True
            if tname == "search":
                has_search = True
            r = tval.get("range")
            if isinstance(r, (int, float)) and r > max_range:
                max_range = r
    return has_radar, has_search, round(max_range)


def _spaa_stats_from_model(model):
    """Anti-air firepower descriptor for an SPAA, all from its tankmodel:
      sam         — carries a surface-to-air missile launcher
      samRange    — max SAM engagement range (m) from AttackMaxRadius
      radar       — has a real radar sensor (not an IR optic)
      radarSearch — has a search radar (can find targets autonomously)
      radarRange  — max radar range (m)
      cal         — largest gun caliber (mm); missile-launcher diameters excluded
      gunAmmo     — total ammo capacity of the main AA gun(s)
    A radar SAM launcher and a towed quad-MG both tag as `type_spaa`; this is
    what tells them apart. SAM range and radar range/quality are the dominant
    anti-air metrics at top tier — a Pantsir (20km SAM, 45km search radar) is
    vastly superior to a Chaparral (10km SAM, IR optic only) despite both being
    'SAM + radar' under the old flat booleans."""
    common = model.get("commonWeapons") or {}
    weps = common.get("Weapon") if isinstance(common, dict) else common
    weps = weps if isinstance(weps, list) else [weps]

    sam = False
    sam_range = 0.0
    cal = 0.0
    gun_ammo = 0
    for w in weps:
        if not (isinstance(w, dict) and isinstance(w.get("blk"), str)):
            continue
        blk = w["blk"].lower()
        if "rocket_launcher" in blk or "missile" in blk:
            sam = True
            r = w.get("AttackMaxRadius")
            if isinstance(r, (int, float)) and r > sam_range:
                sam_range = r
            continue
        if "dummy" in blk or "machinegun" in blk:
            continue
        m = CAL_RE.search(blk.rsplit("/", 1)[-1])
        if not m:
            continue
        bore = float(m.group(1).replace("_", "."))
        if bore >= 85:
            sam = True
            r = w.get("AttackMaxRadius")
            if isinstance(r, (int, float)) and r > sam_range:
                sam_range = r
        else:
            cal = max(cal, bore)
            bullets = w.get("bullets")
            if isinstance(bullets, (int, float)):
                gun_ammo = max(gun_ammo, int(bullets))

    has_radar, radar_search, radar_range = _radar_stats_from_sensors(model)

    # A radar SPAA with no guns (cal=0) and no missile weapons is the FCS/radar
    # half of a split SAM system (e.g. NASAMS FCS, SAMP/T FCS, Iris-SLM FCS).
    if has_radar and not sam and cal == 0:
        sam = True
    return {
        "sam": int(sam), "samRange": round(sam_range),
        "radar": int(has_radar), "radarSearch": int(radar_search),
        "radarRange": radar_range,
        "cal": round(cal, 1), "gunAmmo": gun_ammo,
    }


# --- Armor extraction ------------------------------------------------------
#
# Reads the DamageParts block of the tankmodel — the same file we already
# fetch for mobility/guns/SPAA — and derives, per vehicle:
#
#   h   — raw steel on the hull front (thickest front plate, mm) — DISPLAY
#   t   — raw steel on the turret front (thickest front plate, mm) — DISPLAY
#   eff — effective-protection rating (mm-equivalent) — RANKING ONLY, never
#         shown as a mm figure in the UI
#   era/comp — presence flags (shown as chips on cards)
#
# Raw steel alone is misleading above ~BR 9: cast-turret tanks (T-64/T-72)
# report their solid cast cheeks as 250–400mm of "steel", while welded-turret
# tanks (T-80U/UD, Leo 2A6, M1A2) keep their protection in composite arrays
# and report only 45–80mm backing plates. Ranking on raw steel inverted the
# armor ordering at top tier (a T-72 TURMS outranked a T-80UD).
#
# eff is intentionally a *ranking score*, not a precise penetration barrier:
# War Thunder's real armor model is volumetric and angle-dependent, so a single
# number can never be "correct". What eff must do is order tanks sensibly:
# T-80UD > T-72 TURMS, Leo 2A6 ≈ 2A4, Maus still king of its bracket.
#
# How eff is built (all from DamageParts):
#   • Plates are assigned to a hull path or a turret path. A plate counts as
#     frontal if its own key says "front", OR its *container* does (the T-80UD
#     keeps its 250mm turret backing as `turret_01_back_dm` inside the
#     `turret_front_composite_armor` container — plate-name-only scanning
#     missed it entirely). Roof/floor/flank plates (top/bottom/side) never
#     count; "back" plates only count inside a front-named container.
#   • Steel per path: thickest frontal plate × genericArmorQuality when the
#     game assigns one (the T-64 glacis is 60mm × 1.85 — that multiplier IS
#     the laminate). Display h/t stay the raw plate mm (factual).
#   • Composite per path: the single best composite item, thickness ×
#     genericArmorQuality, capped at +350mm. Max (not sum) because arrays
#     appear once per turret cheek — summing double-counts left+right.
#     Composite = key matches composite_armor_* with a non-steel armorClass or
#     an explicit quality, or any plate inside a container whose armorClass is
#     non-steel (Leo 2A5 NERA wedge).
#   • eff = max(hull path, turret path) + ERA bonus. ERA: +15mm-equivalent per
#     tile, capped at +80 (coverage matters; tile count saturates fast, and the
#     files don't distinguish Kontakt-1 from Kontakt-5).
#
# What counts as "composite" vs "raw steel"? The game's `armorClass` field.
# Standard steel classes (RHA, CHA, aluminium, titanium, structural steel...)
# are raw steel. Anything else — NERA, textolite, spaced_armor, special_armor,
# per-tank composite classes — is a composite/special array. Works game-wide
# without hardcoding tank names.

# Composite array blocks: keys matching composite_armor_* anywhere in DamageParts.
_COMP_RE = re.compile(r"composite_armor", re.I)
# ERA containers: keys starting with era_ (era_hull_front, era_turret_front, etc.)
# and Soviet-style relic_era_* and ex_armor_era_* blocks.
_ERA_RE = re.compile(r"^(era_|relict_era|ex_armor_era)", re.I)

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


# Caps for the eff rating (see the design comment above).
_COMP_CAP = 400.0   # a single huge array is LoS thickness, not that much RHA
_ERA_PER_TILE = 15.0
_ERA_CAP = 80.0
# Unquoted vehicles (Japan's whole tree ships no Shop armor triples) get no
# quote-based artifact cap. A "steel" plate above this on such a vehicle is a
# composite module modeled with LoS thickness and no armorClass (Type 90:
# 450/585mm "steel") — it is routed to the composite path (which _COMP_CAP
# bounds) instead of dominating the steel path.
_NO_QUOTE_STEEL_MAX = 350.0
# Spall liners (spall_liner_NN_dm blocks, top-tier survivability upgrade)
# don't stop the penetrator but keep the crew alive when something gets
# through — worth a flat ranking bonus on both paths.
_SPALL_BONUS = 35.0


def _armor_stats_from_model(model, quotes=None, slope=(1.0, 1.0)):
    """Armor descriptor for a tank, all from its DamageParts block:
      h/t   — thickest frontal steel plate on hull/turret (mm) — display
      eh/et — per-path effective-protection ratings (slope-corrected steel ×
              quality + composite array + ERA bonus) — ranking only. Both
              paths ship so the client can punish a weak hull (Challenger 2:
              monster turret, 70mm hull) instead of only seeing the best path.
      eff   — max(eh, et), kept for the Armor playstyle percentile
      era/comp — presence flags
    `quotes` is this vehicle's shop-quoted armor ({"hull","turret","side"} mm
    or None each) — steel plates far above the quote are gun-mount artifacts,
    not armor, and are skipped (see _steel_cap). `slope` is the curated
    (hull_mult, turret_mult) from _SLOPE_EFF, applied to steel paths only.
    Returns None only if the model has no DamageParts at all."""
    dp = model.get("DamageParts")
    if not isinstance(dp, dict):
        return None
    quotes = quotes or {}
    cap = {
        "hull": _steel_cap(quotes.get("hull")),
        "turret": _steel_cap(quotes.get("turret")),
        "side": _steel_cap(quotes.get("side")),
    }

    steel_raw = {"hull": 0.0, "turret": 0.0}   # display mm (plate as modeled)
    steel_eff = {"hull": 0.0, "turret": 0.0}   # × genericArmorQuality
    comp_eff = {"hull": 0.0, "turret": 0.0}    # best single composite item
    # Thickest real hull-side plate (mm). Feeds the client "should I angle?"
    # advisor: angling only pays when the flank you turn toward the enemy can
    # survive being shot. Turret sides, tracks/wheels and thin skirts/fenders
    # (<20mm) are excluded — they aren't the main flank you present when
    # sidescraping.
    hull_side = 0.0

    def add_comp(target, t, gq):
        q = gq if isinstance(gq, (int, float)) and gq > 0 else 1.0
        comp_eff[target] = max(comp_eff[target], t * q)

    for key, blk in _iter_damage_parts(dp):
        kl = key.lower()
        cont_front = "front" in kl
        # Hull/turret from the CONTAINER name is only a fallback — containers
        # have free-form names ("hull", "inner_armor", "hull_turret_rha" on the
        # Churchill…) while PLATE names are rigidly standardized across the
        # whole datamine (body_*/superstructure_* = hull, turret_*/mask =
        # turret). Substring tests on the container previously misfiled every
        # plate of a mixed-name container: "turret" in "hull_turret_rha" sent
        # the Churchill's 95mm hull sides to the turret and zeroed its `hs`.
        cont_has_turret = "turret" in kl or "mask" in kl
        cont_has_hull = "hull" in kl or "body" in kl or "superstructure" in kl
        cont_turret = cont_has_turret and not cont_has_hull

        def plate_target(k2l):
            """hull|turret for one plate, by its OWN standardized name first."""
            if "turret" in k2l or "mask" in k2l:
                return "turret"
            if "body" in k2l or "superstructure" in k2l or "hull" in k2l:
                return "hull"
            return "turret" if cont_turret else "hull"

        cont_composite = not _is_steel_class(blk.get("armorClass"))
        # Composite containers can carry their own thickness (rare).
        if _COMP_RE.search(key):
            ct = blk.get("armorThickness")
            if isinstance(ct, (int, float)) and ct > 10:
                add_comp("turret" if cont_turret else "hull",
                         ct, blk.get("genericArmorQuality"))
        for k2, v2 in blk.items():
            if not isinstance(v2, dict):
                continue
            t = v2.get("armorThickness")
            if not isinstance(t, (int, float)) or t <= 0:
                continue
            k2l = k2.lower()
            # Capture the main hull-side plate before discarding flank plates
            # (front-armor scan skips them). Turret sides don't count — the
            # angling advice is about the hull flank you expose when turning.
            # Classified by the plate's own name so container naming quirks
            # can't hide a flank (the Churchill bug).
            if ("side" in k2l and plate_target(k2l) == "hull"
                    and "track" not in k2l and "wheel" not in k2l
                    and "skirt" not in k2l and t >= 20
                    and (cap["side"] is None or t <= cap["side"])):
                hull_side = max(hull_side, t)
            # Roof/floor/flank plates never count toward frontal protection.
            if "top" in k2l or "bottom" in k2l or "side" in k2l:
                continue
            is_comp_key = bool(_COMP_RE.search(k2))
            # Frontal if the plate says so, its container says so, or it's a
            # composite_armor_* item (arrays rarely carry a "front" token).
            if not ("front" in k2l or cont_front or is_comp_key):
                continue
            # A "back" plate only counts inside a front-named container (it's
            # the backing plate of a frontal array, e.g. T-80UD's 250mm CHA).
            if "back" in k2l and not cont_front:
                continue
            target = plate_target(k2l)
            gq = v2.get("genericArmorQuality")
            plate_is_composite = (
                (is_comp_key and (not _is_steel_class(v2.get("armorClass"))
                                  or (isinstance(gq, (int, float)) and gq > 0)))
                or (cont_composite and not is_comp_key)
            )
            if plate_is_composite:
                add_comp(target, t, gq)
            else:
                # Steel plate far above the shop quote for this zone = internal
                # gun-mount artifact (M56's 228mm "turret"), not armor.
                if cap[target] is not None and t > cap[target]:
                    continue
                # No quote to check against: an oversized "steel" plate on an
                # unquoted vehicle is a composite module with LoS thickness
                # (Type 90) — count it as composite, not steel.
                if cap[target] is None and t > _NO_QUOTE_STEEL_MAX:
                    add_comp(target, t, gq)
                    continue
                steel_raw[target] = max(steel_raw[target], t)
                q = gq if isinstance(gq, (int, float)) and gq > 0 else 1.0
                steel_eff[target] = max(steel_eff[target], t * q)

    # Spall liners: spall_liner_NN_dm blocks (either level of DamageParts).
    n_spall = 0
    for key, blk in _iter_damage_parts(dp):
        if "spall" in key.lower():
            n_spall += 1
        for k2, v2 in blk.items():
            if isinstance(v2, dict) and "spall" in k2.lower():
                n_spall += 1

    # ERA tile count (Kontakt/Relikt/Blazer boxes).
    n_era = 0
    for key, blk in _iter_damage_parts(dp):
        if not _ERA_RE.search(key):
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

    has_comp = comp_eff["hull"] >= 1 or comp_eff["turret"] >= 1
    hm, tm = slope
    bonus = min(n_era * _ERA_PER_TILE, _ERA_CAP) + (_SPALL_BONUS if n_spall else 0)
    eh = steel_eff["hull"] * hm + min(comp_eff["hull"], _COMP_CAP) + bonus
    et = steel_eff["turret"] * tm + min(comp_eff["turret"], _COMP_CAP) + bonus
    return {
        "h": round(steel_raw["hull"], 1),
        "t": round(steel_raw["turret"], 1),
        "hs": round(hull_side, 1),
        "eh": round(eh),
        "et": round(et),
        "eff": round(max(eh, et)),
        "era": 1 if n_era > 0 else 0,
        "comp": 1 if has_comp else 0,
        "sl": 1 if n_spall else 0,
    }


def _has_autoloader(model):
    """True if any main weapon carries the game's own `autoLoader` flag.
    This is the authoritative signal (T-72's 2A46M has it, M1A1's M256
    doesn't) — reload speed alone can't tell a 5s human loader from a
    carousel."""
    common = model.get("commonWeapons") or {}
    weps = common.get("Weapon") if isinstance(common, dict) else common
    weps = weps if isinstance(weps, list) else [weps]
    for w in weps:
        if not (isinstance(w, dict) and isinstance(w.get("blk"), str)):
            continue
        if "machinegun" in w["blk"].lower():
            continue
        if w.get("autoLoader"):
            return True
    return False


def _stabilizer_planes(model):
    """Number of stabilizer planes: 0 (none), 1 (horizontal-only), 2 (2-plane).
    WWII tanks have no gunStabilizer block at all; modern tanks have
    hasHorizontal/hasVertical booleans. A 1-plane stabilizer (horizontal-only)
    can't fire accurately on the move in the vertical plane — a major
    disadvantage vs a 2-plane stabilizer."""
    common = model.get("commonWeapons") or {}
    weps = common.get("Weapon") if isinstance(common, dict) else common
    weps = weps if isinstance(weps, list) else [weps]
    has_h = False
    has_v = False
    for w in weps:
        if not (isinstance(w, dict) and isinstance(w.get("blk"), str)):
            continue
        if "machinegun" in w["blk"].lower():
            continue
        gs = w.get("gunStabilizer")
        if isinstance(gs, dict):
            if gs.get("hasHorizontal"):
                has_h = True
            if gs.get("hasVertical"):
                has_v = True
    if has_h and has_v:
        return 2
    if has_h or has_v:
        return 1
    return 0


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
    """Returns (thermal, nv, thermal_gen): thermal=True if the tank has thermal
    imaging, nv=True if it has any night vision (IR or thermal), thermal_gen is
    the best thermal generation tier (0=none, 1-3+ from the datamine `tier`
    field on the night_vision_system modification). WWII tanks have no
    nightVision data at all. Signals live in up to three places and a tank often
    has several at once (e.g. stock driverIr in the top-level `nightVision`
    block PLUS gunner/commander thermals in the night_vision_system
    modification), so we OR across ALL of them rather than returning at the first
    hit — otherwise a stock driver-IR device masks the far more important gunner
    thermal and the tank is reported as NV-only.

    Thermal generation (tier) is the game's own 1/2/3 grading of thermal image
    quality. Gen 1 is crude (low resolution, high noise); Gen 3 is the modern
    standard (high resolution, can see through smoke). A Gen 1 thermal scores
    far below a Gen 3 — collapsing both to a flat boolean was the same class of
    bug as the ATGM quality issue."""
    thermal = False
    nv = False
    thermal_gen = 0

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
            # Extract the thermal generation tier from the modification block.
            # The `tier` field is the game's own grading (1/2/3+). Take the best
            # tier across all thermal-bearing modifications.
            if "thermal" in _key_tokens(mod_name) or (
                isinstance(effects, dict) and isinstance(effects.get("nightVision"), dict)
                and any("thermal" in _key_tokens(k) for k in effects["nightVision"])
            ):
                tier = mod.get("tier")
                if isinstance(tier, (int, float)):
                    thermal_gen = max(thermal_gen, int(tier))
                elif isinstance(effects, dict) and isinstance(effects.get("nightVision"), dict):
                    # No tier field — infer generation from the best thermal
                    # optic's resolution. 800x600 = Gen 3, 500x300 = Gen 2,
                    # anything lower or unknown = Gen 1.
                    for k2, v2 in effects["nightVision"].items():
                        if not isinstance(v2, dict):
                            continue
                        if "thermal" not in _key_tokens(k2):
                            continue
                        res = v2.get("resolution")
                        if isinstance(res, list) and len(res) >= 2:
                            w, h = res[0], res[1]
                            if isinstance(w, (int, float)) and isinstance(h, (int, float)):
                                inferred = 1
                                if w >= 800 and h >= 600:
                                    inferred = 3
                                elif w >= 500 and h >= 300:
                                    inferred = 2
                                thermal_gen = max(thermal_gen, inferred)
                thermal = True
                nv = True
    # 3) hasNightVision flag on turret weapons — least specific (NV, not thermal).
    if not (thermal or nv):
        common = model.get("commonWeapons") or {}
        weps = common.get("Weapon") if isinstance(common, dict) else common
        weps = weps if isinstance(weps, list) else [weps]
        for w in weps:
            if not (isinstance(w, dict) and isinstance(w.get("blk"), str)):
                continue
            turret = w.get("turret", {})
            if isinstance(turret, dict) and turret.get("hasNightVision"):
                nv = True
                break
    return thermal, nv, thermal_gen


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
    armor = _armor_stats_from_model(model, _shop_armor.get(unit_id), _slope_mults(unit_id))
    if armor is not None:
        armor["stab"] = _stabilizer_planes(model)
        thermal, nv, thermal_gen = _night_vision(model)
        armor["thermal"] = 1 if thermal else 0
        armor["thermalGen"] = thermal_gen if thermal else 0
        armor["nv"] = 1 if nv else 0
        armor["rev"] = _reverse_ratio(model)
        armor["al"] = 1 if _has_autoloader(model) else 0
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
    _heavy_ids.update(
        k for k in tanks if unittags.get(k, {}).get("tags", {}).get("type_heavy_tank")
    )
    # Shop-quoted armor triples — the artifact-plate sanity anchor (_steel_cap).
    for k in tanks:
        shop = unittags.get(k, {}).get("Shop", {})
        hull = shop.get("armorThicknessHull")
        tur = shop.get("armorThicknessTurret")
        _shop_armor[k] = {
            "hull": hull[0] if isinstance(hull, list) and hull else None,
            "turret": tur[0] if isinstance(tur, list) and tur else None,
            "side": hull[1] if isinstance(hull, list) and len(hull) > 1 else None,
        }
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
                # Bake the should-angle eligibility flag (see _angle_ok) so
                # the site never needs its own flat-front knowledge list.
                ar["ang"] = 1 if _angle_ok(uid, ar.get("hs", 0), ar.get("comp")) else 0
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
    al_n = sum(1 for v in armor.values() if v.get("al"))
    eff_n = sum(1 for v in armor.values() if v.get("eff"))
    hs_n = sum(1 for v in armor.values() if v.get("hs"))
    _guard_field_regression("armor/thermal", "thermal", thermal_n, ARMOR_OUT)
    _guard_field_regression("armor/nv", "nv", nv_n, ARMOR_OUT)
    _guard_field_regression("armor/stab", "stab", stab_n, ARMOR_OUT)
    _guard_field_regression("armor/al", "al", al_n, ARMOR_OUT)
    _guard_field_regression("armor/eff", "eff", eff_n, ARMOR_OUT)
    _guard_field_regression("armor/hs", "hs", hs_n, ARMOR_OUT)
    ang_n = sum(1 for v in armor.values() if v.get("ang"))
    _guard_field_regression("armor/ang", "ang", ang_n, ARMOR_OUT)
    sl_n = sum(1 for v in armor.values() if v.get("sl"))
    _guard_field_regression("armor/sl", "sl", sl_n, ARMOR_OUT)
    # Known-truth plate check — FATAL on mismatch (see _CANON_HS).
    _guard_canon_hs(armor)
    # Angling-data audit: every heavy tank with no measured hull side is a
    # potential Churchill-class extraction gap. Listed loudly (not fatal —
    # some heavies legitimately model armor oddly) so gaps can't hide.
    heavies_no_hs = sorted(
        uid for uid in _heavy_ids if not armor.get(uid, {}).get("hs")
    )
    if heavies_no_hs:
        print(f"AUDIT: {len(heavies_no_hs)} heavy tanks with no hull-side plate "
              f"(angling advice falls back to safe 'don't angle'):")
        for uid in heavies_no_hs:
            print(f"    {uid}")
    # The complete roster of tanks eligible for the "Angle" verdict, printed
    # every build (incl. the nightly cron log). This set is small and finite —
    # any surprise entrant after a game patch shows up here for review before
    # anyone trusts its advice.
    ang_ids = sorted(uid for uid, v in armor.items() if v.get("ang"))
    print(f"AUDIT: {len(ang_ids)} tanks carry the ANGLE verdict:")
    for uid in ang_ids:
        print(f"    {uid}  (hs={armor[uid].get('hs')})")

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
    table_n = sum(1 for v in guns.values() if isinstance(v, dict) and v.get("ps") == "table")
    est_n = sum(1 for v in guns.values() if isinstance(v, dict) and v.get("ps") == "est")
    print(f"Wrote {len(armor)} entries to {os.path.basename(ARMOR_OUT)} "
          f"({era_n} with ERA, {comp_n} with composite, {al_n} autoloaders; "
          f"{thermal_n} thermal, {nv_n} night-vision, {stab_n} stabilized)")
    print(f"Gun pen: {pen_n}/{len(guns)} ({table_n} ArmorPower table, {est_n} estimated)")
    if gun_miss:
        print("No-AP vehicles:", ", ".join(gun_miss[:15]) + (" …" if len(gun_miss) > 15 else ""))


if __name__ == "__main__":
    main()
