"use strict";

/* UI wiring: loads data, keeps the BR dropdown in sync with nation + mode,
 * renders generated lineups, and handles per-slot vehicle swaps. */
(() => {
  const $ = id => document.getElementById(id);
  // Inline SVG icon from the sprite in index.html.
  const ico = id => `<svg class="ico" aria-hidden="true"><use href="#${id}"/></svg>`;

  const CLS_META = {
    light:    { label: "LIGHT TANK", color: "var(--c-light)" },
    medium:   { label: "MEDIUM TANK", color: "var(--c-medium)" },
    heavy:    { label: "HEAVY TANK", color: "var(--c-heavy)" },
    td:       { label: "TANK DESTROYER", color: "var(--c-td)" },
    spaa:     { label: "SPAA", color: "var(--c-spaa)" },
    fighter:  { label: "FIGHTER", color: "var(--c-fighter)" },
    attacker: { label: "CAS / STRIKE", color: "var(--c-attacker)" },
    bomber:   { label: "BOMBER", color: "var(--c-bomber)" },
    heli:     { label: "HELICOPTER", color: "var(--c-heli)" },
  };
  // Plane slots badge by their assigned role; ground/SPAA by vehicle class.
  const CATEGORY_BADGE = {
    fighter:  { label: "FIGHTER", color: "var(--c-fighter)" },
    attacker: { label: "CAS / STRIKE", color: "var(--c-attacker)" },
    heli:     CLS_META.heli,
  };

  const state = { units: [], mode: "realistic", fetchedAt: null, desiredBR: null };
  let current = null; // { result, options } — kept so swaps can mutate the lineup
  let loading = false; // guards against overlapping downloads (e.g. Refresh spam)

  // Form state is remembered across page reloads (see save/loadPrefs). Bump the
  // key if the set of saved fields changes in an incompatible way.
  const PREFS_KEY = "wtlc_prefs_v5";

  /* ---------- data loading ---------- */

  async function loadData(force) {
    // Ignore clicks (e.g. Refresh spam) while a download is already in flight —
    // otherwise every click kicks off another concurrent fetch of the same data.
    if (loading) return;
    loading = true;
    const refreshBtn = $("refreshBtn");
    refreshBtn.disabled = true;
    const overlay = $("loadingOverlay");
    const errEl = $("loadError");
    overlay.hidden = false;
    // Reset to the "downloading" state (a previous attempt may have left the
    // overlay in its error state with the retry button showing).
    errEl.hidden = true;
    $("retryBtn").hidden = true;
    $("loadSpinner").hidden = false;
    $("loadSteps").hidden = false;
    $("loadHint").hidden = false;
    $("loadTitle").textContent = "Downloading game data";
    for (const k of ["wpcost", "unittags", "names", "shop"]) $("step-" + k)?.classList.remove("done");
    try {
      const res = await WT_DATA.load({
        force,
        onStep: k => { const el = $("step-" + k); if (el) el.classList.add("done"); },
      });
      state.units = res.units;
      state.fetchedAt = res.fetchedAt;
      overlay.hidden = true;
      $("generateBtn").disabled = false;
      renderDataStatus(res);
      renderDataWarnings(res.dataWarnings);
      refreshBROptions();
    } catch (err) {
      // Put the overlay into an actionable error state: stop the spinner, hide
      // the step list, and surface a Retry button *inside* the overlay (the
      // header Refresh sits underneath it and can't be clicked).
      $("loadSpinner").hidden = true;
      $("loadSteps").hidden = true;
      $("loadHint").hidden = true;
      $("loadTitle").textContent = "Download failed";
      errEl.textContent = "Could not download game data: " + err.message +
        " — check your internet connection, then retry.";
      errEl.hidden = false;
      $("retryBtn").hidden = false;
      $("dataStatusText").textContent = "No data";
    } finally {
      loading = false;
      refreshBtn.disabled = false;
    }
  }

  // Loud banner for data-integrity problems (see WT_DATA.sanityCheck): if the
  // datamine format shifts under us, show it instead of silently building a
  // broken lineup. Empty in the normal case, so it stays out of the way.
  function renderDataWarnings(warnings) {
    const el = $("dataWarnings");
    if (!warnings || !warnings.length) { el.hidden = true; el.innerHTML = ""; return; }
    el.innerHTML = `<strong>${ico("i-warn")} Data check:</strong> ${warnings.map(esc).join(" ")}`;
    el.hidden = false;
  }

  function renderDataStatus(res) {
    const n = state.units.length.toLocaleString();
    let status;
    if (res.stale) {
      status = `cached ${new Date(res.fetchedAt).toLocaleDateString()} (couldn't reach the mirror)`;
    } else if (res.upToDate) {
      const d = res.gameDataDate ? new Date(res.gameDataDate).toLocaleDateString() : null;
      status = `<span class="fresh">${ico("i-check")} up to date with the game files</span>${d ? ` (last change ${d})` : ""}`;
    } else {
      status = `fetched ${new Date(res.fetchedAt).toLocaleDateString()}`;
    }
    $("dataStatusText").innerHTML = `${n} vehicles · ${status}`;
  }

  /* ---------- form behavior ---------- */

  function nationId() { return $("nation").value; }

  function populateNations() {
    $("nation").innerHTML = WT_DATA.NATIONS
      .map(([id, label]) => `<option value="${id}">${label}</option>`)
      .join("");
  }

  // The BR dropdown only offers BRs that exist for this nation's ground
  // vehicles in the selected mode.
  function refreshBROptions() {
    const sel = $("targetBR");
    // A saved BR (from a previous session) takes precedence on first build, then
    // is consumed; after that we just preserve the current selection.
    const prev = state.desiredBR != null ? state.desiredBR : parseFloat(sel.value);
    state.desiredBR = null;
    // Only main ground vehicles set the target — SPAA are type "tank" too, so a
    // BR that exists only for an SPAA would otherwise be selectable and build a
    // lineup with no actual tanks.
    const brs = new Set();
    for (const u of state.units) {
      if (u.country === nationId() && u.type === "tank" && u.cls !== "spaa" && u.br[state.mode] != null) {
        brs.add(u.br[state.mode]);
      }
    }
    const list = [...brs].sort((a, b) => a - b);
    sel.innerHTML = list.map(br => `<option value="${br}">${br.toFixed(1)}</option>`).join("");
    if (list.length) {
      const keep = list.includes(prev) ? prev : list[list.length - 1];
      sel.value = String(keep);
    }
  }

  function currentOptions() {
    // The CAS-restriction dropdown maps to the two booleans lineup.js consumes:
    // "both" = level OR dive bombers only, "any" = no restriction.
    const casType = $("casType").value;
    return {
      nation: nationId(),
      mode: state.mode,
      targetBR: parseFloat($("targetBR").value),
      slots: Math.max(1, Math.min(12, parseInt($("slots").value, 10) || 5)),
      planeRole: $("planeRole").value,
      airCount: $("airCount")?.value || "auto",
      spaaCount: $("spaaCount")?.value || "auto",
      heliCount: $("heliCount")?.value || "0",
      levelBombersCAS: casType === "level" || casType === "both",
      diveBombersCAS: casType === "dive" || casType === "both",
      incPremium: $("incPremium").checked,
      incSquadron: $("incSquadron").checked,
      incGift: $("incGift").checked,
      playstyle: document.querySelector('input[name="playstyle"]:checked')?.value || "balanced",
    };
  }

  function tryGenerate() {
    if (!state.units.length) return;
    const o = currentOptions();
    if (Number.isNaN(o.targetBR)) return;
    renderResult(LINEUP.generate(state.units, o), o);
  }

  /* ---------- preference persistence ---------- */

  // The bomber CAS options only matter when a CAS slot exists, so hide them for
  // fighter-only / no-aircraft roles to avoid a confusing dead option.
  function updateBomberVis() {
    const role = $("planeRole").value;
    $("bombersOnlyWrap").hidden = !(role === "attacker" || role === "balanced");
    const wrap = $("airCountWrap");
    if (wrap) wrap.hidden = role === "none";
  }

  // Persist the whole form to localStorage so a page reload keeps your setup
  // instead of snapping back to defaults.
  function savePrefs() {
    const p = {
      nation: $("nation").value,
      mode: state.mode,
      targetBR: $("targetBR").value,
      slots: $("slots").value,
      planeRole: $("planeRole").value,
      airCount: $("airCount")?.value || "auto",
      spaaCount: $("spaaCount")?.value || "auto",
      heliCount: $("heliCount")?.value || "0",
      casType: $("casType").value,
      incPremium: $("incPremium").checked,
      incSquadron: $("incSquadron").checked,
      incGift: $("incGift").checked,
      playstyle: document.querySelector('input[name="playstyle"]:checked')?.value,
    };
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(p)); } catch { /* quota — non-critical */ }
  }

  function loadPrefs() {
    try {
      const raw = localStorage.getItem(PREFS_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch { return null; }
  }

  // Apply a saved snapshot to the controls. Called before data loads so the BR
  // list builds for the restored nation/mode; the actual BR value is deferred to
  // refreshBROptions via state.desiredBR (its options don't exist yet).
  function applyPrefs(p) {
    if (!p) return;
    const setChk = (id, v) => { if (typeof v === "boolean") $(id).checked = v; };
    if (p.nation) $("nation").value = p.nation;
    if (p.mode) {
      state.mode = p.mode;
      for (const b of $("modeSeg").children) b.classList.toggle("active", b.dataset.mode === p.mode);
    }
    if (p.slots != null && p.slots !== "") $("slots").value = p.slots;
    if (p.planeRole) $("planeRole").value = p.planeRole;
    // Migrate old airCount "0" → planeRole none.
    if (p.airCount === "0" || p.airCount === 0) {
      if ($("planeRole")) $("planeRole").value = "none";
      if ($("airCount")) $("airCount").value = "auto";
    } else if (p.airCount && $("airCount") && p.airCount !== "0") {
      $("airCount").value = String(p.airCount);
    }
    // Unified SPAA/heli selects. Migrate old checkbox prefs.
    if (p.spaaCount != null && $("spaaCount")) $("spaaCount").value = String(p.spaaCount);
    else if (typeof p.incSPAA === "boolean" && $("spaaCount")) {
      $("spaaCount").value = p.incSPAA ? "auto" : "0";
    }
    if (p.heliCount != null && $("heliCount")) $("heliCount").value = String(p.heliCount);
    else if (typeof p.incHelis === "boolean" && $("heliCount")) {
      $("heliCount").value = p.incHelis ? "auto" : "0";
    }
    // Prefer the new dropdown value; migrate old level/dive checkbox prefs so a
    // returning user keeps their bomber-CAS choice.
    if (p.casType) $("casType").value = p.casType;
    else if (p.levelBombersCAS || p.diveBombersCAS) {
      $("casType").value = (p.levelBombersCAS && p.diveBombersCAS) ? "both"
        : p.diveBombersCAS ? "dive" : "level";
    }
    setChk("incPremium", p.incPremium);
    setChk("incSquadron", p.incSquadron);
    setChk("incGift", p.incGift);
    if (p.playstyle) {
      const r = document.querySelector(`input[name="playstyle"][value="${p.playstyle}"]`);
      if (r) r.checked = true;
    }
    const br = parseFloat(p.targetBR);
    if (!Number.isNaN(br)) state.desiredBR = br;
  }

  /* ---------- rendering ---------- */

  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function badgeFor(slot) {
    return CATEGORY_BADGE[slot.category] || CLS_META[slot.unit.cls] ||
      { label: slot.unit.cls.toUpperCase(), color: "var(--text-dim)" };
  }

  function srcBadges(u) {
    let out = "";
    if (u.premium) out += `<span class="src-badge src-premium" title="Premium vehicle">${ico("i-star")}</span>`;
    if (u.squadron) out += `<span class="src-badge src-squadron" title="Squadron vehicle">${ico("i-users")}</span>`;
    if (u.gift) out += `<span class="src-badge src-gift" title="Event / gift vehicle">${ico("i-gift")}</span>`;
    return out;
  }

  // ATGM badge for a CAS jet or attack heli, with standoff range when known.
  function atgmBadge(u) {
    const km = u.atgmRange ? ` ~${(u.atgmRange / 1000).toFixed(u.atgmRange < 10000 ? 1 : 0)}km` : "";
    return `<span class="stat" title="Carries anti-ground guided missiles${km ? " · standoff range" : ""}">${ico("i-missile")} ATGM${km}</span>`;
  }

  // Role-relevant stat line: hp/ton + armor + gun for ground, radar/SAM/caliber
  // for SPAA, turn time + climb for fighters, ordnance + ATGMs for CAS and helis.
  function metaBits(slot, mode) {
    const u = slot.unit;
    const stat = (title, inner) => `<span class="stat"${title ? ` title="${title}"` : ""}>${inner}</span>`;
    const bits = [`<span class="br-chip">${u.br[mode].toFixed(1)}</span>`, stat("", `Rank ${u.rank}`)];
    if (u.type === "tank" && slot.category !== "spaa") {
      if (u.hpPerTon != null) bits.push(stat("Real horsepower-per-ton", `${ico("i-gauge")} ${u.hpPerTon} hp/t`));
      const armor = [];
      // Truthy (not `!= null`): open-top vehicles with 0 mm omit the shield line.
      if (u.armorHull) armor.push(`H ${u.armorHull}`);
      if (u.armorTurret) armor.push(`T ${u.armorTurret}`);
      if (armor.length) {
        bits.push(stat("Frontal steel thickness (mm) from the tank model", `${ico("i-shield")} ${armor.join(" / ")}`));
      }
      if (u.hasEra) bits.push(stat("ERA present on the model (spaded packs included)", `${ico("i-shield")} ERA`));
      if (u.hasComposite) bits.push(stat("Composite / NERA arrays present on the model", `${ico("i-shield")} Composite`));
      if (u.stabilized) bits.push(stat("Gun stabilizer — can shoot on the move", `${ico("i-bolt")} Stab`));
      if (u.thermal) bits.push(stat("Thermal imaging (researchable upgrades counted)", `${ico("i-scope")} Thermal`));
      else if (u.nv) bits.push(stat("Night vision", `${ico("i-scope")} NV`));
      if (u.reloadTime != null) {
        const auto = u.reloadTime <= 6;
        bits.push(stat(auto ? "Stock autoloader cycle (not crew-trained)" : "Stock manual reload (not crew-trained)",
          `${ico("i-refresh")} ${u.reloadTime}s${auto ? " auto" : ""}`));
      }
      if (u.crewCount != null) bits.push(stat("Crew count", `${ico("i-users")} ${u.crewCount}`));
      // Gun velocity/caliber only on the card — no pen mm (table or estimate).
      // Pen still influences Sniper/uptier ranking behind the scenes.
      if (u.gunVel != null) {
        const cal = u.gunCal ? ` · ${u.gunCal}mm` : "";
        bits.push(stat(`Best equippable AP shell muzzle velocity${u.gunCal ? ` · ${u.gunCal}mm bore` : ""} (spaded)`,
          `${ico("i-target")} ${u.gunVel} m/s${cal}`));
      }
    } else if (slot.category === "spaa") {
      if (u.sam) bits.push(stat("Carries surface-to-air missiles", `${ico("i-missile")} SAM`));
      if (u.radar) bits.push(stat("Has a tracking radar", `${ico("i-radar")} Radar`));
      if (u.aaCal) bits.push(stat("Main gun caliber", `${ico("i-target")} ${u.aaCal}mm`));
    } else if (slot.category === "fighter") {
      if (u.turnTime != null) bits.push(stat("Sustained turn time", `${ico("i-turn")} ${u.turnTime}s turn`));
      if (u.climbRate != null) bits.push(stat("Rate of climb", `${ico("i-bolt")} ${u.climbRate} m/s climb`));
      if (u.maxSpeed != null) bits.push(stat("Max speed (shop)", `${ico("i-gauge")} ${u.maxSpeed}`));
      if (u.aam) bits.push(stat(u.arh ? "Air-to-air missiles incl. radar-homing (BVR-class)" : "Air-to-air missiles",
        `${ico("i-missile")} ${u.arh ? "AAM/BVR" : "AAM"}`));
      if (u.cm) bits.push(stat("Countermeasures (flares/chaff)", `${ico("i-radar")} CM`));
    } else if (slot.category === "attacker" || slot.category === "heli") {
      if (u.atgm) bits.push(atgmBadge(u));
      if (u.ordnanceKg > 0) bits.push(stat("Best same-preset bomb + rocket ordnance (guided bombs weighted double)", `${ico("i-bomb")} ${u.ordnanceKg.toLocaleString()} kg`));
      if (u.climbRate != null && slot.category === "attacker") bits.push(stat("Rate of climb", `${ico("i-bolt")} ${u.climbRate} m/s`));
      if (!u.atgm && u.ordnanceKg === 0) bits.push(stat("", "Guns only"));
    }
    return bits.join(" ");
  }

  function needsSpadedNote(u) {
    // Only where unlockables affect displayed/scored facts.
    if (u.type === "aircraft" || u.type === "helicopter") return true;
    if (u.type !== "tank") return false;
    if (u.thermal || u.hasEra || u.hasComposite) return true;
    if (u.gunPen != null && u.rank >= 4) return true;
    return false;
  }

  function slotCard(slot, i, mode, result) {
    const meta = badgeFor(slot);
    const pool = result.pools[slot.category] || [];
    const alts = pool.filter(u => !result.used.has(u.id)).length;
    const sp = LINEUP.SPADED || {};
    const spadedChip = needsSpadedNote(slot.unit)
      ? `<span class="spaded-chip" title="${esc(sp.title || "")}">${ico("i-info")} ${esc(sp.short || "Spaded")}</span>`
      : "";
    return `
      <div class="slot-card" style="--cls-color:${meta.color}">
        <div class="slot-head">
          <span class="slot-num">SLOT ${String(i + 1).padStart(2, "0")}</span>
          <span class="cls-badge">${meta.label}</span>
        </div>
        <div class="veh-name">${esc(slot.unit.name)} ${srcBadges(slot.unit)} ${spadedChip}</div>
        <div class="veh-meta">${metaBits(slot, mode)}</div>
        <button type="button" class="swap-btn" data-slot="${i}" ${alts ? "" : "disabled"}
          title="Swap for the next-best ${meta.label.toLowerCase()} (respects your playstyle)">
          ${ico("i-swap")} Swap${alts ? ` <span class="alt-count">${alts} more</span>` : ` <span class="alt-count">none left</span>`}
        </button>
      </div>`;
  }

  // Fact-based "is this lineup good enough?" panel. Numbers come straight from
  // LINEUP.assess (top BR, competitive-respawn count, etc.).
  function healthPanel(h) {
    if (!h) return "";
    const NOTE_ICON = { good: ico("i-check"), info: ico("i-info"), warn: ico("i-warn") };
    const VERDICT_ICON = { strong: ico("i-check"), solid: ico("i-shield"), thin: ico("i-warn") };
    const stat = (label, val) => `<div class="hc-stat"><span class="hc-val">${val}</span><span class="hc-lbl">${label}</span></div>`;
    return `
      <div class="health-card hc-${h.verdict.key}">
        <div class="hc-head">
          <span class="hc-verdict">${VERDICT_ICON[h.verdict.key] || ""} ${h.verdict.label}</span>
          <div class="hc-stats">
            ${stat("queue BR", h.topBR.toFixed(1))}
            ${stat("competitive respawns", `${h.core}/${h.total}`)}
            ${stat("avg BR", h.avgBR.toFixed(1))}
          </div>
        </div>
        <ul class="hc-notes">
          ${h.notes.map(n => `<li class="hc-${n.level}">${NOTE_ICON[n.level] || ""} <span>${esc(n.text)}</span></li>`).join("")}
        </ul>
      </div>`;
  }

  function renderResult(result, o) {
    current = { result, options: o };
    const nationLabel = WT_DATA.NATIONS.find(n => n[0] === o.nation)?.[1] || o.nation;
    const modeLabel = { arcade: "Arcade", realistic: "Realistic", simulator: "Simulator" }[o.mode];
    $("results").innerHTML = `
      ${result.warnings.length ? `<div class="warnings">${result.warnings.map(w => `<div class="warning">${ico("i-warn")} <span>${w}</span></div>`).join("")}</div>` : ""}
      <div class="results-head">
        <h2>${nationLabel}</h2>
        <div class="results-tags">
          <span class="tag tag-br">BR ${(o.targetBR - LINEUP.BR_WINDOW).toFixed(1)}–${o.targetBR.toFixed(1)}</span>
          <span class="tag">${modeLabel}</span>
          <span class="tag">${result.slots.length} vehicles</span>
        </div>
      </div>
      ${healthPanel(result.health)}
      <div class="lineup-grid">${result.slots.map((s, i) => slotCard(s, i, o.mode, result)).join("")}</div>
      <p class="pool-note spaded-note" title="${esc(LINEUP.SPADED?.title || "")}">${ico("i-info")} ${esc(LINEUP.SPADED?.note || "")}</p>
      <p class="pool-note">${result.poolSize} vehicles matched your filters in this bracket.
        Use <strong>Swap</strong> to cycle candidates of that role. Same settings always rebuild the same lineup.</p>`;
  }

  // Advance one slot to the next available candidate of its role, in ranked
  // order (wrapping), skipping vehicles already in the lineup.
  function swapSlot(i) {
    if (!current) return;
    const { result, options } = current;
    const slot = result.slots[i];
    const pool = result.pools[slot.category] || [];
    if (pool.length < 2) return;
    const curIdx = pool.findIndex(u => u.id === slot.unit.id);
    for (let step = 1; step <= pool.length; step++) {
      const cand = pool[(curIdx + step) % pool.length];
      if (!cand || cand.id === slot.unit.id || result.used.has(cand.id)) continue;
      result.used.delete(slot.unit.id);
      result.used.add(cand.id);
      slot.unit = cand;
      // Recompute health — queue BR, competitive respawns and avg BR all depend
      // on which vehicles are in the lineup, so a swap can change them.
      result.health = LINEUP.assess(result.slots, options);
      renderResult(result, options); // re-render so every slot's "N more" stays correct
      return;
    }
  }

  /* ---------- events ---------- */

  function init() {
    populateNations();
    // Restore the saved form state before wiring events / loading data, so the
    // BR dropdown is built for the remembered nation + mode.
    applyPrefs(loadPrefs());
    updateBomberVis();

    $("modeSeg").addEventListener("click", e => {
      const btn = e.target.closest("button[data-mode]");
      if (!btn) return;
      state.mode = btn.dataset.mode;
      for (const b of $("modeSeg").children) b.classList.toggle("active", b === btn);
      refreshBROptions();
      savePrefs();
      if (current) tryGenerate();
    });

    $("nation").addEventListener("change", () => {
      refreshBROptions();
      if (current) tryGenerate();
    });
    $("planeRole").addEventListener("change", updateBomberVis);
    $("targetBR").addEventListener("change", () => { if (current) tryGenerate(); });

    // Remember every control change across reloads; rebuild if a lineup is showing.
    $("lineupForm").addEventListener("change", e => {
      savePrefs();
      // Don't auto-regen on every keystroke of slots while typing; change is fine.
      if (current && e.target && e.target.id !== "slots") tryGenerate();
    });
    $("lineupForm").addEventListener("input", savePrefs);
    $("slots").addEventListener("change", () => { if (current) tryGenerate(); });

    $("lineupForm").addEventListener("submit", e => {
      e.preventDefault();
      tryGenerate();
    });

    // Delegated: swap buttons are re-rendered on every update.
    $("results").addEventListener("click", e => {
      const btn = e.target.closest(".swap-btn");
      if (btn && !btn.disabled) swapSlot(parseInt(btn.dataset.slot, 10));
    });

    $("refreshBtn").addEventListener("click", () => loadData(true));
    $("retryBtn").addEventListener("click", () => loadData(true));

    loadData(false);
  }

  init();
})();
