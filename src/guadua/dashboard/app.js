// Controlador del Tablero del Comandante - Guadua (SARD)
// Implementa la interacción con el usuario, llamadas a la API y renderizado del mapa offline.

import { decodeGeohash } from "./geohash.js";
import {
  computeConfidence,
  computeRiskWarning,
  createInitialNodeState,
  formatDecisionRequest,
  formatTimeAgo,
  nodesStartingWith,
  partitionAndSortIncidents,
  updateNodeHealth,
} from "./logic.js";

// Constantes del sistema
export const POLL_INTERVAL_MS = 5000;
export const DEFAULT_NODES = [
  "http://localhost:8000",
  "http://localhost:8001",
  "http://localhost:8002",
];

// Estado global de la aplicación
const state = {
  // Primero el nodo que sirvió este tablero; los demás, de respaldo.
  nodeState: createInitialNodeState(nodesStartingWith(window.location.origin, DEFAULT_NODES)),
  operator: localStorage.getItem("guadua_operator") || "comandante-1",
  barriosMap: new Map(), // code -> { code, name, lat, lon }
  incidents: [],
  detailCache: new Map(), // incident_id -> IncidentDetail
  mapInstance: null,
  mapMarkersLayer: null,
  pendingEdit: null, // { incident, fichaId }
  mockFixture: null,
};

// --- Abstracción de red: fetchJson -------------------------------------------

/**
 * Consulta un endpoint del nodo o retorna datos del fixture con ?mock=1.
 *
 * @param {string} endpoint - Ruta que comienza en /api (ej. /api/incidents)
 * @param {RequestInit} [options]
 * @returns {Promise<{ ok: boolean, status: number, data: any }>}
 */
export async function fetchJson(endpoint, options = {}) {
  const urlParams = new URLSearchParams(window.location.search);
  const isMock = urlParams.get("mock") === "1";

  if (isMock) {
    return fetchMockJson(endpoint, options);
  }

  const activeNodeBase = state.nodeState.connectedNode || state.nodeState.nodes[0];
  const fullUrl = `${activeNodeBase.replace(/\/$/, "")}${endpoint}`;

  try {
    const res = await fetch(fullUrl, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
    });

    const isJson = res.headers.get("content-type")?.includes("application/json");
    const data = isJson ? await res.json() : await res.text();

    return {
      ok: res.ok,
      status: res.status,
      data,
    };
  } catch (err) {
    return {
      ok: false,
      status: 0,
      data: { error: err.message || "Error de conexión" },
    };
  }
}

/**
 * Simulación con tests/fixtures/api_examples.json
 */
async function fetchMockJson(endpoint, options = {}) {
  if (!state.mockFixture) {
    try {
      const res = await fetch("/tests/fixtures/api_examples.json");
      if (!res.ok) throw new Error("No se pudo cargar api_examples.json");
      state.mockFixture = await res.json();
    } catch (err) {
      console.warn("Error cargando fixture de simulación:", err);
      return { ok: false, status: 500, data: { error: "Error cargando fixture" } };
    }
  }

  const method = (options.method || "GET").toUpperCase();
  const validList = state.mockFixture.valid || [];

  if (endpoint === "/api/health") {
    const item = validList.find((v) => v.route === "GET /api/health");
    return { ok: true, status: 200, data: item?.body || { node: "A", version: "0.1.0", peers_alive: ["B"] } };
  }

  if (endpoint === "/api/incidents") {
    const item = validList.find((v) => v.route === "GET /api/incidents");
    return { ok: true, status: 200, data: item?.body || { node: "A", incidents: [] } };
  }

  if (endpoint.startsWith("/api/incidents/")) {
    const incidentId = endpoint.replace("/api/incidents/", "");
    const item = validList.find((v) => v.route === `GET /api/incidents/${incidentId}`);
    if (item) {
      return { ok: true, status: 200, data: item.body };
    }
    // Si no está el id específico, retornar el ejemplo genérico de IncidentDetail si existe
    const generic = validList.find((v) => v.schema === "IncidentDetail");
    if (generic) {
      return { ok: true, status: 200, data: { ...generic.body, incident_id: incidentId } };
    }
    return { ok: false, status: 404, data: { error: "Incidente no encontrado" } };
  }

  if (endpoint === "/api/decisions" && method === "POST") {
    const body = options.body ? JSON.parse(options.body) : {};
    if (body.ficha_id === "404") {
      return { ok: false, status: 404, data: { error: "La ficha no existe en el nodo" } };
    }
    if (body.ficha_id === "409") {
      return { ok: false, status: 409, data: { error: "La ficha ya tiene una decisión" } };
    }
    const ack = validList.find((v) => v.schema === "DecisionAck");
    return { ok: true, status: 201, data: ack?.body || { event_id: "A:58" } };
  }

  return { ok: false, status: 404, data: { error: `Ruta mock no implementada: ${method} ${endpoint}` } };
}

// --- Gestión de Barrios ------------------------------------------------------

async function loadBarrios() {
  try {
    const res = await fetch("/data/barrios.json");
    if (res.ok) {
      const barriosList = await res.json();
      state.barriosMap.clear();
      for (const b of barriosList) {
        state.barriosMap.set(b.code, b);
      }
    }
  } catch (err) {
    console.warn("No se pudo cargar data/barrios.json:", err);
  }
}

// --- Inicialización del Mapa Offline con Leaflet ------------------------------

function initMap() {
  const mapElement = document.getElementById("map");
  if (!mapElement || !window.L) return;

  // Manizales (centro aproximado)
  const manizalesCenter = [5.068, -75.517];

  state.mapInstance = L.map(mapElement, {
    center: manizalesCenter,
    zoom: 14,
    attributionControl: false,
  });

  // Capa para marcadores y círculos de incidentes
  state.mapMarkersLayer = L.layerGroup().addTo(state.mapInstance);
}

const LEVEL_COLORS = {
  4: "#dc2626", // Crítica
  3: "#ea580c", // Alta
  2: "#d97706", // Media
  1: "#16a34a", // Baja
};

function renderMapIncidents(incidents) {
  if (!state.mapMarkersLayer) return;
  state.mapMarkersLayer.clearLayers();

  const bounds = [];

  for (const inc of incidents) {
    const color = LEVEL_COLORS[inc.level] || "#2563eb";
    let lat = null;
    let lon = null;
    let isExact = false;

    if (inc.location?.geohash) {
      try {
        const coords = decodeGeohash(inc.location.geohash);
        lat = coords.lat;
        lon = coords.lon;
        isExact = true;
      } catch (err) {
        console.warn("Error decodificando geohash:", inc.location.geohash, err);
      }
    } else if (inc.location?.barrio) {
      const barrio = state.barriosMap.get(inc.location.barrio);
      if (barrio && barrio.lat !== null && barrio.lon !== null) {
        lat = barrio.lat;
        lon = barrio.lon;
      }
    }

    if (lat !== null && lon !== null) {
      bounds.push([lat, lon]);

      // Punto para ubicación exacta (geohash)
      if (isExact) {
        const marker = L.circleMarker([lat, lon], {
          radius: 6,
          fillColor: color,
          color: "#ffffff",
          weight: 2,
          opacity: 1,
          fillOpacity: 0.9,
        });

        marker.bindPopup(`
          <strong>${escapeHtml(inc.title)}</strong><br>
          <em>Prioridad: ${inc.level}</em><br>
          ${inc.location?.label ? escapeHtml(inc.location.label) : ""}
        `);
        state.mapMarkersLayer.addLayer(marker);
      }

      // Círculo para radio de incertidumbre si radius_m > 0
      if (inc.location?.radius_m && inc.location.radius_m > 0) {
        const circle = L.circle([lat, lon], {
          radius: inc.location.radius_m,
          color,
          fillColor: color,
          fillOpacity: 0.15,
          weight: 1.5,
        });
        state.mapMarkersLayer.addLayer(circle);
      }
    }
  }

  if (bounds.length > 0 && state.mapInstance) {
    state.mapInstance.fitBounds(bounds, { padding: [20, 20], maxZoom: 15 });
  }
}

// --- Renderizado de UI: Incidentes y Barrios ----------------------------------

function renderFeed() {
  const shortContainer = document.getElementById("short-list");
  const otherContainer = document.getElementById("other-list");
  const otherSummary = document.getElementById("other-incidents-summary");
  const barriosContainer = document.getElementById("barrios-list");

  if (!shortContainer || !otherContainer) return;

  const { shortList, otherList } = partitionAndSortIncidents(state.incidents);

  // Render lista corta (Nivel 4 y 3)
  if (shortList.length === 0) {
    shortContainer.innerHTML = `
      <div style="padding: 2rem; text-align: center; color: var(--text-muted); background: white; border-radius: 8px;">
        No hay incidentes críticos o de alta prioridad activos.
      </div>`;
  } else {
    shortContainer.innerHTML = shortList.map((inc) => renderIncidentCardHtml(inc)).join("");
  }

  // Render lista desplegable (Nivel 2 y 1)
  if (otherSummary) {
    otherSummary.textContent = `+${otherList.length} incidentes de prioridad media o baja`;
  }

  if (otherList.length === 0) {
    otherContainer.innerHTML = `
      <div style="padding: 1.5rem; text-align: center; color: var(--text-muted);">
        No hay incidentes de prioridad media o baja.
      </div>`;
  } else {
    otherContainer.innerHTML = otherList.map((inc) => renderIncidentCardHtml(inc)).join("");
  }

  // Render panel de barrios sin coordenadas
  const barriosSinCoords = state.incidents.filter((inc) => {
    if (inc.location?.geohash) return false;
    if (inc.location?.barrio) {
      const b = state.barriosMap.get(inc.location.barrio);
      return !b || b.lat === null || b.lon === null;
    }
    return false;
  });

  if (barriosContainer) {
    if (barriosSinCoords.length === 0) {
      barriosContainer.innerHTML = `<p style="font-size: 0.8125rem; color: var(--text-muted);">No hay reportes aproximados por barrio sin coordenadas.</p>`;
    } else {
      barriosContainer.innerHTML = barriosSinCoords
        .map((inc) => {
          const barrioCode = inc.location.barrio;
          const barrioName = inc.location.label || state.barriosMap.get(barrioCode)?.name || `Barrio #${barrioCode}`;
          return `
          <div class="barrio-item">
            <div>
              <strong>${escapeHtml(barrioName)}</strong>
              <div style="color: var(--text-muted); font-size: 0.75rem;">
                ${escapeHtml(inc.title)} · Radio: ${inc.location.radius_m || 0} m
              </div>
            </div>
            <span class="barrio-tag">Nivel ${inc.level}</span>
          </div>`;
        })
        .join("");
    }
  }

  // Actualizar mapa
  renderMapIncidents(state.incidents);

  // Asociar eventos en tarjetas
  attachCardEvents();
}

function getPriorityLabel(level) {
  switch (level) {
    case 4:
      return "🔴 CRÍTICA";
    case 3:
      return "🟠 ALTA";
    case 2:
      return "🟡 MEDIA";
    case 1:
      return "🟢 BAJA";
    default:
      return `NIVEL ${level}`;
  }
}

function renderIncidentCardHtml(inc) {
  const conf = computeConfidence(inc.confidence, inc.verify_now, inc.rumor);
  const risk = computeRiskWarning(inc);
  const timeAgo = formatTimeAgo(inc.received_at);
  const priorityText = getPriorityLabel(inc.level);
  const hasFicha = Boolean(inc.ficha_id);
  const isActionable = hasFicha && (inc.status === "pendiente" || inc.status === "conflicto");

  let locationText = "Ubicación sin datos";
  if (inc.location?.label) {
    locationText = `📍 ${inc.location.label}`;
  } else if (inc.location?.geohash) {
    locationText = `📍 GPS (${inc.location.geohash})`;
  } else if (inc.location?.barrio) {
    const bName = state.barriosMap.get(inc.location.barrio)?.name || `Barrio #${inc.location.barrio}`;
    locationText = `📍 ${bName}`;
  }

  return `
    <article class="incident-card level-${inc.level}" data-incident-id="${escapeHtml(inc.incident_id)}">
      <div class="card-top">
        <div class="card-title-group">
          <div class="card-badge-row">
            <span class="priority-tag level-${inc.level}">${priorityText}</span>
            <span class="status-badge status-${inc.status}">${inc.status}</span>
            <span class="card-location">${escapeHtml(locationText)}</span>
          </div>
          <h3 class="card-title">${escapeHtml(inc.title)}</h3>
        </div>

        <div class="card-meta-row">
          <span class="confidence-chip">Confianza: ${conf.label}${conf.score !== null ? ` (${Math.round(conf.score * 100)}%)` : ""}</span>
          ${conf.tags.map((tag) => tag === "VERIFICAR YA"
            ? `<span class="tag-alert">${tag}</span>`
            : `<span class="tag-rumor">${tag}</span>`).join("")}
          <span>⏱️ ${timeAgo}</span>
        </div>
      </div>

      ${risk.show ? `
        <div class="risk-box" role="alert">
          <div class="risk-stripe-header">
            <span class="risk-icon" aria-hidden="true">${risk.icon}</span>
            <strong class="risk-title">${escapeHtml(risk.title)}</strong>
          </div>
          <div class="risk-message">${escapeHtml(risk.text)}</div>
        </div>` : ""}

      ${(inc.suggestion || inc.message) ? `
        <div class="suggestion-box">
          ${inc.suggestion ? `<div class="suggestion-text">🚒 Sugerencia (EMRE): ${escapeHtml(inc.suggestion)}</div>` : ""}
          ${inc.message ? `<div class="message-text">💬 Qué decir: "${escapeHtml(inc.message)}"</div>` : ""}
        </div>` : ""}

      <!-- Botones de acción del comandante -->
      <div class="card-actions">
        <button class="btn-primary ${!isActionable ? "btn-disabled" : ""}"
          data-action="aprobar" data-ficha-id="${inc.ficha_id || ""}" ${!isActionable ? "disabled" : ""}>
          ✅ Aprobar
        </button>
        <button class="btn-secondary ${!isActionable ? "btn-disabled" : ""}"
          data-action="editar" data-ficha-id="${inc.ficha_id || ""}" ${!isActionable ? "disabled" : ""}>
          ✏️ Editar mensaje
        </button>
        <button class="btn-danger ${!isActionable ? "btn-disabled" : ""}"
          data-action="rechazar" data-ficha-id="${inc.ficha_id || ""}" ${!isActionable ? "disabled" : ""}>
          ❌ Replanear
        </button>

        ${!hasFicha ? `<span style="font-size: 0.75rem; color: var(--text-muted); margin-left: auto;">(Sin ficha de coordinación generada)</span>` : ""}
      </div>

      <!-- Desplegable 'Por qué está aquí' -->
      <details class="details-toggle" data-details-id="${escapeHtml(inc.incident_id)}">
        <summary>🔎 Por qué está aquí / evidencia / agentes ▼</summary>
        <div class="details-content" id="details-${escapeHtml(inc.incident_id)}">
          <div style="color: var(--text-muted);">Cargando trazabilidad...</div>
        </div>
      </details>
    </article>
  `;
}

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// --- Manejo de Eventos e Interacciones -----------------------------------------

function attachCardEvents() {
  // Manejador de botones de decisión
  document.querySelectorAll(".card-actions button").forEach((btn) => {
    btn.onclick = async (e) => {
      e.stopPropagation();
      const action = btn.dataset.action;
      const fichaId = btn.dataset.fichaId;
      if (!fichaId) {
        showAlert("El incidente no cuenta con una ficha activa.", "error");
        return;
      }

      if (action === "editar") {
        const card = btn.closest(".incident-card");
        const incidentId = card?.dataset.incidentId;
        const incident = state.incidents.find((i) => i.incident_id === incidentId);
        openEditModal(incident, fichaId);
        return;
      }

      await submitDecision({
        fichaId,
        action,
        operator: state.operator,
      });
    };
  });

  // Manejador de desplegable de detalle
  document.querySelectorAll(".details-toggle").forEach((details) => {
    details.ontoggle = async () => {
      if (!details.open) return;
      const incidentId = details.dataset.detailsId;
      const container = document.getElementById(`details-${incidentId}`);
      if (!container) return;

      let detail = state.detailCache.get(incidentId);
      if (!detail) {
        const res = await fetchJson(`/api/incidents/${incidentId}`);
        if (res.ok) {
          detail = res.data;
          state.detailCache.set(incidentId, detail);
        } else {
          container.innerHTML = `<span style="color: var(--color-critica);">Error cargando detalle: ${res.data?.error || "Error"}</span>`;
          return;
        }
      }

      renderIncidentDetailHtml(container, detail);
    };
  });
}

function renderIncidentDetailHtml(container, detail) {
  const probEntries = Object.entries(detail.probabilities || {});
  const rules = detail.rules || [];
  const sources = detail.sources || [];
  const contradictions = detail.contradictions || [];
  const citations = detail.citations || [];
  const steps = detail.steps || [];
  const reports = detail.report_ids || [];

  container.innerHTML = `
    ${rules.length > 0 ? `
      <div class="detail-block">
        <h4>Reglas de urgencia aplicadas</h4>
        <ul class="detail-list">
          ${rules.map((r) => `<li>🔹 ${escapeHtml(r)}</li>`).join("")}
        </ul>
      </div>` : ""}

    ${probEntries.length > 0 ? `
      <div class="detail-block">
        <h4>Probabilidades del clasificador</h4>
        <div style="display: flex; gap: 0.75rem; flex-wrap: wrap; font-size: 0.8125rem;">
          ${probEntries.map(([k, v]) => `<span><strong>${escapeHtml(k)}:</strong> ${Math.round(v * 100)}%</span>`).join("")}
        </div>
      </div>` : ""}

    ${sources.length > 0 ? `
      <div class="detail-block">
        <h4>Fuentes de los reportes</h4>
        <ul class="detail-list">
          ${sources.map((s) => `<li>📡 ${escapeHtml(s)}</li>`).join("")}
        </ul>
      </div>` : ""}

    <div class="detail-block">
      <h4>Contradicciones</h4>
      ${contradictions.length > 0 ? `
        <ul class="detail-list" style="color: var(--color-critica);">
          ${contradictions.map((c) => `<li>⚠️ ${escapeHtml(c)}</li>`).join("")}
        </ul>` : `<span style="color: var(--text-muted); font-size: 0.8125rem;">Sin contradicciones reportadas.</span>`}
    </div>

    ${citations.length > 0 ? `
      <div class="detail-block">
        <h4>Citas normativas (EMRE)</h4>
        <ul class="detail-list">
          ${citations.map((c) => `<li>📖 <strong>${escapeHtml(c.document)}</strong> - ${escapeHtml(c.section)} ${c.is_example ? "(ejemplo)" : ""}</li>`).join("")}
        </ul>
      </div>` : ""}

    ${steps.length > 0 ? `
      <div class="detail-block">
        <h4>Trazabilidad de agentes</h4>
        <ul class="detail-list">
          ${steps.map((st) => `<li>🤖 <strong>${escapeHtml(st.agent)}:</strong> ${escapeHtml(st.detail)}</li>`).join("")}
        </ul>
      </div>` : ""}

    <div class="detail-block">
      <h4>Reportes agrupados (${reports.length})</h4>
      <div style="font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-muted);">
        ${reports.map((r) => escapeHtml(r)).join(", ")}
      </div>
    </div>
  `;
}

// --- Envío de Decisiones al Nodo ----------------------------------------------

async function submitDecision({ fichaId, action, operator, editedMessage = null }) {
  let payload;
  try {
    payload = formatDecisionRequest({
      fichaId,
      action,
      operator,
      editedMessage,
    });
  } catch (err) {
    showAlert(err.message, "error");
    return;
  }

  const res = await fetchJson("/api/decisions", {
    method: "POST",
    body: JSON.stringify(payload),
  });

  if (res.ok) {
    showAlert(`Decisión '${action}' registrada correctamente (Evento: ${res.data?.event_id || "OK"}).`, "success");
    // Actualizar estado en memoria
    const incident = state.incidents.find((i) => i.ficha_id === fichaId);
    if (incident) {
      if (action === "aprobar") incident.status = "aprobada";
      else if (action === "editar") {
        incident.status = "editada";
        if (editedMessage) incident.message = editedMessage;
      } else if (action === "rechazar") incident.status = "rechazada";
      renderFeed();
    }
  } else if (res.status === 404) {
    showAlert("Error 404: La ficha indicada no existe en el registro del nodo.", "error");
  } else if (res.status === 409) {
    showAlert("Error 409: La ficha ya tiene una decisión tomada o se encuentra en conflicto.", "error");
  } else {
    showAlert(`Error (${res.status}): ${res.data?.error || "No se pudo registrar la decisión."}`, "error");
  }
}

// --- Modal de Edición de Mensaje ----------------------------------------------

function openEditModal(incident, fichaId) {
  state.pendingEdit = { incident, fichaId };
  const modal = document.getElementById("modal-edit");
  const textarea = document.getElementById("modal-edit-text");
  if (!modal || !textarea) return;

  textarea.value = incident?.message || "";
  modal.classList.remove("hidden");
  textarea.focus();
}

function closeEditModal() {
  state.pendingEdit = null;
  const modal = document.getElementById("modal-edit");
  if (modal) modal.classList.add("hidden");
}

function setupModal() {
  const modal = document.getElementById("modal-edit");
  const cancelBtn = document.getElementById("modal-edit-cancel");
  const submitBtn = document.getElementById("modal-edit-submit");
  const textarea = document.getElementById("modal-edit-text");

  if (cancelBtn) cancelBtn.onclick = closeEditModal;

  if (modal) {
    modal.onclick = (e) => {
      if (e.target === modal) closeEditModal();
    };
  }

  if (submitBtn && textarea) {
    submitBtn.onclick = async () => {
      const editedMessage = textarea.value.trim();
      if (!editedMessage) {
        showAlert("El mensaje editado no puede estar vacío.", "error");
        return;
      }
      const { fichaId } = state.pendingEdit || {};
      closeEditModal();
      if (fichaId) {
        await submitDecision({
          fichaId,
          action: "editar",
          operator: state.operator,
          editedMessage,
        });
      }
    };
  }
}

// --- Alertas visuales ---------------------------------------------------------

function showAlert(message, type = "error") {
  const container = document.getElementById("alert-container");
  if (!container) return;

  const alertDiv = document.createElement("div");
  alertDiv.className = `alert-banner ${type}`;
  alertDiv.innerHTML = `
    <span>${escapeHtml(message)}</span>
    <button title="Cerrar">&times;</button>
  `;

  alertDiv.querySelector("button").onclick = () => alertDiv.remove();
  container.appendChild(alertDiv);

  setTimeout(() => {
    if (alertDiv.parentNode) alertDiv.remove();
  }, 6000);
}

// --- Conmutación y Polling de Nodos -------------------------------------------

async function checkNodeHealthAndPoll() {
  // 1. Verificar salud del nodo actual
  const healthRes = await fetchJson("/api/health");
  state.nodeState = updateNodeHealth(state.nodeState, {
    ok: healthRes.ok,
    node: healthRes.data?.node,
    error: healthRes.data?.error,
  });

  updateNodeStatusUi();

  // 2. Si hay nodo conectado, consultar incidentes
  if (state.nodeState.connectedNode) {
    const incRes = await fetchJson("/api/incidents");
    if (incRes.ok && incRes.data?.incidents) {
      state.incidents = incRes.data.incidents;
      renderFeed();
      updateLastSyncTime();
    }
  }
}

function updateNodeStatusUi() {
  const textEl = document.getElementById("node-status-text");
  const dotEl = document.getElementById("status-dot");
  if (!textEl || !dotEl) return;

  textEl.textContent = state.nodeState.statusText;

  if (state.nodeState.connectedNode) {
    dotEl.className = "status-dot";
  } else {
    dotEl.className = "status-dot disconnected";
  }
}

function updateLastSyncTime() {
  const syncEl = document.getElementById("last-update");
  if (syncEl) {
    const d = new Date();
    syncEl.textContent = `Actualizado: ${d.toLocaleTimeString("es-CO")}`;
  }
}

function setupOperator() {
  const nameEl = document.getElementById("operator-name");
  const changeBtn = document.getElementById("btn-change-operator");
  if (nameEl) nameEl.textContent = state.operator;

  if (changeBtn) {
    changeBtn.onclick = () => {
      const nuevo = prompt("Nombre o indicativo del operador:", state.operator);
      if (nuevo && nuevo.trim()) {
        state.operator = nuevo.trim();
        localStorage.setItem("guadua_operator", state.operator);
        if (nameEl) nameEl.textContent = state.operator;
      }
    };
  }
}

// --- Inicialización Principal -------------------------------------------------

async function init() {
  // Configurar nodos desde URL si vienen como ?nodes=url1,url2
  const urlParams = new URLSearchParams(window.location.search);
  const nodesParam = urlParams.get("nodes");
  if (nodesParam) {
    const customNodes = nodesParam.split(",").map((n) => n.trim()).filter(Boolean);
    if (customNodes.length > 0) {
      state.nodeState = createInitialNodeState(customNodes);
    }
  }

  setupOperator();
  setupModal();
  initMap();
  await loadBarrios();

  // Primera consulta de salud e incidentes
  await checkNodeHealthAndPoll();

  // Ciclo periódico de consulta
  setInterval(checkNodeHealthAndPoll, POLL_INTERVAL_MS);
}

// Iniciar aplicación al cargar el DOM
window.addEventListener("DOMContentLoaded", init);
