// Pruebas unitarias para el códec de texto en JS (N16).
// Se ejecutan con: node --test "tests/js/*.test.mjs"

import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { Deflate } from "../../citizen/vendor/pako/pako.esm.mjs";
import {
  decodeText,
  DICT_VERSION,
  encodeText,
  MAX_INFLATED_BYTES,
  MAX_TEXT_BYTES,
  TextError,
  TextMode,
} from "../../citizen/protocol/text.js";

const FIXTURES_DIR = new URL("../fixtures/", import.meta.url);
const VECTORS = JSON.parse(
  readFileSync(new URL("text_vectors.json", FIXTURES_DIR), "utf8"),
);
const DICT_BYTES = new Uint8Array(
  readFileSync(new URL("../../data/diccionario_manizales.txt", import.meta.url)),
);

const toHex = (bytes) => Buffer.from(bytes).toString("hex");
const fromHex = (hex) => new Uint8Array(Buffer.from(hex, "hex"));

// --- Verificación del diccionario --------------------------------------------

test("el SHA-256 de data/diccionario_manizales.txt coincide con el del vector", () => {
  const hash = createHash("sha256").update(DICT_BYTES).digest("hex");
  assert.equal(hash, VECTORS.dict_sha256);
  assert.equal(DICT_VERSION, VECTORS.dict_version);
  assert.equal(MAX_TEXT_BYTES, VECTORS.max_text_bytes);
});

// --- Vectores válidos: decodificar -------------------------------------------

for (const c of VECTORS.valid) {
  test(`decodificar vector '${c.name}': los bytes de Python dan exactamente el texto original`, () => {
    const raw = fromHex(c.hex);
    const decoded = decodeText(raw, DICT_BYTES);
    assert.equal(decoded, c.text);
  });
}

// --- Vectores válidos: codificar ---------------------------------------------

for (const c of VECTORS.valid) {
  test(`codificar vector '${c.name}': JS produce exactamente los mismos bytes que el vector`, () => {
    const encoded = encodeText(c.text, DICT_BYTES);
    assert.equal(toHex(encoded), c.hex);
    assert.equal(encoded.length, c.bytes);
    const mode = encoded.length > 0 ? (encoded[0] & 0x0f) : null;
    assert.equal(mode, c.mode);
  });
}

// --- Vectores inválidos: rechazo al decodificar ------------------------------

for (const c of VECTORS.invalid_decode) {
  test(`rechaza al decodificar '${c.name}'`, () => {
    const raw = fromHex(c.hex);
    assert.throws(() => decodeText(raw, DICT_BYTES), TextError);
  });
}

// --- Vectores inválidos: rechazo al codificar --------------------------------

for (const c of VECTORS.invalid_encode) {
  test(`rechaza al codificar '${c.name}'`, () => {
    assert.throws(() => encodeText(c.text, DICT_BYTES), TextError);
  });
}

// --- Pruebas adicionales de robustez y límites -------------------------------

test("rechaza bomba de descompresión que supere MAX_INFLATED_BYTES (2000 B)", () => {
  // Comprimimos 20.000 bytes de 'a' con el diccionario
  const deflator = new Deflate({
    raw: true,
    level: 9,
    windowBits: 15,
    memLevel: 8,
    strategy: 0,
    dictionary: DICT_BYTES,
  });
  deflator.push(new TextEncoder().encode("a".repeat(20000)), true);
  const bombPayload = deflator.result;

  const bombMessage = new Uint8Array(1 + bombPayload.length);
  bombMessage[0] = (DICT_VERSION << 4) | TextMode.DICCIONARIO;
  bombMessage.set(bombPayload, 1);

  assert.ok(bombMessage.length <= MAX_TEXT_BYTES);
  assert.throws(
    () => decodeText(bombMessage, DICT_BYTES),
    /supera 2000 B|deflate inválido/,
  );
});

test("rechaza deflate truncado y con bytes de sobra", () => {
  // Truncado
  assert.throws(() => decodeText(fromHex("114362"), DICT_BYTES), TextError);
  // Con bytes de sobra
  assert.throws(() => decodeText(fromHex("114362030000"), DICT_BYTES), TextError);
});

test("ida y vuelta (round-trip) con caracteres variados y emojis", () => {
  const samples = [
    "á é í ó ú ñ Ñ ü",
    "🆘 ayuda 🚑",
    "a",
    "Milán",
    "cra. 23 # 30-15",
    "\n\t ",
    "Colapso en El Cable, 3 atrapados",
  ];
  for (const s of samples) {
    const enc = encodeText(s, DICT_BYTES);
    const dec = decodeText(enc, DICT_BYTES);
    assert.equal(dec, s);
    // Nunca debe crecer más de 1 byte respecto al UTF-8 sin comprimir
    assert.ok(enc.length <= new TextEncoder().encode(s).length + 1);
  }
});
