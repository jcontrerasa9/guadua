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
