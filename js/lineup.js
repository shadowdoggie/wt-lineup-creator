"use strict";

/**
 * Lineup generation: filters the unit list down to a BR bracket and greedily
 * fills crew slots, scoring vehicles by BR closeness, class preference of the
 * chosen playstyle, and real armor stats.
 */
const LINEUP = (() => {
  const BR_WINDOW = 1.0; // vehicles from (target - 1.0) up to target

  const PLAYSTYLES = {
    balanced: {
      classW: { medium: 1.0, heavy: 0.85, td: 0.7, light: 0.65 },
      armorBias: 0.15, // slight preference for surviving a shot
    },
    armor: {
      classW: { heavy: 1.0, medium: 0.8, td: 0.6, light: 0.15 },
      armorBias: 1.0,
    },
    speed: {
      classW: { light: 1.0, medium: 0.85, td: 0.5, heavy: 0.1 },
      armorBias: -1.0, // lighter = faster; the files have no usable top speed
    },
    sniper: {
      classW: { td: 1.0, medium: 0.7, heavy: 0.5, light: 0.45 },
      armorBias: 0.2,
    },
  };

  const AIR_ROLE_W = { attacker: 1.0, bomber: 0.85, fighter: 0.9 };

  function brScore(br, target) {
    return Math.max(0, 1 - (target - br) / BR_WINDOW);
  }

  // Percentile rank (0..1) of each unit's frontal hull armor within the pool,
  // so "armored" means armored relative to its own BR bracket, not absolutely.
  function armorPercentiles(pool) {
    const sorted = pool
      .map(u => (u.armorHull ?? 0) + (u.armorTurret ?? 0) * 0.5)
      .sort((a, b) => a - b);
    const pct = new Map();
    for (const u of pool) {
      const v = (u.armorHull ?? 0) + (u.armorTurret ?? 0) * 0.5;
      pct.set(u.id, sorted.length > 1 ? sorted.indexOf(v) / (sorted.length - 1) : 0.5);
    }
    return pct;
  }

  /**
   * Greedy pick with class-repetition damping: each already-picked vehicle of
   * the same class multiplies a candidate's score by 0.7, which yields varied
   * lineups without hard composition rules.
   */
  function pickGreedy(pool, count, scoreFn) {
    const picked = [];
    const clsCount = {};
    const cands = pool.slice();
    while (picked.length < count && cands.length) {
      let best = -1;
      let bestScore = -Infinity;
      for (let i = 0; i < cands.length; i++) {
        const s = scoreFn(cands[i]) * Math.pow(0.7, clsCount[cands[i].cls] || 0);
        if (s > bestScore) { bestScore = s; best = i; }
      }
      const u = cands.splice(best, 1)[0];
      clsCount[u.cls] = (clsCount[u.cls] || 0) + 1;
      picked.push(u);
    }
    return picked;
  }

  function generate(units, o) {
    const warnings = [];
    const ps = PLAYSTYLES[o.playstyle] || PLAYSTYLES.balanced;

    const pool = units.filter(u =>
      u.country === o.nation &&
      (!u.premium || o.incPremium) &&
      (!u.squadron || o.incSquadron) &&
      (!u.gift || o.incGift) &&
      u.br[o.mode] != null &&
      u.br[o.mode] <= o.targetBR + 1e-9 &&
      u.br[o.mode] >= o.targetBR - BR_WINDOW - 1e-9
    );

    const mains = pool.filter(u => u.type === "tank" && u.cls !== "spaa");
    const spaas = pool.filter(u => u.cls === "spaa");
    const planes = pool.filter(u => u.type === "aircraft");
    const helis = pool.filter(u => u.type === "helicopter");

    // Decide how many slots go to support vehicles, always keeping at least
    // two slots (or all of them, for tiny lineups) for ground mains.
    const plan = { spaa: 0, plane: 0, heli: 0 };
    if (o.incSPAA && spaas.length && o.slots >= 3) plan.spaa = 1;
    if (o.incPlanes && planes.length) plan.plane = o.slots >= 6 ? 2 : 1;
    if (o.incHelis && helis.length && o.slots >= 5) plan.heli = 1;

    const minGround = Math.min(2, o.slots);
    const support = () => plan.spaa + plan.plane + plan.heli;
    while (o.slots - support() < minGround) {
      if (plan.heli) plan.heli--;
      else if (plan.plane) plan.plane--;
      else if (plan.spaa) plan.spaa--;
      else break;
    }
    const groundCount = o.slots - support();

    const armorPct = armorPercentiles(mains);
    const groundScore = u => {
      const a = armorPct.get(u.id) ?? 0.5;
      const stat = ps.armorBias >= 0 ? a * ps.armorBias : (1 - a) * -ps.armorBias;
      return brScore(u.br[o.mode], o.targetBR) * 1.3 + (ps.classW[u.cls] || 0.5) * 1.6 + stat * 0.9;
    };
    const airScore = u =>
      brScore(u.br[o.mode], o.targetBR) * 1.3 + (AIR_ROLE_W[u.cls] || 0.8) * 1.2;
    const supportScore = u => brScore(u.br[o.mode], o.targetBR);

    const pickedGround = pickGreedy(mains, groundCount, groundScore);
    const pickedSpaa = pickGreedy(spaas, plan.spaa, supportScore);
    const pickedPlanes = pickGreedy(planes, plan.plane, airScore);
    const pickedHelis = pickGreedy(helis, plan.heli, supportScore);

    if (pickedGround.length < groundCount) {
      warnings.push(`Only ${pickedGround.length} ground vehicle(s) available in this BR bracket — try a different BR or enable more vehicle sources.`);
    }
    if (o.incPlanes && !planes.length) warnings.push("No aircraft available in this BR bracket.");
    if (o.incHelis && !helis.length) warnings.push("No helicopters available in this BR bracket.");
    if (o.incSPAA && !spaas.length) warnings.push("No SPAA available in this BR bracket.");
    if (o.incSPAA && spaas.length && o.slots < 3) warnings.push("SPAA skipped — needs at least 3 crew slots.");

    const slots = [...pickedGround, ...pickedSpaa, ...pickedPlanes, ...pickedHelis];

    const altOf = (all, picked, scoreFn, n) => {
      const chosen = new Set(picked.map(u => u.id));
      return all.filter(u => !chosen.has(u.id)).sort((a, b) => scoreFn(b) - scoreFn(a)).slice(0, n);
    };

    return {
      slots,
      alternatives: {
        ground: altOf(mains, pickedGround, groundScore, 8),
        spaa: altOf(spaas, pickedSpaa, supportScore, 3),
        air: altOf([...planes, ...helis], [...pickedPlanes, ...pickedHelis], airScore, 5),
      },
      poolSize: pool.length,
      warnings,
    };
  }

  return { generate, BR_WINDOW };
})();
