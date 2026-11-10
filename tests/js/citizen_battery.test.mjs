import test from "node:test";
import assert from "node:assert/strict";

import {
  CRITICAL_BATTERY_THRESHOLD,
  computeBatteryStatus,
  parseSimulatedBattery,
  setupBatteryMonitor,
} from "../../citizen/battery.js";
import { Flag } from "../../citizen/protocol/header.js";
import {
  buildReportHeaderFields,
  buildReportBinary,
} from "../../citizen/report.js";

test("CRITICAL_BATTERY_THRESHOLD está fijado en 15%", () => {
  assert.equal(CRITICAL_BATTERY_THRESHOLD, 15);
});

test("computeBatteryStatus con nivel nulo o no disponible devuelve estado neutro", () => {
  const s1 = computeBatteryStatus({ level: null });
  assert.equal(s1.levelPercent, null);
  assert.equal(s1.isCritical, false);
  assert.equal(s1.isCharging, false);
  assert.equal(s1.icon, "🔋");
  assert.equal(s1.text, "🔋 --");

  const s2 = computeBatteryStatus({});
  assert.equal(s2.levelPercent, null);
  assert.equal(s2.text, "🔋 --");

  const s3 = computeBatteryStatus({ level: "no-un-numero" });
  assert.equal(s3.levelPercent, null);
  assert.equal(s3.text, "🔋 --");
});

test("computeBatteryStatus detecta batería normal y convierte decimal o porcentaje", () => {
  // Decimal 0.0 - 1.0
  const s1 = computeBatteryStatus({ level: 0.85, charging: false });
  assert.equal(s1.levelPercent, 85);
  assert.equal(s1.isCritical, false);
  assert.equal(s1.isCharging, false);
  assert.equal(s1.text, "🔋 85%");

  // Porcentaje 0 - 100
  const s2 = computeBatteryStatus({ level: 60, charging: false });
  assert.equal(s2.levelPercent, 60);
  assert.equal(s2.isCritical, false);
  assert.equal(s2.text, "🔋 60%");

  // 100%
  const s3 = computeBatteryStatus({ level: 1.0, charging: false });
  assert.equal(s3.levelPercent, 100);
  assert.equal(s3.isCritical, false);
  assert.equal(s3.text, "🔋 100%");
});

test("computeBatteryStatus clasifica batería crítica en <= 15% cuando no está cargando", () => {
  // En el umbral exacto (15%)
  const s15 = computeBatteryStatus({ level: 0.15, charging: false });
  assert.equal(s15.levelPercent, 15);
  assert.equal(s15.isCritical, true);
  assert.equal(s15.icon, "🪫");
  assert.equal(s15.text, "🪫 15%");

  // Por debajo del umbral (5%)
  const s5 = computeBatteryStatus({ level: 0.05, charging: false });
  assert.equal(s5.levelPercent, 5);
  assert.equal(s5.isCritical, true);
  assert.equal(s5.icon, "🪫");
  assert.equal(s5.text, "🪫 5%");

  // Justo por encima del umbral (16%)
  const s16 = computeBatteryStatus({ level: 0.16, charging: false });
  assert.equal(s16.levelPercent, 16);
  assert.equal(s16.isCritical, false);
  assert.equal(s16.icon, "🔋");
  assert.equal(s16.text, "🔋 16%");
});

test("computeBatteryStatus no marca crítica si el dispositivo está cargando", () => {
  const s = computeBatteryStatus({ level: 0.08, charging: true });
  assert.equal(s.levelPercent, 8);
  assert.equal(s.isCritical, false);
  assert.equal(s.isCharging, true);
  assert.equal(s.icon, "⚡");
  assert.equal(s.text, "⚡ 8%");
});

test("parseSimulatedBattery extrae parámetros de URL para pruebas y jurado", () => {
  // Parámetro ?battery=10
  assert.deepEqual(parseSimulatedBattery("?battery=10"), {
    level: 10,
    charging: false,
  });

  // Parámetro ?bateria=12&charging=1
  assert.deepEqual(parseSimulatedBattery("?bateria=12&charging=1"), {
    level: 12,
    charging: true,
  });

  // Sin parámetros o vacíos
  assert.equal(parseSimulatedBattery(""), null);
  assert.equal(parseSimulatedBattery("?otro=123"), null);
});

test("setupBatteryMonitor prioriza simulación de URL", () => {
  let emitted = null;
  setupBatteryMonitor({
    search: "?battery=14",
    onBatteryChange: (status) => {
      emitted = status;
    },
    getBatteryFn: async () => ({ level: 0.99, charging: false }),
  });

  assert.ok(emitted);
  assert.equal(emitted.levelPercent, 14);
  assert.equal(emitted.isCritical, true);
  assert.equal(emitted.text, "🪫 14%");
});

test("setupBatteryMonitor maneja navegadores sin API (Safari, Firefox) reportando nulo", () => {
  let emitted = null;
  setupBatteryMonitor({
    search: "",
    onBatteryChange: (status) => {
      emitted = status;
    },
    getBatteryFn: null,
  });

  assert.ok(emitted);
  assert.equal(emitted.levelPercent, null);
  assert.equal(emitted.isCritical, false);
  assert.equal(emitted.text, "🔋 --");
});

test("setupBatteryMonitor se suscribe a cambios de nivel y de carga con la API nativa", async () => {
  const listeners = {};
  const mockBattery = {
    level: 0.5,
    charging: false,
    addEventListener: (event, fn) => {
      listeners[event] = fn;
    },
    removeEventListener: (event) => {
      delete listeners[event];
    },
  };

  const states = [];
  const cleanup = setupBatteryMonitor({
    onBatteryChange: (status) => states.push(status),
    getBatteryFn: async () => mockBattery,
  });

  // Esperar a que la promesa resuelva
  await new Promise((resolve) => setTimeout(resolve, 10));

  assert.equal(states.length, 1);
  assert.equal(states[0].levelPercent, 50);
  assert.equal(states[0].isCritical, false);

  // Simular evento levelchange a 10%
  mockBattery.level = 0.10;
  listeners["levelchange"]();

  assert.equal(states.length, 2);
  assert.equal(states[1].levelPercent, 10);
  assert.equal(states[1].isCritical, true);
  assert.equal(states[1].text, "🪫 10%");

  // Simular conexión a cargador
  mockBattery.charging = true;
  listeners["chargingchange"]();

  assert.equal(states.length, 3);
  assert.equal(states[2].levelPercent, 10);
  assert.equal(states[2].isCritical, false);
  assert.equal(states[2].isCharging, true);
  assert.equal(states[2].text, "⚡ 10%");

  // Cleanup desuscribe
  cleanup();
  assert.equal(Object.keys(listeners).length, 0);
});

test("integración con reporte: batería crítica envía solo 21 B (omite nota)", () => {
  const headerFields = buildReportHeaderFields({
    deviceId: 42,
    seq: 1,
    incidentKey: "fuga_gas",
    barrioCode: 5,
    criticalBattery: true,
  });

  // Flag.BATERIA_CRITICA activo
  assert.ok(headerFields.flags & Flag.BATERIA_CRITICA);

  const { text, fullBytes } = buildReportBinary({
    headerFields,
    noteText: "Esta nota no debe viajar para ahorrar batería",
    dictionary: new Uint8Array([1, 2, 3]),
  });

  // Texto vacío y exactamente 21 bytes
  assert.equal(text, "");
  assert.equal(fullBytes.length, 21);
});
