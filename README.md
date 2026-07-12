# WT Lineup Creator

A War Thunder lineup generator that pulls **live battle ratings and vehicle data** straight from the community datamine of the actual game files, so it's always up to date with the latest patch — no manual data entry.

## Features

- **Live data** — fetches vehicle data from the [gszabi99/War-Thunder-Datamine](https://github.com/gszabi99/War-Thunder-Datamine) mirror of the game files (BRs, vehicle classes, armor values, premium/event status). On every visit the app compares the mirror's latest commit against its local cache and re-downloads automatically whenever Gaijin changed anything (BR adjustments, new vehicles, patches) — the mirror updates within hours of every game change.
- **Per-mode BRs** — Arcade, Realistic and Simulator battle ratings are all different; the generator uses the right one for your selected mode.
- **Lineup generation** based on:
  - Number of crew slots
  - SPAA and/or helicopters
  - **Aircraft role**: Fighter (air superiority), Ground pounder (CAS), Balanced (both), or none
  - Playstyle preference: Balanced, Armor (brawler), Speed (flanker) or Sniper (flat-shooting guns)
  - Tech-tree only, or including premium / squadron / event vehicles
- **Real stats drive the scoring:**
  - **Armor** — actual frontal hull/turret plate thickness from the game files.
  - **Speed** — real **horsepower-per-ton**, extracted from each tank's physics file (see `tools/build_mobility.py`).
  - **Sniper** — real **muzzle velocity** (of the fastest AP round the vehicle can actually equip) + **bore caliber**. Penetration isn't stored in the game files (it's computed at runtime via DeMarre), but velocity — the thing that gives a flat trajectory and minimal lead — is, and it's what defines a good long-range gun.
  - **Fighters** — ranked by sustained **turn time**.
  - **Ground pounders** — ranked by **bomb/rocket payload**.
- **Per-slot swap** — every slot has a ⟳ Swap button that cycles to the next-best vehicle of the same role (respecting your playstyle). Handy for swapping a premium you don't own for one you do.
- **Lineup health check** — a fact-based "is this good enough?" panel scores every generated lineup against War Thunder's matchmaker rules. It reports the BR you'll actually queue at (your highest vehicle), how many vehicles stay competitive in a full +1.0 uptier (within 0.3 of your top BR), how many are downtier-only ballast (0.7+ below), and whether you have SPAA/air cover — then gives a Strong / Solid / Thin verdict. A 5.7 in a 6.0 lineup is correctly treated as competitive (only 0.3 down), not a problem.

## Live

Deployed at **https://wt.shadowdog.cat** (VPS + Caddy, static files in `/var/www/wt`). Redeploy with `./deploy.sh`.

The `wt.shadowdog.cat` Caddy site sends `Cache-Control: no-cache` (with ETags), so browsers revalidate on every load — deploys reach users immediately (cheap `304`s when nothing changed) instead of getting stuck on a stale cached copy.

### Daily mobility refresh (VPS cron)

`data/mobility.json` (tank hp/ton) and `data/gunstats.json` (gun velocity/caliber)
are regenerated automatically once a day on the server so the live site tracks
engine/weight/gun changes without a manual redeploy. A full rebuild takes
**~20–30 seconds** (1,230 tanks, model + gun files fetched concurrently, weapon
files cached). Setup, reproducible from this repo:

- `tools/build_mobility.py` honors a `MOBILITY_OUT` env var, so it can write
  straight into the web root (gunstats lands next to it); it writes atomically
  and refuses to overwrite a good file if a network hiccup resolves too few tanks.
- `tools/vps-mobility-refresh.sh` → installed at `/opt/wt-mobility/refresh.sh`
  (next to a copy of `build_mobility.py`), sets `MOBILITY_OUT=/var/www/wt/data/mobility.json`
  and logs to `/var/log/wt-mobility.log`.
- `tools/vps-mobility.cron` → installed at `/etc/cron.d/wt-mobility`, runs it
  daily at 04:17 UTC.

## Running it locally

It's a fully static web app — no build step, no backend. Serve the folder with any static file server, e.g.:

```
python -m http.server 8123
```

then open http://localhost:8123. (Opening `index.html` directly via `file://` won't work because the app fetches data over HTTPS.)

## How the data works

| File | What it provides |
|---|---|
| `char.vromfs.bin_u/config/wpcost.blkx` | Economic ranks (→ battle ratings), nation, rank, premium/gift/event flags |
| `char.vromfs.bin_u/config/unittags.blkx` | Vehicle type + class tags (`type_medium_tank`, `type_spaa`, …), armor thickness |
| `lang.vromfs.bin_u/lang/units.csv` | Localized display names |
| `aces.vromfs.bin_u/gamedata/units/tankmodels/<id>.blkx` | Engine hp + mass → hp/ton; main-cannon reference → gun stats (fetched offline by `tools/build_mobility.py`) |
| `aces.vromfs.bin_u/gamedata/weapons/groundmodels_weapons/<gun>.blkx` | Per-shell muzzle velocity + caliber (fetched offline; restricted to the shells a vehicle can actually equip via its `wpcost` modifications) |

Battle rating is derived from the game's economic rank: `BR = economicRank / 3 + 1.0`.

Most data is fetched live in the browser. The exceptions are tank **hp/ton** and
**gun stats**: the economy file caps its `speed` field and stores no shell
ballistics, and the real data lives in ~1,200 separate model/weapon files — too
many to fetch at runtime. So `tools/build_mobility.py` pulls them once and writes
two small tables that ship with the app:

- `data/mobility.json` — `id → hp/ton`
- `data/gunstats.json` — `id → { v: muzzle velocity m/s, c: bore mm }` (fastest AP round the vehicle can equip)

These change rarely, so re-run only after a major patch (the [daily VPS cron](#daily-mobility-refresh-vps-cron) already does this automatically):

```
python tools/build_mobility.py
```

Not affiliated with Gaijin Entertainment.
