// Pruebas unitarias para la cola de reportes y reintentos (N13).
// Se ejecutan con: node --test "tests/js/*.test.mjs"

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  calculateBackoffDelay,
  createReportQueue,
  MemoryReportStore,
  processQueue,
  ReportStatus,
} from "../../citizen/queue.js";

// --- Pruebas de retroceso exponencial con tope -------------------------------

test("los reintentos crecen con tope y retroceso exponencial", () => {
  const cfg = { baseDelayMs: 1000, maxDelayMs: 30000, jitter: 0 };

  assert.equal(calculateBackoffDelay(0, cfg), 1000);
  assert.equal(calculateBackoffDelay(1, cfg), 2000);
  assert.equal(calculateBackoffDelay(2, cfg), 4000);
  assert.equal(calculateBackoffDelay(3, cfg), 8000);
  assert.equal(calculateBackoffDelay(4, cfg), 16000);
  assert.equal(calculateBackoffDelay(5, cfg), 30000); // Tope de 30s
  assert.equal(calculateBackoffDelay(10, cfg), 30000); // Permanece en el tope
});

// --- Pruebas de reenvío y duplicate: true -------------------------------------

test("un reporte Recibido no se reenvía; duplicate: true cuenta como Recibido", async () => {
  const store = new MemoryReportStore();
  let fetchCallCount = 0;

  const mockFetch = async () => {
    fetchCallCount++;
    return {
      status: 200,
      json: async () => ({
        report_id: "01020304-1",
        status: "recibido",
        duplicate: true, // El nodo ya lo tenía
      }),
    };
  };

  // 1. Guardar reporte inicial
  await store.put({
    id: "01020304-1",
    deviceId: 0x01020304,
    seq: 1,
    status: ReportStatus.GUARDADO,
    rawBytes: new Uint8Array(21),
    attempts: 0,
    nextRetry: 0,
  });

  // 2. Procesar cola: debe llamar al fetch y marcarlo como Recibido
  await processQueue({
    store,
    fetchFn: mockFetch,
    clockFn: () => 1000,
  });

  assert.equal(fetchCallCount, 1);
  const updated = await store.get("01020304-1");
  assert.equal(updated.status, ReportStatus.RECIBIDO);
  assert.equal(updated.ack.duplicate, true);

  // 3. Volver a procesar la cola: NO debe llamar a fetchFn porque ya está Recibido
  await processQueue({
    store,
    fetchFn: mockFetch,
    clockFn: () => 2000,
  });

  assert.equal(fetchCallCount, 1, "No debe reenviar reportes con estado Recibido");
});

// --- Criterio exacto de docs/plan.md (N13) -------------------------------------

test("criterio de plan.md: con un fetch que falla y luego responde, un reporte guardado sin red se envía solo al volver la conexión y termina en Recibido", async () => {
  const store = new MemoryReportStore();
  let networkOnline = false;
  let fetchAttempts = 0;
  let simulatedClock = 1000;

  // Mock de fetch que falla si networkOnline es false
  const dynamicFetch = async (url, opts) => {
    fetchAttempts++;
    if (!networkOnline) {
      throw new Error("Sin conexión de red (offline)");
    }
    return {
      status: 200,
      json: async () => ({
        report_id: "a1b2c3d4-1",
        status: "recibido",
        duplicate: false,
      }),
    };
  };

  const statusTransitions = [];
  const queue = createReportQueue({
    store,
    fetchFn: dynamicFetch,
    clockFn: () => simulatedClock,
    onStatusChange: (item) => {
      statusTransitions.push(item.status);
    },
  });

  // 1. Se crea y encola un reporte cuando NO hay red
  const item = await queue.enqueue({
    headerFields: { device_id: 0xa1b2c3d4, seq: 1 },
    fullBytes: new Uint8Array(21),
  });

  assert.equal(item.status, ReportStatus.GUARDADO);
  assert.equal(fetchAttempts, 1); // Intentó enviar inmediatamente y falló por falta de red

  let current = await store.get(item.id);
  assert.equal(current.status, ReportStatus.GUARDADO);
  assert.equal(current.attempts, 1);
  assert.ok(current.nextRetry > simulatedClock);

  // 2. Transcurre el tiempo de retroceso exponencial y vuelve la conexión
  simulatedClock += 5000;
  networkOnline = true;

  // 3. Al volver la conexión (evento online o tick de la cola), se dispara el reintento automático
  await queue.triggerProcess();

  // 4. El reporte se envía exitosamente y termina en Recibido
  assert.equal(fetchAttempts, 2);
  current = await store.get(item.id);
  assert.equal(current.status, ReportStatus.RECIBIDO);
  assert.ok(current.ack);
  assert.equal(current.ack.status, "recibido");

  // Verificar la secuencia de transiciones de estado:
  // Guardado (creado) -> Enviando (1er intento) -> Guardado (fallo) -> Enviando (2do intento) -> Recibido (éxito)
  assert.ok(statusTransitions.includes(ReportStatus.GUARDADO));
  assert.ok(statusTransitions.includes(ReportStatus.ENVIANDO));
  assert.equal(statusTransitions[statusTransitions.length - 1], ReportStatus.RECIBIDO);
});
