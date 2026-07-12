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
    shop:     "char.vromfs.bin_u/config/shop.blkx",
  };
  // Where the raw datamine files come from, tried in order. jsDelivr mirrors the
  // same GitHub repo over a CDN, so it's a genuine fallback when raw.github is
  // unreachable or the (separate) GitHub API is rate-limited.
  const MIRRORS = [
    ref => `https://raw.githubusercontent.com/${REPO}/${ref}/`,
    ref => `https://cdn.jsdelivr.net/gh/${REPO}@${ref}/`,
  ];
  // Real tank stats precomputed by tools/build_mobility.py and shipped with the
  // app: hp/ton (mobility), best-AP-shell muzzle velocity + bore caliber (gun),
  // and SPAA anti-air capability (SAM/radar/caliber). Served from our own
  // origin, merged at load time so refreshing them doesn't bust the vehicle cache.
  const MOBILITY_URL = "data/mobility.json";
  const GUNSTATS_URL = "data/gunstats.json";
  const SPAA_URL = "data/spaa.json";
  const ARMOR_URL = "data/armor.json";
  // Fallback re-download interval when the commit check fails (offline / API
  // rate limit). Generous so a rate-limited first paint still serves cache.
  const MAX_AGE_MS = 7 * 24 * 3600 * 1000;
  // Roughly how many crewable vehicles we expect to parse. The game files only
  // ever grow, so parsing far fewer than this means the format shifted under us.
  const EXPECT_MIN_UNITS = 1500;

  const num = v => (typeof v === "number" ? v : 0);

  // Cache key is derived from a fingerprint of the fields buildUnits emits, so
  // any change to the unit shape automatically invalidates stale caches — no
  // more remembering to bump a manual "_v2". Add new fields to this string.
  // Also bump the trailing sentinel when the *format* of a cached value changes
  // without its field name changing (e.g. name-marker stripping below), so
  // already-cached clients re-parse instead of serving the old-format value.
  const SCHEMA = "id name country type cls diveBomber rank br premium squadron gift " +
    "researchPoints armorHull armorTurret armorEff hasEra hasComposite stabilized thermal nv revRatio " +
    "hpPerTon gunVel gunCal gunPen gunPenSrc autoLoader turnTime maxSpeed climbRate " +
    "crewCount reloadTime turretSpeed " +
    "ordnanceKg atgm atgmRange aam arh cm sam radar aaCal " +
    "fmt:hybrid-pen-table|est;armor:steel+flags+eff;reload:al-flag;air:aam";
  function hash32(s) {
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
    return (h >>> 0).toString(36);
  }
  const CACHE_KEY = "wtlc_" + hash32(SCHEMA);

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

  // Ground-attack firepower for a plane or helicopter. wpcost pre-aggregates
  // each weapon preset's ordnance, so we can weigh real tonnage and guided
  // munitions without opening any weapon file:
  //   totalBombRocketMass / totalNapalmBombMass / totalTorpedoMass — unguided kg
  //   totalGuidedBombMass  — precision bombs (counted double: they hit)
  //   atgmVisibilityType    — the preset carries anti-ground guided missiles
  //   atgmMaxDistance       — ATGM standoff range (m)
  //
  // Honest same-preset pick: ordnance kg and ATGM come from ONE loadout, not
  // "heaviest bombs from preset A + ATGMs from preset B". The chosen preset is
  // the one with the best combat score (kg + ATGM bonus), matching how the
  // lineup scorer values firepower.
  function airFirepower(w) {
    let best = { ordnanceKg: 0, atgm: false, atgmRange: 0, score: -1 };
    for (const preset of Object.values(w.weapons || {})) {
      const kg =
        num(preset.totalBombRocketMass) +
        num(preset.totalNapalmBombMass) +
        num(preset.totalTorpedoMass) * 0.5 +
        num(preset.totalGuidedBombMass) * 2;
      const hasATGM = "atgmVisibilityType" in preset;
      const range = hasATGM ? num(preset.atgmMaxDistance) : 0;
      // Same blend the scorer uses: guided tank-killers punch far above mass.
      const score = kg + (hasATGM ? 2500 + Math.min(range, 8000) * 0.05 : 0);
      if (score > best.score) {
        best = {
          ordnanceKg: Math.round(kg),
          atgm: hasATGM,
          atgmRange: Math.round(range),
          score,
        };
      }
    }
    return { ordnanceKg: best.ordnanceKg, atgm: best.atgm, atgmRange: best.atgmRange };
  }

  // Air-to-air / survivability flags from the same weapon presets (fighters):
  //   aamGuidanceType — IR/basic ("default"), ARH ("arh"), SACLOS, etc.
  //   hasCountermeasures — flares/chaff available on a loadout
  // These are presence flags across any preset (you can equip AAMs or CMs even
  // if the heaviest bomb load doesn't carry them).
  function airCombat(w) {
    let aam = false, arh = false, cm = false;
    for (const preset of Object.values(w.weapons || {})) {
      if (preset.hasCountermeasures) cm = true;
      const g = preset.aamGuidanceType;
      if (g == null) continue;
      aam = true;
      const types = Array.isArray(g) ? g : [g];
      for (const t of types) {
        const s = String(t).toLowerCase();
        // Active-radar and any future SARH-style tags count as beyond-visual-range.
        if (s === "arh" || s === "sarh" || s.includes("radar")) arh = true;
      }
    }
    return { aam, arh, cm };
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

  // War Thunder prefixes ~500 display names with private UI "marker" glyphs
  // (e.g. `▃Skink`, `▄M44`, `☢IL-28`, a U+F059 PUA tag). The game's own font
  // draws these as little rank/premium/trophy tags, but a normal web font shows
  // tofu boxes. Strip them wherever they appear (a few names use one mid-string
  // as a separator, e.g. `Challe's ▄Yak-9T` / `F-86F-40 ▅`), then tidy spacing.
  // These blocks — control pictures, box-drawing, block/geometric shapes, misc
  // symbols/dingbats, arrows, and the Private Use Area — never occur in a real
  // Latin vehicle name, so removing them is safe.
  const NAME_MARKERS = /[​←-➿⬀-⯿-]/g;
  function cleanName(s) {
    return s.replace(NAME_MARKERS, "").replace(/\s{2,}/g, " ").trim();
  }

  // units.csv: `"<id>";"<English>";...` — we only need the `<unit>_shop` rows.
  function parseNames(csv) {
    const names = {};
    const re = /^"((?:[^"]|"")+_shop)";"((?:[^"]|"")*)"/;
    for (const line of csv.split("\n")) {
      const m = re.exec(line);
      if (m) names[m[1].slice(0, -5)] = cleanName(m[2].replace(/""/g, '"'));
    }
    return names;
  }

  // shop.blkx lists every vehicle actually in the game's tech tree / shop.
  // Gaijin keeps removed/test/tutorial vehicles in wpcost (e.g. us_amx_13_75,
  // a French tank that was briefly in the USA tree but is no longer obtainable)
  // — without this cross-reference the app would show them in lineups.
  function collectShopIds(shop) {
    const ids = new Set();
    const walk = obj => {
      if (!obj || typeof obj !== "object") return;
      if (Array.isArray(obj)) { obj.forEach(walk); return; }
      for (const [k, v] of Object.entries(obj)) {
        if (v && typeof v === "object" && "rank" in v) ids.add(k);
        walk(v);
      }
    };
    walk(shop);
    return ids;
  }

  function buildUnits(wpcost, unittags, names, shopIds) {
    const units = [];
    for (const [id, w] of Object.entries(wpcost)) {
      if (!w || typeof w !== "object") continue;
      const t = unittags[id];
      if (!t) continue;
      const type = t.type;
      if (type !== "tank" && type !== "aircraft" && type !== "helicopter") continue;
      const tags = t.tags || {};
      if (tags.hideBrForVehicle) continue; // AI / event-only oddities
      if (shopIds && !shopIds.has(id)) continue; // not in the shop = not obtainable
      const country = (w.country || "").replace("country_", "");
      if (!NATION_IDS.has(country)) continue;
      const cls = classify(type, tags);
      if (!cls) continue;

      const air = type !== "tank";
      const shop = t.Shop || {};
      // Both aircraft and helicopters carry ground-attack ordnance (a heli's
      // whole reason for existing is its ATGMs), so score firepower for both.
      const fire = air ? airFirepower(w) : null;
      const combat = air ? airCombat(w) : null;
      units.push({
        id,
        name: names[id] || prettifyId(id),
        country,
        type,
        cls,
        // Dive bombers (Stukas etc.) are classed as "attacker" for the CAS slot,
        // but we flag them separately so the user can restrict CAS to just dive
        // bombers vs just level/strategic bombers (cls === "bomber").
        diveBomber: !!tags.type_dive_bomber,
        rank: w.rank || 1,
        // Research points (RP) to unlock. 0 for premium/gift/starter vehicles.
        // Used only as a tiebreak between equally-scored picks (prefer the more
        // advanced vehicle when two are otherwise a dead heat).
        researchPoints: num(w.reqExp),
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
        armorEff: null,      // armor.json — ranking-only protection score (never shown as mm)
        hasEra: false,       // armor.json — ERA tiles present in model
        hasComposite: false, // armor.json — composite/NERA arrays present
        autoLoader: false,   // armor.json — game's own autoLoader flag on the main gun
        stabilized: false,
        thermal: false,
        nv: false,
        revRatio: 0,
        hpPerTon: null,
        gunVel: null,
        gunCal: null,
        gunPen: null,    // mm at ~1km when known
        gunPenSrc: null, // "table" (ArmorPower) | "est" (physics) | null
        sam: false,
        radar: false,
        aaCal: null,
        crewCount: typeof w.crewTotalCount === "number" ? w.crewTotalCount : null,
        reloadTime: typeof w.reloadTime_cannon === "number" ? w.reloadTime_cannon : null,
        turretSpeed: Array.isArray(w.turretSpeed) ? w.turretSpeed[0] : null,
        turnTime: air && typeof shop.turnTime === "number" ? shop.turnTime : null,
        maxSpeed: air && typeof shop.maxSpeed === "number" ? shop.maxSpeed : null,
        climbRate: air && typeof shop.climbSpeed === "number" ? shop.climbSpeed : null,
        ordnanceKg: fire ? fire.ordnanceKg : 0,
        atgm: fire ? fire.atgm : false,
        atgmRange: fire ? fire.atgmRange : 0,
        aam: combat ? combat.aam : false,   // any air-to-air missiles
        arh: combat ? combat.arh : false,   // active-radar / BVR-class AAMs
        cm: combat ? combat.cm : false,     // countermeasures (flares/chaff)
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

  // Returns true if the write landed. On quota failure we drop older wtlc_*
  // caches and retry once so a full garage dump doesn't permanently disable caching.
  function writeCache(cache) {
    const payload = JSON.stringify(cache);
    try {
      localStorage.setItem(CACHE_KEY, payload);
      return true;
    } catch {
      try {
        const doomed = [];
        for (let i = 0; i < localStorage.length; i++) {
          const k = localStorage.key(i);
          if (k && k.startsWith("wtlc_") && k !== CACHE_KEY) doomed.push(k);
        }
        for (const k of doomed) localStorage.removeItem(k);
        localStorage.setItem(CACHE_KEY, payload);
        return true;
      } catch {
        return false;
      }
    }
  }

  async function fetchSource(key, ref, onStep) {
    let lastErr;
    for (const base of MIRRORS) {
      try {
        const res = await fetch(base(ref) + PATHS[key]);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = key === "names" ? await res.text() : await res.json();
        onStep?.(key);
        return body;
      } catch (e) {
        lastErr = e; // try the next mirror before giving up
      }
    }
    throw new Error(`Failed to download ${key} (${lastErr?.message || "network error"})`);
  }

  // Latest datamine commit = current version of the game files. The mirror
  // updates within hours of every patch/BR change, so comparing this against
  // the cached commit tells us whether Gaijin changed anything. Only the GitHub
  // API is rate-limited (60 req/h/IP); the raw file download uses a different
  // host, so a rate-limited freshness check just falls back to cached data.
  async function fetchCommitInfo() {
    const res = await fetch(COMMIT_API, { headers: { Accept: "application/vnd.github+json" } });
    if (!res.ok) {
      const limited = res.status === 403 || res.status === 429;
      throw new Error(limited ? "GitHub API rate limit" : `GitHub API HTTP ${res.status}`);
    }
    const c = await res.json();
    return { sha: c.sha, date: c.commit?.committer?.date || null };
  }

  // Loud sanity checks against silent datamine breakage. If Gaijin renames a
  // field/tag or the mirror serves something unexpected, counts collapse — we
  // want a visible banner, not a quietly-broken lineup. Thresholds are generous
  // (the game only grows), so these fire on real breakage, not normal drift.
  function sanityCheck(units, coverage) {
    const warnings = [];
    if (units.length < EXPECT_MIN_UNITS) {
      warnings.push(`Only ${units.length.toLocaleString()} vehicles parsed (expected ` +
        `${EXPECT_MIN_UNITS.toLocaleString()}+). The datamine format may have changed — ` +
        `lineups could be incomplete.`);
    }
    const emptyNations = NATIONS.filter(([id]) => !units.some(u => u.country === id))
      .map(([, label]) => label.replace(/^\S+\s/, ""));
    if (emptyNations.length) {
      warnings.push(`No vehicles found for ${emptyNations.join(", ")} — a nation tag may have ` +
        `been renamed upstream.`);
    }
    const tanks = units.filter(u => u.type === "tank");
    if (tanks.length) {
      const withArmor = tanks.filter(u => u.armorHull != null).length;
      if (withArmor / tanks.length < 0.5) {
        warnings.push(`Armor data is missing for ${Math.round((1 - withArmor / tanks.length) * 100)}% ` +
          `of tanks this patch — armor-based ranking will be degraded.`);
      }
    }
    // Precomputed stat files: a null means the fetch failed (missing/renamed).
    if (coverage.mobility === null) warnings.push("Mobility data (mobility.json) didn't load — Speed ranking has no hp/ton values.");
    if (coverage.guns === null) warnings.push("Gun data (gunstats.json) didn't load — Sniper ranking has no pen/velocity values.");
    if (coverage.spaa === null) warnings.push("SPAA data (spaa.json) didn't load — anti-air is ranked by battle rating only.");
    if (coverage.armor === null) warnings.push("Armor data (armor.json) didn't load — Armor ranking has no steel/ERA flags.");

    // Patch-safety: if precomputed tables loaded but coverage collapsed, the
    // offline builder or datamine format likely broke — shout instead of quietly
    // ranking on empty stats.
    if (coverage.mobility != null && coverage.mobility < 500) {
      warnings.push(`Mobility table looks thin (${coverage.mobility} entries) — Speed ranking may be degraded after a game update.`);
    }
    if (coverage.guns != null && coverage.guns < 400) {
      warnings.push(`Gun table looks thin (${coverage.guns} entries) — Sniper ranking may be degraded after a game update.`);
    }
    if (coverage.armor != null && coverage.armor < 500) {
      warnings.push(`Armor table looks thin (${coverage.armor} entries) — Armor ranking may be degraded after a game update.`);
    }
    if (coverage.penWithValue != null && coverage.guns != null && coverage.guns > 200
        && coverage.penWithValue < 50) {
      warnings.push("Almost no gun penetration values resolved — pen ranking is degraded (builder/datamine change?).");
    }
    if (coverage.thermal != null && coverage.thermal < 20 && coverage.armor != null && coverage.armor > 500) {
      warnings.push("Almost no thermal flags in armor data — optic ranking may be broken after a game update.");
    }
    return warnings;
  }

  // Merge precomputed hp/ton, gun, SPAA, and effective-armor stats onto tank
  // units. Done every load (not baked into the cache) so shipping new data
  // files takes effect immediately. Each file is optional and fails soft to a
  // fallback. Returns a coverage report so the caller can flag a file that
  // went missing/empty.
  async function attachStats(units) {
    const tanks = units.filter(u => u.type === "tank");
    const [mob, guns, spaa, armor] = await Promise.all([
      fetchJsonSoft(MOBILITY_URL),
      fetchJsonSoft(GUNSTATS_URL),
      fetchJsonSoft(SPAA_URL),
      fetchJsonSoft(ARMOR_URL),
    ]);
    // No mobility file: Speed playstyle falls back to the armor-inverse proxy.
    if (mob) for (const u of tanks) u.hpPerTon = mob[u.id] ?? null;
    // No gun file: Sniper falls back to its class-based proxy.
    if (guns) for (const u of tanks) {
      const g = guns[u.id];
      if (g) {
        u.gunVel = g.v ?? null;
        u.gunCal = g.c ?? null;
        u.gunPen = g.p ?? null;
        u.gunPenSrc = g.ps === "table" || g.ps === "est" ? g.ps : (g.p != null ? "est" : null);
      }
    }
    // No SPAA file: anti-air scoring falls back to BR closeness only.
    if (spaa) for (const u of tanks) {
      const s = spaa[u.id];
      if (s) { u.sam = !!s.sam; u.radar = !!s.radar; u.aaCal = s.cal || null; }
    }
    // Armor: factual steel plate thicknesses (displayed) + ranking-only eff
    // protection score + ERA/composite presence flags + stab/thermals/NV/
    // reverse + the game's autoloader flag.
    if (armor) for (const u of tanks) {
      const a = armor[u.id];
      if (!a) continue;
      if (a.h > 0) u.armorHull = a.h;
      if (a.t > 0) u.armorTurret = a.t;
      if (a.eff > 0) u.armorEff = a.eff;
      u.hasEra = !!a.era;
      u.hasComposite = !!a.comp;
      u.autoLoader = !!a.al;
      u.stabilized = !!a.stab;
      u.thermal = !!a.thermal;
      u.nv = !!a.nv;
      u.revRatio = a.rev || 0;
    }
    let penWithValue = 0, thermal = 0;
    if (guns) for (const g of Object.values(guns)) {
      if (g && g.p != null) penWithValue++;
    }
    if (armor) for (const a of Object.values(armor)) {
      if (a && a.thermal) thermal++;
    }
    return {
      mobility: mob ? Object.keys(mob).length : null,
      guns: guns ? Object.keys(guns).length : null,
      spaa: spaa ? Object.keys(spaa).length : null,
      armor: armor ? Object.keys(armor).length : null,
      penWithValue: guns ? penWithValue : null,
      thermal: armor ? thermal : null,
    };
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
  // Attach precomputed stats and run sanity checks, folding the result into the
  // returned payload. Every return path in load() goes through here so the UI
  // always gets a dataWarnings array.
  async function finalize(payload) {
    const coverage = await attachStats(payload.units);
    return { ...payload, dataWarnings: sanityCheck(payload.units, coverage) };
  }

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
        return finalize({ ...cache, fromCache: true, upToDate: true });
      }
      if (!head && Date.now() - cache.fetchedAt < MAX_AGE_MS) {
        return finalize({ ...cache, fromCache: true });
      }
    }

    try {
      // Pin all downloads to one commit so they can't mix versions.
      const ref = head ? head.sha : "master";
      const [wpcost, unittags, namesCsv, shop] = await Promise.all([
        fetchSource("wpcost", ref, onStep),
        fetchSource("unittags", ref, onStep),
        fetchSource("names", ref, onStep),
        fetchSource("shop", ref, onStep),
      ]);
      const fresh = {
        fetchedAt: Date.now(),
        sha: head?.sha || null,
        gameDataDate: head?.date || null,
        units: buildUnits(wpcost, unittags, parseNames(namesCsv), collectShopIds(shop)),
      };
      const cached = writeCache(fresh);
      const out = await finalize({ ...fresh, fromCache: false, upToDate: !!head });
      if (!cached) {
        out.dataWarnings = [
          ...(out.dataWarnings || []),
          "Browser storage is full — vehicle data won't be cached between visits (slower reloads).",
        ];
      }
      return out;
    } catch (err) {
      // Offline / rate-limited: fall back to stale cache rather than a dead app.
      if (cache) {
        const out = await finalize({ ...cache, fromCache: true, stale: true });
        out.dataWarnings = [
          ...(out.dataWarnings || []),
          "Couldn't reach the datamine mirror — using cached vehicle data.",
        ];
        return out;
      }
      throw err;
    }
  }

  return { NATIONS, load, econToBR };
})();
