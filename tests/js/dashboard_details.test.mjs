// Pruebas unitarias para la persistencia del desplegable 'Por qué está aquí' en el tablero (N14).
// Se ejecutan con: node --test "tests/js/*.test.mjs"

import assert from "node:assert/strict";
import { test, beforeEach } from "node:test";

import {
  buildIncidentDetailInnerHtml,
  renderIncidentCardHtml,
  state,
} from "../../src/guadua/dashboard/app.js";

const SAMPLE_INCIDENT = {
  incident_id: "inc-colapso-1",
  level: 4,
  received_at: 1714000000,
  title: "Colapso estructural con personas atrapadas",
  status: "pendiente",
  ficha_id: "ficha-101",
  confidence: 0.9,
  verify_now: false,
  rumor: false,
  location: { geohash: "d2fkcdc" },
};

const SAMPLE_DETAIL = {
  incident_id: "inc-colapso-1",
  probabilities: {
    critica: 0.81,
    alta: 0.15,
    media: 0.03,
    baja: 0.01,
  },
  rules: ["atrapados → crítica"],
  sources: ["ciudadano directo", "radio directo"],
  contradictions: [],
  citations: [
    {
      document: "EMRE Manizales",
      section: "4.2 Colapso estructural",
      is_example: true,
    },
  ],
  steps: [
    { agent: "triaje", detail: "reglas: piso crítica por atrapados" },
    { agent: "coordinacion", detail: "matriz EMRE: Bomberos; riesgo de gas en escena" },
  ],
  report_ids: ["rep-1", "rep-2", "rep-3", "rep-4"],
};

beforeEach(() => {
  state.openDetails.clear();
  state.detailCache.clear();
});

test("renderIncidentCardHtml genera details cerrado cuando no está en openDetails", () => {
  const html = renderIncidentCardHtml(SAMPLE_INCIDENT);

  // No debe tener el atributo open
  assert.ok(html.includes('<details class="details-toggle" data-details-id="inc-colapso-1" >'));
  assert.ok(!html.includes('data-details-id="inc-colapso-1" open'));
  assert.ok(html.includes("Cargando trazabilidad..."));
});

test("renderIncidentCardHtml genera details con atributo open cuando el usuario lo abrió", () => {
  state.openDetails.add("inc-colapso-1");

  const html = renderIncidentCardHtml(SAMPLE_INCIDENT);

  // Debe tener el atributo open
  assert.ok(html.includes('<details class="details-toggle" data-details-id="inc-colapso-1" open>'));
});

test("renderIncidentCardHtml incrusta el detalle en caché sin parpadeos si ya fue consultado", () => {
  state.openDetails.add("inc-colapso-1");
  state.detailCache.set("inc-colapso-1", SAMPLE_DETAIL);

  const html = renderIncidentCardHtml(SAMPLE_INCIDENT);

  // Mantiene open
  assert.ok(html.includes('data-details-id="inc-colapso-1" open'));
  // No muestra el placeholder porque el detalle ya está en caché
  assert.ok(!html.includes("Cargando trazabilidad..."));
  // Muestra las secciones de trazabilidad
  assert.ok(html.includes("atrapados → crítica"));
  assert.ok(html.includes("81%"));
  assert.ok(html.includes("EMRE Manizales"));
  assert.ok(html.includes("Reportes agrupados (4)"));
});

test("buildIncidentDetailInnerHtml formatea correctamente todas las secciones de trazabilidad", () => {
  const html = buildIncidentDetailInnerHtml(SAMPLE_DETAIL);

  // Reglas
  assert.ok(html.includes("Reglas de urgencia aplicadas"));
  assert.ok(html.includes("<li>atrapados → crítica</li>"));

  // Probabilidades
  assert.ok(html.includes("Probabilidades del clasificador"));
  assert.ok(html.includes("critica:</strong> 81%"));
  assert.ok(html.includes("alta:</strong> 15%"));

  // Fuentes
  assert.ok(html.includes("Fuentes de los reportes"));
  assert.ok(html.includes("<li>ciudadano directo</li>"));
  assert.ok(html.includes("<li>radio directo</li>"));

  // Contradicciones vacías
  assert.ok(html.includes("Sin contradicciones reportadas."));

  // Citas EMRE
  assert.ok(html.includes("Citas normativas (EMRE)"));
  assert.ok(html.includes("EMRE Manizales</strong> - 4.2 Colapso estructural (ejemplo)"));

  // Trazabilidad de agentes
  assert.ok(html.includes("Trazabilidad de agentes"));
  assert.ok(html.includes("<li><strong>triaje:</strong> reglas: piso crítica por atrapados"));
  assert.ok(html.includes("<li><strong>coordinacion:</strong> matriz EMRE: Bomberos"));

  // Reportes agrupados
  assert.ok(html.includes("Reportes agrupados (4)"));
  assert.ok(html.includes("rep-1, rep-2, rep-3, rep-4"));
});

test("buildIncidentDetailInnerHtml muestra contradicciones con alerta si existen", () => {
  const detailConContradiccion = {
    ...SAMPLE_DETAIL,
    contradictions: ["Reporte A dice 0 atrapados pero Reporte B reporta 3 personas bajo escombros"],
  };

  const html = buildIncidentDetailInnerHtml(detailConContradiccion);
  assert.ok(html.includes("<li>Reporte A dice 0 atrapados"));
  assert.ok(!html.includes("Sin contradicciones reportadas."));
});

test("simulación de re-renderizado periódico (polling): el desplegable no se cierra", () => {
  // 1. El usuario abre el desplegable del incidente
  state.openDetails.add("inc-colapso-1");
  state.detailCache.set("inc-colapso-1", SAMPLE_DETAIL);

  // 2. Ocurren múltiples ciclos de polling (renderizados sucesivos)
  for (let cycle = 1; cycle <= 5; cycle++) {
    const html = renderIncidentCardHtml(SAMPLE_INCIDENT);
    // El desplegable DEBE seguir teniendo el atributo open
    assert.ok(
      html.includes('data-details-id="inc-colapso-1" open'),
      `El desplegable se cerró en el ciclo de polling #${cycle}`,
    );
    // El contenido DEBE seguir mostrando el detalle sin cerrarse
    assert.ok(html.includes("atrapados → crítica"));
  }

  // 3. Cuando el usuario lo cierra voluntariamente
  state.openDetails.delete("inc-colapso-1");
  const htmlCerrado = renderIncidentCardHtml(SAMPLE_INCIDENT);
  assert.ok(!htmlCerrado.includes('data-details-id="inc-colapso-1" open'));
});

test("la tarjeta no usa emojis: urgencia, acciones y riesgo van en texto e ícono SVG", () => {
  const html = renderIncidentCardHtml({
    ...SAMPLE_INCIDENT,
    risk_warning: "Fuga de gas reportada.",
  });
  assert.ok(html.includes(">Aprobar</button>"));
  assert.ok(html.includes("Riesgo para rescatistas"));
  assert.ok(html.includes('<svg class="risk-icon"'));
  assert.doesNotMatch(html, /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/u);
});

test("la línea de metadatos dice el tipo de incidente, porque el título sale del texto", () => {
  const html = renderIncidentCardHtml({ ...SAMPLE_INCIDENT, incident: 0, title: "Gato atrapado en un arbol" });
  assert.ok(html.includes('<div class="card-meta">Atrapados<span class="sep">·</span>'));
  assert.ok(html.includes(">Gato atrapado en un arbol</h3>"));
});
