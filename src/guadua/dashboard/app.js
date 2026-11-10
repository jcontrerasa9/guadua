// Controlador del Tablero del Comandante - Guadua (SARD)
// Implementa la interacción con el usuario, llamadas a la API y renderizado del mapa offline.

import { decodeGeohash } from "./geohash.js";
import {
  computeConfidence,
  computeRiskWarning,
  createInitialNodeState,
  DEFAULT_DECISION_FILTER,
  filterByDecision,
  DASHBOARD_MOCK_PEOPLE,
  formatDecisionRequest,
  formatTimeAgo,
  matchSoughtPeople,
  nodesStartingWith,
  partitionAndSortIncidents,
  updateNodeHealth,
} from "./logic.js";
import { audioContentType, DEMO_CLIP, radioListHtml, radioSignature } from "./radio.js";
import {
  checkConvergence,
  HttpSimulationClient,
  MockSimulationClient,
} from "./simulation.js";

// Constantes del sistema
export const POLL_INTERVAL_MS = 5000;
export const DEFAULT_NODES = [
  "http://localhost:8000",
  "http://localhost:8001",
  "http://localhost:8002",
];

const DECISION_FILTER_KEY = "guadua_decision_filter";

function readDecisionFilter() {
  try {
    return localStorage.getItem(DECISION_FILTER_KEY) || DEFAULT_DECISION_FILTER;
  } catch {
    return DEFAULT_DECISION_FILTER;
  }
}

const EMPTY_SHORT = {
  por_decidir: "No hay incidentes críticos ni de prioridad alta por decidir.",
  despachados: "Todavía no hay incidentes críticos ni de prioridad alta despachados.",
  todos: "No hay incidentes críticos ni de prioridad alta activos.",
};

const defaultOrigin = typeof window !== "undefined" && window.location ? window.location.origin : "";
const defaultOperator =
  typeof localStorage !== "undefined" ? localStorage.getItem("guadua_operator") || "comandante-1" : "comandante-1";

// Estado global de la aplicación
export const state = {
  // Primero el nodo que sirvió este tablero; los demás, de respaldo.
  nodeState: createInitialNodeState(nodesStartingWith(defaultOrigin, DEFAULT_NODES)),
  operator: defaultOperator,
  barriosMap: new Map(), // code -> { code, name, lat, lon }
  incidents: [],
  safePeople: [],
  soughtText: "",
  detailCache: new Map(), // incident_id -> IncidentDetail
  openDetails: new Set(), // incident_id con desplegable 'Por qué está aquí' abierto
  mapInstance: null,
  mapMarkersLayer: null,
  pendingEdit: null, // { incident, fichaId }
  mockFixture: null,
  simClient: null,
  radioClips: [], // D7: GET /api/radio
  radioSignature: "",
  radioDrafts: {}, // audio_id -> texto que el operador lleva escrito
  decisionFilter: readDecisionFilter(), // selector Por decidir / Despachados / Todos
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
  const urlParams =
    typeof window !== "undefined" && window.location
      ? new URLSearchParams(window.location.search)
      : new URLSearchParams();
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

  if (endpoint === "/api/people") {
    return { ok: true, status: 200, data: DASHBOARD_MOCK_PEOPLE };
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
  4: "#c62828", // Crítica (mismos tonos que styles.css)
  3: "#e65100", // Alta
  2: "#b7791f", // Media
  1: "#2e7d32", // Baja
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

function paintDecisionFilter({ filter, counts }) {
  if (typeof document === "undefined") return;
  document.querySelectorAll("#decision-filter .filter-option").forEach((btn) => {
    btn.setAttribute("aria-pressed", String(btn.dataset.filter === filter));
  });
  document.querySelectorAll("#decision-filter .filter-count").forEach((el) => {
    el.textContent = String(counts[el.dataset.count] ?? 0);
  });
}

function setupDecisionFilter() {
  document.querySelectorAll("#decision-filter .filter-option").forEach((btn) => {
    btn.onclick = () => {
      state.decisionFilter = btn.dataset.filter;
      try {
        localStorage.setItem(DECISION_FILTER_KEY, state.decisionFilter);
      } catch {
        // Sin almacenamiento el filtro funciona igual; solo no se recuerda.
      }
      renderFeed();
    };
  });
}

function renderFeed() {
  const shortContainer = document.getElementById("short-list");
  const otherContainer = document.getElementById("other-list");
  const otherSummary = document.getElementById("other-incidents-summary");
  const barriosContainer = document.getElementById("barrios-list");

  if (!shortContainer || !otherContainer) return;

  // Sincronizar desplegables abiertos en el DOM antes de actualizar
  if (typeof document !== "undefined") {
    document.querySelectorAll(".details-toggle[open]").forEach((el) => {
      if (el.dataset.detailsId) {
        state.openDetails.add(el.dataset.detailsId);
      }
    });
  }

  // El selector filtra las dos listas; el mapa y los barrios muestran todo.
  const filtered = filterByDecision(state.incidents, state.decisionFilter);
  paintDecisionFilter(filtered);
  const { shortList, otherList } = partitionAndSortIncidents(filtered.incidents);

  // Render lista corta (Nivel 4 y 3)
  if (shortList.length === 0) {
    shortContainer.innerHTML = `
      <div class="empty-state card">${EMPTY_SHORT[filtered.filter]}</div>`;
  } else {
    shortContainer.innerHTML = shortList.map((inc) => renderIncidentCardHtml(inc)).join("");
  }

  // Render lista desplegable (Nivel 2 y 1)
  if (otherSummary) {
    otherSummary.textContent = `${otherList.length} ${otherList.length === 1 ? "incidente" : "incidentes"} de prioridad media o baja`;
  }

  if (otherList.length === 0) {
    otherContainer.innerHTML = `
      <div class="empty-state">No hay incidentes de prioridad media o baja.</div>`;
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
      barriosContainer.innerHTML = `<p class="panel-empty">No hay reportes aproximados por barrio sin coordenadas.</p>`;
    } else {
      barriosContainer.innerHTML = barriosSinCoords
        .map((inc) => {
          const barrioCode = inc.location.barrio;
          const barrioName = inc.location.label || state.barriosMap.get(barrioCode)?.name || `Barrio #${barrioCode}`;
          return `
          <div class="barrio-item">
            <div>
              <strong>${escapeHtml(barrioName)}</strong>
              <div class="barrio-sub">
                ${escapeHtml(inc.title)} · radio ${inc.location.radius_m || 0} m
              </div>
            </div>
            <span class="barrio-tag priority-tag level-${inc.level}">${getPriorityLabel(inc.level)}</span>
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

// Ícono de advertencia en línea (SVG): sin emojis ni dependencias.
const WARNING_ICON = `<svg class="risk-icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.5 15 14H1L8 1.5Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M8 6v3.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="8" cy="11.75" r=".9" fill="currentColor"/></svg>`;

// Tipo de incidente de la cabecera (protocol/header.py, Incident): el título sale del texto
// del reporte, así que el tipo va en la línea de metadatos.
const INCIDENT_LABELS = {
  0: "Atrapados",
  1: "Derrumbe",
  2: "Incendio",
  3: "Fuga de gas",
  4: "Herido",
  5: "Vía bloqueada",
  6: "Otro",
};

const STATUS_LABELS = {
  pendiente: "Pendiente",
  aprobada: "Aprobada",
  editada: "Editada",
  rechazada: "Rechazada",
  conflicto: "Conflicto",
  sin_ficha: "Sin ficha",
};

function getPriorityLabel(level) {
  switch (level) {
    case 4:
      return "Crítica";
    case 3:
      return "Alta";
    case 2:
      return "Media";
    case 1:
      return "Baja";
    default:
      return `Nivel ${level}`;
  }
}

export function renderIncidentCardHtml(inc) {
  const conf = computeConfidence(inc.confidence, inc.verify_now, inc.rumor);
  const risk = computeRiskWarning(inc);
  const timeAgo = formatTimeAgo(inc.received_at);
  const priorityText = getPriorityLabel(inc.level);
  const hasFicha = Boolean(inc.ficha_id);
  const isActionable = hasFicha && (inc.status === "pendiente" || inc.status === "conflicto");

  let locationText = "Ubicación sin datos";
  if (inc.location?.label) {
    locationText = inc.location.label;
  } else if (inc.location?.geohash) {
    locationText = `GPS ${inc.location.geohash}`;
  } else if (inc.location?.barrio) {
    locationText = state.barriosMap.get(inc.location.barrio)?.name || `Barrio #${inc.location.barrio}`;
  }
  const confidenceText = `Confianza ${conf.label.toLowerCase()}${conf.score !== null ? ` (${Math.round(conf.score * 100)} %)` : ""}`;

  const isOpen = state.openDetails.has(inc.incident_id);
  const cachedDetail = state.detailCache.get(inc.incident_id);
  const detailInnerHtml = isOpen && cachedDetail
    ? buildIncidentDetailInnerHtml(cachedDetail)
    : `<div class="muted">Cargando trazabilidad...</div>`;

  return `
    <article class="incident-card level-${inc.level}" data-incident-id="${escapeHtml(inc.incident_id)}">
      <div class="card-head">
        <div class="card-tags">
          <span class="priority-tag level-${inc.level}">${priorityText}</span>
          <span class="status-badge status-${inc.status}">${escapeHtml(STATUS_LABELS[inc.status] || inc.status)}</span>
          ${conf.tags.map((tag) => tag === "VERIFICAR YA"
            ? `<span class="tag-alert">Verificar ya</span>`
            : `<span class="tag-rumor">${escapeHtml(tag.charAt(0) + tag.slice(1).toLowerCase())}</span>`).join("")}
        </div>
        <span class="card-time">${timeAgo}</span>
      </div>

      <h3 class="card-title">${escapeHtml(inc.title)}</h3>
      <div class="card-meta">${INCIDENT_LABELS[inc.incident] ? `${INCIDENT_LABELS[inc.incident]}<span class="sep">·</span>` : ""}${escapeHtml(locationText)}<span class="sep">·</span>${escapeHtml(confidenceText)}</div>

      ${risk.show ? `
        <div class="risk-box" role="alert">
          ${WARNING_ICON}
          <div>
            <strong class="risk-title">Riesgo para rescatistas</strong>
            <span class="risk-message">${escapeHtml(risk.text)}</span>
          </div>
        </div>` : ""}

      ${(inc.suggestion || inc.message) ? `
        <dl class="suggestion-box">
          ${inc.suggestion ? `<dt>Unidad (EMRE)</dt><dd class="suggestion-text">${escapeHtml(inc.suggestion)}</dd>` : ""}
          ${inc.message ? `<dt>Mensaje</dt><dd class="message-text">${escapeHtml(inc.message)}</dd>` : ""}
        </dl>` : ""}

      <!-- Botones de acción del comandante -->
      <div class="card-actions">
        <button class="btn-primary ${!isActionable ? "btn-disabled" : ""}"
          data-action="aprobar" data-ficha-id="${inc.ficha_id || ""}" ${!isActionable ? "disabled" : ""}>Aprobar</button>
        <button class="btn-secondary ${!isActionable ? "btn-disabled" : ""}"
          data-action="editar" data-ficha-id="${inc.ficha_id || ""}" ${!isActionable ? "disabled" : ""}>Editar mensaje</button>
        <button class="btn-danger ${!isActionable ? "btn-disabled" : ""}"
          data-action="rechazar" data-ficha-id="${inc.ficha_id || ""}" ${!isActionable ? "disabled" : ""}>Replanear</button>
        ${!hasFicha ? `<span class="no-ficha">Sin ficha de coordinación</span>` : ""}
      </div>

      <!-- Desplegable 'Por qué está aquí' -->
      <details class="details-toggle" data-details-id="${escapeHtml(inc.incident_id)}" ${isOpen ? "open" : ""}>
        <summary>Por qué está aquí: evidencia y agentes</summary>
        <div class="details-content" id="details-${escapeHtml(inc.incident_id)}">
          ${detailInnerHtml}
        </div>
      </details>
    </article>
  `;
}

export function escapeHtml(str) {
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
    const incidentId = details.dataset.detailsId;
    if (!incidentId) return;

    details.ontoggle = async () => {
      if (!details.open) {
        state.openDetails.delete(incidentId);
        return;
      }

      state.openDetails.add(incidentId);
      const container = document.getElementById(`details-${incidentId}`);
      if (!container) return;

      let detail = state.detailCache.get(incidentId);
      if (!detail) {
        const res = await fetchJson(`/api/incidents/${incidentId}`);
        if (res.ok) {
          detail = res.data;
          state.detailCache.set(incidentId, detail);
        } else {
          container.innerHTML = `<span class="detail-error">Error cargando detalle: ${escapeHtml(res.data?.error || "Error")}</span>`;
          return;
        }
      }

      renderIncidentDetailHtml(container, detail);
    };

    // Si ya está abierto pero aún no tiene su detalle cargado en caché
    if (details.open && !state.detailCache.has(incidentId)) {
      details.ontoggle();
    }
  });
}

export function buildIncidentDetailInnerHtml(detail) {
  if (!detail) {
    return `<div class="muted">Cargando trazabilidad...</div>`;
  }
  const probEntries = Object.entries(detail.probabilities || {});
  const rules = detail.rules || [];
  const sources = detail.sources || [];
  const contradictions = detail.contradictions || [];
  const citations = detail.citations || [];
  const steps = detail.steps || [];
  const reports = detail.report_ids || [];

  return `
    ${rules.length > 0 ? `
      <div class="detail-block">
        <h4>Reglas de urgencia aplicadas</h4>
        <ul class="detail-list">
          ${rules.map((r) => `<li>${escapeHtml(r)}</li>`).join("")}
        </ul>
      </div>` : ""}

    ${probEntries.length > 0 ? `
      <div class="detail-block">
        <h4>Probabilidades del clasificador</h4>
        <div class="prob-row">
          ${probEntries.map(([k, v]) => `<span><strong>${escapeHtml(k)}:</strong> ${Math.round(v * 100)}%</span>`).join("")}
        </div>
      </div>` : ""}

    ${sources.length > 0 ? `
      <div class="detail-block">
        <h4>Fuentes de los reportes</h4>
        <ul class="detail-list">
          ${sources.map((s) => `<li>${escapeHtml(s)}</li>`).join("")}
        </ul>
      </div>` : ""}

    <div class="detail-block">
      <h4>Contradicciones</h4>
      ${contradictions.length > 0 ? `
        <ul class="detail-list contradictions">
          ${contradictions.map((c) => `<li>${escapeHtml(c)}</li>`).join("")}
        </ul>` : `<span class="panel-empty">Sin contradicciones reportadas.</span>`}
    </div>

    ${citations.length > 0 ? `
      <div class="detail-block">
        <h4>Citas normativas (EMRE)</h4>
        <ul class="detail-list">
          ${citations.map((c) => `<li><strong>${escapeHtml(c.document)}</strong> - ${escapeHtml(c.section)} ${c.is_example ? "(ejemplo)" : ""}</li>`).join("")}
        </ul>
      </div>` : ""}

    ${steps.length > 0 ? `
      <div class="detail-block">
        <h4>Trazabilidad de agentes</h4>
        <ul class="detail-list">
          ${steps.map((st) => `<li><strong>${escapeHtml(st.agent)}:</strong> ${escapeHtml(st.detail)}</li>`).join("")}
        </ul>
      </div>` : ""}

    <div class="detail-block">
      <h4>Reportes agrupados (${reports.length})</h4>
      <div class="report-ids">
        ${reports.map((r) => escapeHtml(r)).join(", ")}
      </div>
    </div>
  `;
}

export function renderIncidentDetailHtml(container, detail) {
  if (container) {
    container.innerHTML = buildIncidentDetailInnerHtml(detail);
  }
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

  // 2. Si hay nodo conectado, consultar incidentes y personas a salvo
  if (state.nodeState.connectedNode) {
    const incRes = await fetchJson("/api/incidents");
    if (incRes.ok && incRes.data?.incidents) {
      state.incidents = incRes.data.incidents;
      renderFeed();
      updateLastSyncTime();
    }

    const peopleRes = await fetchJson("/api/people");
    if (peopleRes.ok && Array.isArray(peopleRes.data?.people)) {
      state.safePeople = peopleRes.data.people;
      renderSafePeople();
    }

    await loadRadio();
  }

  // 3. Monitorear convergencia de hashes en la barra de simulación
  await updateHashesUi();
}

function renderSoughtMatches() {
  const container = document.getElementById("sought-matches-list");
  if (!container) return;

  const text = state.soughtText ? state.soughtText.trim() : "";
  if (!text) {
    container.innerHTML = "";
    return;
  }

  const matches = matchSoughtPeople(state.soughtText, state.safePeople);
  if (matches.length === 0) {
    container.innerHTML = "";
    return;
  }

  container.innerHTML = matches
    .map(
      (m) => `
      <div class="sought-match ${m.found ? "found" : "not-found"}">
        <span class="sought-name"><strong>${escapeHtml(m.query)}</strong></span>
        <span class="sought-status">${escapeHtml(m.statusText)}</span>
      </div>
    `,
    )
    .join("");
}

function renderSafePeople() {
  const countEl = document.getElementById("safe-people-count");
  if (countEl) {
    countEl.textContent = String(state.safePeople.length);
  }

  renderSoughtMatches();

  const listEl = document.getElementById("safe-people-list");
  if (!listEl) return;

  if (state.safePeople.length === 0) {
    listEl.innerHTML = `<p class="panel-empty">No hay reportes de personas a salvo aún.</p>`;
    return;
  }

  listEl.innerHTML = state.safePeople
    .map((p) => {
      const nameHtml = p.name
        ? `<span class="safe-person-name">${escapeHtml(p.name)}</span>`
        : `<span class="safe-person-anon">(Sin nombre)</span>`;
      const barrioHtml = p.barrio_label
        ? `<span>${escapeHtml(p.barrio_label)}</span>`
        : "";
      const timeHtml = p.received_at
        ? `<span>${formatTimeAgo(p.received_at)}</span>`
        : "";
      return `
        <div class="safe-person-item">
          ${nameHtml}
          <div class="safe-person-meta">
            ${barrioHtml}
            ${timeHtml}
          </div>
        </div>
      `;
    })
    .join("");
}

// --- Radio VHF (D7) ------------------------------------------------------------

function activeNodeBase() {
  return (state.nodeState.connectedNode || state.nodeState.nodes[0] || "").replace(/\/$/, "");
}

async function loadRadio() {
  const res = await fetchJson("/api/radio");
  if (!res.ok || !Array.isArray(res.data?.clips)) return;
  state.radioClips = res.data.clips;
  renderRadio();
}

/** Redibuja solo si cambió algo, y nunca mientras suena un audio o se escribe una corrección. */
function renderRadio(force = false) {
  const listEl = document.getElementById("radio-list");
  const countEl = document.getElementById("radio-count");
  if (countEl) countEl.textContent = String(state.radioClips.length);
  if (!listEl) return;
  const signature = radioSignature(state.radioClips);
  const busy =
    (typeof document.activeElement?.closest === "function" &&
      document.activeElement.closest("#radio-list")) ||
    Array.from(listEl.querySelectorAll?.("audio") || []).some((a) => !a.paused);
  if (!force && (signature === state.radioSignature || busy)) return;
  state.radioSignature = signature;
  listEl.innerHTML = radioListHtml(state.radioClips, activeNodeBase(), state.radioDrafts);
}

function setRadioStatus(text) {
  const el = document.getElementById("radio-status");
  if (el) el.textContent = text;
}

function radioAckText(ack) {
  if (ack.duplicate) return "Ese clip ya estaba transcrito.";
  if (ack.status === "pendiente") return `Clip guardado sin texto: ${ack.note || "escriba la transcripción"}.`;
  return "Clip transcrito: el agente de recepción lo separa en reportes.";
}

async function postAudio(file) {
  setRadioStatus(`Transcribiendo ${file.name}… (unos segundos)`);
  try {
    const res = await fetch(`${activeNodeBase()}/api/radio`, {
      method: "POST",
      headers: { "Content-Type": audioContentType(file) },
      body: file,
    });
    const data = await res.json();
    setRadioStatus(res.ok ? radioAckText(data) : `No se pudo: ${data.error}`);
  } catch (err) {
    setRadioStatus(`Error de conexión: ${err.message}`);
  }
  await loadRadio();
}

async function saveTranscription(audioId) {
  const text = (state.radioDrafts[audioId] ?? "").trim();
  if (!text) {
    showAlert("Escriba el texto de la transmisión antes de guardar.", "error");
    return;
  }
  const res = await fetchJson(`/api/radio/${encodeURIComponent(audioId)}/transcription`, {
    method: "POST",
    body: JSON.stringify({ text, operator: state.operator }),
  });
  if (!res.ok) {
    showAlert(`No se pudo guardar la transcripción: ${res.data?.error || res.status}`, "error");
    return;
  }
  delete state.radioDrafts[audioId];
  setRadioStatus("Transcripción guardada: se crean reportes nuevos y los anteriores se ocultan.");
  if (document.activeElement?.blur) document.activeElement.blur();
  await loadRadio();
  renderRadio(true);
}

function setupRadioPanel() {
  const input = document.getElementById("radio-upload-input");
  if (input) {
    input.addEventListener("change", async () => {
      const file = input.files?.[0];
      if (file) await postAudio(file);
      input.value = "";
    });
  }
  const demoBtn = document.getElementById("btn-radio-demo");
  if (demoBtn) {
    demoBtn.addEventListener("click", async () => {
      setRadioStatus("Transcribiendo el clip de prueba… (unos segundos)");
      const res = await fetchJson(`/api/radio/demo/${DEMO_CLIP}`, { method: "POST" });
      setRadioStatus(res.ok ? radioAckText(res.data) : `No se pudo: ${res.data?.error}`);
      await loadRadio();
    });
  }
  const listEl = document.getElementById("radio-list");
  if (listEl) {
    listEl.addEventListener("input", (e) => {
      if (e.target?.classList?.contains("radio-fix-text")) {
        state.radioDrafts[e.target.dataset.audioId] = e.target.value;
      }
    });
    listEl.addEventListener("click", async (e) => {
      if (e.target?.classList?.contains("radio-fix-save")) {
        await saveTranscription(e.target.dataset.audioId);
      }
    });
  }
}

function setupSoughtInput() {
  const soughtInput = document.getElementById("sought-names-input");
  if (soughtInput) {
    soughtInput.addEventListener("input", () => {
      state.soughtText = soughtInput.value;
      renderSoughtMatches();
    });
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
    syncEl.textContent = `Actualizado ${d.toLocaleTimeString("es-CO")}`;
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

// --- Barra de Simulación para el Jurado (D9) ----------------------------------

export async function updateHashesUi() {
  if (!state.simClient) return;

  try {
    const hashes = await state.simClient.getHashes();
    const convergence = checkConvergence(hashes);

    const badge = document.getElementById("sim-convergence-badge");
    if (badge) {
      if (convergence.converged) {
        badge.classList.remove("diverged");
        badge.textContent = convergence.message || "3/3 nodos convergidos";
      } else {
        badge.classList.add("diverged");
        badge.textContent = convergence.message || "Divergencia (sincronizando)";
      }
    }

    const firstHash = hashes["A"] || "";
    for (const nodeId of ["A", "B", "C"]) {
      const codeEl = document.getElementById(`sim-hash-${nodeId.toLowerCase()}`);
      const boxEl = document.getElementById(`sim-hash-box-${nodeId.toLowerCase()}`);
      const hashVal = hashes[nodeId] || "";

      if (codeEl) {
        codeEl.textContent = hashVal ? `${hashVal.slice(0, 8)}…` : "—";
        codeEl.title = hashVal || "Sin hash";
      }
      if (boxEl) {
        const isKilled = state.simClient.killedNodes?.has(nodeId);
        const isDiverged = !convergence.converged && hashVal !== firstHash;
        boxEl.classList.toggle("killed", Boolean(isKilled));
        boxEl.classList.toggle("diverged", Boolean(isDiverged));
      }
    }
  } catch (err) {
    console.warn("Error actualizando hashes de simulación:", err);
  }
}

/**
 * Cliente de la barra: con ?mock=1, el simulado en memoria; si no, el real (proxy de fallos
 * en :8080 o ?proxy=url, y los nodos A, B y C en :8000, :8001 y :8002 del mismo equipo).
 */
export function defaultSimulationClient(location = typeof window !== "undefined" ? window.location : null) {
  const params = new URLSearchParams(location?.search || "");
  if (params.get("mock") === "1" || !location?.hostname) {
    return new MockSimulationClient();
  }
  const base = `${location.protocol}//${location.hostname}`;
  // ?nodes=urlA,urlB,urlC (como el resto del tablero) cambia dónde están A, B y C.
  const custom = (params.get("nodes") || "").split(",").map((n) => n.trim()).filter(Boolean);
  const [a, b, c] = custom.length === 3 ? custom : [`${base}:8000`, `${base}:8001`, `${base}:8002`];
  return new HttpSimulationClient({
    proxyUrl: params.get("proxy") || `${base}:8080`,
    nodeUrls: { A: a, B: b, C: c },
  });
}

function simStatus(text) {
  const statusEl = document.getElementById("sim-status-msg");
  if (statusEl) statusEl.textContent = text;
}

function simError(what, res) {
  const why = res?.data?.error || (res?.status ? `HTTP ${res.status}` : "sin respuesta");
  simStatus(`No se pudo ${what}: ${why}`);
}

function paintKillButton(killBtn, killed) {
  killBtn.textContent = killed ? "Revivir nodo A" : "Apagar nodo A";
  killBtn.classList.toggle("active-killed", killed);
}

export function setupSimulationBar(client = null) {
  state.simClient = client || defaultSimulationClient();

  // 1. Botones de perfil de canal
  const profileBtns = [
    { id: "btn-sim-profile-wifi", profile: "wifi" },
    { id: "btn-sim-profile-lora", profile: "radio_lora" },
    { id: "btn-sim-profile-sms", profile: "sms" },
  ];

  for (const { id, profile } of profileBtns) {
    const btn = document.getElementById(id);
    if (btn) {
      btn.addEventListener("click", async () => {
        const res = await state.simClient.setProfile(profile);
        if (res && res.ok === false) {
          simError("cambiar el perfil del canal (¿está el proxy en :8080?)", res);
          return;
        }
        for (const p of profileBtns) {
          const b = document.getElementById(p.id);
          if (b) b.classList.toggle("active", p.profile === profile);
        }
        simStatus(`Perfil de canal cambiado a: ${profile.toUpperCase()}`);
      });
    }
  }

  // 2. Botón ráfaga
  const burstBtn = document.getElementById("btn-sim-burst");
  if (burstBtn) {
    burstBtn.addEventListener("click", async () => {
      const res = await state.simClient.burst(10);
      if (res && res.ok === false) {
        simError("lanzar la ráfaga (¿está el proxy en :8080?)", res);
        return;
      }
      const d = res?.data;
      simStatus(
        d && typeof d.delivered === "number"
          ? `Ráfaga de ${d.sent} reportes sintéticos: ${d.delivered} entregados, ${d.lost} perdidos en el canal (${d.profile}).`
          : "Ráfaga de 10 reportes sintéticos inyectada.",
      );
      await updateHashesUi();
    });
  }

  // 3. Botón inyectar radio
  const radioBtn = document.getElementById("btn-sim-radio");
  if (radioBtn) {
    radioBtn.addEventListener("click", async () => {
      simStatus("Clip de radio enviado; transcribiendo…");
      const res = await state.simClient.injectRadio("clip_colapso_dos_incidentes");
      if (res && res.ok === false) {
        simError("inyectar el clip de radio", res);
        return;
      }
      simStatus(
        res?.data?.status === "pendiente"
          ? "Clip de radio recibido sin transcribir: escribe el texto en el panel de radio."
          : "Clip de radio VHF inyectado (2 incidentes transcritos).",
      );
      await updateHashesUi();
    });
  }

  // 4. Botón apagar nodo A
  const killBtn = document.getElementById("btn-sim-kill-a");
  if (killBtn) {
    if (typeof state.simClient.syncPower === "function") {
      state.simClient.syncPower().then((killed) => paintKillButton(killBtn, killed.has("A")));
    }
    killBtn.addEventListener("click", async () => {
      const res = await state.simClient.killNode("A");
      if (res && res.ok === false) {
        simError("cambiar el nodo A", res);
        return;
      }
      paintKillButton(killBtn, Boolean(res.killed));
      simStatus(
        res.killed
          ? "Nodo A apagado (simulando caída de red o equipo). B toma su trabajo en ~10 s."
          : "Nodo A recuperado y sincronizado.",
      );
      await updateHashesUi();
    });
  }

  // 5. Botón refrescar hashes
  const refreshBtn = document.getElementById("btn-sim-refresh-hashes");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", async () => {
      await updateHashesUi();
      simStatus("Hashes actualizados.");
    });
  }
}

// --- Inicialización Principal -------------------------------------------------

async function init() {
  // Configurar nodos desde URL si vienen como ?nodes=url1,url2
  const urlParams =
    typeof window !== "undefined" && window.location
      ? new URLSearchParams(window.location.search)
      : new URLSearchParams();
  const nodesParam = urlParams.get("nodes");
  if (nodesParam) {
    const customNodes = nodesParam.split(",").map((n) => n.trim()).filter(Boolean);
    if (customNodes.length > 0) {
      state.nodeState = createInitialNodeState(customNodes);
    }
  }

  setupOperator();
  setupDecisionFilter();
  setupModal();
  setupSoughtInput();
  setupRadioPanel();
  initMap();
  await loadBarrios();
  setupSimulationBar();
  await updateHashesUi();

  // Primera consulta de salud e incidentes
  await checkNodeHealthAndPoll();

  // Ciclo periódico de consulta
  setInterval(checkNodeHealthAndPoll, POLL_INTERVAL_MS);
  setInterval(updateHashesUi, POLL_INTERVAL_MS); // D9: se ve cómo convergen
}

// Iniciar aplicación al cargar el DOM
if (typeof window !== "undefined" && window.addEventListener) {
  window.addEventListener("DOMContentLoaded", init);
}
