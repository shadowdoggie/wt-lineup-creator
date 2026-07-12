"use strict";

/**
 * Data layer: downloads vehicle data from the War Thunder datamine mirror
 * (gszabi99/War-Thunder-Datamine), which tracks the live game files, and
 * condenses it into a compact unit list cached in localStorage.
 */
const WT_DATA = (() => {
  const REPO = "gszabi99/War-Thunder-Datamine";
  const COMMIT_API = `https://api.github.com/repos/${REPO}/commits/master`;
  const PATHS = {
    wpcost:   "char.vromfs.bin_u/config/wpcost.blkx",
    unittags: "char.vromfs.bin_u/config/unittags.blkx",
    names:    "lang.vromfs.bin_u/lang/units.csv",
  };
  const CACHE_KEY = "wtlc_data_v2";
  // Real tank stats precomputed by tools/build_mobility.py and shipped with the
  // app: hp/ton (mobility) and best-AP-shell muzzle velocity + bore caliber
  // (gun). Served from our own origin, merged at load time so refreshing them
  // doesn't require busting the vehicle cache.
  const MOBILITY_URL = "data/mobility.json";
  const GUNSTATS_URL = "data/gunstats.json";
  // Fallback re-download interval, used only when the commit check fails
  // (offline, or GitHub API rate limit of 60 req/h per IP exhausted).
  const MAX_AGE_MS = 24 * 3600 * 1000;

  const NATIONS = [
    ["usa", "🇺🇸 USA"],
    ["germany", "🇩🇪 Germany"],
    ["ussr", "🇷🇺 USSR"],
    ["britain", "🇬🇧 Great Britain"],
    ["japan", "🇯🇵 Japan"],
    ["china", "🇨🇳 China"],
    ["italy", "🇮🇹 Italy"],
    ["france", "🇫🇷 France"],
    ["sweden", "🇸🇪 Sweden"],
    ["israel", "🇮🇱 Israel"],
  ];
  const NATION_IDS = new Set(NATIONS.map(n => n[0]));

  // The game stores BR as an integer "economic rank": BR = rank / 3 + 1.0
  function econToBR(er) {
    return Math.round((er / 3 + 1) * 10) / 10;
  }

  // Order matters: more specific classes first (a vehicle can carry several tags).
  const GROUND_CLASSES = [
    ["type_spaa", "spaa"],
    ["type_tank_destroyer", "td"],
    ["type_missile_tank", "td"],
    ["type_heavy_tank", "heavy"],
    ["type_light_tank", "light"],
    ["type_medium_tank", "medium"],
  ];
  const AIR_CLASSES = [
    ["type_fighter", "fighter"],
    ["type_assault", "attacker"],
    ["type_dive_bomber", "attacker"],
    ["type_bomber", "bomber"],
  ];

  // Best ground-attack loadout across a plane's weapon presets: how many
  // bombs/rockets/torpedoes it can carry. A rough but effective CAS signal
  // without opening every individual weapon file.
  function payloadCount(w) {
    let best = 0;
    for (const preset of Object.values(w.weapons || {})) {
      let n = 0;
      for (const [name, count] of Object.entries(preset.sum_weapons || {})) {
        if (/bomb|rocket|torpedo/i.test(name)) n += count;
      }
      if (n > best) best = n;
    }
    return best;
  }

  function classify(type, tags) {
    if (type === "tank") {
      for (const [tag, cls] of GROUND_CLASSES) if (tags[tag]) return cls;
      return null;
    }
    if (type === "aircraft") {
      if (tags.type_strike_ucav) return null; // drones aren't crewable lineup vehicles
      for (const [tag, cls] of AIR_CLASSES) if (tags[tag]) return cls;
      return "attacker";
    }
    if (type === "helicopter") return "heli";
    return null;
  }

  // BR for a mode; aircraft/helis have a separate (often different) BR when
  // brought into ground battles — prefer that one, since this is a lineup tool.
  function brFor(w, tankKey, airKey) {
    let er = tankKey ? w[tankKey] : undefined;
    if (typeof er !== "number") er = w[airKey];
    if (typeof er !== "number") er = w.economicRank;
    return typeof er === "number" ? econToBR(er) : null;
  }

  function prettifyId(id) {
    return id.replace(/^(us|germ|ussr|uk|jp|cn|it|fr|sw|il)_/, "").replace(/_/g, " ");
  }

  // units.csv: `"<id>";"<English>";...` — we only need the `<unit>_shop` rows.
  function parseNames(csv) {
    const names = {};
    const re = /^"((?:[^"]|"")+_shop)";"((?:[^"]|"")*)"/;
    for (const line of csv.split("\n")) {
      const m = re.exec(line);
      if (m) names[m[1].slice(0, -5)] = m[2].replace(/""/g, '"');
    }
    return names;
  }

  function buildUnits(wpcost, unittags, names) {
    const units = [];
    for (const [id, w] of Object.entries(wpcost)) {
      if (!w || typeof w !== "object") continue;
      const t = unittags[id];
      if (!t) continue;
      const type = t.type;
      if (type !== "tank" && type !== "aircraft" && type !== "helicopter") continue;
      const tags = t.tags || {};
      if (tags.hideBrForVehicle) continue; // AI / event-only oddities
      const country = (w.country || "").replace("country_", "");
      if (!NATION_IDS.has(country)) continue;
      const cls = classify(type, tags);
      if (!cls) continue;

      const air = type !== "tank";
      const shop = t.Shop || {};
      units.push({
        id,
        name: names[id] || prettifyId(id),
        country,
        type,
        cls,
        rank: w.rank || 1,
        br: {
          arcade:    brFor(w, null, "economicRankArcade"),
          realistic: brFor(w, air ? "economicRankTankHistorical" : null, "economicRankHistorical"),
          simulator: brFor(w, air ? "economicRankTankSimulation" : null, "economicRankSimulation"),
        },
        premium: !!w.costGold,
        squadron: w.researchType === "clanVehicle",
        gift: !!(w.gift || w.event || w.showOnlyWhenBought),
        armorHull: Array.isArray(shop.armorThicknessHull) ? shop.armorThicknessHull[0] : null,
        armorTurret: Array.isArray(shop.armorThicknessTurret) ? shop.armorThicknessTurret[0] : null,
        hpPerTon: null, // filled from mobility.json after load
        gunVel: null,   // filled from gunstats.json after load (best AP shell m/s)
        gunCal: null,   // bore caliber (mm)
        // Aircraft-only: lower turnTime = better dogfighter; payload = CAS punch.
        turnTime: air && typeof shop.turnTime === "number" ? shop.turnTime : null,
        payload: type === "aircraft" ? payloadCount(w) : 0,
      });
    }
    return units;
  }

  function readCache() {
    try {
      const raw = localStorage.getItem(CACHE_KEY);
      if (!raw) return null;
      const c = JSON.parse(raw);
      return c && Array.isArray(c.units) && c.units.length ? c : null;
    } catch {
      return null;
    }
  }

  function writeCache(cache) {
    try {
      localStorage.setItem(CACHE_KEY, JSON.stringify(cache));
    } catch {
      // Cache is an optimization only; a full quota is fine to ignore.
    }
  }

  async function fetchSource(key, ref, onStep) {
    const res = await fetch(`https://raw.githubusercontent.com/${REPO}/${ref}/${PATHS[key]}`);
    if (!res.ok) throw new Error(`Failed to download ${key} (HTTP ${res.status})`);
    const body = key === "names" ? await res.text() : await res.json();
    onStep?.(key);
    return body;
  }

  // Latest datamine commit = current version of the game files. The mirror
  // updates within hours of every patch/BR change, so comparing this against
  // the cached commit tells us whether Gaijin changed anything.
  async function fetchCommitInfo() {
    const res = await fetch(COMMIT_API, { headers: { Accept: "application/vnd.github+json" } });
    if (!res.ok) throw new Error(`GitHub API HTTP ${res.status}`);
    const c = await res.json();
    return { sha: c.sha, date: c.commit?.committer?.date || null };
  }

  // Merge precomputed hp/ton and gun stats onto tank units. Done every load
  // (not baked into the cache) so shipping new data files takes effect
  // immediately. Each file is optional and fails soft to a fallback.
  async function attachStats(units) {
    const tanks = units.filter(u => u.type === "tank");
    const [mob, guns] = await Promise.all([
      fetchJsonSoft(MOBILITY_URL),
      fetchJsonSoft(GUNSTATS_URL),
    ]);
    // No mobility file: Speed playstyle falls back to the armor-inverse proxy.
    if (mob) for (const u of tanks) u.hpPerTon = mob[u.id] ?? null;
    // No gun file: Sniper falls back to its class-based proxy.
    if (guns) for (const u of tanks) {
      const g = guns[u.id];
      if (g) { u.gunVel = g.v ?? null; u.gunCal = g.c ?? null; }
    }
  }

  async function fetchJsonSoft(url) {
    try {
      const res = await fetch(url);
      return res.ok ? await res.json() : null;
    } catch {
      return null;
    }
  }

  /**
   * Loads unit data. Re-downloads whenever the datamine mirror has a new
   * commit (i.e. the game files changed); otherwise serves the local cache.
   * `onStep(key)` fires per finished file.
   */
  async function load({ force = false, onStep } = {}) {
    const cache = readCache();
    let head = null;
    try {
      head = await fetchCommitInfo();
    } catch {
      // Commit check unavailable — fall back to time-based caching below.
    }

    if (cache && !force) {
      if (head && cache.sha === head.sha) {
        await attachStats(cache.units);
        return { ...cache, fromCache: true, upToDate: true };
      }
      if (!head && Date.now() - cache.fetchedAt < MAX_AGE_MS) {
        await attachStats(cache.units);
        return { ...cache, fromCache: true };
      }
    }

    try {
      // Pin all three downloads to one commit so they can't mix versions.
      const ref = head ? head.sha : "master";
      const [wpcost, unittags, namesCsv] = await Promise.all([
        fetchSource("wpcost", ref, onStep),
        fetchSource("unittags", ref, onStep),
        fetchSource("names", ref, onStep),
      ]);
      const fresh = {
        fetchedAt: Date.now(),
        sha: head?.sha || null,
        gameDataDate: head?.date || null,
        units: buildUnits(wpcost, unittags, parseNames(namesCsv)),
      };
      writeCache(fresh);
      await attachStats(fresh.units);
      return { ...fresh, fromCache: false, upToDate: !!head };
    } catch (err) {
      // Offline / rate-limited: fall back to stale cache rather than a dead app.
      if (cache) return { ...cache, fromCache: true, stale: true };
      throw err;
    }
  }

  return { NATIONS, load, econToBR };
})();
