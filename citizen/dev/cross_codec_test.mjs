// Script para prueba cruzada JS -> Python (N16).
// Comprime los textos de text_vectors.json con el códec JS y escribe los resultados en un archivo JSON.
// Uso: node citizen/dev/cross_codec_test.mjs [archivo_salida]

import { readFileSync, writeFileSync } from "node:fs";

import { encodeText } from "../protocol/text.js";

const FIXTURES_DIR = new URL("../../tests/fixtures/", import.meta.url);
const VECTORS = JSON.parse(
  readFileSync(new URL("text_vectors.json", FIXTURES_DIR), "utf8"),
);
const DICT_BYTES = new Uint8Array(
  readFileSync(new URL("../../data/diccionario_manizales.txt", import.meta.url)),
);

const toHex = (bytes) => Buffer.from(bytes).toString("hex");

const results = [];
for (const c of VECTORS.valid) {
  const encoded = encodeText(c.text, DICT_BYTES);
  results.push({
    name: c.name,
    text: c.text,
    js_hex: toHex(encoded),
    bytes: encoded.length,
    matches_python_vector: toHex(encoded) === c.hex,
  });
}

const outputFile = process.argv[2] || "/tmp/guadua_js_encoded_vectors.json";
writeFileSync(outputFile, JSON.stringify(results, null, 2), "utf8");
console.log(`Codificados ${results.length} vectores con JS en ${outputFile}`);
