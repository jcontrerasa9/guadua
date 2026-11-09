// Pruebas unitarias para la lógica pura del tablero del comandante (N14).
// Se ejecutan con: node --test "tests/js/*.test.mjs"

import assert from "node:assert/strict";
import { test } from "node:test";

import { decodeGeohash } from "../../src/guadua/dashboard/geohash.js";
import {
  computeConfidence,
  computeRiskWarning,
  createInitialNodeState,
  formatDecisionRequest,
  formatTimeAgo,
  nodesStartingWith,
  partitionAndSortIncidents,
  updateNodeHealth,
} from "../../src/guadua/dashboard/logic.js";

// --- Pruebas de geohash a lat/lon con "d2fkcdc" -------------------------------

test("geohash a lat/lon con 'd2fkcdc' del fixture", () => {
  const result = decodeGeohash("d2fkcdc");
  assert.ok(result);
  assert.equal(typeof result.lat, "number");
  assert.equal(typeof result.lon, "number");

  // Coordenadas aproximadas de Manizales (El Cable)
  // Latitud ~ 5.0695, Longitud ~ -75.5180
  assert.ok(Math.abs(result.lat - 5.0695) < 0.005, `latitud inesperada: ${result.lat}`);
  assert.ok(Math.abs(result.lon - (-75.518)) < 0.005, `longitud inesperada: ${result.lon}`);
});

test("geohash rechaza cadenas inválidas o caracteres fuera de base32", () => {
  assert.throws(() => decodeGeohash(""), /geohash inválido/);
  assert.throws(() => decodeGeohash("d2fk!dc"), /carácter de geohash inválido/);
});

// --- Pruebas de ordenar y partir en lista corta / resto -----------------------

test("ordenar y partir en lista corta (4 y 3) / resto (2 y 1)", () => {
  const incidents = [
    { incident_id: "inc-1", level: 2, received_at: 100 },
    { incident_id: "inc-2", level: 4, received_at: 300 },
    { incident_id: "inc-3", level: 4, received_at: 200 }, // más antiguo que inc-2
    { incident_id: "inc-4", level: 3, received_at: 150 },
    { incident_id: "inc-5", level: 1, received_at: 50 },
    { incident_id: "inc-6", level: 3, received_at: 400 },
  ];

  const { shortList, otherList, allSorted } = partitionAndSortIncidents(incidents);

  // shortList solo debe tener niveles 4 y 3
  assert.equal(shortList.length, 4);
  assert.deepEqual(
    shortList.map((i) => i.incident_id),
    ["inc-3", "inc-2", "inc-4", "inc-6"], // 4 (viejo a nuevo), luego 3 (viejo a nuevo)
  );

  // otherList solo debe tener niveles 2 y 1
  assert.equal(otherList.length, 2);
  assert.deepEqual(
    otherList.map((i) => i.incident_id),
    ["inc-1", "inc-5"], // nivel 2, luego nivel 1
  );

  // allSorted contiene todos los 6 elementos ordenados
  assert.equal(allSorted.length, 6);
  assert.deepEqual(
    allSorted.map((i) => i.incident_id),
    ["inc-3", "inc-2", "inc-4", "inc-6", "inc-1", "inc-5"],
  );
});

// --- Pruebas de confianza y etiquetas -----------------------------------------

test("confianza y etiquetas (VERIFICAR YA, rumor)", () => {
  // Caso alta confianza sin etiquetas
  const c1 = computeConfidence(0.94, false, false);
  assert.equal(c1.level, "alta");
  assert.equal(c1.label, "Alta");
  assert.deepEqual(c1.tags, []);

  // Caso baja confianza con VERIFICAR YA y Posible rumor
  const c2 = computeConfidence(0.12, true, true);
  assert.equal(c2.level, "baja");
  assert.equal(c2.label, "Baja");
  assert.deepEqual(c2.tags, ["VERIFICAR YA", "Posible rumor"]);

  // Caso media confianza solo con rumor
  const c3 = computeConfidence(0.5, false, true);
  assert.equal(c3.level, "media");
  assert.deepEqual(c3.tags, ["Posible rumor"]);

  // Caso sin correlación (null)
  const c4 = computeConfidence(null, true, false);
  assert.equal(c4.level, "sin_dato");
  assert.equal(c4.label, "Sin correlación");
  assert.deepEqual(c4.tags, ["VERIFICAR YA"]);
});

test("formatTimeAgo calcula minutos transcurridos correctamente", () => {
  const now = 1000;
  assert.equal(formatTimeAgo(1000, now), "hace 0 min");
  assert.equal(formatTimeAgo(760, now), "hace 4 min"); // 240s = 4 min
  assert.equal(formatTimeAgo(now + 10, now), "hace 0 min");
});

// --- Pruebas de DecisionRequest -----------------------------------------------

test("armar el DecisionRequest (editar exige mensaje, aprobar no lo lleva)", () => {
  // Aprobar
  const reqAprobar = formatDecisionRequest({
    fichaId: "B:31",
    action: "aprobar",
    operator: "comandante-1",
  });
  assert.deepEqual(reqAprobar, {
    ficha_id: "B:31",
    action: "aprobar",
    operator: "comandante-1",
  });

  // Aprobar con edited_message debe fallar
  assert.throws(
    () =>
      formatDecisionRequest({
        fichaId: "B:31",
        action: "aprobar",
        operator: "comandante-1",
        editedMessage: "mensaje indebido",
      }),
    /no debe incluirse/,
  );

  // Editar con mensaje
  const reqEditar = formatDecisionRequest({
    fichaId: "B:31",
    action: "editar",
    operator: "comandante-1",
    editedMessage: "Colapso en El Cable, 3 atrapados. Esperar a HazMat.",
  });
  assert.deepEqual(reqEditar, {
    ficha_id: "B:31",
    action: "editar",
    edited_message: "Colapso en El Cable, 3 atrapados. Esperar a HazMat.",
    operator: "comandante-1",
  });

  // Editar sin mensaje debe fallar
  assert.throws(
    () =>
      formatDecisionRequest({
        fichaId: "B:31",
        action: "editar",
        operator: "comandante-1",
      }),
    /requiere un mensaje editado/,
  );

  // Replanear (rechazar)
  const reqRechazar = formatDecisionRequest({
    fichaId: "B:31",
    action: "rechazar",
    operator: "comandante-1",
  });
  assert.deepEqual(reqRechazar, {
    ficha_id: "B:31",
    action: "rechazar",
    operator: "comandante-1",
  });

  // Validación de operador y ficha vacíos
  assert.throws(
    () =>
      formatDecisionRequest({
        fichaId: "",
        action: "aprobar",
        operator: "comandante-1",
      }),
    /ficha_id es obligatorio/,
  );
  assert.throws(
    () =>
      formatDecisionRequest({
        fichaId: "B:31",
        action: "aprobar",
        operator: "   ",
      }),
    /operator es obligatorio/,
  );
});

// --- Pruebas de conmutación automática de nodo --------------------------------

test("elegir nodo: sigue en el actual si responde", () => {
  const initial = createInitialNodeState(["A", "B", "C"]);
  const s1 = updateNodeHealth(initial, { ok: true, node: "A" });

  assert.equal(s1.connectedNode, "A");
  assert.equal(s1.nodeName, "A");
  assert.equal(s1.statusText, "Conectado al nodo A");
  assert.equal(s1.consecutiveFailures, 0);

  // Segunda respuesta exitosa
  const s2 = updateNodeHealth(s1, { ok: true, node: "A" });
  assert.equal(s2.connectedNode, "A");
  assert.equal(s2.statusText, "Conectado al nodo A");
});

test("elegir nodo: pasa al siguiente tras 2 fallos y lo indica", () => {
  const initial = createInitialNodeState(["A", "B", "C"]);
  const stateA = updateNodeHealth(initial, { ok: true, node: "A" });

  // 1er fallo: todavía se queda en A
  const sFail1 = updateNodeHealth(stateA, { ok: false });
  assert.equal(sFail1.currentIndex, 0);
  assert.equal(sFail1.consecutiveFailures, 1);
  assert.equal(sFail1.connectedNode, "A");

  // 2do fallo consecutivo: conmuta a B y lo indica
  const sFail2 = updateNodeHealth(sFail1, { ok: false });
  assert.equal(sFail2.currentIndex, 1);
  assert.equal(sFail2.connectedNode, "B");
  assert.equal(sFail2.statusText, "Conectado al nodo B");
  assert.equal(sFail2.consecutiveFailures, 0);
});

test("elegir nodo: ninguno vivo si todos los nodos fallan consecutivamente", () => {
  const initial = createInitialNodeState(["A", "B"]);

  // Fallos para nodo A (2 veces)
  let state = updateNodeHealth(initial, { ok: false });
  state = updateNodeHealth(state, { ok: false });
  assert.equal(state.connectedNode, "B");
  assert.equal(state.statusText, "Conectado al nodo B");

  // Fallos para nodo B (2 veces)
  state = updateNodeHealth(state, { ok: false });
  state = updateNodeHealth(state, { ok: false });

  // Ahora ambos nodos han fallado 2 veces consecutivas
  assert.equal(state.connectedNode, null);
  assert.equal(state.statusText, "Ningún nodo disponible");
});

// --- Criterio exacto de docs/plan.md (N14) -------------------------------------

test("criterio de plan.md: al apagar el nodo conectado, cambia solo al siguiente y lo indica", () => {
  const nodes = ["A", "B", "C"];
  let state = createInitialNodeState(nodes);

  // Inicialmente conectado y respondiendo en el nodo A
  state = updateNodeHealth(state, { ok: true, node: "A" });
  assert.equal(state.connectedNode, "A");
  assert.equal(state.statusText, "Conectado al nodo A");

  // Se apaga el nodo A: primera consulta falla
  state = updateNodeHealth(state, { ok: false, error: "Connection refused" });
  assert.equal(state.connectedNode, "A", "Aún no debe cambiar con 1 solo fallo");

  // Segunda consulta falla: debe cambiar automáticamente al siguiente nodo (B)
  state = updateNodeHealth(state, { ok: false, error: "Connection refused" });
  assert.equal(state.connectedNode, "B", "Debe cambiar automáticamente al siguiente nodo");
  assert.equal(state.statusText, "Conectado al nodo B", "El texto debe indicar la conexión al nodo B");

  // Al conectarse B responde
  state = updateNodeHealth(state, { ok: true, node: "B" });
  assert.equal(state.connectedNode, "B");
  assert.equal(state.statusText, "Conectado al nodo B");
  assert.equal(state.consecutiveFailures, 0);
});

// --- Cada tablero empieza por el nodo que lo sirvió --------------------------

const NODES = ["http://localhost:8000", "http://localhost:8001", "http://localhost:8002"];

test("el tablero servido por C (8002) lee primero de C y deja A y B de respaldo", () => {
  assert.deepEqual(nodesStartingWith("http://localhost:8002", NODES), [
    "http://localhost:8002",
    "http://localhost:8000",
    "http://localhost:8001",
  ]);
  const state = createInitialNodeState(nodesStartingWith("http://localhost:8002", NODES));
  assert.equal(state.connectedNode, "http://localhost:8002");
});

test("el tablero de A sigue empezando por A, sin repetir nodos", () => {
  assert.deepEqual(nodesStartingWith("http://localhost:8000/", NODES), NODES);
});

test("un origen que no está en la lista (p. ej. el proxy) va primero", () => {
  const list = nodesStartingWith("http://localhost:8080", NODES);
  assert.equal(list[0], "http://localhost:8080");
  assert.equal(list.length, 4);
});

test("sin origen http (file://) la lista no cambia", () => {
  assert.deepEqual(nodesStartingWith("file://", NODES), NODES);
  assert.deepEqual(nodesStartingWith(undefined, NODES), NODES);
});

test("si cae el nodo propio, el tablero pasa al siguiente y lo dice", () => {
  let state = createInitialNodeState(nodesStartingWith("http://localhost:8002", NODES));
  state = updateNodeHealth(state, { ok: false });
  state = updateNodeHealth(state, { ok: false });
  assert.equal(state.connectedNode, "http://localhost:8000");
  state = updateNodeHealth(state, { ok: true, node: "A" });
  assert.equal(state.statusText, "Conectado al nodo A");
});

// --- Pruebas de la franja de advertencia de riesgo (D3c) -----------------------

test("computeRiskWarning: advertencia presente devuelve show=true, icono, titulo y texto recortado", () => {
  const warningText = "Fuga de gas y cables caídos reportados. No ingresar sin HazMat.";
  const res1 = computeRiskWarning(warningText);

  assert.equal(res1.show, true);
  assert.equal(res1.icon, "⚠️");
  assert.equal(res1.title, "RIESGO PARA RESCATISTAS:");
  assert.equal(res1.text, warningText);

  // Funciona pasando directamente el objeto incidente con espacios recortados
  const res2 = computeRiskWarning({
    incident_id: "inc-1",
    risk_warning: "   Posible gas en la zona.   ",
  });

  assert.equal(res2.show, true);
  assert.equal(res2.icon, "⚠️");
  assert.equal(res2.title, "RIESGO PARA RESCATISTAS:");
  assert.equal(res2.text, "Posible gas en la zona.");
});

test("computeRiskWarning: cuando risk_warning es null, undefined, vacio o no-string devuelve show=false", () => {
  // null y undefined
  assert.deepEqual(computeRiskWarning(null), {
    show: false,
    icon: "",
    title: "",
    text: null,
  });
  assert.deepEqual(computeRiskWarning(undefined), {
    show: false,
    icon: "",
    title: "",
    text: null,
  });

  // Cadenas vacías o con solo espacios
  assert.equal(computeRiskWarning("").show, false);
  assert.equal(computeRiskWarning("   \n\t  ").show, false);
  assert.equal(computeRiskWarning("").text, null);

  // Objeto incidente sin riesgo o con risk_warning null
  assert.equal(computeRiskWarning({ incident_id: "inc-2", risk_warning: null }).show, false);
  assert.equal(computeRiskWarning({ incident_id: "inc-3" }).show, false);

  // Tipos no string
  assert.equal(computeRiskWarning(123).show, false);
  assert.equal(computeRiskWarning(true).show, false);
  assert.equal(computeRiskWarning(false).show, false);
});

test("invariante: el riesgo no cambia el orden de prioridad ni la urgencia en partitionAndSortIncidents", () => {
  // Inc-1: nivel 2 con riesgo severo
  // Inc-2: nivel 4 sin riesgo
  // Inc-3: nivel 3 sin riesgo
  // Inc-4: nivel 3 con riesgo (recibido más tarde que inc-3)
  const incidents = [
    {
      incident_id: "inc-1",
      level: 2,
      received_at: 100,
      risk_warning: "Peligro inminente: gas y cables de alta tensión",
    },
    {
      incident_id: "inc-2",
      level: 4,
      received_at: 200,
      risk_warning: null,
    },
    {
      incident_id: "inc-3",
      level: 3,
      received_at: 150,
      risk_warning: null,
    },
    {
      incident_id: "inc-4",
      level: 3,
      received_at: 250,
      risk_warning: "Fuga de gas menor",
    },
  ];

  const { shortList, otherList, allSorted } = partitionAndSortIncidents(incidents);

  // shortList solo tiene niveles 4 y 3
  assert.equal(shortList.length, 3);
  // El nivel 4 va primero, independientemente de que no tenga riesgo
  assert.equal(shortList[0].incident_id, "inc-2");
  // Los de nivel 3 se ordenan por received_at (más antiguo primero), sin importar risk_warning
  assert.equal(shortList[1].incident_id, "inc-3");
  assert.equal(shortList[2].incident_id, "inc-4");

  // El incidente de nivel 2 queda estrictamente en otherList pese a tener riesgo
  assert.equal(otherList.length, 1);
  assert.equal(otherList[0].incident_id, "inc-1");

  // allSorted respeta orden: nivel 4 -> nivel 3 -> nivel 2
  assert.deepEqual(
    allSorted.map((i) => i.incident_id),
    ["inc-2", "inc-3", "inc-4", "inc-1"],
  );
});
