// Pruebas unitarias para el reporte "Estoy a salvo" en la PWA (D2a).
// Se ejecutan con: node --test "tests/js/*.test.mjs"

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  decodeHeader,
  EPOCH_UNIX,
  Flag,
  HEADER_SIZE,
  Incident,
  MessageType,
  People,
} from "../../citizen/protocol/header.js";
import { decodeText } from "../../citizen/protocol/text.js";
import {
  createReportQueue,
  MemoryReportStore,
  processQueue,
  ReportStatus,
} from "../../citizen/queue.js";
import {
  buildSafeBinary,
  buildSafeHeaderFields,
  getSafeDeviceTime,
  MAX_NAME_BYTES,
  SAFE_FLAGS,
  SAFE_INCIDENT,
  SAFE_PEOPLE,
} from "../../citizen/safe.js";

const DICT_BYTES = new Uint8Array(
  readFileSync(new URL("../../data/diccionario_manizales.txt", import.meta.url)),
);

// --- 1. Armado puro de la cabecera para "Estoy a salvo" -----------------------

test("buildSafeHeaderFields arma la cabecera con el contrato exacto de tipo 4", () => {
  const fields = buildSafeHeaderFields({
    deviceId: 0x12345678,
    seq: 42,
    deviceTime: 1000,
    barrioCode: 15,
  });

  assert.equal(fields.msg_type, MessageType.A_SALVO); // 4
  assert.equal(fields.device_id, 0x12345678);
  assert.equal(fields.seq, 42);
  assert.equal(fields.device_time, 1000);
  assert.equal(fields.incident, Incident.OTRO); // 6: no es siniestro
  assert.equal(fields.people, People.UNA); // 0: una persona
  assert.equal(fields.flags, Flag.OBSERVACION_DIRECTA); // 32: observación directa, GPS apagado
  assert.equal(fields.barrio, 15);
  assert.equal(fields.geohash, null);
});

test("buildSafeHeaderFields rechaza códigos de barrio inválidos", () => {
  assert.throws(
    () =>
      buildSafeHeaderFields({
        deviceId: 1,
        seq: 1,
        barrioCode: -1,
      }),
    /Código de barrio inválido/,
  );

  assert.throws(
    () =>
      buildSafeHeaderFields({
        deviceId: 1,
        seq: 1,
        barrioCode: "15",
      }),
    /Código de barrio inválido/,
  );

  assert.throws(
    () =>
      buildSafeHeaderFields({
        deviceId: 1,
        seq: 1,
        barrioCode: NaN,
      }),
    /Código de barrio inválido/,
  );
});

test("getSafeDeviceTime calcula el tiempo relativo a EPOCH_UNIX sin negativos", () => {
  const t = getSafeDeviceTime(EPOCH_UNIX + 500);
  assert.equal(t, 500);

  const past = getSafeDeviceTime(EPOCH_UNIX - 100);
  assert.equal(past, 0);
});

// --- 2. Tamaño real y cumplimiento del contrato de 21 B sin nombre -----------

test("buildSafeBinary sin nombre mide EXACTAMENTE 21 bytes y cumple el contrato", () => {
  const result = buildSafeBinary({
    deviceId: 0xaabbccdd,
    seq: 7,
    deviceTime: 120,
    barrioCode: 24,
  });

  // Tamaño estricto de 21 bytes
  assert.equal(result.fullBytes.length, HEADER_SIZE); // 21 bytes
  assert.equal(result.headerBytes.length, HEADER_SIZE);
  assert.equal(result.textBytes.length, 0);
  assert.equal(result.name, "");

  // NOTA CRÍTICA DE CONTRATO:
  // docs/plan.md y docs/diseno.md dicen "~14 B", pero el contrato header.py / header.js
  // exige exactamente HEADER_SIZE = 21 B mínimo. Cualquier mensaje menor a 21 B
  // es rechazado como HeaderError. Se mantiene el contrato estricto de 21 B.
  assert.ok(
    result.fullBytes.length === 21,
    "El tamaño real sin nombre es exactamente 21 bytes (contrato inmutable)",
  );

  // Decodificación con decodeHeader
  const decoded = decodeHeader(result.fullBytes);
  assert.equal(decoded.msg_type, MessageType.A_SALVO);
  assert.equal(decoded.device_id, 0xaabbccdd);
  assert.equal(decoded.seq, 7);
  assert.equal(decoded.device_time, 120);
  assert.equal(decoded.incident, Incident.OTRO);
  assert.equal(decoded.people, People.UNA);
  assert.equal(decoded.flags, Flag.OBSERVACION_DIRECTA);
  assert.equal(decoded.barrio, 24);
  assert.equal(decoded.geohash, null);
});

// --- 3. Binario con nombre opcional y compresión con diccionario -------------

test("buildSafeBinary con nombre opcional incluye texto decodificable", () => {
  const nombre = "Carlos Gómez";
  const result = buildSafeBinary({
    deviceId: 0x11223344,
    seq: 12,
    deviceTime: 300,
    barrioCode: 8,
    name: nombre,
    dictionary: DICT_BYTES,
  });

  // Tamaño: 21 B de cabecera + bytes de texto (1 B modo + payload)
  assert.ok(result.textBytes.length > 1);
  assert.equal(result.fullBytes.length, HEADER_SIZE + result.textBytes.length);
  assert.equal(result.name, nombre);

  // Cabecera decodifica correctamente
  const decodedHeader = decodeHeader(result.fullBytes.subarray(0, HEADER_SIZE));
  assert.equal(decodedHeader.msg_type, MessageType.A_SALVO);
  assert.equal(decodedHeader.barrio, 8);

  // Texto decodifica exactamente al nombre original
  const decodedName = decodeText(
    result.fullBytes.subarray(HEADER_SIZE),
    DICT_BYTES,
  );
  assert.equal(decodedName, nombre);
});

// --- 4. Truncamiento de nombre a 40 bytes UTF-8 ------------------------------

test("buildSafeBinary trunca nombres que excedan MAX_NAME_BYTES (40 B) sin romper UTF-8", () => {
  // String largo con tildes y caracteres multy-byte
  const nombreLargo = "María Cristina de los Ángeles Gómez Rodríguez";
  const result = buildSafeBinary({
    deviceId: 1,
    seq: 1,
    barrioCode: 5,
    name: nombreLargo,
    dictionary: DICT_BYTES,
  });

  const nameBytes = new TextEncoder().encode(result.name);
  assert.ok(nameBytes.length <= MAX_NAME_BYTES);
  assert.ok(result.name.length > 0);
  assert.ok(nombreLargo.startsWith(result.name));

  // No debe romper caracteres multy-byte (se puede decodificar y recodificar limpiamente)
  const roundtrip = new TextDecoder("utf-8", { fatal: true }).decode(nameBytes);
  assert.equal(roundtrip, result.name);
});

// --- 5. Reporte de tamaños reales -------------------------------------------

test("reporte de tamaños reales con y sin nombre", () => {
  const sinNombre = buildSafeBinary({
    deviceId: 1,
    seq: 1,
    barrioCode: 1,
  });
  assert.equal(sinNombre.fullBytes.length, 21, "Sin nombre: exactamente 21 B");

  const conNombreCorto = buildSafeBinary({
    deviceId: 1,
    seq: 1,
    barrioCode: 1,
    name: "Ana",
    dictionary: DICT_BYTES,
  });
  // 21 B cabecera + 1 B prefijo + texto (~25-30 B total)
  assert.ok(
    conNombreCorto.fullBytes.length >= 23 && conNombreCorto.fullBytes.length <= 35,
    `Nombre corto mide ${conNombreCorto.fullBytes.length} B`,
  );

  const conNombreMaximo = buildSafeBinary({
    deviceId: 1,
    seq: 1,
    barrioCode: 1,
    name: "1234567890123456789012345678901234567890", // 40 bytes
    dictionary: DICT_BYTES,
  });
  // Máximo teórico con texto plano: 21 B cabecera + 1 B prefijo + 40 B = 62 B
  assert.ok(
    conNombreMaximo.fullBytes.length <= 62,
    `Nombre máximo mide ${conNombreMaximo.fullBytes.length} B (<= 62 B)`,
  );
});

// --- 6. Integración con cola de N13 y pipeline Guardado -> Recibido ----------

test("integración con la cola de N13: encola Estoy a salvo y transiciona a Recibido", async () => {
  const store = new MemoryReportStore();
  let sentPayload = null;
  const statusTransitions = [];

  const mockFetch = async (url, opts) => {
    assert.equal(url, "/api/messages");
    assert.equal(opts.method, "POST");
    assert.equal(opts.headers["Content-Type"], "application/octet-stream");
    sentPayload = new Uint8Array(opts.body);
    return {
      status: 200,
      json: async () => ({ status: "recibido", duplicate: false }),
    };
  };

  const queue = createReportQueue({
    store,
    fetchFn: mockFetch,
    clockFn: () => 1000,
    onStatusChange: (item) => {
      statusTransitions.push(item.status);
    },
  });

  store.meta.set("device_id", 0x44332211);
  await store.saveLastBarrio(33);

  const lastBarrio = await store.getLastBarrio();
  assert.equal(lastBarrio, 33);

  const deviceId = await store.getDeviceId();
  const seq = await store.getNextSeq();

  const { headerFields, fullBytes } = buildSafeBinary({
    deviceId,
    seq,
    barrioCode: lastBarrio,
    name: "Pedro",
    dictionary: DICT_BYTES,
  });

  const enqueued = await queue.enqueue({
    headerFields,
    fullBytes,
    priority: 0,
  });

  assert.equal(enqueued.headerFields.msg_type, MessageType.A_SALVO);

  // El pipeline transiciona por GUARDADO -> ENVIANDO -> RECIBIDO
  assert.ok(statusTransitions.includes(ReportStatus.GUARDADO));
  assert.ok(statusTransitions.includes(ReportStatus.ENVIANDO));
  assert.ok(statusTransitions.includes(ReportStatus.RECIBIDO));

  assert.deepEqual(sentPayload, fullBytes);

  const storedReport = await store.get(enqueued.id);
  assert.equal(storedReport.status, ReportStatus.RECIBIDO);
});
