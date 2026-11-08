// Pruebas unitarias para el armado de reportes ciudadanos y geohash (N13).
// Se ejecutan con: node --test "tests/js/*.test.mjs"

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { decodeGeohash, encodeGeohash } from "../../citizen/geohash.js";
import { decodeHeader, Flag, Incident, MessageType } from "../../citizen/protocol/header.js";
import {
  buildReportBinary,
  buildReportHeaderFields,
  measureUtf8Bytes,
  truncateUtf8,
} from "../../citizen/report.js";

// --- Verificación de copia idéntica de barrios.json ---------------------------

test("citizen/barrios.json es idéntico byte por byte a data/barrios.json", () => {
  const citizenBarrios = readFileSync(new URL("../../citizen/barrios.json", import.meta.url), "utf8");
  const dataBarrios = readFileSync(new URL("../../data/barrios.json", import.meta.url), "utf8");

  assert.equal(citizenBarrios, dataBarrios);
  const parsed = JSON.parse(citizenBarrios);
  assert.ok(Array.isArray(parsed));
  assert.ok(parsed.length >= 6);
});

// --- Pruebas de geohash.js contra el fixture ----------------------------------

test("geohash.js encode(lat, lon) a 7 caracteres coincide con el fixture 'd2fkcdc'", () => {
  // Las coordenadas de "d2fkcdc" según el vector de decodificación
  const targetGeohash = "d2fkcdc";
  const decoded = decodeGeohash(targetGeohash);

  // Al codificar esas mismas coordenadas, debe producir exactamente "d2fkcdc"
  const encoded = encodeGeohash(decoded.lat, decoded.lon, 7);
  assert.equal(encoded, targetGeohash);

  // Probar con coordenadas aproximadas de El Cable, Manizales
  const encodedNear = encodeGeohash(5.069503, -75.517959, 7);
  assert.equal(encodedNear, targetGeohash);
});

// --- Pruebas del mensaje armado con decodeHeader() de header.js ---------------

test("el mensaje armado decodifica con decodeHeader() de header.js y devuelve los mismos campos (con GPS)", () => {
  const headerFields = buildReportHeaderFields({
    deviceId: 16909060, // 0x01020304
    seq: 1,
    deviceTime: 3600,
    incidentKey: "colapso", // Incident.COLAPSO (1)
    isDirectObservation: true,
    smellsGasOrWires: false,
    notBreathing: false,
    geohash: "d2fkcdc",
  });

  const { fullBytes, headerBytes } = buildReportBinary({
    headerFields,
    noteText: "Colapso en primer piso",
  });

  assert.equal(headerBytes.length, 21);
  assert.ok(fullBytes.length > 21);

  // Decodificar los primeros 21 bytes con la función oficial de header.js
  const decoded = decodeHeader(fullBytes);

  assert.equal(decoded.msg_type, MessageType.REPORTE);
  assert.equal(decoded.device_id, 16909060);
  assert.equal(decoded.seq, 1);
  assert.equal(decoded.device_time, 3600);
  assert.equal(decoded.incident, Incident.COLAPSO);
  assert.equal(decoded.geohash, "d2fkcdc");
  assert.equal(decoded.barrio, null);
  assert.ok(decoded.flags & Flag.GPS);
  assert.ok(decoded.flags & Flag.OBSERVACION_DIRECTA);
});

test("el mensaje armado decodifica con decodeHeader() de header.js y devuelve los mismos campos (sin GPS, con barrio)", () => {
  const headerFields = buildReportHeaderFields({
    deviceId: 42,
    seq: 2,
    deviceTime: 120,
    incidentKey: "fuga_gas", // Incident.FUGA_GAS (3)
    isDirectObservation: false, // Me contaron
    smellsGasOrWires: true, // Riesgo escena
    notBreathing: true,
    barrioCode: 17,
  });

  const { fullBytes } = buildReportBinary({
    headerFields,
    noteText: "Fuga fuerte en la esquina",
  });

  const decoded = decodeHeader(fullBytes);

  assert.equal(decoded.msg_type, MessageType.REPORTE);
  assert.equal(decoded.device_id, 42);
  assert.equal(decoded.seq, 2);
  assert.equal(decoded.incident, Incident.FUGA_GAS);
  assert.equal(decoded.barrio, 17);
  assert.equal(decoded.geohash, null);
  assert.equal(decoded.flags & Flag.GPS, 0);
  assert.equal(decoded.flags & Flag.OBSERVACION_DIRECTA, 0);
  assert.ok(decoded.flags & Flag.RIESGO_ESCENA);
  assert.ok(decoded.flags & Flag.NO_RESPIRA_O_SANGRADO);
});

// --- Pruebas de límite de texto de 100 bytes UTF-8 ----------------------------

test("el texto de más de 100 bytes se corta de forma segura", () => {
  // Cadena larga con tildes y caracteres multibyte
  const longText = "Esta es una nota sumamente extensa con información detallada sobre el incidente en la montaña con árboles caídos y postes desplomados en la vía principal de la ciudad de Manizales";
  assert.ok(measureUtf8Bytes(longText) > 100);

  const truncated = truncateUtf8(longText, 100);
  assert.ok(measureUtf8Bytes(truncated) <= 100);

  // buildReportBinary trunca automáticamente por defecto
  const headerFields = buildReportHeaderFields({
    deviceId: 1,
    seq: 1,
    incidentKey: "colapso",
    barrioCode: 1,
  });

  const { text, fullBytes } = buildReportBinary({
    headerFields,
    noteText: longText,
    strict: false,
  });

  assert.ok(measureUtf8Bytes(text) <= 100);
  assert.equal(fullBytes.length, 21 + measureUtf8Bytes(text));
});

test("el texto de más de 100 bytes se rechaza en modo estricto", () => {
  const longText = "A".repeat(101);
  const headerFields = buildReportHeaderFields({
    deviceId: 1,
    seq: 1,
    incidentKey: "colapso",
    barrioCode: 1,
  });

  assert.throws(() => {
    buildReportBinary({
      headerFields,
      noteText: longText,
      strict: true,
    });
  }, /excede el límite de 100 bytes/);
});

test("con batería crítica no se envía texto según el diseño", () => {
  const headerFields = buildReportHeaderFields({
    deviceId: 1,
    seq: 1,
    incidentKey: "colapso",
    barrioCode: 1,
    criticalBattery: true,
  });

  assert.ok(headerFields.flags & Flag.BATERIA_CRITICA);

  const { text, fullBytes } = buildReportBinary({
    headerFields,
    noteText: "Nota que debe descartarse por batería baja",
  });

  assert.equal(text, "");
  assert.equal(fullBytes.length, 21); // Solo la cabecera
});
