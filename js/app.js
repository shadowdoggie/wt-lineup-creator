"use strict";

/* UI wiring: loads data, keeps the BR dropdown in sync with nation + mode,
 * renders generated lineups, and handles per-slot vehicle swaps. */
(() => {
  const $ = id => document.getElementById(id);

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

  const state = { units: [], mode: "realistic", fetchedAt: null };
  let current = null; // { result, options } — kept so swaps can mutate the lineup

  /* ---------- data loading ---------- */

  async function loadData(force) {
    const overlay = $("loadingOverlay");
    const errEl = $("loadError");
    overlay.hidden = false;
    errEl.hidden = true;
    for (const k of ["wpcost", "unittags", "names"]) $("step-" + k).classList.remove("done");
    try {
      const res = await WT_DATA.load({
        force,
        onStep: k => $("step-" + k).classList.add("done"),
      });
      state.units = res.units;
      state.fetchedAt = res.fetchedAt;
      overlay.hidden = true;
      $("generateBtn").disabled = false;
      renderDataStatus(res);
      refreshBROptions();
    } catch (err) {
      errEl.textContent = "Could not download game data: " + err.message +
        " — check your internet connection, then hit Refresh.";
      errEl.hidden = false;
      $("dataStatusText").textContent = "No data";
    }
  }

  function renderDataStatus(res) {
    const n = state.units.length.toLocaleString();
    let status;
    if (res.stale) {
      status = `cached ${new Date(res.fetchedAt).toLocaleDateString()} (couldn't reach the mirror)`;
    } else if (res.upToDate) {
      const d = res.gameDataDate ? new Date(res.gameDataDate).toLocaleDateString() : null;
      status = `<span class="fresh">✔ up to date with the game files</span>${d ? ` (last change ${d})` : ""}`;
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
    const prev = parseFloat(sel.value);
    const brs = new Set();
    for (const u of state.units) {
      if (u.country === nationId() && u.type === "tank" && u.br[state.mode] != null) {
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
    return {
      nation: nationId(),
      mode: state.mode,
      targetBR: parseFloat($("targetBR").value),
      slots: Math.max(1, Math.min(12, parseInt($("slots").value, 10) || 5)),
      incSPAA: $("incSPAA").checked,
      incHelis: $("incHelis").checked,
      planeRole: $("planeRole").value,
      incPremium: $("incPremium").checked,
      incSquadron: $("incSquadron").checked,
      incGift: $("incGift").checked,
      playstyle: document.querySelector('input[name="playstyle"]:checked').value,
    };
  }

  /* ---------- rendering ---------- */

  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
  }

  function badgeFor(slot) {
    return CATEGORY_BADGE[slot.category] || CLS_META[slot.unit.cls] ||
      { label: slot.unit.cls.toUpperCase(), color: "var(--text-dim)" };
  }

  function srcBadges(u) {
    let out = "";
    if (u.premium) out += `<span class="src-badge" title="Premium vehicle">⭐</span>`;
    if (u.squadron) out += `<span class="src-badge" title="Squadron vehicle">🤝</span>`;
    if (u.gift) out += `<span class="src-badge" title="Event / gift vehicle">🎁</span>`;
    return out;
  }

  // Role-relevant stat line: hp/ton + armor for ground, turn time for
  // fighters, payload for CAS.
  function metaBits(slot, mode) {
    const u = slot.unit;
    const bits = [`<span class="br-chip">${u.br[mode].toFixed(1)}</span>`, `<span>Rank ${u.rank}</span>`];
    if (u.type === "tank" && slot.category !== "spaa") {
      if (u.hpPerTon != null) bits.push(`<span title="Real horsepower-per-ton">${u.hpPerTon} hp/t</span>`);
      const armor = [];
      if (u.armorHull != null) armor.push(`H ${u.armorHull}`);
      if (u.armorTurret != null) armor.push(`T ${u.armorTurret}`);
      if (armor.length) bits.push(`<span title="Frontal armor (mm)">🛡️ ${armor.join(" / ")}</span>`);
      if (u.gunVel != null) bits.push(`<span title="Fastest AP shell muzzle velocity${u.gunCal ? ` · ${u.gunCal}mm bore` : ""}">🎯 ${u.gunVel} m/s</span>`);
    } else if (slot.category === "fighter") {
      if (u.turnTime != null) bits.push(`<span title="Sustained turn time">↻ ${u.turnTime}s turn</span>`);
    } else if (slot.category === "attacker") {
      bits.push(u.payload > 0
        ? `<span title="Bombs + rockets carried">💣 ${u.payload} ordnance</span>`
        : `<span>guns only</span>`);
    }
    return bits.join(" ");
  }

  function slotCard(slot, i, mode, result) {
    const meta = badgeFor(slot);
    const pool = result.pools[slot.category] || [];
    const alts = pool.filter(u => !result.used.has(u.id)).length; // others not in the lineup
    return `
      <div class="slot-card" style="--cls-color:${meta.color}">
        <div class="slot-head">
          <span class="slot-num">SLOT ${i + 1}</span>
          <span class="cls-badge">${meta.label}</span>
        </div>
        <div class="veh-name">${esc(slot.unit.name)} ${srcBadges(slot.unit)}</div>
        <div class="veh-meta">${metaBits(slot, mode)}</div>
        <button class="swap-btn" data-slot="${i}" ${alts ? "" : "disabled"}
          title="Swap for the next-best ${meta.label.toLowerCase()} (respects your playstyle)">
          ⟳ Swap${alts ? ` <span class="alt-count">${alts} more</span>` : " (none left)"}
        </button>
      </div>`;
  }

  // Fact-based "is this lineup good enough?" panel. Numbers come straight from
  // LINEUP.assess (top BR, competitive-respawn count, etc.).
  function healthPanel(h) {
    if (!h) return "";
    const NOTE_ICON = { good: "✅", info: "ℹ️", warn: "⚠️" };
    const stat = (label, val) => `<div class="hc-stat"><span class="hc-val">${val}</span><span class="hc-lbl">${label}</span></div>`;
    return `
      <div class="health-card hc-${h.verdict.key}">
        <div class="hc-head">
          <span class="hc-verdict">${h.verdict.label}</span>
          <div class="hc-stats">
            ${stat("queue BR", h.topBR.toFixed(1))}
            ${stat("competitive respawns", `${h.core}/${h.total}`)}
            ${stat("avg BR", h.avgBR.toFixed(1))}
          </div>
        </div>
        <ul class="hc-notes">
          ${h.notes.map(n => `<li class="hc-${n.level}">${NOTE_ICON[n.level] || "•"} ${esc(n.text)}</li>`).join("")}
        </ul>
      </div>`;
  }

  function renderResult(result, o) {
    current = { result, options: o };
    const nationLabel = WT_DATA.NATIONS.find(n => n[0] === o.nation)?.[1] || o.nation;
    const modeLabel = { arcade: "Arcade", realistic: "Realistic", simulator: "Simulator" }[o.mode];
    $("results").innerHTML = `
      ${result.warnings.length ? `<div class="warnings">${result.warnings.map(w => `<div class="warning">⚠️ ${w}</div>`).join("")}</div>` : ""}
      <h2>${nationLabel} · BR ${(o.targetBR - LINEUP.BR_WINDOW).toFixed(1)}–${o.targetBR.toFixed(1)}
        <span class="sub">· ${modeLabel} · ${result.slots.length} vehicles</span></h2>
      ${healthPanel(result.health)}
      <div class="lineup-grid">${result.slots.map((s, i) => slotCard(s, i, o.mode, result)).join("")}</div>
      <p class="pool-note">${result.poolSize} vehicles matched your filters in this bracket.
        Use <strong>⟳ Swap</strong> on any slot to cycle to the next-best pick of that role — handy for
        swapping a premium you don't own for one you do. Hit Generate to reroll from scratch.</p>`;
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
      renderResult(result, options); // re-render so every slot's "N more" stays correct
      return;
    }
  }

  /* ---------- events ---------- */

  function init() {
    populateNations();

    $("modeSeg").addEventListener("click", e => {
      const btn = e.target.closest("button[data-mode]");
      if (!btn) return;
      state.mode = btn.dataset.mode;
      for (const b of $("modeSeg").children) b.classList.toggle("active", b === btn);
      refreshBROptions();
    });

    $("nation").addEventListener("change", refreshBROptions);

    $("lineupForm").addEventListener("submit", e => {
      e.preventDefault();
      const o = currentOptions();
      if (Number.isNaN(o.targetBR)) return;
      renderResult(LINEUP.generate(state.units, o), o);
    });

    // Delegated: swap buttons are re-rendered on every update.
    $("results").addEventListener("click", e => {
      const btn = e.target.closest(".swap-btn");
      if (btn && !btn.disabled) swapSlot(parseInt(btn.dataset.slot, 10));
    });

    $("refreshBtn").addEventListener("click", () => loadData(true));

    loadData(false);
  }

  init();
})();
