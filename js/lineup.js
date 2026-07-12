"use strict";

/**
 * Lineup generation: filters the unit list to a BR bracket, scores vehicles by
 * BR closeness + playstyle + real stats (armor thickness, hp/ton, plane turn
 * time and payload), then fills crew slots. Also returns a ranked candidate
 * pool per role so the UI can offer per-slot swaps.
 */
const LINEUP = (() => {
  const BR_WINDOW = 1.0; // vehicles from (target - 1.0) up to target

  // Ground playstyles. `stat` returns a 0..1 bonus from the vehicle's real
  // stats (armor or mobility percentile within the current bracket).
  const PLAYSTYLES = {
    balanced: {
      classW: { medium: 1.0, heavy: 0.85, td: 0.7, light: 0.65 },
      stat: (u, p) => 0.35 * p.armorPct(u) + 0.35 * p.mobPct(u),
    },
    armor: {
      classW: { heavy: 1.0, medium: 0.8, td: 0.6, light: 0.15 },
      stat: (u, p) => p.armorPct(u),
    },
    speed: {
      classW: { light: 1.0, medium: 0.85, td: 0.5, heavy: 0.1 },
      stat: (u, p) => p.mobPct(u), // real hp/ton, not a proxy
    },
    sniper: {
      classW: { td: 1.0, medium: 0.7, heavy: 0.5, light: 0.45 },
      stat: (u, p) => 0.2 * p.armorPct(u),
    },
  };

  function brScore(br, target) {
    return Math.max(0, 1 - (target - br) / BR_WINDOW);
  }

  // Returns fn(u) -> percentile 0..1 of valueFn(u) within `pool`, or null when
  // the value is missing for that unit.
  function percentiler(pool, valueFn) {
    const vals = pool.map(valueFn).filter(v => v != null).sort((a, b) => a - b);
    return u => {
      const v = valueFn(u);
      if (v == null) return null;
      return vals.length > 1 ? vals.indexOf(v) / (vals.length - 1) : 0.5;
    };
  }

  function rankBy(arr, scoreFn) {
    return arr.slice().sort((a, b) => scoreFn(b) - scoreFn(a));
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

    // --- scoring context ---
    const armorPctRaw = percentiler(mains, u => (u.armorHull ?? 0) + (u.armorTurret ?? 0) * 0.5);
    const mobPctRaw = percentiler(mains, u => u.hpPerTon);
    const p = {
      armorPct: u => armorPctRaw(u) ?? 0.5,
      // Real hp/ton percentile; for the rare vehicle missing it, fall back to
      // "lighter armor ≈ faster" so Speed still ranks it sensibly.
      mobPct: u => mobPctRaw(u) ?? (1 - (armorPctRaw(u) ?? 0.5)),
    };
    // BR closeness still matters (avoid heavy downtiers) but the playstyle
    // stat is now a co-equal driver, so Speed really favors high hp/ton etc.
    const groundScore = u =>
      brScore(u.br[o.mode], o.targetBR) * 1.1 + (ps.classW[u.cls] || 0.5) * 1.4 + ps.stat(u, p) * 1.4;

    const turnPctRaw = percentiler(planes, u => u.turnTime);
    const payPctRaw = percentiler(planes, u => u.payload);
    const turnQuality = u => 1 - (turnPctRaw(u) ?? 0.7); // lower turn time is better
    const fighterScore = u =>
      brScore(u.br[o.mode], o.targetBR) * 1.2 + turnQuality(u) * 1.4 + (u.cls === "fighter" ? 0.4 : 0);
    const attackerScore = u =>
      brScore(u.br[o.mode], o.targetBR) * 1.2 + (payPctRaw(u) ?? 0) * 1.4 +
      (u.cls === "attacker" || u.cls === "bomber" ? 0.4 : 0);
    const supportScore = u => brScore(u.br[o.mode], o.targetBR);

    // Ranked candidate pools per role — the UI swaps within these.
    const pools = {
      ground: rankBy(mains, groundScore),
      spaa: rankBy(spaas, supportScore),
      fighter: rankBy(planes, fighterScore),
      attacker: rankBy(planes, attackerScore),
      heli: rankBy(helis, supportScore),
    };

    // --- slot allocation ---
    const wantPlanes = o.planeRole && o.planeRole !== "none" && planes.length;
    let fighterN = 0, attackerN = 0;
    if (wantPlanes) {
      const total = o.slots >= 6 ? 2 : 1;
      if (o.planeRole === "fighter") fighterN = total;
      else if (o.planeRole === "attacker") attackerN = total;
      else { // balanced: split when there's room, else take the stronger option
        if (total >= 2) { fighterN = 1; attackerN = 1; }
        else {
          const bf = pools.fighter[0], ba = pools.attacker[0];
          if (bf && ba) (fighterScore(bf) >= attackerScore(ba) ? fighterN = 1 : attackerN = 1);
          else if (bf) fighterN = 1; else if (ba) attackerN = 1;
        }
      }
    }
    let spaaN = (o.incSPAA && spaas.length && o.slots >= 3) ? 1 : 0;
    let heliN = (o.incHelis && helis.length && o.slots >= 5) ? 1 : 0;

    // Always reserve at least two slots (or all, for tiny lineups) for mains.
    const minGround = Math.min(2, o.slots);
    const support = () => fighterN + attackerN + spaaN + heliN;
    while (o.slots - support() < minGround) {
      if (heliN) heliN--;
      else if (attackerN) attackerN--;
      else if (fighterN) fighterN--;
      else if (spaaN) spaaN--;
      else break;
    }
    const groundCount = o.slots - support();

    // --- fill slots (used set prevents fighter/attacker picking the same jet) ---
    const used = new Set();
    const take = (ranked, n, category) => {
      const out = [];
      for (const u of ranked) {
        if (out.length >= n) break;
        if (used.has(u.id)) continue;
        used.add(u.id);
        out.push({ unit: u, category });
      }
      return out;
    };
    // Ground uses greedy class-repetition damping so lineups aren't 5 mediums.
    const takeGround = n => {
      const out = [];
      const clsCount = {};
      const cands = pools.ground.filter(u => !used.has(u.id));
      while (out.length < n && cands.length) {
        let best = -1, bestScore = -Infinity;
        for (let i = 0; i < cands.length; i++) {
          // Mild, capped variety penalty: nudges toward mixed classes without
          // overriding a strong playstyle preference (e.g. Speed wanting lights).
          const damp = Math.max(0.7, 1 - 0.12 * (clsCount[cands[i].cls] || 0));
          const s = groundScore(cands[i]) * damp;
          if (s > bestScore) { bestScore = s; best = i; }
        }
        const u = cands.splice(best, 1)[0];
        clsCount[u.cls] = (clsCount[u.cls] || 0) + 1;
        used.add(u.id);
        out.push({ unit: u, category: "ground" });
      }
      return out;
    };

    const slots = [
      ...takeGround(groundCount),
      ...take(pools.spaa, spaaN, "spaa"),
      ...take(pools.fighter, fighterN, "fighter"),
      ...take(pools.attacker, attackerN, "attacker"),
      ...take(pools.heli, heliN, "heli"),
    ];

    // --- warnings ---
    const groundGot = slots.filter(s => s.category === "ground").length;
    if (groundGot < groundCount) {
      warnings.push(`Only ${groundGot} ground vehicle(s) available in this BR bracket — try a different BR or enable more vehicle sources.`);
    }
    if (o.planeRole !== "none" && !planes.length) warnings.push("No aircraft available in this BR bracket.");
    if (o.incSPAA && !spaas.length) warnings.push("No SPAA available in this BR bracket.");
    if (o.incSPAA && spaas.length && o.slots < 3) warnings.push("SPAA skipped — needs at least 3 crew slots.");
    if (o.incHelis && !helis.length) warnings.push("No helicopters available in this BR bracket.");

    return { slots, pools, used, poolSize: pool.length, warnings };
  }

  return { generate, BR_WINDOW };
})();
