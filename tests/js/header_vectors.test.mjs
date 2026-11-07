import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { decodeHeader, encodeHeader, HeaderError } from "../../citizen/protocol/header.js";

const vectors = JSON.parse(
  readFileSync(new URL("../fixtures/header_vectors.json", import.meta.url), "utf8"),
);
const toHex = (bytes) => Buffer.from(bytes).toString("hex");
const fromHex = (hex) => new Uint8Array(Buffer.from(hex, "hex"));

for (const c of vectors.valid) {
  test(`codifica ${c.name}`, () => {
    assert.equal(toHex(encodeHeader(c.fields)), c.hex);
  });
  test(`decodifica ${c.name}`, () => {
    assert.deepEqual(decodeHeader(fromHex(c.hex)), { geohash: null, barrio: null, ...c.fields });
  });
}

for (const c of vectors.invalid_decode) {
  test(`rechaza al decodificar ${c.name}`, () => {
    assert.throws(() => decodeHeader(fromHex(c.hex)), HeaderError);
  });
}

for (const c of vectors.invalid_encode) {
  test(`rechaza al codificar ${c.name}`, () => {
    assert.throws(() => encodeHeader(c.fields), HeaderError);
  });
}
