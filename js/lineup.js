"use strict";

/**
 * Lineup generation: filters the unit list to a BR bracket, scores vehicles by
 * BR closeness + playstyle + real stats (armor thickness, hp/ton, plane turn
 * time and payload), then fills crew slots. Also returns a ranked candidate
 * pool per role so the UI can offer per-slot swaps.
 */
const LINEUP = (() => {
  const BR_WINDOW = 1.0; // vehicles from (target - 1.0) up to target

  // Single source of truth for the "spaded vehicle" assumption. Shown on cards
  // and in the results note. Keep this accurate when the offline builders gain
  // new unlockable-dependent fields (ERA packs, top shells, thermals, etc.).
  const SPADED = {
    short: "Spaded",
    title:
      "Uses game-file data for a fully unlocked vehicle: best equippable AP shell, " +
      "ERA/composite if present in the model, thermal/NV upgrades. Pen prefers the " +
      "game's ArmorPower table; if missing, a labeled estimate is used for ranking. " +
      "Stock reload is not crew-trained.",
    note:
      "Spaded game-file stats. Gun cards show velocity (not pen mm). Cards show factual " +
      "steel + ERA/composite flags; armor RANKING uses an internal protection score that " +
      "also counts composite arrays and ERA coverage. Reload is stock.",
  };

  // Ground playstyles. `stat` returns a 0..1 bonus from the vehicle's real
  // stats (armor or mobility percentile within the current bracket).
  // `desc` is shown in the UI so players know what the scorer optimizes for.
  const PLAYSTYLES = {
    balanced: {
      classW: { medium: 1.0, heavy: 0.85, td: 0.7, light: 0.65 },
      stat: (u, p) => 0.35 * p.armorPct(u) + 0.35 * p.mobPct(u) + 0.15 * p.crewPct(u) + 0.15 * p.penPct(u),
      variety: true, // mild class-mix damping is welcome here
      desc: "Mixed classes near your BR. Armor + mobility + crew + gun pen, with mild variety so you don't get five mediums. Spaded stats.",
    },
    armor: {
      classW: { heavy: 1.0, medium: 0.8, td: 0.6, light: 0.15 },
      // Effective-protection score (steel×quality + composite arrays + ERA
      // coverage, precomputed in armor.json). ERA/composite are already folded
      // into that score, so no separate boolean bonuses here.
      stat: (u, p) =>
        0.7 * p.armorPct(u) + 0.2 * p.reloadPct(u) + 0.1 * p.crewPct(u),
      variety: false, // mono-class wall is the point
      desc: "Heavy wall. Effective protection (steel + composite + ERA coverage), reload & crew. No class-mix damping.",
    },
    speed: {
      classW: { light: 1.0, medium: 0.85, td: 0.5, heavy: 0.1 },
      stat: (u, p) => 0.65 * p.mobPct(u) + 0.15 * p.revPct(u) + 0.1 * p.turretPct(u) + 0.1 * p.crewPct(u),
      variety: false,
      desc: "Lights & flankers. hp/ton, reverse speed, turret traverse. Spaded mobility/optics. No class-mix damping.",
    },
    sniper: {
      classW: { td: 1.0, medium: 0.7, heavy: 0.5, light: 0.45 },
      // Real gun data: pen primary. Missile/HEAT-only TDs with no AP shell get
      // a hard floor so they can't win Sniper on class weight alone.
      stat: (u, p) => {
        if (u.gunPen == null || u.gunPen <= 0) return 0.04;
        return 0.55 * p.penPct(u) + 0.3 * p.velPct(u) + 0.15 * p.calPct(u);
      },
      variety: false,
      desc: "Gun pen (ArmorPower table preferred, labeled estimate if needed), velocity & caliber. Missile-only TDs heavily penalized.",
    },
  };

  // BR closeness shaped to match the health check: vehicles within 0.3 of
  // target are "core", ≥0.7 below are "ballast". Linear 0..1 falloff alone let
  // a strong 0.8-downtier outscore an average top-BR pick — then the health
  // panel called that same pick ballast. This curve keeps the generator and
  // the evaluator pointing the same way.
  function brScore(br, target) {
    const delta = target - br;
    if (delta <= 0) return 1;
    if (delta <= 0.3) return 1.0 - delta * 0.15;           // 1.00 → 0.955
    if (delta <= 0.7) return 0.955 - (delta - 0.3) * 1.1;  // 0.955 → 0.515
    return Math.max(0, 0.515 - (delta - 0.7) * 1.7);       // 0.515 → 0 at ~1.0
  }

  // Returns fn(u) -> percentile 0..1 of valueFn(u) within `pool`, or null when
  // the value is missing for that unit.
  function percentiler(pool, valueFn) {
    const vals = pool.map(valueFn).filter(v => v != null).sort((a, b) => a - b);
    return u => {
      const v = valueFn(u);
      if (v == null) return null;
      if (vals.length <= 1) return 0.5;
      // Tied values share a rank: use the midpoint of their index range so a
      // whole tied group scores as the middle of the tie, not its bottom
      // (indexOf alone would rank every tie as the worst of the group).
      const rank = (vals.indexOf(v) + vals.lastIndexOf(v)) / 2;
      return rank / (vals.length - 1);
    };
  }

  // Sort by score, high to low. Exact score ties break toward tech-tree
  // vehicles (more likely owned), then higher research points.
  function rankBy(arr, scoreFn) {
    return arr.slice().sort((a, b) => {
      const d = scoreFn(b) - scoreFn(a);
      if (Math.abs(d) > 1e-9) return d;
      const ownA = (!a.premium && !a.squadron && !a.gift) ? 1 : 0;
      const ownB = (!b.premium && !b.squadron && !b.gift) ? 1 : 0;
      if (ownA !== ownB) return ownB - ownA;
      return (b.researchPoints || 0) - (a.researchPoints || 0);
    });
  }

  // Include a vehicle if it's tech-tree, OR any of its special sources is
  // enabled. A premium that also carries a gift/event flag (e.g. MiG-23ML has
  // costGold + gift: msi_notebook) must still appear when Premium is checked —
  // requiring every flag to match excluded those vehicles entirely.
  function sourceAllowed(u, o) {
    if (!u.premium && !u.squadron && !u.gift) return true;
    return (u.premium && o.incPremium) ||
           (u.squadron && o.incSquadron) ||
           (u.gift && o.incGift);
  }

  function generate(units, o) {
    const warnings = [];
    const ps = PLAYSTYLES[o.playstyle] || PLAYSTYLES.balanced;

    const pool = units.filter(u =>
      u.country === o.nation &&
      sourceAllowed(u, o) &&
      u.br[o.mode] != null &&
      u.br[o.mode] <= o.targetBR + 1e-9 &&
      u.br[o.mode] >= o.targetBR - BR_WINDOW - 1e-9
    );

    const mains = pool.filter(u => u.type === "tank" && u.cls !== "spaa");
    const spaas = pool.filter(u => u.cls === "spaa");
    const planes = pool.filter(u => u.type === "aircraft");
    const helis = pool.filter(u => u.type === "helicopter");

    // --- scoring context ---
    // Armor RANKING uses the precomputed effective-protection score (steel ×
    // quality + composite arrays + ERA coverage). Raw steel alone inverted the
    // ordering at top tier: welded-composite turrets (T-80UD, Leo 2A6) report
    // 45–80mm backing plates while cast turrets (T-64/T-72) report 250–400mm.
    // Cards still display only the factual steel mm — eff is never shown.
    const armorValue = u =>
      u.armorEff ?? Math.max(u.armorHull ?? 0, u.armorTurret ?? 0);
    const armorPctRaw = percentiler(mains, u => {
      const a = armorValue(u);
      return a > 0 ? a : null;
    });
    const mobPctRaw = percentiler(mains, u => u.hpPerTon);
    const velPctRaw = percentiler(mains, u => u.gunVel);
    const calPctRaw = percentiler(mains, u => u.gunCal);
    // Pen only when ArmorPower table exists (gunPen set). No estimates.
    const penPctRaw = percentiler(mains, u => u.gunPen);
    // Reload: don't mix autoloaders with manual loaders in one percentile —
    // manual reloads improve with crew training, autoloader cycles don't. The
    // split uses the game's own autoLoader flag (a 5s human-loaded M1A1 is not
    // an autoloader; a 7.1s T-72 carousel is). Autoloaders get a high fixed
    // band; manuals are ranked among themselves.
    const manualReloadPool = mains.filter(u => u.reloadTime != null && !u.autoLoader);
    const manualReloadPct = percentiler(manualReloadPool, u => -u.reloadTime);
    const revPctRaw = percentiler(mains, u => u.revRatio > 0 ? u.revRatio : null);
    const turretPctRaw = percentiler(mains, u => u.turretSpeed ?? null);
    const crewPctRaw = percentiler(mains, u => u.crewCount ?? null);

    // Missing stats: only score with real values. Null components contribute 0
    // to playstyles that need them (sniper without ArmorPower pen), not a fake
    // "0.35 median" guess.
    const p = {
      armorPct: u => armorPctRaw(u) ?? 0,
      mobPct: u => mobPctRaw(u) ?? 0,
      velPct: u => velPctRaw(u) ?? 0,
      calPct: u => calPctRaw(u) ?? 0,
      penPct: u => penPctRaw(u) ?? 0,
      reloadPct: u => {
        if (u.reloadTime == null) return 0;
        // Autoloader band 0.88–1.0, faster cycle = higher (8s → 0.88, 4s → 0.98).
        if (u.autoLoader) return 0.88 + Math.min(0.12, Math.max(0, 8 - u.reloadTime) / 40);
        return manualReloadPct(u) ?? 0;
      },
      revPct: u => revPctRaw(u) ?? 0,
      turretPct: u => turretPctRaw(u) ?? 0,
      crewPct: u => crewPctRaw(u) ?? 0,
    };

    // Uptier fightability: factual ArmorPower pen vs 75th-percentile frontal
    // steel of medium/heavy/TDs in [target, target+1]. Both numbers come from
    // game files — not synthetic effective armor.
    const uptierArmor = [];
    for (const u of units) {
      if (u.type !== "tank" || u.cls === "spaa" || u.cls === "light") continue;
      const br = u.br[o.mode];
      if (br == null || br < o.targetBR - 1e-9 || br > o.targetBR + 1.0 + 1e-9) continue;
      const a = armorValue(u);
      if (a > 0) uptierArmor.push(a);
    }
    uptierArmor.sort((a, b) => a - b);
    const needArmor = uptierArmor.length
      ? uptierArmor[Math.min(uptierArmor.length - 1, Math.floor(uptierArmor.length * 0.75))]
      : null;
    const fightUptier = u => {
      if (u.gunPen == null || u.gunPen <= 0 || needArmor == null) return 0;
      const ratio = u.gunPen / Math.max(needArmor, 1);
      return Math.max(0, Math.min(1, ratio * 0.65));
    };

    // Mild tech-tree preference when scores are otherwise close (more likely owned).
    const ownershipNudge = u =>
      (!u.premium && !u.squadron && !u.gift) ? 0.12 : 0;

    // BR closeness is the dominant term (×2.0) so the generator optimizes for
    // the same thing the health panel measures. Class fit + stats still matter
    // but can no longer promote a 0.8-downtier over a merely-average top pick.
    const groundScore = u =>
      brScore(u.br[o.mode], o.targetBR) * 2.0 +
      (ps.classW[u.cls] || 0.5) * 1.2 +
      ps.stat(u, p) * 1.3 +
      fightUptier(u) * 0.9 +
      (u.stabilized ? 0.12 : 0) +
      (u.thermal ? 0.12 : u.nv ? 0.05 : 0) +
      ownershipNudge(u);

    // Ground-attack firepower = real ordnance weight with a big premium for
    // ATGMs (guided tank-killers punch far above their mass).
    const firepower = u => u.ordnanceKg + (u.atgm ? 2500 : 0);

    const fightersOnly = planes.filter(u => u.cls === "fighter");
    const turnPctRaw = percentiler(planes, u => u.turnTime);
    const payPctRaw = percentiler(planes, firepower);
    const climbPctRaw = percentiler(planes, u => u.climbRate);
    const speedPctRaw = percentiler(planes, u => u.maxSpeed);
    const turnQuality = u => 1 - (turnPctRaw(u) ?? 0.7); // lower turn time is better

    // Fighter and attacker scores are intentionally normalized to ~0..1 so the
    // balanced single-slot tiebreak compares like with like (raw attackerScore
    // used to sum higher and almost always win).
    // Fighters: dogfight stats + AAM presence + BVR (ARH) + countermeasures.
    const fighterRaw = u =>
      brScore(u.br[o.mode], o.targetBR) * 1.2 +
      turnQuality(u) * 0.75 +
      (climbPctRaw(u) ?? 0.5) * 0.55 +
      (speedPctRaw(u) ?? 0.5) * 0.35 +
      (u.cls === "fighter" ? 0.7 : 0) +
      (u.aam ? 0.45 : 0) +
      (u.arh ? 0.35 : 0) +
      (u.cm ? 0.2 : 0) +
      ownershipNudge(u);
    // Max theoretical ≈ 1.2+0.75+0.55+0.35+0.7+0.45+0.35+0.2+0.55 ≈ 5.1
    const FIGHTER_NORM = 5.1;
    const fighterScore = u => fighterRaw(u) / FIGHTER_NORM;

    // CAS: payload + ATGM, plus enough flight stats to prefer a jet that can
    // actually deliver over a lumbering bomb truck with the same tonnage.
    const attackerRaw = u => {
      const isBomber = u.cls === "bomber";
      const pay = isBomber ? Math.min(payPctRaw(u) ?? 0, 0.3) : (payPctRaw(u) ?? 0);
      return brScore(u.br[o.mode], o.targetBR) * 1.2 +
        pay * 1.0 +
        (climbPctRaw(u) ?? 0.4) * 0.35 +
        (speedPctRaw(u) ?? 0.4) * 0.3 +
        turnQuality(u) * 0.2 +
        (u.cls === "attacker" ? 0.55 : 0) +
        (u.atgm ? 0.4 : 0) +
        (u.cm ? 0.1 : 0) +
        ownershipNudge(u) -
        (!o.levelBombersCAS && isBomber ? 1.0 : 0);
    };
    // Max theoretical ≈ 1.2+1.0+0.35+0.3+0.2+0.55+0.4+0.1+0.55 ≈ 4.65
    const ATTACKER_NORM = 4.65;
    const attackerScore = u => attackerRaw(u) / ATTACKER_NORM;

    // Helicopters: BR closeness + ATGM standoff range dominate. Ordnance kg used
    // to swamp everything and pick a 9.7 with more rockets over a 10.7 with
    // longer-range ATGMs. Range is now the main capability term.
    const heliPayRaw = percentiler(helis, firepower);
    const heliScore = u =>
      brScore(u.br[o.mode], o.targetBR) * 1.8 +
      (u.atgm ? 0.6 + Math.min((u.atgmRange || 0) / 8000, 0.75) : 0) +
      (heliPayRaw(u) ?? 0) * 0.5 +
      ownershipNudge(u);

    const spaaCalRaw = percentiler(spaas, u => u.aaCal);
    const spaaScore = u => {
      const calScore = (u.aaCal == null || u.aaCal === 0) ? 0.4 : (spaaCalRaw(u) ?? 0.4);
      return brScore(u.br[o.mode], o.targetBR) * 1.4 +
        (u.sam ? 1.2 : 0) + (u.radar ? 0.6 : 0) +
        calScore * 0.6 +
        ownershipNudge(u);
    };

    // CAS candidate pool. Restrict to level/dive bombers when asked.
    const wantBomberCAS = o.levelBombersCAS || o.diveBombersCAS;
    let casPlanes = planes;
    if (wantBomberCAS) {
      const filtered = planes.filter(u =>
        (o.levelBombersCAS && u.cls === "bomber") ||
        (o.diveBombersCAS && u.diveBomber));
      if (filtered.length) casPlanes = filtered;
    }

    // Fighter pool is fighters-only so "Fighter — air superiority" can't hand
    // you a nimble attacker. Fall back to all planes only if the bracket has
    // zero fighters (with a warning).
    const fighterPool = fightersOnly.length ? fightersOnly : planes;
    if (o.planeRole === "fighter" && !fightersOnly.length && planes.length) {
      warnings.push("No fighters in this BR bracket — ranking all aircraft by dogfight stats instead.");
    }

    const pools = {
      ground: rankBy(mains, groundScore),
      spaa: rankBy(spaas, spaaScore),
      fighter: rankBy(fighterPool, fighterScore),
      attacker: rankBy(casPlanes, attackerScore),
      heli: rankBy(helis, heliScore),
    };

    // --- slot allocation ---
    // Plane / SPAA / heli counts: user can override the auto heuristics.
    const parseCount = (v, auto) => {
      if (v == null || v === "" || v === "auto") return auto;
      const n = parseInt(v, 10);
      return Number.isFinite(n) ? Math.max(0, n) : auto;
    };

    // Air role is the master switch: "none" means no aircraft (airCount ignored).
    // airCount is Auto/1/2 only — no separate "0" path that fights the role.
    const wantPlanes = o.planeRole && o.planeRole !== "none" && planes.length;
    const autoAir = wantPlanes ? (o.slots >= 6 ? 2 : 1) : 0;
    const airTotal = wantPlanes ? parseCount(o.airCount, autoAir) : 0;

    let fighterN = 0, attackerN = 0;
    if (wantPlanes && airTotal > 0) {
      if (o.planeRole === "fighter") fighterN = airTotal;
      else if (o.planeRole === "attacker") attackerN = airTotal;
      else { // balanced: split when there's room, else take the stronger option
        if (airTotal >= 2) {
          fighterN = Math.ceil(airTotal / 2);
          attackerN = airTotal - fighterN;
        } else {
          const bf = pools.fighter[0], ba = pools.attacker[0];
          if (bf && ba) (fighterScore(bf) >= attackerScore(ba) ? fighterN = 1 : attackerN = 1);
          else if (bf) fighterN = 1; else if (ba) attackerN = 1;
        }
      }
    }

    // Unified support counts: "off"/"0" = none, "auto" = heuristic, "1"/"2" = fixed.
    // (Replaces the old checkbox + separate count dual-control.)
    const spaaMode = o.spaaCount == null || o.spaaCount === "" ? "auto" : String(o.spaaCount);
    const heliMode = o.heliCount == null || o.heliCount === "" ? "auto" : String(o.heliCount);
    const autoSPAA = (spaas.length && o.slots >= 3)
      ? ((o.slots >= 8 && spaas.length >= 2) ? 2 : 1)
      : 0;
    let spaaN = 0;
    if (spaaMode === "auto") spaaN = autoSPAA;
    else if (spaaMode !== "off" && spaaMode !== "0") {
      spaaN = Math.min(parseCount(spaaMode, 0), spaas.length);
    }

    const autoHeli = (helis.length && o.slots >= 5) ? 1 : 0;
    let heliN = 0;
    if (heliMode === "auto") heliN = autoHeli;
    else if (heliMode !== "off" && heliMode !== "0") {
      heliN = Math.min(parseCount(heliMode, 0), helis.length);
    }

    // Intent flags for the health panel (so "no SPAA" isn't a warning when chosen).
    // Auto SPAA only "wants" SPAA when slots can actually hold it (≥3).
    const wantSPAA = spaaMode === "auto"
      ? (o.slots >= 3)
      : (spaaMode !== "off" && spaaMode !== "0");
    const wantHeli = heliMode === "auto"
      ? (o.slots >= 5)
      : (heliMode !== "off" && heliMode !== "0");
    const wantAir = !!(o.planeRole && o.planeRole !== "none");

    // Always reserve at least two slots (or all, for tiny lineups) for mains.
    const minGround = Math.min(2, o.slots);
    const support = () => fighterN + attackerN + spaaN + heliN;
    const stripped = [];
    while (o.slots - support() < minGround) {
      if (heliN) { heliN--; stripped.push("helicopter"); }
      else if (attackerN) { attackerN--; stripped.push("CAS"); }
      else if (fighterN) { fighterN--; stripped.push("fighter"); }
      else if (spaaN) { spaaN--; stripped.push("SPAA"); }
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
    // Ground uses greedy class-repetition damping only for Balanced. Explicit
    // playstyles (Armor/Speed/Sniper) want mono-class and damping would fight
    // that choice after 2–3 heavies/lights/TDs.
    const takeGround = n => {
      const out = [];
      const clsCount = {};
      const cands = pools.ground.filter(u => !used.has(u.id));
      const useVariety = ps.variety !== false;
      while (out.length < n && cands.length) {
        let best = -1, bestScore = -Infinity;
        for (let i = 0; i < cands.length; i++) {
          const damp = useVariety
            ? Math.max(0.75, 1 - 0.1 * (clsCount[cands[i].cls] || 0))
            : 1;
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

    // Backfill support shortfalls with ground so the lineup is always full.
    const airWanted = fighterN + attackerN + heliN;
    const airGot = slots.filter(s => ["fighter", "attacker", "heli"].includes(s.category)).length;
    if (slots.length < o.slots) {
      slots.push(...takeGround(o.slots - slots.length));
    }

    // --- warnings ---
    if (airGot < airWanted) {
      warnings.push(`Only ${airGot} of ${airWanted} aircraft slot(s) could be filled from this bracket — the rest went to ground vehicles.`);
    }
    const groundGot = slots.filter(s => s.category === "ground").length;
    if (groundGot < groundCount) {
      warnings.push(`Only ${groundGot} ground vehicle(s) available in this BR bracket — try a different BR or enable more vehicle sources.`);
    }
    if (o.planeRole !== "none" && !planes.length) warnings.push("No aircraft available in this BR bracket.");
    if (wantBomberCAS && attackerN > 0 && planes.length && casPlanes === planes) {
      const kinds = [o.levelBombersCAS && "level", o.diveBombersCAS && "dive"].filter(Boolean).join(" or ");
      warnings.push(`No ${kinds} bombers in this BR bracket — using the best available attacker for CAS instead.`);
    }
    if (wantSPAA && !spaas.length) {
      warnings.push("No SPAA available in this BR bracket.");
    }
    if (spaaMode === "auto" && spaas.length && o.slots < 3) {
      warnings.push("SPAA skipped — needs at least 3 crew slots.");
    }
    if (wantHeli && !helis.length) {
      warnings.push("No helicopters available in this BR bracket.");
    }
    if (heliMode === "auto" && helis.length && o.slots < 5) {
      warnings.push("Helicopter skipped — needs at least 5 crew slots.");
    }
    if (stripped.length) {
      const uniq = [...new Set(stripped)];
      warnings.push(
        `Dropped ${uniq.join(", ")} to keep at least ${minGround} ground slot(s) ` +
        `(crew slots too few for the full support request).`
      );
    }

    const health = assess(slots, { ...o, wantSPAA, wantHeli, wantAir });

    return { slots, pools, used, poolSize: pool.length, warnings, health };
  }

  // Fact-based sufficiency check. Everything here follows from War Thunder's
  // matchmaker rules and the actual BRs of the vehicles that got picked.
  function assess(slots, o) {
    // Derive intent from options so post-swap reassess still knows when empty
    // support was deliberate (generate also passes explicit want* flags).
    const spaaMode = o.spaaCount == null || o.spaaCount === "" ? "auto" : String(o.spaaCount);
    const wantSPAA = o.wantSPAA !== undefined
      ? o.wantSPAA
      : (spaaMode === "auto" ? (o.slots == null || o.slots >= 3) : (spaaMode !== "off" && spaaMode !== "0"));
    const wantAir = o.wantAir !== undefined
      ? o.wantAir
      : !!(o.planeRole && o.planeRole !== "none");

    const mode = o.mode;
    const brs = slots.map(s => s.unit.br[mode]).filter(b => b != null);
    if (!brs.length) return null;

    const topBR = Math.max(...brs);
    const avgBR = brs.reduce((a, b) => a + b, 0) / brs.length;
    const groundBrs = slots
      .filter(s => s.category === "ground")
      .map(s => s.unit.br[mode])
      .filter(b => b != null);
    const corePool = groundBrs.length ? groundBrs : brs;
    const core = corePool.filter(b => topBR - b <= 0.3 + 1e-9).length;
    const ballast = corePool.filter(b => topBR - b >= 0.7 - 1e-9).length;
    const coreTotal = corePool.length;
    const hasSPAA = slots.some(s => s.category === "spaa");
    const hasAir = slots.some(s => ["fighter", "attacker", "heli"].includes(s.category));
    const belowTarget = o.targetBR - topBR;

    let verdict;
    if (core >= 3 && ballast <= core) verdict = { key: "strong", label: "Strong lineup" };
    else if (core >= 2) verdict = { key: "solid", label: "Solid lineup" };
    else verdict = { key: "thin", label: "Thin at top BR" };

    const notes = [];
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
      text: `${core} of ${coreTotal} ground vehicle(s) sit within 0.3 of your top BR — these stay competitive in a full uptier. ${core < 2 ? "With fewer than 2, once your top vehicle dies you're spawning into a disadvantage." : "That's enough depth to keep respawning effectively."}`,
    });

    if (ballast > 0) {
      notes.push({
        level: ballast > core ? "warn" : "info",
        text: `${ballast} vehicle(s) are 0.7+ below your top BR — strong in a downtier but outmatched when you're uptiered.${ballast > core ? " More than half your lineup is in this range, so uptiers will be rough." : ""}`,
      });
    }

    // Inspect ALL SPAA slots (not just the first) so a gun SPAA + SAM lineup
    // is credited for the SAM.
    const spaaUnits = slots.filter(s => s.category === "spaa").map(s => s.unit);
    const anySam = spaaUnits.some(u => u.sam);
    const anyRadar = spaaUnits.some(u => u.radar);
    if (anySam) {
      notes.push({ level: "good", text: "SAM SPAA included — guided missiles answer enemy CAS and helicopters at long range." });
    } else if (anyRadar) {
      notes.push({ level: "good", text: "Radar SPAA included — it can track and engage aircraft at range." });
    } else if (hasSPAA) {
      notes.push({ level: "good", text: "SPAA included — you can answer enemy CAS (gun-based, best at shorter range)." });
    } else if (!wantSPAA) {
      notes.push({ level: "info", text: "No SPAA (as requested) — you'll rely on allies or your own air for anti-air." });
    } else {
      notes.push({ level: "warn", text: "No SPAA — you'll be exposed to enemy aircraft with no dedicated answer." });
    }

    if (!hasAir) {
      if (!wantAir) {
        notes.push({ level: "info", text: "No aircraft (as requested)." });
      } else {
        notes.push({ level: "info", text: "No aircraft — no way to contribute from the air or counter enemy planes offensively." });
      }
    }

    return { topBR, avgBR, targetBR: o.targetBR, core, ballast, total: coreTotal, hasSPAA, hasAir, verdict, notes };
  }

  return { generate, assess, BR_WINDOW, PLAYSTYLES, SPADED };
})();
