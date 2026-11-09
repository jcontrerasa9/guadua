// Pruebas unitarias para fragmentos y ACK con mapa de bits en JS (N16).
// Se ejecutan con: node --test "tests/js/*.test.mjs"

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { MessageType } from "../../citizen/protocol/header.js";
import {
  ackComplete,
  ackMissing,
  ACK_HEADER_SIZE,
  decodeAck,
  decodeFragment,
  encodeAck,
  encodeFragment,
  FragmentError,
  FRAGMENT_HEADER_SIZE,
  fragmentCapacity,
  makeAck,
  makeFragment,
  MAX_FRAGMENTS,
  Reassembler,
  Sender,
  split,
} from "../../citizen/protocol/fragments.js";

const FIXTURES_DIR = new URL("../fixtures/", import.meta.url);
const VECTORS = JSON.parse(
  readFileSync(new URL("fragment_vectors.json", FIXTURES_DIR), "utf8"),
);

const toHex = (bytes) => Buffer.from(bytes).toString("hex");
const fromHex = (hex) => new Uint8Array(Buffer.from(hex, "hex"));

// --- Verificación de constantes ----------------------------------------------

test("constantes coinciden con los vectores", () => {
  assert.equal(FRAGMENT_HEADER_SIZE, VECTORS.fragment_header_size);
  assert.equal(ACK_HEADER_SIZE, VECTORS.ack_header_size);
  assert.equal(MAX_FRAGMENTS, VECTORS.max_fragments);
});

// --- Vectores de fragmentos: codificar y decodificar --------------------------

for (const c of VECTORS.fragments) {
  test(`fragmento vector '${c.name}': codificar y decodificar coinciden con Python`, () => {
    const rawData = fromHex(c.fields.data);
    const frag = makeFragment({ ...c.fields, data: rawData });

    const encoded = encodeFragment(frag);
    assert.equal(toHex(encoded), c.hex);

    const decoded = decodeFragment(fromHex(c.hex));
    assert.equal(decoded.msg_type, c.fields.msg_type);
    assert.equal(decoded.device_id, c.fields.device_id);
    assert.equal(decoded.seq, c.fields.seq);
    assert.equal(decoded.index, c.fields.index);
    assert.equal(decoded.total, c.fields.total);
    assert.equal(toHex(decoded.data), c.fields.data);
  });
}

// --- Vectores de ACK: codificar y decodificar ---------------------------------

for (const c of VECTORS.acks) {
  test(`ACK vector '${c.name}': mapa de bits y bytes coinciden con Python`, () => {
    const ack = makeAck(c.fields);

    const encoded = encodeAck(ack);
    assert.equal(toHex(encoded), c.hex);

    const decoded = decodeAck(fromHex(c.hex));
    assert.equal(decoded.acked_type, c.fields.acked_type);
    assert.equal(decoded.device_id, c.fields.device_id);
    assert.equal(decoded.seq, c.fields.seq);
    assert.equal(decoded.total, c.fields.total);
    assert.deepEqual(decoded.received, [...c.fields.received].sort((a, b) => a - b));
  });
}

// --- Rechazos de fragmentos inválidos ----------------------------------------

for (const c of VECTORS.invalid_fragment_decode) {
  test(`rechaza al decodificar fragmento '${c.name}'`, () => {
    assert.throws(() => decodeFragment(fromHex(c.hex)), FragmentError);
  });
}

// --- Rechazos de ACK inválidos -----------------------------------------------

for (const c of VECTORS.invalid_ack_decode) {
  test(`rechaza al decodificar ACK '${c.name}'`, () => {
    assert.throws(() => decodeAck(fromHex(c.hex)), FragmentError);
  });
}

// --- Pruebas de split y Reassembler (desorden y piezas faltantes) -------------

test("split y Reassembler: reensambla con orden alterado", () => {
  const message = new TextEncoder().encode("Mensaje de prueba para partir en fragmentos múltiples con MTU pequeño");
  const mtu = 30; // fragmentCapacity = 30 - 11 = 19 bytes
  const fragments = split(message, {
    deviceId: 12345,
    seq: 99,
    mtu,
    msgType: MessageType.FRAGMENTO,
  });

  assert.ok(fragments.length > 1);

  // Desordenar fragmentos
  const shuffled = [...fragments].reverse();
  const reassembler = new Reassembler();

  let reassembled = null;
  for (const f of shuffled) {
    const res = reassembler.add(f);
    if (res !== null) {
      reassembled = res;
    }
  }

  assert.ok(reassembled !== null);
  assert.equal(new TextDecoder().decode(reassembled), new TextDecoder().decode(message));

  // Generar ACK tras recibir todos
  const ack = reassembler.ack(MessageType.FRAGMENTO, 12345, 99);
  assert.ok(ackComplete(ack));
  assert.deepEqual(ackMissing(ack), []);
});

test("Reassembler con piezas faltantes y ACK correspondiente", () => {
  const message = new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
  const mtu = 14; // capacidad = 3 B por fragmento -> 4 fragmentos (3, 3, 3, 1)
  const fragments = split(message, {
    deviceId: 55,
    seq: 10,
    mtu,
  });

  assert.equal(fragments.length, 4);

  const reassembler = new Reassembler();
  // Agregamos solo fragmentos 0 y 2 (faltan 1 y 3)
  assert.equal(reassembler.add(fragments[0]), null);
  assert.equal(reassembler.add(fragments[2]), null);

  const ack = reassembler.ackFor(fragments[0]);
  assert.equal(ackComplete(ack), false);
  assert.deepEqual(ack.received, [0, 2]);
  assert.deepEqual(ackMissing(ack), [1, 3]);

  // Si llega fragmento repetido con los mismos datos, no pasa nada
  assert.equal(reassembler.add(fragments[0]), null);

  // Si llega fragmento repetido con datos alterados, lanza FragmentError
  const corrupted = makeFragment({
    ...fragments[0],
    data: new Uint8Array([99, 99, 99]),
  });
  assert.throws(() => reassembler.add(corrupted), /repetido con otros datos/);
});

// --- Sender y tracking de ACK -------------------------------------------------

test("Sender rastrea fragmentos pendientes y detecta cuándo finaliza", () => {
  const message = new Uint8Array(50);
  for (let i = 0; i < 50; i++) message[i] = i;
  const fragments = split(message, {
    deviceId: 1,
    seq: 1,
    mtu: 25, // cap = 14 B -> 4 fragmentos
  });

  const sender = new Sender(fragments);
  assert.equal(sender.pending().length, 4);
  assert.equal(sender.done, false);

  // Recibe ACK de fragmentos 0 y 1
  const ack1 = makeAck({
    acked_type: MessageType.FRAGMENTO,
    device_id: 1,
    seq: 1,
    total: 4,
    received: [0, 1],
  });
  sender.onAck(ack1);
  assert.equal(sender.pending().length, 2);
  assert.deepEqual(
    sender.pending().map((f) => f.index),
    [2, 3],
  );
  assert.equal(sender.done, false);

  // Recibe ACK de fragmentos 2 y 3
  const ack2 = makeAck({
    acked_type: MessageType.FRAGMENTO,
    device_id: 1,
    seq: 1,
    total: 4,
    received: [2, 3],
  });
  sender.onAck(ack2);
  assert.equal(sender.pending().length, 0);
  assert.equal(sender.done, true);
});
