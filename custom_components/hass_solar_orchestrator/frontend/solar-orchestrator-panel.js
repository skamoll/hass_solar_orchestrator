/* Solar Orchestrator custom sidebar panel (zero-build native Web Component) */
class SolarOrchestratorPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._snapshot = null;
    this._tab = window.location.hash === "#config" ? "config" : "status";
    this._preview = null;
    this._verifyResult = null;
    this._verifyEntity = "";
    this._advancedOpen = false;
    this._blackboxOpen = false;
    this._alertsOpen = false;
    this._timer = null;
    this._rendered = false;
    this._toast = "";
  }

  connectedCallback() {
    this._renderSkeleton();
    this._fetchSnapshot();
    this._timer = window.setInterval(() => this._fetchSnapshot(), 5000);
  }

  disconnectedCallback() {
    if (this._timer) {
      window.clearInterval(this._timer);
      this._timer = null;
    }
  }

  set hass(value) {
    const first = !this._hass;
    this._hass = value;
    if (first && this.isConnected) {
      this._fetchSnapshot();
    }
  }

  async _fetchSnapshot() {
    if (!this._hass) return;
    try {
      const res = await this._hass.callWS({ type: "hass_solar_orchestrator/snapshot" });
      this._snapshot = res;
      this._renderContent();
    } catch (err) {
      this._showToast(`Error leyendo estado: ${err?.message || err}`);
    }
  }

  async _callService(service, data) {
    if (!this._hass) return;
    try {
      const resp = await this._hass.callService("hass_solar_orchestrator", service, data);
      await this._fetchSnapshot();
      return resp;
    } catch (err) {
      this._showToast(`Error en ${service}: ${err?.message || err}`);
      return null;
    }
  }

  _showToast(msg) {
    this._toast = msg;
    const el = this.shadowRoot.getElementById("so-toast");
    if (el) {
      el.textContent = msg;
      el.style.display = msg ? "block" : "none";
      if (msg) {
        window.setTimeout(() => {
          this._toast = "";
          if (el) el.style.display = "none";
        }, 4000);
      }
    }
  }

  _fmtW(val) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) return "—";
    const n = Math.round(Number(val));
    if (Math.abs(n) >= 1000) return `${(n / 1000).toFixed(2)} kW`;
    return `${n} W`;
  }

  _fmtFresh(age) {
    if (age === null || age === undefined) return "sin dato";
    if (age > 60) return `obsoleto (${age}s)`;
    return `hace ${age}s`;
  }

  _renderSkeleton() {
    if (this._rendered) return;
    this._rendered = true;
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          min-height: 100vh;
          background: var(--primary-background-color, #111);
          color: var(--primary-text-color, #eee);
          font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
        }
        .header {
          position: sticky;
          top: 0;
          z-index: 20;
          background: var(--app-header-background-color, var(--card-background-color, #1c1c1c));
          border-bottom: 1px solid var(--divider-color, #333);
          padding: 12px 20px;
          display: flex;
          flex-wrap: wrap;
          align-items: center;
          gap: 12px;
          justify-content: space-between;
        }
        .brand { display: flex; align-items: center; gap: 10px; font-weight: 600; font-size: 18px; }
        .pill {
          display: inline-flex; align-items: center; gap: 6px;
          padding: 4px 10px; border-radius: 16px; font-size: 12px; font-weight: 600;
          background: rgba(128,128,128,0.18);
        }
        .pill.optimal { background: rgba(76,175,80,0.2); color: #66bb6a; }
        .pill.warning { background: rgba(255,152,0,0.22); color: #ffa726; }
        .pill.critical { background: rgba(244,67,54,0.22); color: #ef5350; }
        .pill.shadow { background: rgba(33,150,243,0.2); color: #42a5f5; }
        .pill.manual { background: rgba(156,39,176,0.22); color: #ba68c8; }
        .tabs { display: flex; gap: 8px; }
        .tab-btn {
          border: 1px solid var(--divider-color, #444);
          background: transparent; color: inherit;
          padding: 8px 14px; border-radius: 8px; cursor: pointer; font-weight: 500;
        }
        .tab-btn.active {
          background: var(--primary-color, #03a9f4);
          color: #fff; border-color: transparent;
        }
        .shadow-banner {
          background: rgba(33,150,243,0.16);
          border-bottom: 1px solid rgba(33,150,243,0.35);
          padding: 8px 20px; font-size: 13px;
          display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;
        }
        .wrap { max-width: 1280px; margin: 0 auto; padding: 18px; }
        .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 16px; }
        .card {
          background: var(--ha-card-background, var(--card-background-color, #1e1e1e));
          border: 1px solid var(--divider-color, #333);
          border-radius: 12px; padding: 16px;
        }
        .c12 { grid-column: span 12; }
        .c8 { grid-column: span 8; }
        .c6 { grid-column: span 6; }
        .c4 { grid-column: span 4; }
        @media (max-width: 960px) {
          .c8, .c6, .c4 { grid-column: span 12; }
        }
        h3 { margin: 0 0 12px 0; font-size: 15px; font-weight: 600; opacity: 0.92; }
        .muted { color: var(--secondary-text-color, #aaa); font-size: 12px; }
        .big { font-size: 20px; font-weight: 700; }
        .row { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
        .btn {
          border: 1px solid var(--divider-color, #555);
          background: rgba(255,255,255,0.06); color: inherit;
          padding: 7px 12px; border-radius: 8px; cursor: pointer; font-size: 13px;
        }
        .btn.primary { background: var(--primary-color, #03a9f4); color: #fff; border-color: transparent; }
        .btn.danger { background: rgba(244,67,54,0.2); color: #ef5350; border-color: rgba(244,67,54,0.4); }
        .btn:disabled { opacity: 0.45; cursor: not-allowed; }
        .icp-bar { width: 160px; height: 8px; border-radius: 4px; background: rgba(128,128,128,0.25); overflow: hidden; }
        .icp-fill { height: 100%; background: #66bb6a; transition: width 0.3s; }
        .icp-fill.warn { background: #ffa726; }
        .icp-fill.crit { background: #ef5350; }
        .flow-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-top: 8px; }
        .node {
          padding: 12px; border-radius: 10px; background: rgba(128,128,128,0.08);
          border: 1px solid var(--divider-color, #333);
        }
        .node .lbl { font-size: 12px; color: var(--secondary-text-color, #aaa); }
        .node .val { font-size: 20px; font-weight: 700; margin: 4px 0; }
        .load-row {
          padding: 10px 12px; border-radius: 8px; background: rgba(128,128,128,0.07);
          margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap;
        }
        input, select {
          background: var(--primary-background-color, #111);
          color: var(--primary-text-color, #eee);
          border: 1px solid var(--divider-color, #555);
          border-radius: 6px; padding: 6px 8px; font-size: 13px;
        }
        .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; }
        .field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
        .bb-list { max-height: 280px; overflow-y: auto; font-family: monospace; font-size: 12px; }
        .bb-item { padding: 6px 0; border-bottom: 1px solid var(--divider-color, #2a2a2a); }
        #so-toast {
          display: none; position: fixed; bottom: 20px; right: 20px; z-index: 50;
          background: #323232; color: #fff; padding: 10px 16px; border-radius: 8px; font-size: 13px;
          box-shadow: 0 4px 16px rgba(0,0,0,0.4);
        }
      </style>
      <div id="so-root">Cargando Solar Orchestrator...</div>
      <div id="so-toast"></div>
    `;
  }

  _renderContent() {
    const d = this._snapshot;
    if (!d) return;
    const p = d.plan || {};
    const s = d.snapshot || {};
    const cfg = d.config || {};
    const tl = p.traffic_light || "shadow";
    const icpPct = p.icp_load_ratio_pct ?? null;
    const icpCls = icpPct === null ? "" : icpPct >= 90 ? "crit" : icpPct >= 75 ? "warn" : "";
    const alertsCount = (p.alerts || []).length;

    const headerHtml = `
      <div class="header">
        <div class="brand">
          <span>☀️ Solar Orchestrator</span>
          <span class="pill ${tl}">${tl.toUpperCase()}</span>
          <span class="pill">${d.mode === "observing" ? "🛡️ MODO SOMBRA" : d.mode === "manual_total" ? "⏸️ MANUAL TOTAL" : `⚡ ${d.mode.toUpperCase()}`}</span>
        </div>
        <div class="row">
          <div title="Margen ICP en vivo">
            <div class="muted">Margen ICP: ${p.icp_headroom_w !== null && p.icp_headroom_w !== undefined ? this._fmtW(p.icp_headroom_w) : "no disponible"}</div>
            <div class="icp-bar"><div class="icp-fill ${icpCls}" style="width:${Math.min(100, icpPct || 0)}%"></div></div>
          </div>
          <button class="btn danger" id="btn-manual-total">⏸ Manual total (1h)</button>
          <button class="btn" id="btn-alerts">🔔 ${alertsCount}</button>
          <div class="tabs">
            <button class="tab-btn ${this._tab === "status" ? "active" : ""}" id="tab-status">📊 Estado General</button>
            <button class="tab-btn ${this._tab === "config" ? "active" : ""}" id="tab-config">⚙️ Configuración</button>
          </div>
        </div>
      </div>
      ${d.mode === "observing" ? `
        <div class="shadow-banner">
          <span>🛡️ <strong>Modo Sombra activo:</strong> observando en vivo y simulando decisiones. <strong>0 escrituras físicas</strong> a equipos.</span>
          <span class="muted">Para activar control real usa Configuración → Modo operativo.</span>
        </div>` : ""}
      ${this._alertsOpen ? `
        <div class="wrap"><div class="card c12">
          <h3>Alertas activas (${alertsCount})</h3>
          ${alertsCount === 0 ? '<div class="muted">Sin alertas activas.</div>' : (p.alerts || []).map(a => `
            <div class="load-row"><div><strong>${a.title}</strong><div class="muted">${a.detail}</div></div><span class="pill ${a.severity === "critical" ? "critical" : "warning"}">${a.severity}</span></div>
          `).join("")}
        </div></div>` : ""}
    `;

    const bodyHtml = this._tab === "status" ? this._renderStatusTab(d, p, s) : this._renderConfigTab(d, cfg);
    const root = this.shadowRoot.getElementById("so-root");
    root.innerHTML = `${headerHtml}<div class="wrap">${bodyHtml}</div>`;
    this._bindEvents();
  }

  _renderStatusTab(d, p, s) {
    const evc = p.ev_commitment || {};
    const gridDir = s.grid_w === null || s.grid_w === undefined ? "—" : s.grid_w > 20 ? `Importando ${this._fmtW(s.grid_w)}` : s.grid_w < -20 ? `Exportando ${this._fmtW(-s.grid_w)}` : "Equilibrado (0 W)";
    const batDir = s.battery_w === null || s.battery_w === undefined ? "—" : s.battery_w < -20 ? `Cargando ${this._fmtW(-s.battery_w)}` : s.battery_w > 20 ? `Descargando ${this._fmtW(s.battery_w)}` : "Reposo";

    return `
      <div class="grid">
        <div class="card c12">
          <div class="row">
            <div>
              <div class="muted">¿Qué pasa ahora?</div>
              <div class="big">${p.human_headline || "—"}</div>
            </div>
            ${p.washer_suggestion ? `<span class="pill optimal">🧺 ${p.washer_suggestion}</span>` : ""}
          </div>
          ${p.battery_advice ? `<div class="muted" style="margin-top:8px">🔋 ${p.battery_advice}</div>` : ""}
        </div>

        <div class="card c8">
          <h3>Flujo energético en vivo (M01)</h3>
          <div class="flow-grid">
            <div class="node"><div class="lbl">☀️ Solar</div><div class="val">${this._fmtW(s.solar_w)}</div><div class="muted">${this._fmtFresh((p.freshness_by_source || {}).modbus_solar)}</div></div>
            <div class="node"><div class="lbl">🏠 Casa (#1 fija)</div><div class="val">${this._fmtW(s.house_w)}</div><div class="muted">${(p.why_by_device || {}).house || ""}</div></div>
            <div class="node"><div class="lbl">🔌 Red</div><div class="val">${gridDir}</div><div class="muted">${this._fmtFresh((p.freshness_by_source || {}).modbus_grid)}</div></div>
            <div class="node"><div class="lbl">🔋 Batería (${s.battery_soc ?? "—"}%)</div><div class="val">${batDir}</div><div class="muted">Solo lectura (D2)</div></div>
          </div>
        </div>

        <div class="card c4">
          <h3>🚗 Vehículo eléctrico (M04 / M05)</h3>
          <div class="row">
            <div>SoC actual: <strong>${s.ev_soc ?? "—"}%</strong> · Meta: <strong>${evc.target_soc ?? 90}% a las ${evc.target_time_hhmm ?? "07:30"}</strong></div>
          </div>
          <div class="muted" style="margin:6px 0">${(p.why_by_device || {}).ev || ""} · Consumo: ${this._fmtW(s.ev_power_w)} (${s.ev_current_a ?? "—"} A)</div>
          <div class="row" style="margin-top:10px">
            <button class="btn primary" id="btn-ev-boost" ${d.mode === "observing" ? "title='En Modo Sombra solo actualiza propuesta'" : ""}>⚡ Cargar ahora (1h)</button>
            <button class="btn" id="btn-ev-auto">↺ Automático</button>
          </div>
        </div>

        <div class="card c6">
          <h3>📡 Radar de cargas flexibles (M06)</h3>
          ${(p.flex_loads || []).map(l => `
            <div class="load-row">
              <div>
                <strong>${l.name}</strong> <span class="pill">${l.state}</span>
                <div class="muted">${l.reason} · ${this._fmtW(l.power_w)}</div>
              </div>
              <div class="row">
                ${l.quarantined ? `<button class="btn danger btn-clear-q" data-load="${l.load_id}">Rehabilitar</button>` : `
                  <button class="btn btn-ov" data-load="${l.load_id}" data-act="on">ON 1h</button>
                  <button class="btn btn-ov" data-load="${l.load_id}" data-act="off">OFF 1h</button>
                  <button class="btn btn-ov" data-load="${l.load_id}" data-act="auto">Auto</button>
                `}
              </div>
            </div>
          `).join("")}
        </div>

        <div class="card c6">
          <h3>💶 Ahorro estimado honesto (M08)</h3>
          <div class="row">
            <div><div class="muted">Hoy</div><div class="big">${p.estimated_savings_today_eur !== null && p.estimated_savings_today_eur !== undefined ? `${p.estimated_savings_today_eur.toFixed(2)} €` : "Sin tarifa"}</div></div>
            <div><div class="muted">Este mes</div><div class="big">${p.estimated_savings_month_eur !== null && p.estimated_savings_month_eur !== undefined ? `${p.estimated_savings_month_eur.toFixed(2)} €` : "Sin tarifa"}</div></div>
          </div>
          <div class="muted" style="margin-top:10px">Fórmula transparente: ${p.savings_formula || "—"}</div>
        </div>

        <div class="card c12">
          <div class="row">
            <h3>🖤 Caja Negra local (últimas decisiones · fuera de recorder)</h3>
            <button class="btn" id="btn-toggle-bb">${this._blackboxOpen ? "Ocultar" : "Mostrar"} (${(d.blackbox || []).length})</button>
          </div>
          ${this._blackboxOpen ? `
            <div class="bb-list" style="margin-top:10px">
              ${(d.blackbox || []).map(b => `<div class="bb-item">[${(b.ts || "").slice(11,19)}] <strong>${b.type}</strong> (${b.mode}) · ${b.message} ${b.executed ? "✅" : "🛡️"}</div>`).join("")}
            </div>` : ""}
        </div>
      </div>
    `;
  }

  _renderConfigTab(d, cfg) {
    const ents = cfg.entities || {};
    return `
      <div class="grid">
        <div class="card c6">
          <h3>🎛️ Modo Operativo y Perfiles 1-Toque (M10 / M20)</h3>
          <div class="field" style="margin-bottom:12px">
            <label>Modo global</label>
            <select id="sel-mode">
              <option value="observing" ${d.mode === "observing" ? "selected" : ""}>🛡️ Modo Sombra (solo observar · recomendado)</option>
              <option value="auto_solar" ${d.mode === "auto_solar" ? "selected" : ""}>☀️ Auto Solar (Trydan FV Exclusivo)</option>
              <option value="auto_ev_modulated" ${d.mode === "auto_ev_modulated" ? "selected" : ""}>⚡ Auto Modulado (6-32A + auxiliares)</option>
              <option value="force_ev" ${d.mode === "force_ev" ? "selected" : ""}>🚗 Forzar carga VE</option>
              <option value="house_priority" ${d.mode === "house_priority" ? "selected" : ""}>🏠 Prioridad Casa/Batería</option>
              <option value="manual_total" ${d.mode === "manual_total" ? "selected" : ""}>⏸️ Manual total (vuelve a Sombra al vencer)</option>
            </select>
          </div>
          <div class="muted" style="margin-bottom:6px">Perfiles rápidos (con vista previa de impacto):</div>
          <div class="row">
            <button class="btn btn-prof" data-prof="saving">🌿 Ahorro</button>
            <button class="btn btn-prof" data-prof="comfort">⚖️ Confort</button>
            <button class="btn btn-prof" data-prof="fast_charge">⚡ Carga rápida</button>
            <button class="btn btn-prof" data-prof="vacation">🏖️ Vacaciones</button>
          </div>
          ${this._preview ? `
            <div class="card" style="margin-top:12px;background:rgba(33,150,243,0.1)">
              <div><strong>Vista previa (${this._preview.profile}):</strong> ${this._preview.summary}</div>
              <div class="row" style="margin-top:8px">
                <button class="btn primary" id="btn-apply-prof">Confirmar y aplicar</button>
                <button class="btn" id="btn-cancel-prof">Cancelar</button>
              </div>
            </div>` : ""}
        </div>

        <div class="card c6">
          <h3>🧱 Matriz de Prioridades (M11 · Casa fija #1)</h3>
          ${(cfg.priority_order || ["house", "ev", "heater", "pool"]).map((id, idx, arr) => `
            <div class="load-row">
              <div><strong>#${idx + 1} ${id === "house" ? "🏠 Casa (Inamovible)" : id.toUpperCase()}</strong></div>
              <div class="row">
                ${id === "house" ? '<span class="pill optimal">🔒 Fija #1</span>' : `
                  <button class="btn btn-prio" data-idx="${idx}" data-dir="-1" ${idx <= 1 ? "disabled" : ""}>↑</button>
                  <button class="btn btn-prio" data-idx="${idx}" data-dir="1" ${idx >= arr.length - 1 ? "disabled" : ""}>↓</button>
                `}
              </div>
            </div>
          `).join("")}
        </div>

        <div class="card c6">
          <h3>💶 Tarifas y Potencia Contratada (M13)</h3>
          <div class="form-grid">
            <div class="field"><label>Potencia contratada (W)</label><input id="inp-contracted" type="number" value="${cfg.contracted_power_w ?? 5750}" min="1000" step="250"></div>
            <div class="field"><label>Margen seguridad ICP (W)</label><input id="inp-icp-margin" type="number" value="${cfg.icp_margin_w ?? 500}" min="100" step="50"></div>
            <div class="field"><label>Modo tarifa</label>
              <select id="sel-tariff-mode">
                <option value="manual_20td" ${cfg.tariff_mode === "manual_20td" ? "selected" : ""}>Manual 2.0TD (P1/P2/P3)</option>
                <option value="sensor" ${cfg.tariff_mode === "sensor" ? "selected" : ""}>Sensor HA (PVPC/ESIOS)</option>
                <option value="conservative" ${cfg.tariff_mode === "conservative" ? "selected" : ""}>Conservador (sin cálculo €)</option>
              </select>
            </div>
            <div class="field"><label>P1 Punta (€/kWh)</label><input id="inp-p1" type="number" step="0.01" value="${cfg.tariff_p1_eur ?? 0.24}"></div>
            <div class="field"><label>P2 Llano (€/kWh)</label><input id="inp-p2" type="number" step="0.01" value="${cfg.tariff_p2_eur ?? 0.16}"></div>
            <div class="field"><label>P3 Valle (€/kWh)</label><input id="inp-p3" type="number" step="0.01" value="${cfg.tariff_p3_eur ?? 0.10}"></div>
          </div>
          <div class="row" style="margin-top:12px">
            <button class="btn primary" id="btn-save-tariff">Guardar tarifas y potencia</button>
          </div>
        </div>

        <div class="card c6">
          <h3>🔎 Verificador en Vivo de Signos y Unidades (M09)</h3>
          <div class="row">
            <input id="inp-verify-ent" style="flex:1" placeholder="sensor.sungrow_grid_power" value="${this._verifyEntity || ents.grid_power_entity || ""}">
            <button class="btn primary" id="btn-verify-ent">Comprobar en vivo</button>
          </div>
          ${this._verifyResult ? `
            <div class="load-row" style="margin-top:10px">
              <div>
                <div><strong>${this._verifyResult.entity_id}</strong>: ${this._verifyResult.state ?? "—"} ${this._verifyResult.unit || ""}</div>
                <div class="muted">${this._verifyResult.hint}</div>
              </div>
            </div>` : '<div class="muted" style="margin-top:8px">Comprueba que importar de la red sea positivo (+) y exportar negativo (−).</div>'}
        </div>

        <div class="card c12">
          <div class="row">
            <h3>🔧 Opciones avanzadas (M14 · Suelo ${cfg.house_soc_floor ?? 20}% · VE max ${cfg.ev_max_amps ?? 32}A)</h3>
            <button class="btn" id="btn-toggle-adv">${this._advancedOpen ? "Ocultar" : "Mostrar"}</button>
          </div>
          ${this._advancedOpen ? `
            <div class="form-grid" style="margin-top:12px">
              <div class="field"><label>Suelo batería casa (%)</label><input id="inp-soc-floor" type="number" min="10" max="90" value="${cfg.house_soc_floor ?? 20}"></div>
              <div class="field"><label>Amperios máx VE (A)</label><input id="inp-ev-max" type="number" min="6" max="32" value="${cfg.ev_max_amps ?? 32}"></div>
              <div class="field"><label>Temp objetivo termo (°C)</label><input id="inp-heater-temp" type="number" min="30" max="75" value="${cfg.heater_target_temp_c ?? 45}"></div>
              <div class="field"><label>Cuota diaria depuradora (min)</label><input id="inp-pool-max" type="number" min="30" max="720" value="${cfg.pool_daily_max_minutes ?? 360}"></div>
            </div>
            <div class="row" style="margin-top:12px">
              <button class="btn primary" id="btn-save-adv">Guardar avanzadas</button>
              <button class="btn danger" id="btn-restore-def">↺ Restaurar recomendados</button>
            </div>` : ""}
        </div>
      </div>
    `;
  }

  _bindEvents() {
    const q = (id) => this.shadowRoot.getElementById(id);
    q("tab-status")?.addEventListener("click", () => {
      this._tab = "status";
      window.location.hash = "estado";
      this._renderContent();
    });
    q("tab-config")?.addEventListener("click", () => {
      this._tab = "config";
      window.location.hash = "config";
      this._renderContent();
    });
    q("btn-manual-total")?.addEventListener("click", () => {
      this._callService("set_mode", { mode: "manual_total", duration_minutes: 60 });
      this._showToast("Manual total activado (1h). Volverá a Modo Sombra al vencer.");
    });
    q("btn-alerts")?.addEventListener("click", () => {
      this._alertsOpen = !this._alertsOpen;
      this._renderContent();
    });
    q("btn-toggle-bb")?.addEventListener("click", () => {
      this._blackboxOpen = !this._blackboxOpen;
      this._renderContent();
    });
    q("btn-toggle-adv")?.addEventListener("click", () => {
      this._advancedOpen = !this._advancedOpen;
      this._renderContent();
    });
    q("btn-ev-boost")?.addEventListener("click", () => {
      this._callService("override_load", { load_id: "ev", action: "charge_now", duration_minutes: 60 });
    });
    q("btn-ev-auto")?.addEventListener("click", () => {
      this._callService("override_load", { load_id: "ev", action: "auto" });
    });

    this.shadowRoot.querySelectorAll(".btn-ov").forEach(btn => {
      btn.addEventListener("click", () => {
        this._callService("override_load", {
          load_id: btn.dataset.load,
          action: btn.dataset.act,
          duration_minutes: 60,
        });
      });
    });
    this.shadowRoot.querySelectorAll(".btn-clear-q").forEach(btn => {
      btn.addEventListener("click", () => {
        this._callService("clear_quarantine", { load_id: btn.dataset.load });
      });
    });
    q("sel-mode")?.addEventListener("change", (ev) => {
      this._callService("set_mode", { mode: ev.target.value, duration_minutes: 60 });
    });
    this.shadowRoot.querySelectorAll(".btn-prof").forEach(btn => {
      btn.addEventListener("click", async () => {
        const prof = btn.dataset.prof;
        const summaryMap = {
          saving: "Prioriza autoconsumo y protege la batería hasta el 30% sin apoyo de red al VE.",
          comfort: "Equilibrio diario: reserva 20% y permite apoyo de red en valle si el VE lo necesita.",
          fast_charge: "Carga rápida del VE hasta el SoC objetivo respetando siempre el margen ICP.",
          vacation: "Modo Vacaciones: desactiva termo eléctrico y mantiene depuradora solo con excedente.",
        };
        this._preview = { profile: prof, summary: summaryMap[prof] || prof };
        this._renderContent();
      });
    });
    q("btn-cancel-prof")?.addEventListener("click", () => {
      this._preview = null;
      this._renderContent();
    });
    q("btn-apply-prof")?.addEventListener("click", async () => {
      if (!this._preview) return;
      const prof = this._preview.profile;
      const payload = { profile: prof, preview_only: false };
      if (prof === "vacation") {
        const nextWeek = new Date(Date.now() + 7 * 86400000).toISOString();
        payload.vacation_until = nextWeek;
      }
      this._preview = null;
      await this._callService("apply_profile", payload);
      this._showToast(`Perfil ${prof} aplicado.`);
    });

    this.shadowRoot.querySelectorAll(".btn-prio").forEach(btn => {
      btn.addEventListener("click", () => {
        const order = [...(this._snapshot?.config?.priority_order || ["house", "ev", "heater", "pool"])];
        const i = Number(btn.dataset.idx);
        const dir = Number(btn.dataset.dir);
        const j = i + dir;
        if (i < 1 || j < 1 || j >= order.length) return;
        [order[i], order[j]] = [order[j], order[i]];
        this._callService("set_priorities", { priority_order: order });
      });
    });

    q("btn-save-tariff")?.addEventListener("click", () => {
      this._callService("save_settings", {
        data: {
          contracted_power_w: Number(q("inp-contracted").value),
          icp_margin_w: Number(q("inp-icp-margin").value),
          tariff_mode: q("sel-tariff-mode").value,
          tariff_p1_eur: Number(q("inp-p1").value),
          tariff_p2_eur: Number(q("inp-p2").value),
          tariff_p3_eur: Number(q("inp-p3").value),
        },
      });
      this._showToast("Tarifas y potencia contratada guardadas.");
    });

    q("btn-verify-ent")?.addEventListener("click", async () => {
      const entityId = q("inp-verify-ent").value.trim();
      if (!entityId || !this._hass) return;
      this._verifyEntity = entityId;
      this._verifyResult = await this._hass.callWS({
        type: "hass_solar_orchestrator/validate_entity",
        entity_id: entityId,
      });
      this._renderContent();
    });

    q("btn-save-adv")?.addEventListener("click", () => {
      this._callService("save_settings", {
        data: {
          house_soc_floor: Number(q("inp-soc-floor").value),
          ev_max_amps: Number(q("inp-ev-max").value),
          heater_target_temp_c: Number(q("inp-heater-temp").value),
          pool_daily_max_minutes: Number(q("inp-pool-max").value),
        },
      });
      this._showToast("Opciones avanzadas guardadas.");
    });

    q("btn-restore-def")?.addEventListener("click", () => {
      if (window.confirm("¿Restaurar los valores recomendados por el Consejo?")) {
        this._callService("restore_defaults", { section: "all" });
        this._showToast("Valores recomendados restaurados.");
      }
    });
  }
}

customElements.define("solar-orchestrator-panel", SolarOrchestratorPanel);
