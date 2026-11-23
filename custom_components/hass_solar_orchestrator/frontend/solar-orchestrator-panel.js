/* Solar Orchestrator custom sidebar panel v0.3.0 (single-mount DOM + rAF surgical patch) */
class SolarOrchestratorPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._snapshot = null;
    this._scan = null;
    this._tab = window.location.hash === "#config" ? "config" : "status";
    this._draft = {};
    this._dirty = false;
    this._rafPending = false;
    this._mounted = false;
    this._timer = null;
    this._bbFilterEquip = "";
    this._bbFilterType = "";
    this._bbOpen = false;
    this._advOpen = false;
    this._alertsOpen = false;
  }

  connectedCallback() {
    if (!this._mounted) {
      this._mountOnce();
      this._mounted = true;
    }
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

  _fmtW(val) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) return "—";
    const n = Number(val);
    if (Math.abs(n) >= 1000) return `${(n / 1000).toFixed(2)} kW`;
    return `${Math.round(n)} W`;
  }

  _fmtFresh(age) {
    if (age === null || age === undefined) return "sin dato";
    if (age > 60) return `obsoleto (${age}s)`;
    return `hace ${age}s`;
  }

  async _fetchSnapshot() {
    if (!this._hass) return;
    try {
      this._snapshot = await this._hass.callWS({ type: "hass_solar_orchestrator/snapshot" });
      this._schedulePatch();
    } catch (err) {
      this._showToast(`Error leyendo estado: ${err?.message || err}`);
    }
  }

  async _callService(service, data) {
    if (!this._hass) return null;
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
    const el = this.shadowRoot.getElementById("so-toast");
    if (!el) return;
    el.textContent = msg;
    el.style.display = msg ? "block" : "none";
    if (msg) {
      window.setTimeout(() => {
        if (el) el.style.display = "none";
      }, 4500);
    }
  }

  _mountOnce() {
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block; min-height: 100vh;
          background: var(--primary-background-color, #111);
          color: var(--primary-text-color, #eee);
          font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
        }
        * { box-sizing: border-box; }
        .header {
          position: sticky; top: 0; z-index: 20;
          background: var(--app-header-background-color, var(--card-background-color, #1c1c1c));
          border-bottom: 1px solid var(--divider-color, #333);
          padding: 10px 16px; display: flex; flex-wrap: wrap; align-items: center;
          gap: 10px; justify-content: space-between;
        }
        .brand { display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 17px; }
        .pill {
          display: inline-flex; align-items: center; gap: 5px;
          padding: 3px 9px; border-radius: 14px; font-size: 12px; font-weight: 600;
          background: rgba(128,128,128,0.18);
        }
        .pill.optimal { background: rgba(76,175,80,0.2); color: #66bb6a; }
        .pill.warning { background: rgba(255,152,0,0.22); color: #ffa726; }
        .pill.critical { background: rgba(244,67,54,0.22); color: #ef5350; }
        .pill.shadow { background: rgba(33,150,243,0.2); color: #42a5f5; }
        .pill.manual { background: rgba(156,39,176,0.22); color: #ba68c8; }
        .tabs { display: flex; gap: 6px; }
        .tab-btn {
          border: 1px solid var(--divider-color, #444);
          background: transparent; color: inherit; padding: 7px 12px;
          border-radius: 8px; cursor: pointer; font-weight: 500;
        }
        .tab-btn.active { background: var(--primary-color, #03a9f4); color: #fff; border-color: transparent; }
        .shadow-banner {
          background: rgba(33,150,243,0.15); border-bottom: 1px solid rgba(33,150,243,0.35);
          padding: 8px 16px; font-size: 13px; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px;
        }
        .wrap { max-width: 1280px; margin: 0 auto; padding: 14px; }
        .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 14px; }
        .card {
          background: var(--ha-card-background, var(--card-background-color, #1e1e1e));
          border: 1px solid var(--divider-color, #333); border-radius: 12px; padding: 14px;
        }
        .c12 { grid-column: span 12; }
        .c8 { grid-column: span 8; }
        .c6 { grid-column: span 6; }
        .c4 { grid-column: span 4; }
        @media (max-width: 900px) { .c8, .c6, .c4 { grid-column: span 12; } }
        h3 { margin: 0 0 10px 0; font-size: 15px; font-weight: 600; }
        .muted { color: var(--secondary-text-color, #aaa); font-size: 12px; }
        .big { font-size: 19px; font-weight: 700; }
        .row { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
        .btn {
          border: 1px solid var(--divider-color, #555);
          background: rgba(255,255,255,0.06); color: inherit;
          padding: 7px 11px; border-radius: 8px; cursor: pointer; font-size: 13px;
        }
        .btn.primary { background: var(--primary-color, #03a9f4); color: #fff; border-color: transparent; }
        .btn.danger { background: rgba(244,67,54,0.2); color: #ef5350; border-color: rgba(244,67,54,0.4); }
        .btn:disabled { opacity: 0.45; cursor: not-allowed; }
        .icp-bar { width: 140px; height: 8px; border-radius: 4px; background: rgba(128,128,128,0.25); overflow: hidden; }
        .icp-fill { height: 100%; background: #66bb6a; transition: width 0.3s; }
        .icp-fill.warn { background: #ffa726; }
        .icp-fill.crit { background: #ef5350; }
        .flow-svg { width: 100%; max-height: 190px; display: block; margin-bottom: 10px; }
        .flow-line { stroke: var(--primary-color, #03a9f4); stroke-width: 3; fill: none; stroke-dasharray: 6 6; animation: flowdash 1.2s linear infinite; }
        .flow-line.idle { stroke: rgba(128,128,128,0.3); stroke-dasharray: none; animation: none; }
        @keyframes flowdash { to { stroke-dashoffset: -24; } }
        @media (prefers-reduced-motion: reduce) { .flow-line { animation: none; stroke-dasharray: none; } }
        .flow-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(135px, 1fr)); gap: 10px; }
        .node { padding: 10px; border-radius: 10px; background: rgba(128,128,128,0.08); border: 1px solid var(--divider-color, #333); }
        .node .val { font-size: 18px; font-weight: 700; margin: 3px 0; }
        .load-row {
          padding: 9px 11px; border-radius: 8px; background: rgba(128,128,128,0.07);
          margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap;
        }
        .fc-strip { display: flex; gap: 8px; overflow-x: auto; padding-bottom: 4px; }
        .fc-day { min-width: 96px; padding: 8px; border-radius: 8px; background: rgba(128,128,128,0.08); text-align: center; font-size: 12px; }
        input, select {
          background: var(--primary-background-color, #111); color: var(--primary-text-color, #eee);
          border: 1px solid var(--divider-color, #555); border-radius: 6px; padding: 6px 8px; font-size: 13px;
        }
        .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; }
        .field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
        .bb-list { max-height: 280px; overflow-y: auto; font-family: monospace; font-size: 12px; }
        .bb-item { padding: 6px 0; border-bottom: 1px solid var(--divider-color, #2a2a2a); }
        .help-tip { display: inline-block; width: 16px; height: 16px; line-height: 16px; text-align: center; border-radius: 50%; background: rgba(128,128,128,0.3); font-size: 11px; cursor: help; }
        [hidden] { display: none !important; }
        #so-toast {
          display: none; position: fixed; bottom: 20px; right: 20px; z-index: 50;
          background: #323232; color: #fff; padding: 10px 16px; border-radius: 8px; font-size: 13px;
          box-shadow: 0 4px 16px rgba(0,0,0,0.4);
        }
      </style>

      <div class="header">
        <div class="brand">
          <span>☀️ Solar Orchestrator</span>
          <span class="pill shadow" id="hdr-tl">SHADOW</span>
          <span class="pill" id="hdr-mode">🛡️ MODO SOMBRA</span>
        </div>
        <div class="row">
          <div>
            <div class="muted" id="hdr-icp-txt">Margen ICP: —</div>
            <div class="icp-bar"><div class="icp-fill" id="hdr-icp-bar" style="width:0%"></div></div>
          </div>
          <button class="btn danger" id="btn-manual-total">⏸ Manual total (1h)</button>
          <button class="btn" id="btn-alerts">🔔 <span id="hdr-alert-cnt">0</span></button>
          <div class="tabs">
            <button class="tab-btn active" id="tab-btn-status">📊 Estado General</button>
            <button class="tab-btn" id="tab-btn-config">⚙️ Configuración</button>
          </div>
        </div>
      </div>

      <div class="shadow-banner" id="shadow-banner">
        <span>🛡️ <strong>Modo Sombra:</strong> observando en vivo y simulando decisiones. <strong>0 escrituras físicas</strong>.</span>
        <span class="muted">Activa cargas individualmente en Configuración → Control granular.</span>
      </div>

      <div class="wrap" id="alerts-drawer" hidden>
        <div class="card"><h3>Alertas activas</h3><div id="alerts-list"></div></div>
      </div>

      <div class="wrap" id="view-status">
        <div class="grid">
          <div class="card c12">
            <div class="row">
              <div>
                <div class="muted">¿Qué pasa ahora?</div>
                <div class="big" id="st-headline">Cargando...</div>
              </div>
              <div class="row">
                <span class="pill optimal" id="st-washer" hidden></span>
                <span class="pill warning" id="st-storm" hidden>⛈️ Temporal en camino</span>
              </div>
            </div>
            <div class="muted" id="st-bat-advice" style="margin-top:6px" hidden></div>
            <div class="muted" id="st-climate-advice" style="margin-top:4px" hidden></div>
          </div>

          <div class="card c12" id="byd-nag-banner" style="border-color:#ffa726;background:rgba(255,152,0,0.1)" hidden>
            <div class="row">
              <div><strong>🚗 BYD en casa con sol y sin enchufar</strong><div class="muted" id="byd-nag-detail"></div></div>
              <button class="btn" id="btn-mute-byd">Silenciar hoy</button>
            </div>
          </div>

          <div class="card c8">
            <h3>Flujo energético en vivo (V3-M11)</h3>
            <svg class="flow-svg" viewBox="0 0 420 150" aria-label="Flujo energético solar">
              <circle cx="210" cy="24" r="18" fill="rgba(255,193,7,0.22)" stroke="#ffc107"/>
              <text x="210" y="28" text-anchor="middle" fill="currentColor" font-size="11">☀️ Sol</text>
              <circle cx="210" cy="82" r="20" fill="rgba(3,169,244,0.2)" stroke="#03a9f4"/>
              <text x="210" y="86" text-anchor="middle" fill="currentColor" font-size="11">🏠 Casa</text>
              <circle cx="65" cy="82" r="18" fill="rgba(76,175,80,0.2)" stroke="#4caf50"/>
              <text x="65" y="86" text-anchor="middle" fill="currentColor" font-size="11">🔋 Bat</text>
              <circle cx="355" cy="82" r="18" fill="rgba(156,39,176,0.2)" stroke="#ab47bc"/>
              <text x="355" y="86" text-anchor="middle" fill="currentColor" font-size="11">🔌 Red</text>
              <circle cx="130" cy="132" r="15" fill="rgba(3,169,244,0.15)" stroke="#03a9f4"/>
              <text x="130" y="136" text-anchor="middle" fill="currentColor" font-size="10">🚗 VE</text>
              <circle cx="290" cy="132" r="15" fill="rgba(255,152,0,0.18)" stroke="#ff9800"/>
              <text x="290" y="136" text-anchor="middle" fill="currentColor" font-size="10">🚿 Termo</text>
              <path id="svg-l-solar" class="flow-line idle" d="M210,42 L210,62"/>
              <path id="svg-l-bat" class="flow-line idle" d="M83,82 L190,82"/>
              <path id="svg-l-grid" class="flow-line idle" d="M230,82 L337,82"/>
              <path id="svg-l-ev" class="flow-line idle" d="M196,98 L140,121"/>
              <path id="svg-l-heater" class="flow-line idle" d="M224,98 L280,121"/>
            </svg>
            <div class="flow-grid">
              <div class="node"><div class="muted">☀️ Solar</div><div class="val" id="val-solar">—</div><div class="muted" id="fr-solar">—</div></div>
              <div class="node"><div class="muted">🏠 Casa (#1 fija)</div><div class="val" id="val-house">—</div><div class="muted">Inamovible</div></div>
              <div class="node"><div class="muted">🔌 Red</div><div class="val" id="val-grid">—</div><div class="muted" id="fr-grid">—</div></div>
              <div class="node"><div class="muted">🔋 Batería (<span id="val-batsoc">—</span>%)</div><div class="val" id="val-bat">—</div><div class="muted">Solo lectura (D2)</div></div>
            </div>
          </div>

          <div class="card c4">
            <h3>🚿 Termo eléctrico (1,05 kW · V3-M01..M03)</h3>
            <div class="row">
              <div>Agua: <strong id="val-termo-temp">—</strong> °C · Potencia: <strong id="val-termo-pwr">—</strong></div>
              <span class="pill" id="badge-termo-state">—</span>
            </div>
            <div class="muted" id="val-termo-reason" style="margin:6px 0">—</div>
            <div class="row" style="margin-top:8px">
              <button class="btn primary" id="btn-extra-shower">+ Ducha extra hoy</button>
              <button class="btn" id="btn-termo-auto">Auto</button>
            </div>
          </div>

          <div class="card c6">
            <h3>🚗 BYD Atto 3 & Trydan (M04 / M05)</h3>
            <div class="row">
              <div>SoC: <strong id="val-ev-soc">—</strong>% · Carga: <strong id="val-ev-pwr">—</strong> (<span id="val-ev-amps">—</span> A)</div>
              <span class="pill" id="badge-ev-mode">observing</span>
            </div>
            <div class="muted" id="val-ev-reason" style="margin:6px 0">—</div>
            <div class="row" style="margin-top:8px">
              <button class="btn primary" id="btn-ev-boost">⚡ Cargar ahora (1h)</button>
              <button class="btn" id="btn-ev-auto">↺ Automático</button>
            </div>
          </div>

          <div class="card c6">
            <h3>🏊 Depuradora & Ahorro (V3-M07 / M08)</h3>
            <div class="row">
              <div>Depuradora hoy: <strong id="val-pool-min">0</strong> / <span id="val-pool-max">360</span> min</div>
              <span class="pill" id="badge-pool-mode">observing</span>
            </div>
            <div class="icp-bar" style="width:100%;margin:8px 0"><div class="icp-fill" id="pool-bar" style="width:0%"></div></div>
            <div class="row">
              <div class="muted">Ahorro hoy / mes: <strong id="val-savings">—</strong></div>
              <div class="muted">Nubes hoy: <span id="val-cloud-pocket">0.00</span> / 0.50 kWh</div>
            </div>
          </div>

          <div class="card c12">
            <div class="row">
              <h3>🌤️ Previsión 3–5 días & SEM (V3-M05)</h3>
              <div class="muted">SEM Mañana: <strong id="val-sem-tmw">—</strong> kWh</div>
            </div>
            <div class="fc-strip" id="fc-strip" style="margin-top:8px"></div>
          </div>

          <div class="card c12">
            <div class="row">
              <h3>🖤 Caja Negra local (<span id="bb-count">0</span>)</h3>
              <div class="row">
                <button class="btn" id="btn-bb-download">⬇ Descargar JSON</button>
                <button class="btn" id="btn-bb-toggle">Mostrar / Ocultar</button>
              </div>
            </div>
            <div id="bb-body" hidden style="margin-top:10px">
              <div class="row" style="margin-bottom:8px">
                <select id="bb-filter-equip">
                  <option value="">Todos los equipos</option>
                  <option value="heater">Termo</option>
                  <option value="ev">VE / BYD</option>
                  <option value="pool">Depuradora</option>
                  <option value="system">Sistema</option>
                </select>
                <select id="bb-filter-type">
                  <option value="">Todos los tipos</option>
                  <option value="simulated">Simulado (Sombra)</option>
                  <option value="action">Acción real</option>
                  <option value="shower_pattern">Patrón de ducha</option>
                  <option value="conflict">Conflicto HA</option>
                  <option value="override">Override</option>
                </select>
              </div>
              <div class="bb-list" id="bb-list"></div>
            </div>
          </div>
        </div>
      </div>

      <div class="wrap" id="view-config" hidden>
        <div class="grid">
          <div class="card c6">
            <h3>🎛️ Activación granular por carga (V3-M10) <span class="help-tip" title="Cada carga arranca en Observación (0 escrituras). Si hay automatizaciones antiguas activas en HA, el paso a Activo queda bloqueado.">?</span></h3>
            <div id="granular-loads-list"></div>
            <div class="field" style="margin-top:10px">
              <label>Modo global del árbitro</label>
              <select id="sel-global-mode">
                <option value="observing">🛡️ Modo Sombra (observing)</option>
                <option value="auto_solar">☀️ Auto Solar</option>
                <option value="auto_ev_modulated">⚡ Auto VE Modulado</option>
                <option value="force_ev">🚗 Forzar carga VE</option>
                <option value="house_priority">🏠 Prioridad Casa/Batería</option>
                <option value="manual_total">⏸️ Manual total (vuelve a Sombra al vencer)</option>
              </select>
            </div>
          </div>

          <div class="card c6">
            <h3>🔍 Auto-descubrimiento de entidades HA (V3-M09) <span class="help-tip" title="Escanea tus integraciones Sungrow, Trydan, BYD, Termo, Galería y Weather.">?</span></h3>
            <button class="btn primary" id="btn-scan-entities">🔍 Escanear entidades HA</button>
            <div id="scan-results" style="margin-top:10px" class="muted">Pulsa Escanear para comprobar tus entidades en vivo.</div>
          </div>

          <div class="card c6">
            <h3>💶 Tarifas y Potencia Contratada <span class="help-tip" title="El margen ICP evita saltos del contador manteniendo reserva doméstica.">?</span></h3>
            <div class="form-grid">
              <div class="field"><label>Potencia contratada (W)</label><input id="inp-contracted" data-cfg="contracted_power_w" type="number" min="1000" step="250"></div>
              <div class="field"><label>Margen ICP (W)</label><input id="inp-icp-margin" data-cfg="icp_margin_w" type="number" min="100" step="50"></div>
              <div class="field"><label>Modo tarifa</label>
                <select id="sel-tariff-mode" data-cfg="tariff_mode">
                  <option value="manual_20td">Manual 2.0TD (P1/P2/P3)</option>
                  <option value="sensor">Sensor HA (PVPC/ESIOS)</option>
                  <option value="conservative">Conservador</option>
                </select>
              </div>
              <div class="field"><label>P1 (€/kWh)</label><input id="inp-p1" data-cfg="tariff_p1_eur" type="number" step="0.01"></div>
              <div class="field"><label>P2 (€/kWh)</label><input id="inp-p2" data-cfg="tariff_p2_eur" type="number" step="0.01"></div>
              <div class="field"><label>P3 (€/kWh)</label><input id="inp-p3" data-cfg="tariff_p3_eur" type="number" step="0.01"></div>
            </div>
          </div>

          <div class="card c6">
            <div class="row">
              <h3>🔧 Opciones avanzadas</h3>
              <button class="btn" id="btn-toggle-adv">Mostrar / Ocultar</button>
            </div>
            <div id="adv-body" hidden style="margin-top:10px">
              <div class="form-grid">
                <div class="field"><label>Suelo batería casa (%)</label><input id="inp-soc-floor" data-cfg="house_soc_floor" type="number" min="10" max="90"></div>
                <div class="field"><label>Amperios máx VE (A)</label><input id="inp-ev-max" data-cfg="ev_max_amps" type="number" min="6" max="32"></div>
                <div class="field"><label>Temp objetivo termo (°C)</label><input id="inp-heater-temp" data-cfg="heater_target_temp_c" type="number" min="30" max="75"></div>
                <div class="field"><label>Cuota depuradora (min/día)</label><input id="inp-pool-max" data-cfg="pool_daily_max_minutes" type="number" min="30" max="720"></div>
              </div>
            </div>
          </div>

          <div class="card c12" id="dirty-bar" hidden style="border-color:#03a9f4">
            <div class="row">
              <strong>Cambios pendientes en borrador</strong>
              <div class="row">
                <button class="btn" id="btn-cancel-draft">Cancelar</button>
                <button class="btn primary" id="btn-save-draft">Guardar cambios</button>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div id="so-toast"></div>
    `;
    this._bindStaticEvents();
  }

  _bindStaticEvents() {
    const q = (id) => this.shadowRoot.getElementById(id);
    q("tab-btn-status").addEventListener("click", () => {
      this._tab = "status";
      window.location.hash = "estado";
      this._schedulePatch();
    });
    q("tab-btn-config").addEventListener("click", () => {
      this._tab = "config";
      window.location.hash = "config";
      this._schedulePatch();
    });
    q("btn-manual-total").addEventListener("click", () => {
      this._callService("set_mode", { mode: "manual_total", duration_minutes: 60 });
      this._showToast("Manual total activado (1h). Volverá a Modo Sombra al vencer.");
    });
    q("btn-alerts").addEventListener("click", () => {
      this._alertsOpen = !this._alertsOpen;
      this._schedulePatch();
    });
    q("btn-extra-shower").addEventListener("click", () => {
      this._callService("extra_shower_today", {});
      this._showToast("Ducha extra activada hasta medianoche.");
    });
    q("btn-termo-auto").addEventListener("click", () => {
      this._callService("override_load", { load_id: "heater", action: "auto" });
    });
    q("btn-ev-boost").addEventListener("click", () => {
      this._callService("override_load", { load_id: "ev", action: "charge_now", duration_minutes: 60 });
    });
    q("btn-ev-auto").addEventListener("click", () => {
      this._callService("override_load", { load_id: "ev", action: "auto" });
    });
    q("btn-mute-byd").addEventListener("click", () => {
      this._callService("mute_ev_nag_today", {});
      this._showToast("Aviso BYD silenciado hasta medianoche.");
    });
    q("btn-bb-toggle").addEventListener("click", () => {
      this._bbOpen = !this._bbOpen;
      q("bb-body").hidden = !this._bbOpen;
      if (this._bbOpen) this._refreshBlackbox();
    });
    q("bb-filter-equip").addEventListener("change", (e) => {
      this._bbFilterEquip = e.target.value;
      this._refreshBlackbox();
    });
    q("bb-filter-type").addEventListener("change", (e) => {
      this._bbFilterType = e.target.value;
      this._refreshBlackbox();
    });
    q("btn-bb-download").addEventListener("click", async () => {
      if (!this._hass) return;
      const res = await this._hass.callWS({
        type: "hass_solar_orchestrator/blackbox",
        equipment: this._bbFilterEquip || undefined,
        event_type: this._bbFilterType || undefined,
        collapse: false,
      });
      const blob = new Blob([JSON.stringify(res.entries || [], null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `solar-orchestrator-blackbox-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
    });
    q("sel-global-mode").addEventListener("change", (e) => {
      this._callService("set_mode", { mode: e.target.value, duration_minutes: 60 });
    });
    q("btn-scan-entities").addEventListener("click", async () => {
      if (!this._hass) return;
      this._scan = await this._hass.callWS({ type: "hass_solar_orchestrator/entities_scan" });
      this._renderScan();
    });
    q("btn-toggle-adv").addEventListener("click", () => {
      this._advOpen = !this._advOpen;
      q("adv-body").hidden = !this._advOpen;
    });
    this.shadowRoot.querySelectorAll("[data-cfg]").forEach((el) => {
      el.addEventListener("input", (e) => {
        const key = e.target.dataset.cfg;
        this._draft[key] = e.target.type === "number" ? Number(e.target.value) : e.target.value;
        this._dirty = true;
        q("dirty-bar").hidden = false;
      });
    });
    q("btn-cancel-draft").addEventListener("click", () => {
      this._draft = {};
      this._dirty = false;
      q("dirty-bar").hidden = true;
      this._syncConfigInputs(true);
    });
    q("btn-save-draft").addEventListener("click", async () => {
      const data = { ...this._draft };
      this._draft = {};
      this._dirty = false;
      q("dirty-bar").hidden = true;
      await this._callService("save_settings", { data });
      this._showToast("Configuración guardada.");
    });
  }

  async _refreshBlackbox() {
    if (!this._hass) return;
    const res = await this._hass.callWS({
      type: "hass_solar_orchestrator/blackbox",
      equipment: this._bbFilterEquip || undefined,
      event_type: this._bbFilterType || undefined,
      collapse: true,
    });
    const list = this.shadowRoot.getElementById("bb-list");
    if (!list) return;
    const entries = res.entries || [];
    list.innerHTML = entries
      .map(
        (b) =>
          `<div class="bb-item">[${(b.ts || "").slice(11, 19)}] <strong>${b.type}</strong> (${b.target}) · ${b.message} ${b.count > 1 ? `<span class="pill">×${b.count}</span>` : ""} ${b.executed ? "✅" : "🛡️"}</div>`
      )
      .join("");
  }

  _renderScan() {
    const el = this.shadowRoot.getElementById("scan-results");
    if (!el || !this._scan) return;
    const roles = this._scan.roles || {};
    el.innerHTML = Object.entries(roles)
      .map(([k, v]) => {
        const icon = v.status === "ok" ? "✅" : v.status === "warning" ? "⚠️" : "❌";
        return `<div class="load-row"><div>${icon} <strong>${k}</strong>: ${v.selected || "No encontrada"}</div><span class="muted">${v.live}</span></div>`;
      })
      .join("");
  }

  _syncConfigInputs(force = false) {
    const cfg = this._snapshot?.config || {};
    this.shadowRoot.querySelectorAll("[data-cfg]").forEach((el) => {
      const key = el.dataset.cfg;
      if (!force && (this._dirty || this.shadowRoot.activeElement === el)) return;
      if (key in this._draft && !force) return;
      if (cfg[key] !== undefined && String(el.value) !== String(cfg[key])) {
        el.value = cfg[key];
      }
    });
  }

  _schedulePatch() {
    if (this._rafPending) return;
    this._rafPending = true;
    window.requestAnimationFrame(() => {
      this._rafPending = false;
      this._patchDOM();
    });
  }

  _patchDOM() {
    const d = this._snapshot;
    if (!d) return;
    const p = d.plan || {};
    const s = d.snapshot || {};
    const q = (id) => this.shadowRoot.getElementById(id);
    const setTxt = (id, v) => {
      const el = q(id);
      if (el && el.textContent !== String(v)) el.textContent = String(v);
    };

    q("view-status").hidden = this._tab !== "status";
    q("view-config").hidden = this._tab !== "config";
    q("tab-btn-status").classList.toggle("active", this._tab === "status");
    q("tab-btn-config").classList.toggle("active", this._tab === "config");
    q("shadow-banner").hidden = d.mode !== "observing";
    q("alerts-drawer").hidden = !this._alertsOpen;

    const tl = p.traffic_light || "shadow";
    const tlEl = q("hdr-tl");
    tlEl.className = `pill ${tl}`;
    setTxt("hdr-tl", tl.toUpperCase());
    setTxt("hdr-mode", d.mode === "observing" ? "🛡️ MODO SOMBRA" : d.mode === "manual_total" ? "⏸️ MANUAL TOTAL" : `⚡ ${d.mode.toUpperCase()}`);
    setTxt("hdr-icp-txt", `Margen ICP: ${p.icp_headroom_w !== null && p.icp_headroom_w !== undefined ? this._fmtW(p.icp_headroom_w) : "no disponible"}`);
    const icpPct = p.icp_load_ratio_pct ?? 0;
    const bar = q("hdr-icp-bar");
    bar.style.width = `${Math.min(100, icpPct)}%`;
    bar.className = `icp-fill ${icpPct >= 90 ? "crit" : icpPct >= 75 ? "warn" : ""}`;
    setTxt("hdr-alert-cnt", (p.alerts || []).length);

    setTxt("st-headline", p.human_headline || "—");
    q("st-washer").hidden = !p.washer_suggestion;
    if (p.washer_suggestion) setTxt("st-washer", `🧺 ${p.washer_suggestion}`);
    q("st-storm").hidden = !p.storm_alert;
    q("st-bat-advice").hidden = !p.battery_advice;
    if (p.battery_advice) setTxt("st-bat-advice", `🔋 ${p.battery_advice}`);
    q("st-climate-advice").hidden = !p.climate_advice;
    if (p.climate_advice) setTxt("st-climate-advice", `❄️ ${p.climate_advice}`);

    q("byd-nag-banner").hidden = !p.byd_unplugged_surplus_alert;
    if (p.byd_unplugged_surplus_alert) {
      setTxt("byd-nag-detail", `SoC ${s.ev_soc ?? "—"}% · Excedente libre ${this._fmtW(p.usable_surplus_w)}`);
    }

    setTxt("val-solar", this._fmtW(s.solar_w));
    setTxt("fr-solar", this._fmtFresh((p.freshness_by_source || {}).modbus_solar));
    setTxt("val-house", this._fmtW(s.house_w));
    const gridDir = s.grid_w === null || s.grid_w === undefined ? "—" : s.grid_w > 20 ? `Importa ${this._fmtW(s.grid_w)}` : s.grid_w < -20 ? `Exporta ${this._fmtW(-s.grid_w)}` : "0 W";
    setTxt("val-grid", gridDir);
    setTxt("fr-grid", this._fmtFresh((p.freshness_by_source || {}).modbus_grid));
    setTxt("val-batsoc", s.battery_soc ?? "—");
    const batDir = s.battery_w === null || s.battery_w === undefined ? "—" : s.battery_w < -20 ? `Carga ${this._fmtW(-s.battery_w)}` : s.battery_w > 20 ? `Descarga ${this._fmtW(s.battery_w)}` : "Reposo";
    setTxt("val-bat", batDir);

    q("svg-l-solar").classList.toggle("idle", !(s.solar_w > 50));
    q("svg-l-bat").classList.toggle("idle", !(Math.abs(s.battery_w || 0) > 50));
    q("svg-l-grid").classList.toggle("idle", !(Math.abs(s.grid_w || 0) > 50));
    q("svg-l-ev").classList.toggle("idle", !(s.ev_power_w > 50));
    q("svg-l-heater").classList.toggle("idle", !(s.heater_power_w > 50));

    setTxt("val-termo-temp", s.heater_temp_c !== null && s.heater_temp_c !== undefined ? Number(s.heater_temp_c).toFixed(1) : "—");
    setTxt("val-termo-pwr", p.heater_thermostat_idle ? "Reposo termostato" : this._fmtW(s.heater_power_w));
    setTxt("badge-termo-state", p.shower_veto_active ? "🚿 Ducha / Veto" : p.extra_shower_active ? "🚿 Ducha extra" : s.heater_on ? "ON" : "OFF");
    const hLoad = (p.flex_loads || []).find((x) => x.load_id === "heater");
    setTxt("val-termo-reason", hLoad?.reason || "—");

    setTxt("val-ev-soc", s.ev_soc ?? "—");
    setTxt("val-ev-pwr", this._fmtW(s.ev_power_w));
    setTxt("val-ev-amps", s.ev_current_a ?? "—");
    setTxt("badge-ev-mode", (d.load_modes || {}).ev || "observing");
    setTxt("val-ev-reason", (p.why_by_device || {}).ev || "—");

    setTxt("val-pool-min", s.pool_minutes_today ?? 0);
    setTxt("val-pool-max", s.pool_daily_max_minutes ?? 360);
    setTxt("badge-pool-mode", (d.load_modes || {}).pool || "observing");
    const poolPct = Math.min(100, ((s.pool_minutes_today || 0) / Math.max(1, s.pool_daily_max_minutes || 360)) * 100);
    q("pool-bar").style.width = `${poolPct}%`;

    const savTxt = p.estimated_savings_today_eur !== null && p.estimated_savings_today_eur !== undefined ? `${p.estimated_savings_today_eur.toFixed(2)} € / ${Number(p.estimated_savings_month_eur || 0).toFixed(2)} €` : "Sin tarifa";
    setTxt("val-savings", savTxt);
    setTxt("val-cloud-pocket", Number(p.cloud_pocket_used_kwh_today || 0).toFixed(2));
    setTxt("val-sem-tmw", s.solar_forecast_tomorrow_kwh ?? "—");
    setTxt("bb-count", (d.blackbox || []).length);

    // Forecast strip
    const fc = s.weather_daily_forecast || [];
    q("fc-strip").innerHTML = fc.length
      ? fc.map((day) => `<div class="fc-day"><strong>${(day.datetime || "").slice(0, 10)}</strong><div>${day.condition || "—"}</div><div>${day.templow ?? "—"}° / ${day.temperature ?? "—"}°</div></div>`).join("")
      : '<div class="muted">Sin previsión diaria cacheada aún.</div>';

    // Granular loads list in Config (only update if not hovering/editing)
    const lm = d.load_modes || { pool: "observing", heater: "observing", ev: "observing" };
    const conflicts = d.conflicts || [];
    const glEl = q("granular-loads-list");
    if (glEl && this.shadowRoot.activeElement?.closest("#granular-loads-list") === null) {
      glEl.innerHTML = ["pool", "heater", "ev"]
        .map((lid) => {
          const mode = lm[lid] || "observing";
          const c = conflicts.find((x) => x.load === lid);
          const label = lid === "pool" ? "Depuradora" : lid === "heater" ? "Termo eléctrico" : "Vehículo eléctrico";
          return `<div class="load-row">
            <div><strong>${label}</strong>${c ? `<div class="muted">🔒 Conflicto: ${c.entity_id}</div>` : ""}</div>
            <div class="row">
              <button class="btn ${mode === "observing" ? "primary" : ""} btn-lm" data-lid="${lid}" data-mode="observing">Observación</button>
              <button class="btn ${mode === "active" ? "primary" : ""} btn-lm" data-lid="${lid}" data-mode="active" ${c ? "disabled" : ""}>Activo</button>
            </div>
          </div>`;
        })
        .join("");
      glEl.querySelectorAll(".btn-lm").forEach((b) => {
        b.addEventListener("click", () => this._callService("set_load_mode", { load_id: b.dataset.lid, mode: b.dataset.mode }));
      });
    }

    if (q("sel-global-mode") && this.shadowRoot.activeElement !== q("sel-global-mode")) {
      q("sel-global-mode").value = d.mode;
    }
    this._syncConfigInputs(false);
    if (this._bbOpen) this._refreshBlackbox();
  }
}

customElements.define("solar-orchestrator-panel", SolarOrchestratorPanel);
