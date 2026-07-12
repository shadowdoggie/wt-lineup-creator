"use strict";

/**
 * Data layer: downloads vehicle data from the War Thunder datamine mirror
 * (gszabi99/War-Thunder-Datamine), which tracks the live game files, and
 * condenses it into a compact unit list cached in localStorage.
 */
const WT_DATA = (() => {
  const BASE = "https://raw.githubusercontent.com/gszabi99/War-Thunder-Datamine/master/";
  const SOURCES = {
    wpcost:   BASE + "char.vromfs.bin_u/config/wpcost.blkx",
    unittags: BASE + "char.vromfs.bin_u/config/unittags.blkx",
    names:    BASE + "lang.vromfs.bin_u/lang/units.csv",
  };
  const CACHE_KEY = "wtlc_data_v1";
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

  async function fetchSource(key, onStep) {
    const res = await fetch(SOURCES[key]);
    if (!res.ok) throw new Error(`Failed to download ${key} (HTTP ${res.status})`);
    const body = key === "names" ? await res.text() : await res.json();
    onStep?.(key);
    return body;
  }

  /**
   * Loads unit data. Uses the local cache when it is fresh enough, otherwise
   * downloads from the datamine mirror. `onStep(key)` fires per finished file.
   */
  async function load({ force = false, onStep } = {}) {
    if (!force) {
      const cache = readCache();
      if (cache && Date.now() - cache.fetchedAt < MAX_AGE_MS) {
        return { ...cache, fromCache: true };
      }
    }
    try {
      const [wpcost, unittags, namesCsv] = await Promise.all([
        fetchSource("wpcost", onStep),
        fetchSource("unittags", onStep),
        fetchSource("names", onStep),
      ]);
      const cache = { fetchedAt: Date.now(), units: buildUnits(wpcost, unittags, parseNames(namesCsv)) };
      writeCache(cache);
      return { ...cache, fromCache: false };
    } catch (err) {
      // Offline / rate-limited: fall back to stale cache rather than a dead app.
      const cache = readCache();
      if (cache) return { ...cache, fromCache: true, stale: true };
      throw err;
    }
  }

  return { NATIONS, load, econToBR };
})();
