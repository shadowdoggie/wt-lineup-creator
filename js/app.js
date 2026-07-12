"use strict";

/* UI wiring: loads data, keeps the BR dropdown in sync with nation + mode,
 * and renders generated lineups. */
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

  const state = { units: [], mode: "realistic", fetchedAt: null };

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
    const age = Date.now() - res.fetchedAt;
    const hours = Math.round(age / 3600000);
    const when = hours < 1 ? "just now" : hours < 24 ? `${hours} h ago` : new Date(res.fetchedAt).toLocaleDateString();
    $("dataStatusText").innerHTML =
      `${state.units.length.toLocaleString()} vehicles · data fetched <span class="${res.stale ? "" : "fresh"}">${when}</span>${res.stale ? " (offline — using old cache)" : ""}`;
  }

  /* ---------- form behavior ---------- */

  function nationId() { return $("nation").value; }

  function populateNations() {
    $("nation").innerHTML = WT_DATA.NATIONS
      .map(([id, label]) => `<option value="${id}">${label}</option>`)
      .join("");
  }

  // The BR dropdown only offers BRs that actually exist for the selected
  // nation's ground vehicles in the selected mode.
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
      incPlanes: $("incPlanes").checked,
      incHelis: $("incHelis").checked,
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

  function srcBadges(u) {
    let out = "";
    if (u.premium) out += `<span class="src-badge" title="Premium vehicle">⭐</span>`;
    if (u.squadron) out += `<span class="src-badge" title="Squadron vehicle">🤝</span>`;
    if (u.gift) out += `<span class="src-badge" title="Event / gift vehicle">🎁</span>`;
    return out;
  }

  function armorLine(u) {
    if (u.type !== "tank" || (u.armorHull == null && u.armorTurret == null)) return "";
    const parts = [];
    if (u.armorHull != null) parts.push(`Hull ${u.armorHull} mm`);
    if (u.armorTurret != null) parts.push(`Turret ${u.armorTurret} mm`);
    return `<span>${parts.join(" · ")}</span>`;
  }

  function slotCard(u, i, mode) {
    const meta = CLS_META[u.cls] || { label: u.cls.toUpperCase(), color: "var(--text-dim)" };
    return `
      <div class="slot-card" style="--cls-color:${meta.color}">
        <div class="slot-head">
          <span class="slot-num">SLOT ${i + 1}</span>
          <span class="cls-badge">${meta.label}</span>
        </div>
        <div class="veh-name">${esc(u.name)} ${srcBadges(u)}</div>
        <div class="veh-meta">
          <span class="br-chip">${u.br[mode].toFixed(1)}</span>
          <span>Rank ${u.rank}</span>
          ${armorLine(u)}
        </div>
      </div>`;
  }

  function altChip(u, mode) {
    const meta = CLS_META[u.cls] || { color: "var(--text-dim)" };
    return `
      <span class="alt-chip" style="--cls-color:${meta.color}">
        <span class="br-chip">${u.br[mode].toFixed(1)}</span>
        ${esc(u.name)} ${srcBadges(u)}
      </span>`;
  }

  function renderResult(r, o) {
    const nationLabel = WT_DATA.NATIONS.find(n => n[0] === o.nation)?.[1] || o.nation;
    const modeLabel = { arcade: "Arcade", realistic: "Realistic", simulator: "Simulator" }[o.mode];
    const altSection = (title, list) => list.length ? `
      <div class="alt-section">
        <h2>${title}</h2>
        <div class="alt-chips">${list.map(u => altChip(u, o.mode)).join("")}</div>
      </div>` : "";

    $("results").innerHTML = `
      ${r.warnings.length ? `<div class="warnings">${r.warnings.map(w => `<div class="warning">⚠️ ${w}</div>`).join("")}</div>` : ""}
      <h2>${nationLabel} · BR ${(o.targetBR - LINEUP.BR_WINDOW).toFixed(1)}–${o.targetBR.toFixed(1)}
        <span class="sub">· ${modeLabel} · ${r.slots.length} vehicles</span></h2>
      <div class="lineup-grid">${r.slots.map((u, i) => slotCard(u, i, o.mode)).join("")}</div>
      ${altSection("Ground alternatives", r.alternatives.ground)}
      ${altSection("SPAA alternatives", r.alternatives.spaa)}
      ${altSection("Air alternatives", r.alternatives.air)}
      <p class="pool-note">${r.poolSize} vehicles matched your filters in this BR bracket. Same settings can give different picks — hit Generate again to reroll ties.</p>`;
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

    $("refreshBtn").addEventListener("click", () => loadData(true));

    loadData(false);
  }

  init();
})();
