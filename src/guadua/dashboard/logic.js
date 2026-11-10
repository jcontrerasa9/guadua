// Lógica pura del tablero del comandante (sin acceso al DOM).
// CONTRATO y reglas: ver docs/diseno.md §3.2, §9, docs/contratos.md y src/guadua/node/api.py.

/**
 * Ordena y parte la lista de incidentes en:
 * - shortList: incidentes de nivel 4 (crítica) y nivel 3 (alta).
 * - otherList: incidentes de nivel 2 (media), 1 (baja) o cualquier otro.
 *
 * El orden es: mayor nivel primero (4, 3, 2, 1); a igual nivel, mayor antigüedad
 * (received_at ascendente: reportes más viejos primero).
 *
 * @param {Array<Object>} incidents - Lista de IncidentCard
 * @returns {{ shortList: Array<Object>, otherList: Array<Object>, allSorted: Array<Object> }}
 */
export function partitionAndSortIncidents(incidents = []) {
  if (!Array.isArray(incidents)) {
    return { shortList: [], otherList: [], allSorted: [] };
  }

  const sorted = [...incidents].sort((a, b) => {
    const levelA = Number(a?.level ?? 0);
    const levelB = Number(b?.level ?? 0);
    if (levelA !== levelB) {
      return levelB - levelA; // Mayor nivel primero
    }
    const timeA = Number(a?.received_at ?? 0);
    const timeB = Number(b?.received_at ?? 0);
    return timeA - timeB; // Más viejo primero (menor timestamp)
  });

  const shortList = sorted.filter((inc) => {
    const lvl = Number(inc?.level ?? 0);
    return lvl === 4 || lvl === 3;
  });

  const otherList = sorted.filter((inc) => {
    const lvl = Number(inc?.level ?? 0);
    return lvl !== 4 && lvl !== 3;
  });

  return {
    shortList,
    otherList,
    allSorted: sorted,
  };
}

/**
 * Filtros del selector del tablero según el estado de la ficha (status de la API).
 * - por_decidir: esperan acción del comandante o de coordinación.
 * - despachados: ya salieron a la unidad.
 * - todos: incluye los rechazados (vuelven a coordinación como una ficha nueva).
 */
export const DECISION_FILTERS = Object.freeze({
  por_decidir: Object.freeze(["pendiente", "conflicto", "sin_ficha"]),
  despachados: Object.freeze(["aprobada", "editada"]),
  todos: null,
});

export const DEFAULT_DECISION_FILTER = "por_decidir";

/**
 * Filtra incidentes por grupo de decisión y cuenta cuántos hay en cada grupo.
 * Un filtro desconocido se trata como el de por defecto.
 *
 * @param {Array<Object>} incidents
 * @param {string} filter - Clave de DECISION_FILTERS
 * @returns {{ filter: string, incidents: Array<Object>, counts: Record<string, number> }}
 */
export function filterByDecision(incidents = [], filter = DEFAULT_DECISION_FILTER) {
  const list = Array.isArray(incidents) ? incidents : [];
  const key = Object.hasOwn(DECISION_FILTERS, filter) ? filter : DEFAULT_DECISION_FILTER;
  const matches = (k, inc) => DECISION_FILTERS[k] === null || DECISION_FILTERS[k].includes(inc?.status);
  const counts = {};
  for (const k of Object.keys(DECISION_FILTERS)) {
    counts[k] = list.filter((inc) => matches(k, inc)).length;
  }
  return { filter: key, incidents: list.filter((inc) => matches(key, inc)), counts };
}

/**
 * Calcula el nivel cualitativo de confianza y las etiquetas visuales.
 *
 * @param {number | null | undefined} confidence - Valor entre 0 y 1 o null
 * @param {boolean} verifyNow - Bandera verify_now
 * @param {boolean} rumor - Bandera rumor
 * @returns {{ level: "alta" | "media" | "baja" | "sin_dato", score: number | null, label: string, tags: string[] }}
 */
export function computeConfidence(confidence, verifyNow = false, rumor = false) {
  const tags = [];
  if (verifyNow) {
    tags.push("VERIFICAR YA");
  }
  if (rumor) {
    tags.push("Posible rumor");
  }

  if (confidence === null || confidence === undefined || typeof confidence !== "number" || Number.isNaN(confidence)) {
    return {
      level: "sin_dato",
      score: null,
      label: "Sin correlación",
      tags,
    };
  }

  let level = "baja";
  let label = "Baja";
  if (confidence >= 0.7) {
    level = "alta";
    label = "Alta";
  } else if (confidence >= 0.3) {
    level = "media";
    label = "Media";
  }

  return {
    level,
    score: confidence,
    label,
    tags,
  };
}

/**
 * Formatea el tiempo transcurrido desde received_at como "hace N min".
 *
 * @param {number} receivedAt - Tiempo Unix en segundos
 * @param {number} nowSeconds - Tiempo Unix actual en segundos
 * @returns {string}
 */
export function formatTimeAgo(receivedAt, nowSeconds = Date.now() / 1000) {
  if (typeof receivedAt !== "number" || Number.isNaN(receivedAt)) {
    return "hace 0 min";
  }
  const diffSec = Math.max(0, nowSeconds - receivedAt);
  const minutes = Math.floor(diffSec / 60);
  return `hace ${minutes} min`;
}

/**
 * Arma y valida el cuerpo de la petición DecisionRequest para POST /api/decisions.
 *
 * Reglas de contrato (api.py):
 * - ficha_id: no vacío.
 * - action: "aprobar" | "editar" | "rechazar".
 * - operator: no vacío.
 * - edited_message: va solo, y siempre, con la acción "editar".
 *
 * @param {{ fichaId: string, action: string, operator: string, editedMessage?: string | null }} params
 * @returns {{ ficha_id: string, action: "aprobar" | "editar" | "rechazar", operator: string, edited_message?: string }}
 */
export function formatDecisionRequest({ fichaId, action, operator, editedMessage = null }) {
  if (!fichaId || typeof fichaId !== "string" || !fichaId.trim()) {
    throw new Error("ficha_id es obligatorio y no puede ser vacío");
  }
  if (!operator || typeof operator !== "string" || !operator.trim()) {
    throw new Error("operator es obligatorio y no puede ser vacío");
  }

  const validActions = ["aprobar", "editar", "rechazar"];
  if (!validActions.includes(action)) {
    throw new Error(`action inválida: '${action}'. Debe ser una de: ${validActions.join(", ")}`);
  }

  const trimmedFicha = fichaId.trim();
  const trimmedOperator = operator.trim();

  if (action === "editar") {
    if (!editedMessage || typeof editedMessage !== "string" || !editedMessage.trim()) {
      throw new Error("edited_message es obligatorio y requiere un mensaje editado no vacío para la acción 'editar'");
    }
    return {
      ficha_id: trimmedFicha,
      action: "editar",
      edited_message: editedMessage.trim(),
      operator: trimmedOperator,
    };
  }

  if (editedMessage !== null && editedMessage !== undefined && String(editedMessage).trim() !== "") {
    throw new Error(`edited_message no debe incluirse con la acción '${action}'`);
  }

  return {
    ficha_id: trimmedFicha,
    action,
    operator: trimmedOperator,
  };
}

/**
 * Obtiene el identificador legible de un nodo.
 *
 * @param {string | { id: string }} node
 * @returns {string}
 */
function getNodeIdentifier(node) {
  if (!node) return "desconocido";
  if (typeof node === "object" && node.id) return String(node.id);
  return String(node);
}

/**
 * Lista de nodos que empieza por el nodo que sirvió el tablero (`origin`).
 *
 * Así el tablero de cada nodo lee primero sus propios datos; los demás quedan de respaldo
 * para el cambio automático si ese nodo cae. Si `origin` no es http(s) (p. ej. file://),
 * la lista queda igual.
 *
 * @param {string} origin - p. ej. window.location.origin
 * @param {string[]} nodes - URLs base de los nodos
 * @returns {string[]}
 */
export function nodesStartingWith(origin, nodes = []) {
  const clean = (url) => String(url).replace(/\/+$/, "");
  const list = nodes.map(clean);
  if (typeof origin !== "string" || !/^https?:\/\//.test(origin)) return list;
  const own = clean(origin);
  return [own, ...list.filter((node) => node !== own)];
}

/**
 * Crea el estado inicial para la selección y conmutación automática de nodos.
 *
 * @param {Array<string | Object>} nodes - Lista de nodos disponibles
 * @param {{ maxConsecutiveFailures?: number }} [options]
 * @returns {Object}
 */
export function createInitialNodeState(nodes = ["A", "B", "C"], options = {}) {
  const nodeList = Array.isArray(nodes) ? [...nodes] : [];
  const maxConsecutiveFailures = options.maxConsecutiveFailures ?? 2;
  return {
    nodes: nodeList,
    currentIndex: 0,
    consecutiveFailures: 0,
    totalFailures: 0,
    connectedNode: nodeList.length > 0 ? nodeList[0] : null,
    nodeName: nodeList.length > 0 ? getNodeIdentifier(nodeList[0]) : null,
    statusText: nodeList.length > 0
      ? `Conectado al nodo ${getNodeIdentifier(nodeList[0])}`
      : "Ningún nodo disponible",
    maxConsecutiveFailures,
  };
}

/**
 * Actualiza el estado de los nodos según el resultado de salud (GET /api/health).
 *
 * - Si responde (ok: true): reinicia contador de fallos y actualiza texto a "Conectado al nodo X".
 * - Si falla 1 vez: conserva el nodo actual.
 * - Si falla 2 veces seguidas: pasa al siguiente nodo y actualiza a "Conectado al nodo Y".
 * - Si todos los nodos fallan consecutivamente: indica "Ningún nodo disponible".
 *
 * @param {Object} state - Estado actual
 * @param {{ ok: boolean, node?: string, error?: any }} healthResult
 * @returns {Object} Nuevo estado inmutable
 */
export function updateNodeHealth(state, healthResult) {
  if (!state || !Array.isArray(state.nodes) || state.nodes.length === 0) {
    return {
      ...state,
      connectedNode: null,
      nodeName: null,
      statusText: "Ningún nodo disponible",
    };
  }

  const nodeCount = state.nodes.length;

  if (healthResult && healthResult.ok) {
    const activeNode = state.nodes[state.currentIndex];
    const nodeIdentifier = healthResult.node || getNodeIdentifier(activeNode);
    return {
      ...state,
      consecutiveFailures: 0,
      totalFailures: 0,
      connectedNode: activeNode,
      nodeName: nodeIdentifier,
      statusText: `Conectado al nodo ${nodeIdentifier}`,
    };
  }

  // Fallo en la consulta al nodo actual
  const failures = state.consecutiveFailures + 1;
  const totalFailures = state.totalFailures + 1;

  if (failures >= state.maxConsecutiveFailures) {
    const nextIndex = (state.currentIndex + 1) % nodeCount;
    // Si todos los nodos han fallado en secuencia
    if (totalFailures >= nodeCount * state.maxConsecutiveFailures) {
      return {
        ...state,
        currentIndex: nextIndex,
        consecutiveFailures: 0,
        totalFailures,
        connectedNode: null,
        nodeName: null,
        statusText: "Ningún nodo disponible",
      };
    }

    const nextNode = state.nodes[nextIndex];
    const nextNodeIdentifier = getNodeIdentifier(nextNode);
    return {
      ...state,
      currentIndex: nextIndex,
      consecutiveFailures: 0,
      totalFailures,
      connectedNode: nextNode,
      nodeName: nextNodeIdentifier,
      statusText: `Conectado al nodo ${nextNodeIdentifier}`,
    };
  }

  return {
    ...state,
    consecutiveFailures: failures,
    totalFailures,
  };
}

/**
 * Decide y estructura los datos para la franja visible de advertencia de riesgo de una tarjeta.
 *
 * Reglas (docs/diseno.md §3.2, §11 y contratos):
 * - Solo es visible si risk_warning no es null, undefined ni cadena vacía tras trim.
 * - Retorna { show: boolean, icon: string, title: string, text: string | null }.
 * - No altera ni depende de la urgencia ni de la prioridad del incidente.
 *
 * @param {string | null | undefined | { risk_warning?: string | null }} input - Texto de advertencia o tarjeta de incidente
 * @returns {{ show: boolean, icon: string, title: string, text: string | null }}
 */
export function computeRiskWarning(input) {
  const raw = typeof input === "object" && input !== null ? input.risk_warning : input;
  if (typeof raw !== "string" || !raw.trim()) {
    return {
      show: false,
      icon: "",
      title: "",
      text: null,
    };
  }
  const text = raw.trim();
  return {
    show: true,
    icon: "⚠️",
    title: "RIESGO PARA RESCATISTAS:",
    text,
  };
}

/**
 * Normaliza un nombre para comparación:
 * - Sin tildes / diacríticos
 * - En minúsculas
 * - Sin espacios iniciales, finales ni redundantes
 *
 * @param {string | null | undefined} name
 * @returns {string}
 */
export function normalizeName(name) {
  if (typeof name !== "string") return "";
  return name
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .trim()
    .replace(/\s+/g, " ");
}

/**
 * Cruza la lista de buscados (ingresada por el operador) con la lista de personas a salvo.
 *
 * Compara nombres normalizados (sin tildes, sin mayúsculas, espacios colapsados).
 * Cuando coincide, marca "Encontrada a salvo en <barrio>".
 *
 * @param {string | string[]} soughtInput - Texto con nombres (separados por línea) o array de nombres
 * @param {Array<{ report_id: string, name?: string | null, barrio_label?: string | null, received_at?: number }>} safePeople - Lista de personas a salvo
 * @returns {Array<{ query: string, normalizedQuery: string, found: boolean, person: Object | null, statusText: string }>}
 */
// Palabras que no identifican a nadie ("María de los Ángeles").
const NAME_STOPWORDS = new Set(["de", "del", "la", "las", "los", "y"]);

function editDistanceAtMost1(a, b) {
  if (a === b) return true;
  if (Math.abs(a.length - b.length) > 1) return false;
  let i = 0;
  let j = 0;
  let edits = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      i++;
      j++;
      continue;
    }
    if (++edits > 1) return false;
    if (a.length > b.length) i++;
    else if (a.length < b.length) j++;
    else {
      i++;
      j++;
    }
  }
  return edits + (a.length - i) + (b.length - j) <= 1;
}

/**
 * Qué tan bien una búsqueda coincide con un nombre (ya normalizados). 0 si no coincide.
 * Cada palabra buscada debe aparecer en el nombre: igual, como inicio (≥ 3 letras) o con
 * una letra de diferencia (≥ 5 letras: "Peres" ↔ "Perez"). El orden no importa.
 *
 * @param {string} query
 * @param {string} name
 * @returns {number}
 */
export function nameMatchScore(query, name) {
  const words = (t) => t.split(" ").filter((w) => w && !NAME_STOPWORDS.has(w));
  const q = words(query);
  const n = words(name);
  if (q.length === 0 || n.length === 0) return 0;
  let score = 0;
  for (const w of q) {
    let best = 0;
    for (const v of n) {
      if (v === w) best = Math.max(best, 3);
      else if (w.length >= 3 && v.startsWith(w)) best = Math.max(best, 2);
      else if (w.length >= 5 && editDistanceAtMost1(w, v)) best = Math.max(best, 1);
    }
    if (best === 0) return 0; // toda palabra buscada debe estar
    score += best;
  }
  return score;
}

export function matchSoughtPeople(soughtInput, safePeople = []) {
  let lines = [];
  if (Array.isArray(soughtInput)) {
    lines = soughtInput;
  } else if (typeof soughtInput === "string") {
    lines = soughtInput.split(/\r?\n/);
  }

  const peopleList = Array.isArray(safePeople) ? safePeople : [];
  const results = [];

  for (const line of lines) {
    if (typeof line !== "string") continue;
    const trimmed = line.trim();
    if (!trimmed) continue;

    const normQuery = normalizeName(trimmed);
    if (!normQuery) continue;

    // Primero el nombre exacto; si no, la mejor coincidencia por palabras.
    const named = peopleList.filter((p) => p && typeof p.name === "string" && p.name.trim());
    let matchedPerson = named.find((p) => normalizeName(p.name) === normQuery) || null;
    let exact = Boolean(matchedPerson);
    if (!matchedPerson) {
      const scored = named
        .map((p) => ({ p, score: nameMatchScore(normQuery, normalizeName(p.name)) }))
        .filter((x) => x.score > 0)
        .sort((x, y) => y.score - x.score);
      matchedPerson = scored[0]?.p || null;
    }

    if (matchedPerson) {
      const barrio = matchedPerson.barrio_label || "barrio no especificado";
      const who = exact ? "" : ` (como «${matchedPerson.name}»)`;
      results.push({
        query: trimmed,
        normalizedQuery: normQuery,
        found: true,
        exact,
        person: matchedPerson,
        statusText: `Encontrada a salvo en ${barrio}${who}`,
      });
    } else {
      results.push({
        query: trimmed,
        normalizedQuery: normQuery,
        found: false,
        person: null,
        statusText: "Sin reporte a salvo",
      });
    }
  }

  return results;
}

/**
 * Datos simulados propios del mock del dashboard para GET /api/people con ?mock=1.
 * Contrato: no modifica el fixture api_examples.json.
 */
export const DASHBOARD_MOCK_PEOPLE = {
  people: [
    {
      report_id: "00000063-3",
      name: "Ana",
      barrio_label: "Palermo",
      received_at: 1794574000.0,
    },
    {
      report_id: "00000064-1",
      name: "Carlos Gómez",
      barrio_label: "Chipre",
      received_at: 1794574100.0,
    },
    {
      report_id: "00000065-2",
      name: "María José Pérez",
      barrio_label: "El Cable",
      received_at: 1794574200.0,
    },
    {
      report_id: "00000066-1",
      name: null,
      barrio_label: "Centro",
      received_at: 1794574300.0,
    },
  ],
};

// --- Posibles duplicados ---------------------------------------------------------------
// La correlación del nodo no fusiona reportes del mismo barrio si no está segura (en un
// sismo hay varios colapsos por barrio y fusionar dos direcciones escondería atrapados):
// los deja como "vínculo dudoso" para una persona. Aquí se le muestra esa duda al
// comandante, para que no despache dos veces lo mismo. No se fusiona nada.

export const DUPLICATE_WINDOW_S = 30 * 60;
const DUPLICATE_RADIUS_M = 600;
// Tipos que describen el mismo hecho desde ángulos distintos (Incident de la cabecera)
const SAME_EVENT_TYPES = [
  new Set([0, 1, 4]), // atrapados, derrumbe, herido
  new Set([2, 3]), // incendio, fuga de gas
];

function compatibleTypes(a, b) {
  return a === b || SAME_EVENT_TYPES.some((g) => g.has(a) && g.has(b));
}

function distanceM(lat1, lon1, lat2, lon2) {
  const rad = Math.PI / 180;
  const dLat = (lat2 - lat1) * rad;
  const dLon = (lon2 - lon1) * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * 6371000 * Math.asin(Math.sqrt(h));
}

function samePlace(a, b, decode) {
  const la = a.location || {};
  const lb = b.location || {};
  if (la.geohash && lb.geohash && decode) {
    const pa = decode(la.geohash);
    const pb = decode(lb.geohash);
    return distanceM(pa.lat, pa.lon, pb.lat, pb.lon) <= DUPLICATE_RADIUS_M;
  }
  return la.barrio != null && la.barrio === lb.barrio;
}

/**
 * Para cada incidente, los otros que podrían ser el mismo hecho: mismo barrio (o GPS a
 * menos de 600 m), tipos compatibles y menos de 30 min entre sus primeros reportes.
 *
 * @param {Array<Object>} incidents - IncidentCard
 * @param {(geohash: string) => {lat: number, lon: number}} [decode]
 * @returns {Map<string, Array<{incident_id: string, title: string, deltaS: number}>>}
 */
export function possibleDuplicates(incidents = [], decode = null) {
  const out = new Map();
  const list = Array.isArray(incidents) ? incidents : [];
  for (let i = 0; i < list.length; i++) {
    for (let j = i + 1; j < list.length; j++) {
      const a = list[i];
      const b = list[j];
      const deltaS = Math.abs(Number(a.received_at) - Number(b.received_at));
      if (!(deltaS <= DUPLICATE_WINDOW_S)) continue;
      if (!compatibleTypes(a.incident, b.incident) || !samePlace(a, b, decode)) continue;
      for (const [x, y] of [[a, b], [b, a]]) {
        if (!out.has(x.incident_id)) out.set(x.incident_id, []);
        out.get(x.incident_id).push({ incident_id: y.incident_id, title: y.title, deltaS });
      }
    }
  }
  return out;
}

// --- Casos olvidados (mismos umbrales que el vigía, agents/vigia.py) ---------------------

export const FORGOTTEN_HIGH_S = 300; // crítica y alta: 5 min sin despacho
export const FORGOTTEN_OTHER_S = 900; // media y baja: 15 min

/**
 * Minutos sin despacho si el caso ya pasó el umbral del vigía; null si no. El vigía deja su
 * alerta en "por qué está aquí"; esto la pone a la vista en la tarjeta.
 *
 * @param {{ level: number, status: string, received_at: number }} inc
 * @param {number} [nowS] - segundos Unix
 * @returns {number | null}
 */
export function forgottenMinutes(inc, nowS = Date.now() / 1000) {
  if (!inc || !["pendiente", "sin_ficha", "conflicto"].includes(inc.status)) return null;
  const age = nowS - Number(inc.received_at);
  const limit = Number(inc.level) >= 3 ? FORGOTTEN_HIGH_S : FORGOTTEN_OTHER_S;
  return age >= limit ? Math.floor(age / 60) : null;
}
