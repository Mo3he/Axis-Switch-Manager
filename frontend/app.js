/* =========================================================
   Axis Switch Manager - Application JS (v2)
   ========================================================= */

const API = "/api";
let currentView = "dashboard";
let currentSwitchId = null;
let currentSwitchName = null;
let previousView = "dashboard";
let scanResults = [];
let allSwitches = [];

// Auto-refresh timers
let _dashTimer = null;
let _detailTimer = null;
const DEFAULT_DASH_INTERVAL = 30000;
const DEFAULT_DETAIL_INTERVAL = 15000;

function _getSettings() {
  return {
    dashInterval: parseInt(localStorage.getItem("dashInterval") || DEFAULT_DASH_INTERVAL),
    detailInterval: parseInt(localStorage.getItem("detailInterval") || DEFAULT_DETAIL_INTERVAL),
  };
}

function _startDashTimer() {
  _stopAllTimers();
  const { dashInterval } = _getSettings();
  _dashTimer = setInterval(() => {
    if (currentView === "dashboard") refreshDashboard();
  }, dashInterval);
}

function _startDetailTimer() {
  _stopAllTimers();
  const { detailInterval } = _getSettings();
  _detailTimer = setInterval(() => {
    if (currentView === "switch-detail" && currentSwitchId) {
      // Only refresh live-data tabs, not the configure tab
      loadOverview(currentSwitchId);
      loadPorts(currentSwitchId);
      loadPoe(currentSwitchId);
      loadTraffic(currentSwitchId);
    }
  }, detailInterval);
}

function _stopAllTimers() {
  if (_dashTimer) { clearInterval(_dashTimer); _dashTimer = null; }
  if (_detailTimer) { clearInterval(_detailTimer); _detailTimer = null; }
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

function showView(name) {
  document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
  document.querySelectorAll(".nav-link").forEach(a => a.classList.remove("active"));
  const view = document.getElementById(`view-${name}`);
  if (view) view.classList.add("active");
  const link = document.querySelector(`.nav-link[data-view="${name}"]`);
  if (link) link.classList.add("active");
  currentView = name;
}

function goBack() {
  _stopAllTimers();
  showView(previousView || "dashboard");
  if (previousView === "dashboard") { refreshDashboard(); _startDashTimer(); }
}

document.querySelectorAll(".nav-link").forEach(link => {
  link.addEventListener("click", e => {
    e.preventDefault();
    const view = link.dataset.view;
    if (view === "dashboard") { refreshDashboard(); _startDashTimer(); }
    else if (view === "switches") { loadSwitchesList(); _stopAllTimers(); }
    else if (view === "topology") { loadTopologyView(); _stopAllTimers(); }
    else if (view === "settings") { loadSettingsView(); _stopAllTimers(); }
    showView(view);
  });
});

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1048576) return `${(bytes/1024).toFixed(1)} KB`;
  if (bytes < 1073741824) return `${(bytes/1048576).toFixed(1)} MB`;
  return `${(bytes/1073741824).toFixed(2)} GB`;
}

function toast(msg, type = "") {
  const el = document.createElement("div");
  el.className = `toast${type ? " " + type : ""}`;
  el.textContent = msg;
  document.getElementById("toast-container").appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

async function apiFetch(path, opts = {}) {
  try {
    const resp = await fetch(`${API}${path}`, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || resp.statusText);
    }
    return resp.json();
  } catch (e) {
    toast(e.message, "error");
    throw e;
  }
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------

async function refreshDashboard() {
  const container = document.getElementById("switch-cards");
  const summaryEl = document.getElementById("summary-cards");

  // Only show spinner on first load (container is empty), not on auto-refresh
  const firstLoad = container.innerHTML.trim() === "";
  if (firstLoad) container.innerHTML = `<div class="spinner"></div>`;

  let data;
  try {
    data = await apiFetch("/dashboard");
  } catch {
    if (firstLoad) {
      container.innerHTML = `<div class="empty-state"><h3>Could not load dashboard</h3><p>Is the backend running?</p></div>`;
    }
    return;
  }

  // Summary cards - update only changed values to avoid repaint
  const online = data.filter(s => s.status === "online").length;
  const totalPoE = data.reduce((sum, s) => sum + s.total_poe_watts, 0);
  const totalActivePorts = data.reduce((sum, s) => sum + s.active_poe_ports, 0);

  const newSummary = `
    <div class="summary-card">
      <div class="label">Total Switches</div>
      <div class="value">${data.length}</div>
    </div>
    <div class="summary-card">
      <div class="label">Online</div>
      <div class="value" style="color:#00A88F">${online}</div>
    </div>
    <div class="summary-card">
      <div class="label">Total PoE Load</div>
      <div class="value">${totalPoE.toFixed(1)}<span class="unit">W</span></div>
    </div>
    <div class="summary-card">
      <div class="label">Active PoE Ports</div>
      <div class="value">${totalActivePorts}</div>
    </div>
  `;
  if (summaryEl.innerHTML !== newSummary) summaryEl.innerHTML = newSummary;

  if (!data.length) {
    const empty = `<div class="empty-state"><h3>No switches configured</h3><p>Go to <a href="#" onclick="loadSwitchesList();showView('switches')">Switches</a> to add your first switch.</p></div>`;
    if (container.innerHTML !== empty) container.innerHTML = empty;
    return;
  }

  const newCards = data.map(sw => renderSwitchCard(sw)).join("");
  if (container.innerHTML !== newCards) container.innerHTML = newCards;
}

function renderSwitchCard(sw) {
  const ov = sw.overview || {};
  const model = ov["Model Name"] || "Unknown";
  const firmware = ov["Firmware Version"] || "-";
  const uptime = ov["System Uptime"] || "-";
  const statusBadge = sw.status === "online"
    ? `<span class="badge badge-online">Online</span>`
    : `<span class="badge badge-offline">Offline</span>`;

  return `
    <div class="switch-card" onclick="openSwitchDetail('${sw.id}','${escHtml(sw.name)}')">
      <div class="switch-card-header">
        <div class="switch-icon">
          <svg viewBox="0 0 22 14" fill="none">
            <rect width="22" height="14" rx="2" fill="rgba(255,255,255,0.15)"/>
            <rect x="2" y="3" width="18" height="3" rx="1" fill="white"/>
            <rect x="2" y="8" width="18" height="3" rx="1" fill="white"/>
            ${[2,5,8,11,14,17].map(x=>`<circle cx="${x+1}" cy="4.5" r="0.9" fill="#00C4A7"/>`).join("")}
          </svg>
        </div>
        <div style="flex:1">
          <div class="switch-name">${escHtml(sw.name)}</div>
          <div class="switch-model">${escHtml(model)}</div>
          <div class="switch-ip">${escHtml(sw.ip)}</div>
        </div>
        ${statusBadge}
      </div>
      <div class="switch-card-body">
        <div class="switch-stats">
          <div class="stat-item">
            <div class="stat-label">Firmware</div>
            <div class="stat-value">${escHtml(firmware)}</div>
          </div>
          <div class="stat-item">
            <div class="stat-label">Uptime</div>
            <div class="stat-value">${escHtml(uptime)}</div>
          </div>
          <div class="stat-item">
            <div class="stat-label">Active PoE Ports</div>
            <div class="stat-value">${sw.active_poe_ports}</div>
          </div>
          <div class="stat-item">
            <div class="stat-label">PoE Load</div>
            <div class="stat-value">${sw.total_poe_watts.toFixed(1)} W</div>
          </div>
        </div>
      </div>
    </div>`;
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ---------------------------------------------------------------------------
// Switches list
// ---------------------------------------------------------------------------

async function loadSwitchesList() {
  const el = document.getElementById("switches-list");
  el.innerHTML = `<div class="spinner"></div>`;
  try {
    allSwitches = await apiFetch("/switches");
  } catch {
    el.innerHTML = `<div class="empty-state"><h3>Failed to load switches</h3></div>`;
    return;
  }

  if (!allSwitches.length) {
    el.innerHTML = `<div class="empty-state"><h3>No switches yet</h3><p>Click "Scan Network" or "+ Add Switch" to get started.</p></div>`;
    return;
  }

  el.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Name</th>
          <th>IP Address</th>
          <th>Username</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        ${allSwitches.map(sw => `
          <tr>
            <td><strong>${escHtml(sw.name)}</strong></td>
            <td><a href="http://${escHtml(sw.ip)}" target="_blank" rel="noopener noreferrer">${escHtml(sw.ip)}</a></td>
            <td>${escHtml(sw.username)}</td>
            <td>
              <button class="btn btn-ghost btn-sm" onclick="openSwitchDetail('${sw.id}','${escHtml(sw.name)}')">Details</button>
              <button class="btn btn-ghost btn-sm" onclick="openEditSwitchModal('${sw.id}','${escHtml(sw.name)}','${escHtml(sw.ip)}','${escHtml(sw.username)}')">Edit</button>
              <button class="btn btn-danger btn-sm" onclick="deleteSwitch('${sw.id}','${escHtml(sw.name)}')">Delete</button>
            </td>
          </tr>`).join("")}
      </tbody>
    </table>`;
}

// ---------------------------------------------------------------------------
// Switch Detail
// ---------------------------------------------------------------------------

// Resolve topology node (may have sw_{ip} id) to managed switch, then open detail
async function openTopoSwitch(nodeId, ip, name) {
  // If nodeId looks like a sw_{ip} form, look up the real switch ID by IP
  if (nodeId.startsWith('sw_') && ip) {
    try {
      const switches = await apiFetch('/switches');
      const match = switches.find(s => s.ip === ip);
      if (match) {
        openSwitchDetail(match.id, match.name || name);
        return;
      }
    } catch (_) {}
  }
  // Fallback: use nodeId as-is (works for legacy internal IDs)
  openSwitchDetail(nodeId, name);
}

async function openSwitchDetail(id, name) {
  previousView = currentView;
  currentSwitchId = id;
  currentSwitchName = name;
  document.getElementById("detail-title").textContent = name;
  // Reset to first tab
  document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
  document.querySelectorAll(".tab-content").forEach(t => t.classList.remove("active"));
  document.querySelector(".tab[data-tab='ports']").classList.add("active");
  document.getElementById("tab-ports").classList.add("active");
  showView("switch-detail");
  _startDetailTimer();
  await refreshDetail();
}

async function refreshDetail() {
  if (!currentSwitchId) return;
  loadOverview(currentSwitchId);
  loadPorts(currentSwitchId);
  loadPoe(currentSwitchId);
  loadTraffic(currentSwitchId);
  loadConfigTab(currentSwitchId);
}

async function loadOverview(id) {
  const el = document.getElementById("detail-overview");
  if (!el.innerHTML.trim()) el.innerHTML = `<div class="spinner"></div>`;
  let data;
  try {
    data = await apiFetch(`/switches/${id}/overview`);
  } catch {
    el.innerHTML = `<div class="empty-state"><h3>Cannot connect to switch</h3></div>`;
    return;
  }

  const keys = [
    "Model Name", "Firmware Version", "Hardware Version",
    "MAC Address", "IP Address", "Subnet Mask", "Gateway",
    "System Uptime", "System Name", "Location",
    "PoE Power Consumption", "Total PoE Available", "Connected Devices",
  ];
  el.innerHTML = keys
    .filter(k => data[k] !== undefined && data[k] !== "")
    .map(k => {
      let val = data[k];
      // PoE values from sys_overview are in tenths of watts
      if (k === "PoE Power Consumption" || k === "Total PoE Available") {
        val = (parseInt(val) / 10).toFixed(1) + " W";
      }
      return `
      <div class="info-card">
        <div class="key">${escHtml(k)}</div>
        <div class="val">${escHtml(val)}</div>
      </div>`;
    }).join("");
}

async function loadPorts(id) {
  const el = document.getElementById("tab-ports");
  if (!el.innerHTML.trim()) el.innerHTML = `<div class="spinner"></div>`;
  let ports;
  try {
    ports = await apiFetch(`/switches/${id}/ports`);
  } catch {
    el.innerHTML = `<div class="empty-state"><h3>Could not load port data</h3></div>`;
    return;
  }

  const newPorts = `
    <div class="ports-table-wrap">
      <table>
        <thead>
          <tr>
            <th>Port</th>
            <th>Admin</th>
            <th>Link</th>
            <th>Speed</th>
            <th>PoE Status</th>
            <th>PoE Power</th>
          </tr>
        </thead>
        <tbody>
          ${ports.map(p => {
            const isUp = p.link_state === "Up";
            const poe = p.poe || {};
            const poeStat = poe.status || "-";
            const poeOn = poeStat.includes("ON");
            return `
              <tr>
                <td><strong>Port ${p.port}</strong></td>
                <td>${p.admin_enabled
                  ? `<span class="badge badge-online">Enabled</span>`
                  : `<span class="badge badge-offline">Disabled</span>`}</td>
                <td class="${isUp ? "link-up" : "link-down"}">${isUp ? "&#9679; Up" : "&#9675; Down"}</td>
                <td>${isUp ? escHtml(p.speed || "-") : "-"}</td>
                <td>${poe.status ? escHtml(poeStat) : "No PoE"}</td>
                <td>${poeOn ? `${poe.current_power.toFixed(1)} W` : "-"}</td>
              </tr>`;
          }).join("")}
        </tbody>
      </table>
    </div>`;
  if (el.innerHTML !== newPorts) el.innerHTML = newPorts;
}

async function loadPoe(id) {
  const el = document.getElementById("tab-poe");
  if (!el.innerHTML.trim()) el.innerHTML = `<div class="spinner"></div>`;
  let ports;
  try {
    ports = await apiFetch(`/switches/${id}/poe`);
  } catch {
    el.innerHTML = `<div class="empty-state"><h3>Could not load PoE data</h3></div>`;
    return;
  }

  if (!ports.length) {
    el.innerHTML = `<div class="empty-state"><h3>No PoE data available</h3></div>`;
    return;
  }

  const newPoe = `<div class="poe-grid">${ports.map(p => {
    const on = p.status && p.status.includes("ON");
    const pct = p.max_power > 0 ? Math.min(100, (p.current_power / p.max_power) * 100) : 0;
    return `
      <div class="poe-card">
        <div class="port-label">Port ${p.port}</div>
        <div class="poe-status ${on ? "on" : "off"}">${on ? "&#9889; Active" : "No PD"}</div>
        <div class="poe-meter">
          <div class="poe-meter-fill" style="width:${pct.toFixed(0)}%"></div>
        </div>
        <div class="poe-details">
          <span>${p.current_power.toFixed(1)} W / ${p.max_power.toFixed(1)} W max</span>
          ${p.voltage > 0 ? `<span>${p.voltage.toFixed(1)} V &bull; ${p.current_ma} mA</span>` : ""}
          ${p.poe_class ? `<span>Class ${p.poe_class}</span>` : ""}
          <span>${escHtml(p.status)}</span>
        </div>
      </div>`;
  }).join("")}</div>`;
  if (el.innerHTML !== newPoe) el.innerHTML = newPoe;
}

async function loadTraffic(id) {
  const el = document.getElementById("tab-traffic");
  if (!el.innerHTML.trim()) el.innerHTML = `<div class="spinner"></div>`;
  let ports;
  try {
    ports = await apiFetch(`/switches/${id}/traffic`);
  } catch {
    el.innerHTML = `<div class="empty-state"><h3>Could not load traffic data</h3></div>`;
    return;
  }

  const newTraffic = `
    <div class="traffic-table-wrap">
      <table>
        <thead>
          <tr>
            <th>Port</th>
            <th>RX Packets</th>
            <th>TX Packets</th>
            <th>RX Bytes</th>
            <th>TX Bytes</th>
            <th>RX Errors</th>
            <th>TX Errors</th>
            <th>RX Drops</th>
          </tr>
        </thead>
        <tbody>
          ${ports.map(p => `
            <tr>
              <td><strong>Port ${p.port}</strong></td>
              <td>${p.rx_packets.toLocaleString()}</td>
              <td>${p.tx_packets.toLocaleString()}</td>
              <td>${formatBytes(p.rx_bytes)}</td>
              <td>${formatBytes(p.tx_bytes)}</td>
              <td class="${p.rx_errors > 0 ? "link-down" : ""}">${p.rx_errors}</td>
              <td class="${p.tx_errors > 0 ? "link-down" : ""}">${p.tx_errors}</td>
              <td>${p.rx_drops}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
  if (el.innerHTML !== newTraffic) el.innerHTML = newTraffic;
}

// ---------------------------------------------------------------------------
// Configure Tab
// ---------------------------------------------------------------------------

let _cfgPortsCache = [];
let _cfgPoeCache = [];

// ---------------------------------------------------------------------------
// Configure Tab - full comprehensive config
// ---------------------------------------------------------------------------

let _cfgAllData = {};

async function loadConfigTab(id) {
  const el = document.getElementById("tab-configure");
  el.innerHTML = `<div class="spinner"></div>`;

  let sysConf, poeConf, portConf, ntpConf, portDesc, loopConf, vlanConf, pvlanConf, aggrConf, snmpConf;
  try {
    const all = await apiFetch(`/switches/${id}/config/all`);
    sysConf   = all.system;
    poeConf   = all.poe;
    portConf  = all.ports;
    ntpConf   = all.ntp;
    portDesc  = all.ports_desc;
    loopConf  = all.loop;
    vlanConf  = all.vlan;
    pvlanConf = all.pvlan;
    aggrConf  = all.aggregation;
    snmpConf  = all.snmp;
  } catch {
    el.innerHTML = `<div class="empty-state"><h3>Could not load configuration</h3></div>`;
    return;
  }

  _cfgPortsCache = portConf;
  _cfgPoeCache = poeConf.ports || [];
  _cfgAllData = { sysConf, poeConf, portConf, ntpConf, portDesc, loopConf, vlanConf, pvlanConf, aggrConf, snmpConf };

  const descByPort = Object.fromEntries((portDesc || []).map(p => [p.port, p.description]));
  const loopByPort = Object.fromEntries((loopConf.ports || []).map(p => [p.port, p]));
  const vlanByPort = Object.fromEntries((vlanConf.ports || []).map(p => [p.port, p]));
  const pvlanByPort = Object.fromEntries((pvlanConf.ports || []).map(p => [p.port, p]));

  el.innerHTML = `
    <div class="cfg-sections">

      <!-- System Info -->
      <details class="cfg-panel" open>
        <summary>System Information</summary>
        <div class="cfg-panel-body">
          <div class="cfg-two-col">
            <div class="form-group">
              <label>System Name</label>
              <input type="text" id="cfg-sys-name" value="${escHtml(sysConf.sys_name || '')}" maxlength="45" />
            </div>
            <div class="form-group">
              <label>Contact</label>
              <input type="text" id="cfg-sys-contact" value="${escHtml(sysConf.sys_contact || '')}" maxlength="255" />
            </div>
            <div class="form-group" style="grid-column:1/-1">
              <label>Location</label>
              <input type="text" id="cfg-sys-location" value="${escHtml(sysConf.sys_location || '')}" maxlength="255" />
            </div>
          </div>
          <div class="cfg-actions"><button class="btn btn-primary btn-sm" onclick="saveSystemConfig('${id}')">Save System Info</button></div>
        </div>
      </details>

      <!-- NTP -->
      <details class="cfg-panel">
        <summary>NTP / Time Server</summary>
        <div class="cfg-panel-body">
          <div class="cfg-two-col">
            <div class="form-group">
              <label>NTP Mode</label>
              <select id="cfg-ntp-mode">
                <option value="0" ${ntpConf.mode === 0 ? 'selected' : ''}>Disabled</option>
                <option value="1" ${ntpConf.mode === 1 ? 'selected' : ''}>Enabled</option>
              </select>
            </div>
            <div class="form-group">
              <label>Sync Interval (s)</label>
              <input type="number" id="cfg-ntp-interval" value="${ntpConf.interval || 3600}" min="60" max="86400" />
            </div>
            ${[1,2,3,4,5].map(n => `
            <div class="form-group">
              <label>Server ${n}</label>
              <input type="text" id="cfg-ntp-server${n}" value="${escHtml(ntpConf['server' + n] || '')}" placeholder="hostname or IP" />
            </div>`).join('')}
          </div>
          <div class="cfg-actions"><button class="btn btn-primary btn-sm" onclick="saveNtpConfig('${id}')">Save NTP</button></div>
        </div>
      </details>

      <!-- Port Admin + Description + Speed -->
      <details class="cfg-panel" open>
        <summary>Port Configuration</summary>
        <div class="cfg-panel-body" style="padding:0">
          <table class="config-port-table">
            <thead>
              <tr>
                <th>Port</th>
                <th>Link</th>
                <th>Admin</th>
                <th>Flow Ctrl</th>
                <th>Max Frame</th>
                <th>Description</th>
              </tr>
            </thead>
            <tbody>
              ${portConf.map(p => `
                <tr>
                  <td><strong>Port ${p.port}</strong></td>
                  <td class="${p.link_state === 'Up' ? 'link-up' : 'link-down'}">${p.link_state === 'Up' ? '&#9679; Up' : '&#9675; Down'}</td>
                  <td>
                    <label class="toggle-switch">
                      <input type="checkbox" id="port-admin-${p.port}" ${p.admin_enabled ? 'checked' : ''} />
                      <span class="toggle-slider"></span>
                    </label>
                  </td>
                  <td>
                    <label class="toggle-switch">
                      <input type="checkbox" id="port-flow-${p.port}" ${p.flow_ctrl ? 'checked' : ''} />
                      <span class="toggle-slider"></span>
                    </label>
                  </td>
                  <td><input type="number" id="port-mtu-${p.port}" value="${p.max_frame || 9600}" min="1518" max="9600" style="width:72px;padding:3px 6px;border:1px solid var(--border);border-radius:5px;font-size:12px" /></td>
                  <td><input type="text" id="port-desc-${p.port}" value="${escHtml(descByPort[p.port] || '')}" maxlength="47" placeholder="Description..." style="width:160px;padding:3px 8px;border:1px solid var(--border);border-radius:5px;font-size:12px" /></td>
                </tr>`).join('')}
            </tbody>
          </table>
          <div class="cfg-actions" style="padding:12px 16px;display:flex;gap:8px">
            <button class="btn btn-primary btn-sm" onclick="savePortFullConfig('${id}')">Save Port Settings</button>
            <button class="btn btn-ghost btn-sm" onclick="savePortsDesc('${id}')">Save Descriptions</button>
          </div>
        </div>
      </details>

      <!-- PoE -->
      ${_cfgPoeCache.length ? `
      <details class="cfg-panel">
        <summary>PoE Power Configuration</summary>
        <div class="cfg-panel-body" style="padding:0">
          <table class="config-port-table">
            <thead><tr><th>Port</th><th>PoE</th><th>Priority</th><th>Max Power</th><th>Live Power</th><th>Status</th></tr></thead>
            <tbody>
              ${_cfgPoeCache.map(p => {
                const livePoe = (portConf.find(pp => pp.port === p.port) || {}).poe || {};
                return `
                <tr>
                  <td><strong>Port ${p.port}</strong></td>
                  <td>
                    <label class="toggle-switch">
                      <input type="checkbox" id="poe-enabled-${p.port}" ${p.poe_enabled ? 'checked' : ''} />
                      <span class="toggle-slider"></span>
                    </label>
                  </td>
                  <td>
                    <select class="priority-sel" id="poe-priority-${p.port}">
                      <option value="1" ${p.priority === 1 ? 'selected' : ''}>Low</option>
                      <option value="2" ${p.priority === 2 ? 'selected' : ''}>High</option>
                      <option value="3" ${p.priority === 3 ? 'selected' : ''}>Critical</option>
                    </select>
                  </td>
                  <td>${p.max_power_w.toFixed(1)} W</td>
                  <td>${livePoe.current_power != null ? livePoe.current_power.toFixed(1) + ' W' : '-'}</td>
                  <td>${livePoe.status ? escHtml(livePoe.status) : '-'}</td>
                </tr>`;
              }).join('')}
            </tbody>
          </table>
          <div class="cfg-actions" style="padding:12px 16px">
            <button class="btn btn-primary btn-sm" onclick="savePoeConfig('${id}')">Save PoE</button>
          </div>
        </div>
      </details>` : ''}

      <!-- Loop Protection -->
      <details class="cfg-panel">
        <summary>Loop Protection</summary>
        <div class="cfg-panel-body">
          <div class="cfg-two-col" style="margin-bottom:16px">
            <div class="form-group">
              <label>Global Loop Protection</label>
              <select id="cfg-loop-global">
                <option value="1" ${loopConf.global_enable ? 'selected' : ''}>Enabled</option>
                <option value="0" ${!loopConf.global_enable ? 'selected' : ''}>Disabled</option>
              </select>
            </div>
            <div class="form-group">
              <label>TX Interval (s)</label>
              <input type="number" id="cfg-loop-interval" value="${loopConf.tx_interval || 5}" min="1" max="10" />
            </div>
            <div class="form-group">
              <label>Shutdown Time (s)</label>
              <input type="number" id="cfg-loop-shutdown" value="${loopConf.shutdown_time || 180}" min="0" max="604800" />
            </div>
          </div>
          <table class="config-port-table">
            <thead><tr><th>Port</th><th>Enable</th><th>Action</th><th>TX Mode</th></tr></thead>
            <tbody>
              ${(loopConf.ports || []).map(p => `
                <tr>
                  <td><strong>Port ${p.port}</strong></td>
                  <td>
                    <label class="toggle-switch">
                      <input type="checkbox" id="loop-enable-${p.port}" ${p.enable ? 'checked' : ''} />
                      <span class="toggle-slider"></span>
                    </label>
                  </td>
                  <td>
                    <select class="priority-sel" id="loop-action-${p.port}">
                      <option value="0" ${p.action === 0 ? 'selected' : ''}>Shutdown Port</option>
                      <option value="1" ${p.action === 1 ? 'selected' : ''}>Shutdown + Log</option>
                      <option value="2" ${p.action === 2 ? 'selected' : ''}>Log Only</option>
                    </select>
                  </td>
                  <td>
                    <label class="toggle-switch">
                      <input type="checkbox" id="loop-txmode-${p.port}" ${p.tx_mode ? 'checked' : ''} />
                      <span class="toggle-slider"></span>
                    </label>
                  </td>
                </tr>`).join('')}
            </tbody>
          </table>
          <div class="cfg-actions"><button class="btn btn-primary btn-sm" onclick="saveLoopConfig('${id}')">Save Loop Protection</button></div>
        </div>
      </details>

      <!-- VLAN -->
      <details class="cfg-panel">
        <summary>VLAN Port Configuration</summary>
        <div class="cfg-panel-body" style="padding:0">
          <table class="config-port-table">
            <thead>
              <tr>
                <th>Port</th>
                <th>Mode</th>
                <th>PVID</th>
                <th>Frame Type</th>
                <th>Ingress Filter</th>
                <th>TX Tag</th>
                <th>Allowed VLANs</th>
              </tr>
            </thead>
            <tbody>
              ${(vlanConf.ports || []).map(p => `
                <tr>
                  <td><strong>Port ${p.port}</strong></td>
                  <td>
                    <select class="priority-sel" id="vlan-mode-${p.port}">
                      <option value="0" ${p.mode === 0 ? 'selected' : ''}>Access</option>
                      <option value="1" ${p.mode === 1 ? 'selected' : ''}>Trunk</option>
                      <option value="2" ${p.mode === 2 ? 'selected' : ''}>Hybrid</option>
                    </select>
                  </td>
                  <td><input type="number" id="vlan-pvid-${p.port}" value="${p.pvid}" min="1" max="4095" style="width:60px;padding:3px 6px;border:1px solid var(--border);border-radius:5px;font-size:12px" /></td>
                  <td>
                    <select class="priority-sel" id="vlan-frame-${p.port}">
                      <option value="0" ${p.frame_type === 0 ? 'selected' : ''}>All</option>
                      <option value="1" ${p.frame_type === 1 ? 'selected' : ''}>Tagged+Untagged</option>
                      <option value="2" ${p.frame_type === 2 ? 'selected' : ''}>Tagged Only</option>
                    </select>
                  </td>
                  <td>
                    <label class="toggle-switch">
                      <input type="checkbox" id="vlan-ingress-${p.port}" ${p.ingress_filter ? 'checked' : ''} />
                      <span class="toggle-slider"></span>
                    </label>
                  </td>
                  <td>
                    <select class="priority-sel" id="vlan-txtag-${p.port}">
                      <option value="0" ${p.tx_tag === 0 ? 'selected' : ''}>Untag PVID</option>
                      <option value="2" ${p.tx_tag === 2 ? 'selected' : ''}>Tag All</option>
                      <option value="3" ${p.tx_tag === 3 ? 'selected' : ''}>Untag All</option>
                    </select>
                  </td>
                  <td><input type="text" id="vlan-allowed-${p.port}" value="${escHtml(p.allowed_vlans || '1-4095')}" placeholder="e.g. 1-4095" style="width:90px;padding:3px 6px;border:1px solid var(--border);border-radius:5px;font-size:12px" /></td>
                </tr>`).join('')}
            </tbody>
          </table>
          <div class="cfg-actions" style="padding:12px 16px">
            <button class="btn btn-primary btn-sm" onclick="saveVlanConfig('${id}')">Save VLAN</button>
          </div>
        </div>
      </details>

      <!-- Private VLAN -->
      <details class="cfg-panel">
        <summary>Private VLAN (Port Isolation)</summary>
        <div class="cfg-panel-body">
          <p class="hint-text">Set each port to Promiscuous (can talk to all) or Isolated (can only talk to promiscuous ports).</p>
          <table class="config-port-table">
            <thead><tr><th>Port</th><th>Link</th><th>PVLAN Mode</th></tr></thead>
            <tbody>
              ${(pvlanConf.ports || []).map(p => `
                <tr>
                  <td><strong>Port ${p.port}</strong></td>
                  <td class="${(portConf.find(pp => pp.port === p.port) || {}).link_state === 'Up' ? 'link-up' : 'link-down'}">${(portConf.find(pp => pp.port === p.port) || {}).link_state === 'Up' ? '&#9679; Up' : '&#9675; Down'}</td>
                  <td>
                    <select class="priority-sel" id="pvlan-mode-${p.port}">
                      <option value="1" ${p.mode === 1 ? 'selected' : ''}>Promiscuous</option>
                      <option value="0" ${p.mode === 0 ? 'selected' : ''}>Isolated</option>
                    </select>
                  </td>
                </tr>`).join('')}
            </tbody>
          </table>
          <div class="cfg-actions"><button class="btn btn-primary btn-sm" onclick="savePvlanConfig('${id}')">Save Private VLAN</button></div>
        </div>
      </details>

      <!-- Link Aggregation (read only display) -->
      ${aggrConf && aggrConf.groups ? `
      <details class="cfg-panel">
        <summary>Link Aggregation (Current State)</summary>
        <div class="cfg-panel-body">
          ${aggrConf.groups.length > 0
            ? `<table class="config-port-table">
                <thead><tr><th>Group</th><th>Member Ports</th></tr></thead>
                <tbody>
                  ${aggrConf.groups.map(g => `<tr><td>Group ${g.group}</td><td>${g.ports.map(p => 'Port ' + p).join(', ')}</td></tr>`).join('')}
                </tbody>
              </table>`
            : '<p class="hint-text">No active aggregation groups configured.</p>'}
          <p class="hint-text" style="margin-top:8px">To change aggregation settings, access the switch web UI directly.</p>
        </div>
      </details>` : ''}

      <!-- SNMP -->
      <details class="cfg-panel">
        <summary>SNMP</summary>
        <div class="cfg-panel-body">
          <div class="cfg-two-col">
            <div class="form-group">
              <label>SNMP</label>
              <select id="cfg-snmp-enabled">
                <option value="1" ${snmpConf.enabled ? 'selected' : ''}>Enabled</option>
                <option value="0" ${!snmpConf.enabled ? 'selected' : ''}>Disabled</option>
              </select>
            </div>
            <div class="form-group">
              <label>Version</label>
              <select id="cfg-snmp-version">
                <option value="1" ${snmpConf.version === 1 ? 'selected' : ''}>SNMPv1</option>
                <option value="2" ${snmpConf.version === 2 ? 'selected' : ''}>SNMPv2c</option>
              </select>
            </div>
            <div class="form-group">
              <label>Community String (read)</label>
              <input type="text" id="cfg-snmp-community" value="${escHtml(snmpConf.community_ro || 'public')}" maxlength="32" placeholder="public" />
            </div>
            <div class="form-group">
              <label>Trap Host</label>
              <input type="text" id="cfg-snmp-trap" value="${escHtml(snmpConf.trap_host || '')}" placeholder="IP or hostname (optional)" />
            </div>
          </div>
          <div class="cfg-actions"><button class="btn btn-primary btn-sm" onclick="saveSnmpConfig('${id}')">Save SNMP</button></div>
        </div>
      </details>

    </div>`;
}

async function saveSystemConfig(id) {
  const payload = {
    sys_name: document.getElementById("cfg-sys-name").value.trim(),
    sys_location: document.getElementById("cfg-sys-location").value.trim(),
    sys_contact: document.getElementById("cfg-sys-contact").value.trim(),
  };
  try {
    await apiFetch(`/switches/${id}/config/system`, { method: "POST", body: JSON.stringify(payload) });
    toast("System configuration saved", "success");
    loadOverview(id);
  } catch { /* error already toasted */ }
}

async function saveNtpConfig(id) {
  const payload = {
    mode: parseInt(document.getElementById("cfg-ntp-mode").value),
    interval: parseInt(document.getElementById("cfg-ntp-interval").value) || 3600,
    server1: document.getElementById("cfg-ntp-server1").value.trim(),
    server2: document.getElementById("cfg-ntp-server2").value.trim(),
    server3: document.getElementById("cfg-ntp-server3").value.trim(),
    server4: document.getElementById("cfg-ntp-server4").value.trim(),
    server5: document.getElementById("cfg-ntp-server5").value.trim(),
  };
  try {
    await apiFetch(`/switches/${id}/config/ntp`, { method: "POST", body: JSON.stringify(payload) });
    toast("NTP configuration saved", "success");
  } catch { /* error already toasted */ }
}

async function savePortFullConfig(id) {
  const ports = _cfgPortsCache.map(p => ({
    port: p.port,
    admin_enabled: document.getElementById(`port-admin-${p.port}`)?.checked ?? p.admin_enabled,
    flow_ctrl: document.getElementById(`port-flow-${p.port}`)?.checked ?? p.flow_ctrl,
    max_frame: parseInt(document.getElementById(`port-mtu-${p.port}`)?.value) || p.max_frame || 9600,
    auto_neg: p.auto_neg,
    speed: p.speed,
  }));
  try {
    await apiFetch(`/switches/${id}/config/ports`, { method: "POST", body: JSON.stringify({ ports }) });
    toast("Port settings saved", "success");
    loadPorts(id);
  } catch { /* error already toasted */ }
}

async function savePortsDesc(id) {
  const ports = _cfgPortsCache.map(p => ({
    port: p.port,
    description: document.getElementById(`port-desc-${p.port}`)?.value?.trim() || "",
  }));
  try {
    await apiFetch(`/switches/${id}/config/ports_desc`, { method: "POST", body: JSON.stringify({ ports }) });
    toast("Port descriptions saved", "success");
  } catch { /* error already toasted */ }
}

async function savePortAdminConfig(id) {
  const ports = _cfgPortsCache.map(p => ({
    port: p.port,
    admin_enabled: document.getElementById(`port-admin-${p.port}`)?.checked ?? p.admin_enabled,
  }));
  try {
    await apiFetch(`/switches/${id}/config/ports`, { method: "POST", body: JSON.stringify({ ports }) });
    toast("Port configuration saved", "success");
    loadPorts(id);
  } catch { /* error already toasted */ }
}

async function savePoeConfig(id) {
  const ports = _cfgPoeCache.map(p => ({
    port: p.port,
    poe_enabled: document.getElementById(`poe-enabled-${p.port}`)?.checked ?? p.poe_enabled,
    priority: parseInt(document.getElementById(`poe-priority-${p.port}`)?.value ?? p.priority),
  }));
  try {
    await apiFetch(`/switches/${id}/config/poe`, { method: "POST", body: JSON.stringify({ ports }) });
    toast("PoE configuration saved", "success");
    loadPoe(id);
  } catch { /* error already toasted */ }
}

async function saveLoopConfig(id) {
  const loopPorts = (_cfgAllData.loopConf?.ports || []).map(p => ({
    port: p.port,
    enable: document.getElementById(`loop-enable-${p.port}`)?.checked ?? p.enable,
    action: parseInt(document.getElementById(`loop-action-${p.port}`)?.value ?? p.action),
    tx_mode: document.getElementById(`loop-txmode-${p.port}`)?.checked ?? p.tx_mode,
  }));
  const payload = {
    global_enable: document.getElementById("cfg-loop-global")?.value === "1",
    tx_interval: parseInt(document.getElementById("cfg-loop-interval")?.value) || 5,
    shutdown_time: parseInt(document.getElementById("cfg-loop-shutdown")?.value) || 180,
    ports: loopPorts,
  };
  try {
    await apiFetch(`/switches/${id}/config/loop`, { method: "POST", body: JSON.stringify(payload) });
    toast("Loop protection saved", "success");
  } catch { /* error already toasted */ }
}

async function saveSnmpConfig(id) {
  const payload = {
    enabled: document.getElementById("cfg-snmp-enabled")?.value === "1",
    version: parseInt(document.getElementById("cfg-snmp-version")?.value) || 1,
    community_ro: document.getElementById("cfg-snmp-community")?.value.trim() || "public",
    trap_host: document.getElementById("cfg-snmp-trap")?.value.trim() || "",
  };
  try {
    await apiFetch(`/switches/${id}/config/snmp`, { method: "POST", body: JSON.stringify(payload) });
    toast("SNMP configuration saved", "success");
  } catch { /* error already toasted */ }
}

async function saveVlanConfig(id) {
  const ports = (_cfgAllData.vlanConf?.ports || []).map(p => ({
    port: p.port,
    pvid: parseInt(document.getElementById(`vlan-pvid-${p.port}`)?.value ?? p.pvid),
    frame_type: parseInt(document.getElementById(`vlan-frame-${p.port}`)?.value ?? p.frame_type),
    ingress_filter: document.getElementById(`vlan-ingress-${p.port}`)?.checked ?? p.ingress_filter,
    tx_tag: parseInt(document.getElementById(`vlan-txtag-${p.port}`)?.value ?? p.tx_tag),
    allowed_vlans: document.getElementById(`vlan-allowed-${p.port}`)?.value?.trim() || "1-4095",
  }));
  try {
    await apiFetch(`/switches/${id}/config/vlan`, { method: "POST", body: JSON.stringify({ tpid: _cfgAllData.vlanConf?.tpid || "88A8", ports }) });
    toast("VLAN configuration saved", "success");
  } catch { /* error already toasted */ }
}

async function savePvlanConfig(id) {
  const ports = (_cfgAllData.pvlanConf?.ports || []).map(p => ({
    port: p.port,
    mode: parseInt(document.getElementById(`pvlan-mode-${p.port}`)?.value ?? p.mode),
  }));
  try {
    await apiFetch(`/switches/${id}/config/pvlan`, { method: "POST", body: JSON.stringify({ pvlan_id: _cfgAllData.pvlanConf?.pvlan_id || 1, ports }) });
    toast("Private VLAN configuration saved", "success");
  } catch { /* error already toasted */ }
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

function switchTab(btn, tabName) {
  document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
  document.querySelectorAll(".tab-content").forEach(t => t.classList.remove("active"));
  btn.classList.add("active");
  document.getElementById(`tab-${tabName}`).classList.add("active");
}

// ---------------------------------------------------------------------------
// Add / Edit Switch Modal
// ---------------------------------------------------------------------------

function openAddSwitchModal() {
  document.getElementById("modal-title").textContent = "Add Switch";
  document.getElementById("form-id").value = "";
  document.getElementById("switch-form").reset();
  document.getElementById("modal-switch").style.display = "flex";
}

function openEditSwitchModal(id, name, ip, username) {
  document.getElementById("modal-title").textContent = "Edit Switch";
  document.getElementById("form-id").value = id;
  document.getElementById("form-name").value = name;
  document.getElementById("form-ip").value = ip;
  document.getElementById("form-username").value = username;
  document.getElementById("form-password").value = "";
  document.getElementById("modal-switch").style.display = "flex";
}

function closeModal(id) {
  document.getElementById(id).style.display = "none";
}

document.querySelectorAll(".modal-overlay").forEach(overlay => {
  overlay.addEventListener("click", e => {
    if (e.target === overlay) overlay.style.display = "none";
  });
});

async function saveSwitch(e) {
  e.preventDefault();
  const id = document.getElementById("form-id").value;
  const payload = {
    name: document.getElementById("form-name").value.trim(),
    ip: document.getElementById("form-ip").value.trim(),
    username: document.getElementById("form-username").value.trim(),
    password: document.getElementById("form-password").value,
  };

  try {
    if (id) {
      await apiFetch(`/switches/${id}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      toast("Switch updated", "success");
    } else {
      await apiFetch("/switches", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      toast("Switch added", "success");
    }
    closeModal("modal-switch");
    loadSwitchesList();
  } catch {
    // error already toasted
  }
}

async function deleteSwitch(id, name) {
  if (!confirm(`Delete switch "${name}"?`)) return;
  try {
    await apiFetch(`/switches/${id}`, { method: "DELETE" });
    toast("Switch deleted");
    loadSwitchesList();
  } catch {
    // error already toasted
  }
}

// ---------------------------------------------------------------------------
// Network Scanner
// ---------------------------------------------------------------------------

function openScanModal() {
  document.getElementById("scan-progress").style.display = "none";
  document.getElementById("scan-results").style.display = "none";
  document.getElementById("scan-results-list").innerHTML = "";
  document.getElementById("modal-scan").style.display = "flex";
}

async function startScan() {
  const subnet = document.getElementById("scan-subnet").value.trim();
  const username = document.getElementById("scan-username").value.trim() || "root";
  const password = document.getElementById("scan-password").value;

  if (!subnet) {
    toast("Please enter a subnet or IP range", "error");
    return;
  }

  document.getElementById("scan-results").style.display = "none";
  const progressEl = document.getElementById("scan-progress");
  const barEl = document.getElementById("scan-bar");
  const statusEl = document.getElementById("scan-status-text");
  progressEl.style.display = "block";
  barEl.style.width = "30%";
  statusEl.textContent = `Scanning ${subnet}...`;

  try {
    const data = await apiFetch("/scan", {
      method: "POST",
      body: JSON.stringify({ subnet, username, password, timeout: 2.0 }),
    });

    barEl.style.width = "100%";
    scanResults = data.found;
    statusEl.textContent = `Scanned ${data.scanned} addresses, found ${data.found.length} Axis switch${data.found.length !== 1 ? "es" : ""}.`;

    setTimeout(() => {
      progressEl.style.display = "none";
      renderScanResults(data.found, username, password);
    }, 600);
  } catch {
    progressEl.style.display = "none";
  }
}

function renderScanResults(found, username, password) {
  const el = document.getElementById("scan-results");
  const listEl = document.getElementById("scan-results-list");
  const countEl = document.getElementById("scan-results-count");

  if (!found.length) {
    listEl.innerHTML = `<div class="empty-state"><h3>No Axis switches found</h3><p>Try a different subnet or check that the switches are reachable.</p></div>`;
    el.style.display = "block";
    return;
  }

  countEl.textContent = `Found ${found.length} switch${found.length !== 1 ? "es" : ""}`;
  listEl.innerHTML = found.map((sw, i) => `
    <div class="scan-result-item">
      <input type="checkbox" id="scan-check-${i}" ${sw.already_added ? "" : "checked"} ${sw.already_added ? "disabled" : ""} />
      <div class="scan-result-info">
        <div class="scan-result-ip">${escHtml(sw.ip)}</div>
        <div class="scan-result-model">${escHtml(sw.model)} &bull; ${escHtml(sw.description || "")} &bull; ${sw.port_count} ports</div>
      </div>
      <div class="scan-result-name">
        <input type="text" id="scan-name-${i}" value="${escHtml(sw.model + " - " + sw.ip)}" placeholder="Name for this switch"
          ${sw.already_added ? "disabled" : ""} style="padding:6px 10px;border:1px solid var(--border);border-radius:6px;width:100%;font-size:13px" />
      </div>
      <div style="display:flex;gap:6px">
        <input type="text" id="scan-uname-${i}" value="${escHtml(username)}" placeholder="user"
          style="width:70px;padding:5px 8px;border:1px solid var(--border);border-radius:5px;font-size:12px" ${sw.already_added ? "disabled" : ""} />
        <input type="password" id="scan-pass-${i}" value="${escHtml(password)}" placeholder="pass"
          style="width:80px;padding:5px 8px;border:1px solid var(--border);border-radius:5px;font-size:12px" ${sw.already_added ? "disabled" : ""} />
      </div>
      ${sw.already_added ? `<span class="already-added-badge">Already added</span>` : ""}
    </div>`).join("");

  el.style.display = "block";
}

function selectAllScanResults(checked) {
  scanResults.forEach((sw, i) => {
    if (!sw.already_added) {
      const cb = document.getElementById(`scan-check-${i}`);
      if (cb) cb.checked = checked;
    }
  });
}

async function addSelectedSwitches() {
  const toAdd = [];
  scanResults.forEach((sw, i) => {
    const cb = document.getElementById(`scan-check-${i}`);
    if (cb && cb.checked && !sw.already_added) {
      toAdd.push({
        ip: sw.ip,
        name: document.getElementById(`scan-name-${i}`)?.value?.trim() || sw.model,
        username: document.getElementById(`scan-uname-${i}`)?.value?.trim() || "root",
        password: document.getElementById(`scan-pass-${i}`)?.value || "",
      });
    }
  });

  if (!toAdd.length) {
    toast("No switches selected", "error");
    return;
  }

  try {
    const result = await apiFetch("/switches/bulk-add", {
      method: "POST",
      body: JSON.stringify({ switches: toAdd }),
    });
    toast(`Added ${result.added.length} switch${result.added.length !== 1 ? "es" : ""}`, "success");
    closeModal("modal-scan");
    loadSwitchesList();
  } catch { /* error already toasted */ }
}

// ---------------------------------------------------------------------------
// Bulk Configure
// ---------------------------------------------------------------------------

function openBulkConfigModal() {
  const listEl = document.getElementById("bulk-switch-list");
  listEl.innerHTML = allSwitches.length
    ? allSwitches.map(sw => `
        <label class="bulk-switch-item">
          <input type="checkbox" class="bulk-sw-check" value="${sw.id}" checked />
          <div>
            <div class="bulk-switch-name">${escHtml(sw.name)}</div>
            <div class="bulk-switch-ip">${escHtml(sw.ip)}</div>
          </div>
        </label>`).join("")
    : `<p style="color:var(--text-muted);font-size:13px">No switches in inventory.</p>`;

  document.getElementById("bulk-progress").style.display = "none";
  document.getElementById("modal-bulk-config").style.display = "flex";
}

function addBulkPoeRow() {
  const container = document.getElementById("bulk-poe-ports");
  const row = document.createElement("div");
  row.className = "bulk-poe-row";
  row.innerHTML = `
    <label>Port</label>
    <input type="number" class="bulk-poe-port" min="1" max="24" placeholder="#" style="width:60px" />
    <label>PoE</label>
    <select class="bulk-poe-enable">
      <option value="true">Enable</option>
      <option value="false">Disable</option>
    </select>
    <label>Priority</label>
    <select class="bulk-poe-priority">
      <option value="1">Low</option>
      <option value="2" selected>High</option>
      <option value="3">Critical</option>
    </select>
    <button type="button" class="btn btn-ghost btn-sm" onclick="this.parentElement.remove()">&#215;</button>`;
  container.appendChild(row);
}

function addBulkPortRow() {
  const container = document.getElementById("bulk-port-rows");
  const row = document.createElement("div");
  row.className = "bulk-port-row";
  row.innerHTML = `
    <label>Port</label>
    <input type="number" class="bulk-port-num" min="1" max="24" placeholder="#" style="width:60px" />
    <label>Admin</label>
    <select class="bulk-port-admin">
      <option value="true">Enable</option>
      <option value="false">Disable</option>
    </select>
    <button type="button" class="btn btn-ghost btn-sm" onclick="this.parentElement.remove()">&#215;</button>`;
  container.appendChild(row);
}

function addBulkDescRow() {
  const container = document.getElementById("bulk-desc-rows");
  const row = document.createElement("div");
  row.className = "bulk-port-row";
  row.innerHTML = `
    <label>Port</label>
    <input type="number" class="bulk-desc-port" min="1" max="24" placeholder="#" style="width:60px" />
    <label>Description</label>
    <input type="text" class="bulk-desc-text" maxlength="47" placeholder="e.g. Camera 1" style="flex:1" />
    <button type="button" class="btn btn-ghost btn-sm" onclick="this.parentElement.remove()">&#215;</button>`;
  container.appendChild(row);
}

async function applyBulkConfig() {
  const checkedIds = [...document.querySelectorAll(".bulk-sw-check:checked")].map(c => c.value);
  if (!checkedIds.length) {
    toast("No switches selected", "error");
    return;
  }

  const sysName = document.getElementById("bulk-sys-name").value.trim();
  const sysLoc = document.getElementById("bulk-sys-location").value.trim();
  const sysContact = document.getElementById("bulk-sys-contact").value.trim();
  const systemPayload = (sysName || sysLoc || sysContact)
    ? { sys_name: sysName || null, sys_location: sysLoc || null, sys_contact: sysContact || null }
    : null;

  const poeRows = [...document.querySelectorAll(".bulk-poe-row")];
  const poePorts = poeRows.map(row => {
    const portNum = parseInt(row.querySelector(".bulk-poe-port")?.value);
    if (!portNum || isNaN(portNum)) return null;
    return {
      port: portNum,
      poe_enabled: row.querySelector(".bulk-poe-enable")?.value === "true",
      priority: parseInt(row.querySelector(".bulk-poe-priority")?.value || "2"),
    };
  }).filter(Boolean);

  const portRows = [...document.querySelectorAll(".bulk-port-row")];
  const portItems = portRows.map(row => {
    const portNum = parseInt(row.querySelector(".bulk-port-num")?.value);
    if (!portNum || isNaN(portNum)) return null;
    return {
      port: portNum,
      admin_enabled: row.querySelector(".bulk-port-admin")?.value === "true",
    };
  }).filter(Boolean);

  // NTP
  const ntpMode = document.getElementById("bulk-ntp-mode")?.value;
  const ntpServer1 = document.getElementById("bulk-ntp-server1")?.value.trim();
  const ntpServer2 = document.getElementById("bulk-ntp-server2")?.value.trim();
  const ntpInterval = document.getElementById("bulk-ntp-interval")?.value;
  const ntpPayload = (ntpMode !== "" && ntpMode != null) || ntpServer1
    ? {
        mode: ntpMode !== "" ? parseInt(ntpMode) : 1,
        interval: parseInt(ntpInterval) || 3600,
        server1: ntpServer1 || "",
        server2: ntpServer2 || "",
        server3: "", server4: "", server5: "",
      }
    : null;

  // Loop protection
  const loopGlobal = document.getElementById("bulk-loop-global")?.value;
  const loopInterval = document.getElementById("bulk-loop-interval")?.value;
  const loopShutdown = document.getElementById("bulk-loop-shutdown")?.value;
  const loopPayload = (loopGlobal !== "" && loopGlobal != null)
    ? {
        global_enable: loopGlobal === "true",
        tx_interval: parseInt(loopInterval) || 5,
        shutdown_time: parseInt(loopShutdown) || 180,
        ports: [],
      }
    : null;

  // Port descriptions
  const descItems = [...document.querySelectorAll("#bulk-desc-rows .bulk-port-row")].map(row => {
    const portNum = parseInt(row.querySelector(".bulk-desc-port")?.value);
    const desc = row.querySelector(".bulk-desc-text")?.value.trim() || "";
    if (!portNum || isNaN(portNum)) return null;
    return { port: portNum, description: desc };
  }).filter(Boolean);

  // SNMP
  const snmpMode = document.getElementById("bulk-snmp-enabled")?.value;
  const snmpPayload = snmpMode !== ""
    ? {
        enabled: snmpMode === "1",
        version: parseInt(document.getElementById("bulk-snmp-version")?.value) || 1,
        community_ro: document.getElementById("bulk-snmp-community")?.value.trim() || "public",
        trap_host: document.getElementById("bulk-snmp-trap")?.value.trim() || "",
      }
    : null;

  if (!systemPayload && !poePorts.length && !portItems.length && !ntpPayload && !loopPayload && !descItems.length && !snmpPayload) {
    toast("Nothing to configure - fill in at least one field", "error");
    return;
  }

  const body = {
    switch_ids: checkedIds,
    system: systemPayload,
    poe: poePorts.length ? { ports: poePorts } : null,
    ports: portItems.length ? { ports: portItems } : null,
    ntp: ntpPayload,
    loop: loopPayload,
    ports_desc: descItems.length ? { ports: descItems } : null,
    snmp: snmpPayload,
  };

  const progressEl = document.getElementById("bulk-progress");
  const barEl = document.getElementById("bulk-bar");
  const statusEl = document.getElementById("bulk-status-text");
  progressEl.style.display = "block";
  barEl.style.width = "20%";
  statusEl.textContent = `Applying configuration to ${checkedIds.length} switch${checkedIds.length !== 1 ? "es" : ""}...`;

  try {
    const result = await apiFetch("/bulk/apply", {
      method: "POST",
      body: JSON.stringify(body),
    });

    barEl.style.width = "100%";
    const failed = result.results.filter(r => !r.ok);
    if (failed.length) {
      statusEl.textContent = `Done with ${failed.length} error${failed.length !== 1 ? "s" : ""}.`;
      failed.forEach(f => toast(`${f.id}: ${f.errors.join(", ")}`, "error"));
    } else {
      statusEl.textContent = `Configuration applied to ${result.results.length} switch${result.results.length !== 1 ? "es" : ""} successfully.`;
      toast("Bulk configuration applied", "success");
    }
  } catch {
    progressEl.style.display = "none";
  }
}

// ---------------------------------------------------------------------------
// Network Topology
// ---------------------------------------------------------------------------

let _visNetwork    = null;
let _topoViewMode  = "list";    // "list" (HTML tree, default) or "canvas" (vis-network)
let _topoLayoutMode = "physics"; // canvas sub-mode: "physics" or "hierarchical"
let _topoNodes     = null;
let _topoEdges     = null;

async function topoRefresh() {
  try { await apiFetch("/topology/refresh", { method: "POST" }); } catch {}
  await loadTopologyView();
}

async function loadTopologyView() {
  const statusEl = document.getElementById("topology-status");
  const canvasEl = document.getElementById("topology-canvas");

  statusEl.textContent = "Loading topology\u2026";
  if (_visNetwork) { _visNetwork.destroy(); _visNetwork = null; }
  canvasEl.innerHTML = '<div class="topo-loading">Loading\u2026</div>';

  let data;
  try {
    data = await apiFetch("/topology");
  } catch {
    statusEl.textContent = "Failed to load topology data.";
    canvasEl.innerHTML = '';
    return;
  }

  const { nodes, edges, snmp_status } = data;
  _topoNodes = nodes;
  _topoEdges = edges;

  const switchCnt = nodes.filter(n => n.managed).length;
  const devCnt    = nodes.filter(n => n.device).length;
  const linkCnt   = edges.filter(e => !e.target.startsWith("dev_") && e.source !== "__upstream__" && e.target !== "__upstream__").length;
  const discovery = snmp_status?.discovery || "";
  const statusMsg = discovery
    ? `${switchCnt} switches, ${devCnt} devices, ${linkCnt} inter-switch link${linkCnt !== 1 ? "s" : ""}  \u00b7  ${discovery}`
    : `${switchCnt} switches, ${devCnt} devices, ${linkCnt} inter-switch link${linkCnt !== 1 ? "s" : ""}`;
  statusEl.textContent = statusMsg;

  if (!nodes.length) {
    canvasEl.innerHTML = '<div class="topo-loading">No switches configured.</div>';
    return;
  }

  canvasEl.innerHTML = '';
  if (_topoViewMode === "list") {
    _renderTopologyTree(canvasEl, nodes, edges);
  } else {
    _renderTopologyCanvas(canvasEl, nodes, edges);
  }
}

// ---------------------------------------------------------------------------
// Topology: HTML tree list view
// ---------------------------------------------------------------------------

function _buildTopoAdjacency(nodes, edges) {
  const nodeMap = {};
  const adjMap  = {};  // id -> [{nodeId, parentPort}]

  nodes.forEach(n => { nodeMap[n.id] = n; adjMap[n.id] = []; });

  edges.forEach(e => {
    if (nodeMap[e.source] && nodeMap[e.target]) {
      adjMap[e.source].push({ nodeId: e.target, parentPort: e.source_port || '' });
    }
  });

  // Sort: managed switches before devices; within switches by port number
  const portNum = p => { const m = String(p || '').match(/(\d+)$/); return m ? parseInt(m[1]) : 999; };
  Object.keys(adjMap).forEach(id => {
    adjMap[id].sort((a, b) => {
      const an = nodeMap[a.nodeId], bn = nodeMap[b.nodeId];
      if (!an || !bn) return 0;
      if (an.managed !== bn.managed) return an.managed ? -1 : 1;
      return portNum(a.parentPort) - portNum(b.parentPort);
    });
  });

  return { nodeMap, adjMap };
}

function _renderTopologyTree(container, nodes, edges) {
  const { nodeMap, adjMap } = _buildTopoAdjacency(nodes, edges);

  function renderChildren(parentId) {
    const kids = adjMap[parentId] || [];
    if (!kids.length) return '';

    const items = kids.map(c => {
      const node = nodeMap[c.nodeId];
      if (!node) return '';

      const isSwitch   = node.managed && node.id !== '__upstream__';
      const portBadge  = c.parentPort
        ? `<span class="topo-port">${_topoPortLabel(c.parentPort)}</span>` : '';
      const iconCls    = isSwitch ? 'sw' : 'dev';
      const icon       = `<span class="topo-icon ${iconCls}"></span>`;
      const ipHtml     = `<span class="topo-ip">${node.ip || ''}</span>`;
      const nameHtml   = isSwitch
        ? `<span class="topo-sw-name" onclick="openTopoSwitch('${node.id}','${(node.ip||'')}','${(node.name||'').replace(/'/g,"\\\'")}')">${node.name || ''}</span>`
        : `<span class="topo-dev-name">${node.name || ''}</span>`;
      const rowCls     = isSwitch ? 'topo-row' : 'topo-row topo-row--dev';

      return `<li class="topo-li${isSwitch ? '' : ' topo-li--dev'}">` +
        `<div class="${rowCls}">${portBadge}${icon}${ipHtml}${nameHtml}</div>` +
        renderChildren(c.nodeId) +
        `</li>`;
    }).filter(Boolean);

    return `<ul class="topo-ul">${items.join('')}</ul>`;
  }

  const root     = nodeMap['__upstream__'];
  const rootName = root ? (root.name || 'Upstream Network') : 'Upstream Network';
  const rootIp   = root ? (root.ip || '') : '';
  const rootIpHtml = rootIp ? `<span class="topo-ip">${rootIp}</span>` : '';
  const html =
    `<div class="topo-tree-list">` +
    `<div class="topo-root-row"><span class="topo-icon up"></span>${rootIpHtml}<span class="topo-root-name">${rootName}</span></div>` +
    renderChildren('__upstream__') +
    `</div>`;

  container.innerHTML = html;
}

function topoToggleView() {
  _topoViewMode = _topoViewMode === "list" ? "canvas" : "list";
  const btn = document.getElementById("topo-btn-view");
  if (btn) btn.textContent = _topoViewMode === "list" ? "\u2609 Canvas" : "\u2630 List";
  const canvasEl = document.getElementById("topology-canvas");
  if (!canvasEl || !_topoNodes) return;
  if (_visNetwork) { _visNetwork.destroy(); _visNetwork = null; }
  canvasEl.innerHTML = '';
  if (_topoViewMode === "list") {
    _renderTopologyTree(canvasEl, _topoNodes, _topoEdges);
  } else {
    _renderTopologyCanvas(canvasEl, _topoNodes, _topoEdges);
  }
}

function _renderTopologyCanvas(container, nodes, edges) {
  const visNodes = new vis.DataSet(nodes.map(n => {
    const isUpstream = n.id === "__upstream__";
    const isDevice   = n.device === true;
    let color, fontSz, shape, bw;
    if (isUpstream) {
      color = { background: "#1e293b", border: "#0f172a", highlight: { background: "#334155", border: "#0f172a" }, hover: { background: "#2d3f52", border: "#0f172a" } };
      fontSz = 14; shape = "ellipse"; bw = 3;
    } else if (isDevice) {
      color = { background: "#15803d", border: "#14532d", highlight: { background: "#16a34a", border: "#14532d" }, hover: { background: "#22c55e", border: "#14532d" } };
      fontSz = 10; shape = "box"; bw = 1;
    } else {
      color = { background: n.managed ? "#0069B4" : "#475569", border: n.managed ? "#004d80" : "#2d3f52", highlight: { background: n.managed ? "#1a85cc" : "#5a6f84", border: "#003d66" }, hover: { background: n.managed ? "#0074b8" : "#536070", border: "#003d66" } };
      fontSz = 13; shape = "box"; bw = 2;
    }
    return {
      id: n.id,
      label: isDevice
        ? ((n.name || n.ip || '') + (n.ip && n.name !== n.ip ? '\n' + n.ip : ''))
        : (n.ip ? `${n.name}\n${n.ip}` : n.name),
      title: n.ip ? `${n.name} (${n.ip})` : n.name,
      color,
      font: { color: isDevice ? "#ffffff" : "#ffffff", size: fontSz, face: "system-ui, sans-serif", multi: "plain" },
      shape,
      borderWidth: bw,
      borderWidthSelected: bw + 1,
      shadow: { enabled: true, size: 4, x: 1, y: 2, color: "rgba(0,0,0,0.15)" },
      margin: isDevice ? { top: 5, right: 8, bottom: 5, left: 8 } : { top: 9, right: 14, bottom: 9, left: 14 },
    };
  }));

  const visEdges = new vis.DataSet(edges.map((e, i) => {
    const sp = _topoPortNum(e.source_port);
    const tp = _topoPortNum(e.target_port);
    const isUpstreamEdge = e.source === "__upstream__" || e.target === "__upstream__";
    const isDeviceEdge   = (e.target || "").startsWith("dev_") || (e.source || "").startsWith("dev_");
    const label = (sp != null && tp != null) ? `P${sp} \u2194 P${tp}`
                : (sp != null) ? `P${sp}`
                : (tp != null) ? `P${tp}` : "";
    return {
      id: i,
      from: e.source,
      to: e.target,
      label,
      font: { size: 10, color: "#94a3b8", background: "#1e2a3a", strokeWidth: 0, align: "middle" },
      color: isUpstreamEdge
        ? { color: "#64748b", highlight: "#475569", hover: "#475569" }
        : isDeviceEdge
          ? { color: "#166534", highlight: "#15803d", hover: "#22c55e" }
          : { color: "#94a3b8", highlight: "#0063a3", hover: "#607080" },
      width: isDeviceEdge ? 1 : isUpstreamEdge ? 1 : 2,
      dashes: isUpstreamEdge,
      hoverWidth: 2,
      selectionWidth: 2,
      smooth: { type: "cubicBezier", forceDirection: "none", roundness: 0.25 },
    };
  }));

  _visNetwork = new vis.Network(container, { nodes: visNodes, edges: visEdges }, _topoNetworkOptions());

  // Click on a managed node -> open switch detail
  _visNetwork.on("click", params => {
    if (params.nodes.length === 1) {
      const nd = nodes.find(n => n.id === params.nodes[0]);
      if (nd && nd.managed) openSwitchDetail(nd.id, nd.name);
    }
  });

  // Pointer cursor on managed nodes
  _visNetwork.on("hoverNode", params => {
    const nd = nodes.find(n => n.id === params.node);
    container.style.cursor = (nd && nd.managed) ? "pointer" : "default";
  });
  _visNetwork.on("blurNode", () => { container.style.cursor = "default"; });
}

function _topoNetworkOptions() {
  const hierarchical = _topoLayoutMode === "hierarchical";
  return {
    layout: hierarchical
      ? { hierarchical: { enabled: true, direction: "UD", sortMethod: "directed", nodeSpacing: 80, levelSeparation: 120, treeSpacing: 100 } }
      : { hierarchical: false },
    physics: {
      enabled: !hierarchical,
      stabilization: { iterations: 200 },
      barnesHut: { gravitationalConstant: -9000, springConstant: 0.04, springLength: 280 },
    },
    interaction: { hover: true, tooltipDelay: 200, zoomView: true, dragView: true, dragNodes: true },
    edges: { arrows: { to: { enabled: false } } },
  };
}

function _topoPortNum(p) {
  if (p == null || p === '') return null;
  const m = String(p).trim().match(/(\d+)$/);
  const n = m ? parseInt(m[1], 10) : NaN;
  return isNaN(n) ? String(p) : n;
}

function _topoPortLabel(p) {
  if (!p) return p;
  const s = String(p).trim();
  // Shorten HPE Comware style: "GigabitEthernet1/0/5" -> "1/0/5",
  // "Ten-GigabitEthernet1/0/49" -> "XG1/0/49"
  const comware = s
    .replace(/Ten-GigabitEthernet/i, 'XG')
    .replace(/GigabitEthernet/i, '')
    .replace(/FastEthernet/i, 'FA');
  if (comware !== s) return comware.trim();
  // SNMP fallback formats: "portN" or "bpN" (bridge port == physical port on HP)
  const m = s.match(/^(?:port|bp)(\d+)$/i);
  if (m) return `1/0/${m[1]}`;
  return s;
}

function topoFitAll() {
  if (_visNetwork) _visNetwork.fit({ animation: { duration: 400, easingFunction: "easeInOutQuad" } });
}

function topoToggleLayout() {
  // Only meaningful in canvas mode; also available as a keyboard shortcut if needed
  if (_topoViewMode !== "canvas") return;
  _topoLayoutMode = _topoLayoutMode === "hierarchical" ? "physics" : "hierarchical";
  if (_visNetwork) {
    if (_topoLayoutMode === "physics") {
      const canvas = document.getElementById("topology-canvas");
      const w = (canvas.clientWidth  || 800) * 0.6;
      const h = (canvas.clientHeight || 500) * 0.6;
      const ids = _visNetwork.body.data.nodes.getIds();
      _visNetwork.body.data.nodes.update(ids.map(id => ({
        id,
        x: (Math.random() - 0.5) * w,
        y: (Math.random() - 0.5) * h,
      })));
      _visNetwork.setOptions(_topoNetworkOptions());
      setTimeout(() => _visNetwork && _visNetwork.fit({ animation: { duration: 400, easingFunction: "easeInOutQuad" } }), 900);
    } else {
      _visNetwork.setOptions(_topoNetworkOptions());
      setTimeout(() => _visNetwork && _visNetwork.fit({ animation: { duration: 400, easingFunction: "easeInOutQuad" } }), 700);
    }
  }
}


// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

function loadSettingsView() {
  const { dashInterval, detailInterval } = _getSettings();
  document.getElementById("setting-dash-interval").value = dashInterval / 1000;
  document.getElementById("setting-detail-interval").value = detailInterval / 1000;
  // Load SNMP / core-switch settings
  apiFetch("/settings/core-switch").then(cfg => {
    const seeds = cfg.seed_ips && cfg.seed_ips.length ? cfg.seed_ips.join(", ") : (cfg.ip || "");
    document.getElementById("snmp-seed-ips").value  = seeds;
    document.getElementById("snmp-community").value = cfg.community || "public";
  }).catch(() => {});
}

function saveSettings() {
  const dashSecs = Math.max(5, parseInt(document.getElementById("setting-dash-interval").value) || 5);
  const detailSecs = Math.max(5, parseInt(document.getElementById("setting-detail-interval").value) || 5);
  localStorage.setItem("dashInterval", dashSecs * 1000);
  localStorage.setItem("detailInterval", detailSecs * 1000);
  // Restart active timer with new interval
  if (currentView === "dashboard") _startDashTimer();
  else if (currentView === "switch-detail") _startDetailTimer();
  toast("Settings saved", "success");
}

function resetSettings() {
  localStorage.removeItem("dashInterval");
  localStorage.removeItem("detailInterval");
  loadSettingsView();
  toast("Reset to defaults (5s)", "success");
}

async function saveSnmpSettings() {
  const rawIps    = (document.getElementById("snmp-seed-ips").value || "").trim();
  const community = (document.getElementById("snmp-community").value || "public").trim();
  const seed_ips  = rawIps.split(/[,\s]+/).map(s => s.trim()).filter(Boolean);
  const enabled   = seed_ips.length > 0;
  try {
    await apiFetch("/settings/core-switch", { method: "PUT", body: JSON.stringify({ enabled, seed_ips, community }) });
    toast("SNMP discovery settings saved", "success");
    document.getElementById("snmp-test-result").textContent = "";
  } catch (e) {
    toast("Save failed: " + e.message, "error");
  }
}

async function testSnmpSettings() {
  const resultEl = document.getElementById("snmp-test-result");
  resultEl.textContent = "Testing\u2026";
  resultEl.style.color = "var(--text-muted)";
  const rawIps    = (document.getElementById("snmp-seed-ips").value || "").trim();
  const community = (document.getElementById("snmp-community").value || "public").trim();
  const seed_ips  = rawIps.split(/[,\s]+/).map(s => s.trim()).filter(Boolean);
  if (!seed_ips.length) { resultEl.textContent = "Enter at least one seed IP first."; return; }
  try {
    await apiFetch("/settings/core-switch", { method: "PUT", body: JSON.stringify({ enabled: true, seed_ips, community }) });
    const res = await apiFetch("/settings/core-switch/test", { method: "POST" });
    resultEl.textContent = `\u2713 ${res.sys_name || seed_ips[0]}  \u2014  ${(res.sys_descr || "").split("\n")[0].slice(0, 80)}`;
    resultEl.style.color = "#4ade80";
  } catch (e) {
    resultEl.textContent = "\u2717 " + (e.message || "No response");
    resultEl.style.color = "#f87171";
  }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

refreshDashboard();
_startDashTimer();
