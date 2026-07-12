# WT Lineup Creator

A War Thunder lineup generator that pulls **live battle ratings and vehicle data** straight from the community datamine of the actual game files, so it's always up to date with the latest patch — no manual data entry.

## Features

- **Live data** — fetches vehicle data from the [gszabi99/War-Thunder-Datamine](https://github.com/gszabi99/War-Thunder-Datamine) mirror of the game files (BRs, vehicle classes, armor values, premium/event status). On every visit the app compares the mirror's latest commit against its local cache and re-downloads automatically whenever Gaijin changed anything (BR adjustments, new vehicles, patches) — the mirror updates within hours of every game change.
- **Per-mode BRs** — Arcade, Realistic and Simulator battle ratings are all different; the generator uses the right one for your selected mode.
- **Lineup generation** based on:
  - Number of crew slots
  - Whether you want aircraft, helicopters and/or SPAA in the lineup
  - Playstyle preference: Balanced, Armor (brawler), Speed (flanker) or Sniper (tank destroyer focus)
  - Tech-tree only, or including premium / squadron / event vehicles
- **Real stats** — playstyle scoring uses actual hull/turret armor thickness and vehicle class tags from the game files.

## Live

Deployed at **https://wt.shadowdog.cat** (VPS + Caddy, static files in `/var/www/wt`). Redeploy with `./deploy.sh`.

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

Battle rating is derived from the game's economic rank: `BR = economicRank / 3 + 1.0`.

Not affiliated with Gaijin Entertainment.
