// Pruebas unitarias para el armado de reportes ciudadanos y geohash (N13).
// Se ejecutan con: node --test "tests/js/*.test.mjs"

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { decodeGeohash, encodeGeohash } from "../../citizen/geohash.js";
import { decodeHeader, Flag, Incident, MessageType } from "../../citizen/protocol/header.js";
import { decodeText, encodeText } from "../../citizen/protocol/text.js";
import {
  buildReportBinary,
  buildReportHeaderFields,
  encodedNoteBytes,
  fitNote,
  MAX_NOTE_BYTES,
  measureUtf8Bytes,
  truncateUtf8,
} from "../../citizen/report.js";

const DICT_BYTES = new Uint8Array(
  readFileSync(new URL("../../citizen/diccionario_manizales.txt", import.meta.url)),
);
const TEXT_VECTORS = JSON.parse(
  readFileSync(new URL("../fixtures/text_vectors.json", import.meta.url), "utf8"),
);
const toHex = (bytes) => Buffer.from(bytes).toString("hex");

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
    dictionary: DICT_BYTES,
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
    dictionary: DICT_BYTES,
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

// --- Nota codificada con protocol/text.js (prefijo + comprimido, máx. 100 B) -----

test("citizen/diccionario_manizales.txt es idéntico byte por byte a data/", () => {
  const data = readFileSync(new URL("../../data/diccionario_manizales.txt", import.meta.url));
  assert.ok(Buffer.from(DICT_BYTES).equals(data));
});

test("la nota del reporte son los mismos bytes que produce Python (text_vectors.json)", () => {
  const headerFields = buildReportHeaderFields({ deviceId: 1, seq: 1, incidentKey: "colapso", barrioCode: 1 });
  for (const v of TEXT_VECTORS.valid.filter((c) => c.text && c.text.length <= 100)) {
    const { fullBytes, textBytes } = buildReportBinary({ headerFields, noteText: v.text, dictionary: DICT_BYTES });
    if (v.text.trim() !== v.text) continue; // la PWA recorta espacios de los extremos
    assert.equal(toHex(textBytes), v.hex, v.name);
    assert.equal(decodeText(fullBytes.subarray(21), DICT_BYTES), v.text);
  }
});

test("la nota viaja comprimida: ocupa menos que el texto plano", () => {
  const headerFields = buildReportHeaderFields({ deviceId: 1, seq: 1, incidentKey: "colapso", barrioCode: 1 });
  const note = "colapso casa 3 atrapados huele a gas";
  const { textBytes, fullBytes } = buildReportBinary({ headerFields, noteText: note, dictionary: DICT_BYTES });
  assert.ok(textBytes.length < measureUtf8Bytes(note));
  assert.equal(fullBytes.length, 21 + textBytes.length);
  assert.equal(textBytes[0] >> 4, 1); // prefijo: versión 1 del diccionario
});

test("la nota larga se recorta para que ya codificada quepa en 100 bytes", () => {
  const longText = "Esta es una nota sumamente extensa con información detallada sobre el incidente en la montaña con árboles caídos y postes desplomados en la vía principal de la ciudad de Manizales, con humo y gente gritando 🆘🆘 por todas partes, xq7#Zk!9wQ zr8@pL";
  const headerFields = buildReportHeaderFields({ deviceId: 1, seq: 1, incidentKey: "colapso", barrioCode: 1 });
  const { text, textBytes, fullBytes } = buildReportBinary({ headerFields, noteText: longText, dictionary: DICT_BYTES });

  assert.ok(textBytes.length <= MAX_NOTE_BYTES);
  assert.ok(text.length > 0 && longText.startsWith(text));
  assert.equal(fullBytes.length, 21 + textBytes.length);
  assert.equal(decodeText(textBytes, DICT_BYTES), text);
});

test("fitNote nunca pasa de 100 B ni parte una tilde o un emoji", () => {
  const samples = ["ñáéíóú".repeat(40), "🆘🚑🔥".repeat(60), "x#Q9!k@z".repeat(30), "atrapados ".repeat(80)];
  for (const sample of samples) {
    const fitted = fitNote(sample, DICT_BYTES);
    assert.ok(encodedNoteBytes(fitted, DICT_BYTES) <= MAX_NOTE_BYTES);
    assert.ok(sample.startsWith(fitted));
    assert.equal(Array.from(fitted).join(""), fitted);
    assert.ok(!fitted.includes("\uFFFD"));
    // con una letra más ya no cabría
    const next = Array.from(sample).slice(0, Array.from(fitted).length + 1).join("");
    assert.ok(next === fitted || encodedNoteBytes(next, DICT_BYTES) === null);
  }
  assert.equal(fitNote("", DICT_BYTES), "");
  assert.equal(fitNote("hola", null), "");
});

test("la nota que no cabe se rechaza en modo estricto", () => {
  // 120 B pseudoaleatorios (sin repeticiones): no comprimen
  let seed = 7;
  const incompressible = Array.from({ length: 120 }, () => {
    seed = (seed * 1103515245 + 12345) % 2147483648;
    return String.fromCharCode(33 + (seed % 94));
  }).join("");
  const headerFields = buildReportHeaderFields({ deviceId: 1, seq: 1, incidentKey: "colapso", barrioCode: 1 });
  assert.throws(
    () => buildReportBinary({ headerFields, noteText: incompressible, strict: true, dictionary: DICT_BYTES }),
    /excede el límite de 100 bytes/,
  );
});

test("sin diccionario el reporte sale igual, pero sin la nota", () => {
  const headerFields = buildReportHeaderFields({ deviceId: 1, seq: 1, incidentKey: "colapso", barrioCode: 1 });
  const { fullBytes, text, noteDropped } = buildReportBinary({ headerFields, noteText: "hay atrapados" });
  assert.equal(fullBytes.length, 21);
  assert.equal(text, "");
  assert.equal(noteDropped, true);
});

test("truncateUtf8 sigue cortando sin romper caracteres", () => {
  const truncated = truncateUtf8("ñ".repeat(80), 101);
  assert.ok(measureUtf8Bytes(truncated) <= 101);
  assert.ok(!truncated.includes("\uFFFD"));
  assert.equal(encodeText("", DICT_BYTES).length, 0);
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
    dictionary: DICT_BYTES,
  });

  assert.equal(text, "");
  assert.equal(fullBytes.length, 21); // Solo la cabecera
});
