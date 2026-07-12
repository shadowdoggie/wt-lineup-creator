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
      // Real gun data: a flat-shooting, big-bore gun is what makes a sniper.
      // 60% muzzle velocity (flat trajectory, minimal lead) + 40% bore caliber.
      stat: (u, p) => 0.6 * p.velPct(u) + 0.4 * p.calPct(u),
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
    const velPctRaw = percentiler(mains, u => u.gunVel);
    const calPctRaw = percentiler(mains, u => u.gunCal);
    const p = {
      armorPct: u => armorPctRaw(u) ?? 0.5,
      // Real hp/ton percentile; for the rare vehicle missing it, fall back to
      // "lighter armor ≈ faster" so Speed still ranks it sensibly.
      mobPct: u => mobPctRaw(u) ?? (1 - (armorPctRaw(u) ?? 0.5)),
      // Real gun velocity / bore percentiles. A vehicle with no AP round
      // (missile/HE-only) is a poor sniper, so missing data falls below median.
      velPct: u => velPctRaw(u) ?? 0.35,
      calPct: u => calPctRaw(u) ?? 0.35,
    };
    // BR closeness still matters (avoid heavy downtiers) but the playstyle
    // stat is now a co-equal driver, so Speed really favors high hp/ton etc.
    const groundScore = u =>
      brScore(u.br[o.mode], o.targetBR) * 1.1 + (ps.classW[u.cls] || 0.5) * 1.4 + ps.stat(u, p) * 1.4;

    // Ground-attack firepower = real ordnance weight with a big premium for
    // ATGMs (guided tank-killers punch far above their mass). Blended here so
    // the stored ordnanceKg stays an honest display figure.
    const firepower = u => u.ordnanceKg + (u.atgm ? 2500 : 0);

    const turnPctRaw = percentiler(planes, u => u.turnTime);
    const payPctRaw = percentiler(planes, firepower);
    const turnQuality = u => 1 - (turnPctRaw(u) ?? 0.7); // lower turn time is better
    const fighterScore = u =>
      brScore(u.br[o.mode], o.targetBR) * 1.2 + turnQuality(u) * 1.4 + (u.cls === "fighter" ? 0.4 : 0);
    // CAS by real firepower — a modern ATGM/guided-bomb jet no longer scores
    // like a WWII light bomber that happens to carry more small bombs.
    const attackerScore = u =>
      brScore(u.br[o.mode], o.targetBR) * 1.2 + (payPctRaw(u) ?? 0) * 1.4 +
      (u.cls === "attacker" || u.cls === "bomber" ? 0.4 : 0);

    // Helicopters live and die by their anti-tank punch, so rank by firepower
    // and ATGM capability — not BR closeness, which is all the old model used.
    const heliPayRaw = percentiler(helis, firepower);
    const heliScore = u =>
      brScore(u.br[o.mode], o.targetBR) * 1.0 + (heliPayRaw(u) ?? 0) * 1.6 + (u.atgm ? 0.6 : 0);

    // SPAA by real anti-air capability: a radar SAM launcher massively outranks
    // a WWII quad-MG. SAM (guided, all-aspect) weighs most, radar (track at
    // range) next, then gun caliber percentile among SPAA in the bracket.
    const spaaCalRaw = percentiler(spaas, u => u.aaCal);
    const spaaScore = u =>
      brScore(u.br[o.mode], o.targetBR) * 1.0 + (u.sam ? 1.2 : 0) + (u.radar ? 0.6 : 0) +
      (spaaCalRaw(u) ?? 0.4) * 0.6;

    // Ranked candidate pools per role — the UI swaps within these.
    const pools = {
      ground: rankBy(mains, groundScore),
      spaa: rankBy(spaas, spaaScore),
      fighter: rankBy(planes, fighterScore),
      attacker: rankBy(planes, attackerScore),
      heli: rankBy(helis, heliScore),
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

    const health = assess(slots, o);

    return { slots, pools, used, poolSize: pool.length, warnings, health };
  }

  // Fact-based sufficiency check. Everything here follows from War Thunder's
  // matchmaker rules and the actual BRs of the vehicles that got picked — no
  // opinion, just numbers the player can verify.
  //
  //   • You queue at the HIGHEST BR vehicle in your lineup (your "top BR").
  //   • The matchmaker can up/down-tier you by ±1.0, so at top BR T you face
  //     anything from T-1.0 to T+1.0.
  //   • A vehicle is a competitive respawn if it's within 0.3 of your top BR
  //     (still ≤ enemy top even in a full uptier band). One that sits ≥0.7
  //     below is "downtier ballast": fine in a downtier, badly outmatched when
  //     you're uptiered.
  function assess(slots, o) {
    const mode = o.mode;
    const brs = slots.map(s => s.unit.br[mode]).filter(b => b != null);
    if (!brs.length) return null;

    const topBR = Math.max(...brs);
    const avgBR = brs.reduce((a, b) => a + b, 0) / brs.length;
    const core = brs.filter(b => topBR - b <= 0.3 + 1e-9).length;      // competitive respawns
    const ballast = brs.filter(b => topBR - b >= 0.7 - 1e-9).length;   // weak when uptiered
    const hasSPAA = slots.some(s => s.category === "spaa");
    const hasAir = slots.some(s => ["fighter", "attacker", "heli"].includes(s.category));
    const belowTarget = o.targetBR - topBR;

    // Headline verdict is driven by how many competitive respawns you have.
    let verdict;
    if (core >= 3 && ballast <= core) verdict = { key: "strong", label: "Strong lineup" };
    else if (core >= 2) verdict = { key: "solid", label: "Solid lineup" };
    else verdict = { key: "thin", label: "Thin at top BR" };

    const notes = [];
    // The core fact that answers "why is there a lower-BR vehicle here?"
    notes.push({
      level: "info",
      text: `You queue at BR ${topBR.toFixed(1)} (your highest vehicle). The matchmaker can uptier you +1.0, so expect enemies up to BR ${(topBR + 1.0).toFixed(1)} and downtiers to ${(topBR - 1.0).toFixed(1)}.`,
    });

    if (belowTarget >= 0.3 - 1e-9) {
      notes.push({
        level: "warn",
        text: `Your best vehicle is only BR ${topBR.toFixed(1)}, so you'll queue ${belowTarget.toFixed(1)} below your ${o.targetBR.toFixed(1)} target. Enable more vehicle sources (premium/squadron/event) or pick a lower target BR to get a full top-BR lineup.`,
      });
    }

    notes.push({
      level: core >= 3 ? "good" : core >= 2 ? "info" : "warn",
      text: `${core} of ${brs.length} vehicle(s) sit within 0.3 of your top BR — these stay competitive in a full uptier. ${core < 2 ? "With fewer than 2, once your top vehicle dies you're spawning into a disadvantage." : "That's enough depth to keep respawning effectively."}`,
    });

    if (ballast > 0) {
      notes.push({
        level: ballast > core ? "warn" : "info",
        text: `${ballast} vehicle(s) are 0.7+ below your top BR — strong in a downtier but outmatched when you're uptiered.${ballast > core ? " More than half your lineup is in this range, so uptiers will be rough." : ""}`,
      });
    }

    const spaaUnit = slots.find(s => s.category === "spaa")?.unit;
    if (spaaUnit?.sam) {
      notes.push({ level: "good", text: "SAM SPAA included — guided missiles answer enemy CAS and helicopters at long range." });
    } else if (spaaUnit?.radar) {
      notes.push({ level: "good", text: "Radar SPAA included — it can track and engage aircraft at range." });
    } else if (hasSPAA) {
      notes.push({ level: "good", text: "SPAA included — you can answer enemy CAS (gun-based, best at shorter range)." });
    } else {
      notes.push({ level: "warn", text: "No SPAA — you'll be exposed to enemy aircraft with no dedicated answer." });
    }

    if (!hasAir) notes.push({ level: "info", text: "No aircraft — no way to contribute from the air or counter enemy planes offensively." });

    return { topBR, avgBR, targetBR: o.targetBR, core, ballast, total: brs.length, hasSPAA, hasAir, verdict, notes };
  }

  return { generate, BR_WINDOW };
})();
