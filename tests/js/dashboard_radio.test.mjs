// Pruebas del panel de radio VHF del tablero (D7).
// Se ejecutan con: node --test "tests/js/*.test.mjs"

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  audioContentType,
  radioClipHtml,
  radioListHtml,
  radioSignature,
  radioSourceLabel,
} from "../../src/guadua/dashboard/radio.js";

const DONE = {
  audio_id: "7c120c39db3eba4d",
  status: "transcrito",
  received_at: 1794573600,
  has_audio: true,
  name: "clip_colapso_dos_incidentes",
  transcription_id: "A:12",
  text: "Estamos en el sitio, incendio en el Centro <b>",
  source: "whisper",
  model: "faster-whisper/small-int8",
  confidence: 0.82,
  corrections: 0,
  report_ids: ["6a1f04c2-1", "6a1f04c2-2"],
  split_by: "reglas",
};
const PENDING = {
  audio_id: "0b5d7e9a11c2f3a4",
  status: "pendiente",
  received_at: 1794573500,
  has_audio: false,
  note: "Whisper no reconoció palabras",
};

test("radioClipHtml: audio del nodo, texto escapado y reportes ligados", () => {
  const html = radioClipHtml(DONE, "http://localhost:8000");
  assert.match(html, /<audio controls[^>]*src="http:\/\/localhost:8000\/api\/radio\/7c120c39db3eba4d\/audio"/);
  assert.match(html, /incendio en el Centro &lt;b&gt;/);
  assert.doesNotMatch(html, /<b>/);
  assert.match(html, /2 reporte\(s\): 6a1f04c2-1, 6a1f04c2-2 · separado por reglas/);
  assert.match(html, /Corregir el texto/);
});

test("radioClipHtml: un clip pendiente pide escribir el texto y abre el campo", () => {
  const html = radioClipHtml(PENDING, "http://localhost:8000");
  assert.match(html, /Audio guardado en otro nodo/);
  assert.match(html, /Sin transcripción: Whisper no reconoció palabras/);
  assert.match(html, /<details class="radio-fix" open>/);
  assert.match(html, /Escribir el texto/);
});

test("radioClipHtml: conserva lo que el operador lleva escrito", () => {
  const html = radioClipHtml(DONE, "", "texto corregido");
  assert.match(html, />texto corregido<\/textarea>/);
});

test("radioSourceLabel: Whisper con confianza, operador o pendiente", () => {
  assert.equal(radioSourceLabel(DONE), "faster-whisper/small-int8 · 82 %");
  assert.equal(
    radioSourceLabel({ ...DONE, source: "operador", operator: "comandante-1" }),
    "corregido por comandante-1",
  );
  assert.equal(radioSourceLabel(PENDING), "pendiente");
});

test("radioSignature cambia solo si cambia algo que se ve", () => {
  const a = radioSignature([DONE, PENDING]);
  assert.equal(a, radioSignature([{ ...DONE, received_at: 1 }, PENDING]));
  assert.notEqual(a, radioSignature([{ ...DONE, transcription_id: "A:13" }, PENDING]));
});

test("radioListHtml: lista vacía", () => {
  assert.match(radioListHtml([], ""), /No hay clips de radio/);
  assert.equal((radioListHtml([DONE, PENDING], "").match(/class="radio-clip[ "]/g) || []).length, 2);
});

test("audioContentType: por la extensión, aunque el navegador diga otra cosa", () => {
  assert.equal(audioContentType({ name: "PTT-20261109.ogg", type: "application/ogg" }), "audio/ogg");
  assert.equal(audioContentType({ name: "nota.opus", type: "" }), "audio/ogg");
  assert.equal(audioContentType({ name: "grabacion.m4a", type: "audio/x-m4a" }), "audio/mp4");
  assert.equal(audioContentType({ name: "sin_extension", type: "audio/wav" }), "audio/wav");
  assert.equal(audioContentType({ name: "sin_extension", type: "" }), "application/octet-stream");
});
