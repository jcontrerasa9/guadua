// Fragmentos y ACK con mapa de bits. Equivale a src/guadua/protocol/fragments.py (N2).
// CONTRATO: ver docs/contratos.md y tests/fixtures/fragment_vectors.json.
//
// Fragmento (11 B + datos), big-endian (">BIIBB" en Python):
//   0      versión + tipo (2 fragmento · 8 fragmento de foto)
//   1–4    dispositivo (uint32)   5–8 consecutivo (uint32)
//   9      índice (desde 0)       10  total (1 a 255)
//   11…    datos (al menos 1 B)
//
// ACK (11 B + mapa de bits):
//   0      versión + tipo (3 ACK)
//   1–8    dispositivo + consecutivo del mensaje confirmado
//   9      tipo confirmado (cualquiera menos ACK)
//   10     total
//   11…    ⌈total/8⌉ B; el fragmento i es el bit i % 8 del byte i // 8 (primero el menos
//          significativo). Los bits de relleno van en cero.
//
// Aquí no hay red ni temporizadores: los reintentos son de la cola y del canal.

import { MessageType, VERSION } from "./header.js";

export const FRAGMENT_HEADER_SIZE = 11;
export const ACK_HEADER_SIZE = 11;
export const MAX_FRAGMENTS = 255;
export const FRAGMENT_TYPES = Object.freeze([MessageType.FRAGMENTO, MessageType.FRAGMENTO_FOTO]);
const MAX_UINT32 = 0xffffffff;
const KNOWN_TYPES = new Set(Object.values(MessageType));

export class FragmentError extends Error {}

function checkUint(name, value, max) {
  if (!Number.isInteger(value) || value < 0 || value > max) {
    throw new FragmentError(`${name} fuera de rango: ${value}`);
  }
}

function checkTotal(total) {
  checkUint("total", total, MAX_FRAGMENTS);
  if (total === 0) throw new FragmentError("total debe ser al menos 1");
}

function checkMessageType(name, value) {
  if (!KNOWN_TYPES.has(value)) throw new FragmentError(`${name} desconocido: ${value}`);
}

function bitmapSize(total) {
  return Math.floor((total + 7) / 8);
}

function messageKey(msgType, deviceId, seq) {
  return `${msgType}:${deviceId}:${seq}`;
}

/** Valida y normaliza un fragmento: { msg_type, device_id, seq, index, total, data }. */
export function makeFragment({ msg_type, device_id, seq, index, total, data }) {
  checkMessageType("msg_type", msg_type);
  if (!FRAGMENT_TYPES.includes(msg_type)) {
    throw new FragmentError(`tipo de fragmento inválido: ${msg_type}`);
  }
  checkUint("device_id", device_id, MAX_UINT32);
  checkUint("seq", seq, MAX_UINT32);
  checkTotal(total);
  checkUint("index", index, total - 1);
  if (!(data instanceof Uint8Array) || data.length === 0) {
    throw new FragmentError("el fragmento necesita al menos 1 B de datos");
  }
  return Object.freeze({ msg_type, device_id, seq, index, total, data });
}

/** Valida y normaliza un ACK: { acked_type, device_id, seq, total, received }. */
export function makeAck({ acked_type, device_id, seq, total, received }) {
  checkMessageType("acked_type", acked_type);
  if (acked_type === MessageType.ACK) {
    throw new FragmentError("un ACK no se confirma con otro ACK");
  }
  checkUint("device_id", device_id, MAX_UINT32);
  checkUint("seq", seq, MAX_UINT32);
  checkTotal(total);
  const set = new Set(received);
  for (const i of set) checkUint("índice recibido", i, total - 1);
  return Object.freeze({
    acked_type,
    device_id,
    seq,
    total,
    received: Object.freeze([...set].sort((a, b) => a - b)),
  });
}

export function ackComplete(ack) {
  return ack.received.length === ack.total;
}

export function ackMissing(ack) {
  const got = new Set(ack.received);
  const out = [];
  for (let i = 0; i < ack.total; i++) if (!got.has(i)) out.push(i);
  return out;
}

function writePrefix(view, first, deviceId, seq, b9, total) {
  view.setUint8(0, first);
  view.setUint32(1, deviceId, false); // big-endian
  view.setUint32(5, seq, false);
  view.setUint8(9, b9);
  view.setUint8(10, total);
}

function readPrefix(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, FRAGMENT_HEADER_SIZE);
  return {
    first: view.getUint8(0),
    deviceId: view.getUint32(1, false),
    seq: view.getUint32(5, false),
    b9: view.getUint8(9),
    total: view.getUint8(10),
  };
}

export function encodeFragment(fragment) {
  const f = makeFragment(fragment);
  const out = new Uint8Array(FRAGMENT_HEADER_SIZE + f.data.length);
  writePrefix(new DataView(out.buffer), (VERSION << 4) | f.msg_type, f.device_id, f.seq, f.index, f.total);
  out.set(f.data, FRAGMENT_HEADER_SIZE);
  return out;
}

export function decodeFragment(bytes) {
  if (!(bytes instanceof Uint8Array) || bytes.length <= FRAGMENT_HEADER_SIZE) {
    throw new FragmentError(
      `un fragmento necesita más de ${FRAGMENT_HEADER_SIZE} B, llegaron ${bytes?.length ?? 0}`,
    );
  }
  const p = readPrefix(bytes);
  if (p.first >> 4 !== VERSION) throw new FragmentError(`versión no soportada: ${p.first >> 4}`);
  return makeFragment({
    msg_type: p.first & 0x0f,
    device_id: p.deviceId,
    seq: p.seq,
    index: p.b9,
    total: p.total,
    data: bytes.slice(FRAGMENT_HEADER_SIZE),
  });
}

export function encodeAck(ack) {
  const a = makeAck(ack);
  const out = new Uint8Array(ACK_HEADER_SIZE + bitmapSize(a.total));
  writePrefix(new DataView(out.buffer), (VERSION << 4) | MessageType.ACK, a.device_id, a.seq, a.acked_type, a.total);
  for (const i of a.received) out[ACK_HEADER_SIZE + Math.floor(i / 8)] |= 1 << (i % 8);
  return out;
}

export function decodeAck(bytes) {
  if (!(bytes instanceof Uint8Array) || bytes.length < ACK_HEADER_SIZE + 1) {
    throw new FragmentError(`un ACK necesita más de ${ACK_HEADER_SIZE} B, llegaron ${bytes?.length ?? 0}`);
  }
  const p = readPrefix(bytes);
  if (p.first >> 4 !== VERSION) throw new FragmentError(`versión no soportada: ${p.first >> 4}`);
  if ((p.first & 0x0f) !== MessageType.ACK) {
    throw new FragmentError(`no es un ACK: tipo ${p.first & 0x0f}`);
  }
  checkTotal(p.total);
  const bitmap = bytes.subarray(ACK_HEADER_SIZE);
  if (bitmap.length !== bitmapSize(p.total)) {
    throw new FragmentError(`mapa de ${bitmap.length} B para ${p.total} fragmentos`);
  }
  const received = [];
  for (let i = 0; i < bitmap.length * 8; i++) {
    if ((bitmap[Math.floor(i / 8)] >> (i % 8)) & 1) {
      if (i >= p.total) throw new FragmentError("bits de relleno del mapa distintos de cero");
      received.push(i);
    }
  }
  return makeAck({ acked_type: p.b9, device_id: p.deviceId, seq: p.seq, total: p.total, received });
}

/** Bytes de datos que caben en cada fragmento con esta trama. */
export function fragmentCapacity(mtu) {
  const capacity = mtu - FRAGMENT_HEADER_SIZE;
  if (!Number.isInteger(mtu) || capacity < 1) {
    throw new FragmentError(`una trama de ${mtu} B no deja espacio para datos`);
  }
  return capacity;
}

/** Parte `message` en fragmentos que caben, con cabecera, en tramas de `mtu` bytes. */
export function split(message, { deviceId, seq, mtu, msgType = MessageType.FRAGMENTO }) {
  if (!(message instanceof Uint8Array) || message.length === 0) {
    throw new FragmentError("no hay nada que fragmentar");
  }
  const size = fragmentCapacity(mtu);
  const total = Math.ceil(message.length / size);
  if (total > MAX_FRAGMENTS) {
    throw new FragmentError(
      `${message.length} B necesitan ${total} fragmentos de ${size} B; el máximo es ${MAX_FRAGMENTS}`,
    );
  }
  const out = [];
  for (let i = 0; i < total; i++) {
    out.push(
      makeFragment({
        msg_type: msgType,
        device_id: deviceId,
        seq,
        index: i,
        total,
        data: message.slice(i * size, (i + 1) * size),
      }),
    );
  }
  return out;
}

function sameBytes(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

/**
 * Receptor: junta fragmentos de varios mensajes y arma el ACK de cada uno.
 * Un fragmento repetido no se guarda dos veces. Tras completar un mensaje se recuerda que está
 * completo, para responder ACK a los repetidos sin entregarlo de nuevo.
 */
export class Reassembler {
  constructor() {
    this._partials = new Map();
  }

  /** Guarda el fragmento. Devuelve el mensaje completo la primera vez que se completa; si no, null. */
  add(fragment) {
    const f = makeFragment(fragment);
    const key = messageKey(f.msg_type, f.device_id, f.seq);
    let partial = this._partials.get(key);
    if (!partial) {
      partial = { total: f.total, parts: new Map(), delivered: false };
      this._partials.set(key, partial);
    }
    if (partial.total !== f.total) {
      throw new FragmentError(`total ${f.total} distinto del conocido ${partial.total}`);
    }
    const known = partial.parts.get(f.index);
    if (known !== undefined) {
      if (!sameBytes(known, f.data)) {
        throw new FragmentError(`fragmento ${f.index} repetido con otros datos`);
      }
      return null;
    }
    partial.parts.set(f.index, f.data);
    if (partial.delivered || partial.parts.size < partial.total) return null;
    partial.delivered = true;

    let length = 0;
    for (const part of partial.parts.values()) length += part.length;
    const out = new Uint8Array(length);
    let offset = 0;
    for (let i = 0; i < partial.total; i++) {
      const part = partial.parts.get(i);
      out.set(part, offset);
      offset += part.length;
    }
    return out;
  }

  ack(msgType, deviceId, seq) {
    checkMessageType("msg_type", msgType);
    const partial = this._partials.get(messageKey(msgType, deviceId, seq));
    if (!partial) throw new FragmentError("no hay fragmentos de ese mensaje");
    return makeAck({
      acked_type: msgType,
      device_id: deviceId,
      seq,
      total: partial.total,
      received: [...partial.parts.keys()],
    });
  }

  ackFor(fragment) {
    return this.ack(fragment.msg_type, fragment.device_id, fragment.seq);
  }
}

/** Emisor de un mensaje fragmentado: con cada ACK sabe qué falta reenviar. */
export class Sender {
  constructor(fragments) {
    if (!Array.isArray(fragments) || fragments.length === 0) {
      throw new FragmentError("no hay fragmentos que enviar");
    }
    const first = fragments[0];
    this._key = messageKey(first.msg_type, first.device_id, first.seq);
    this._total = first.total;
    const ok =
      fragments.length === this._total &&
      fragments.every(
        (f, i) => messageKey(f.msg_type, f.device_id, f.seq) === this._key && f.index === i,
      );
    if (!ok) throw new FragmentError("los fragmentos deben ser un mensaje completo y en orden");
    this._fragments = fragments.map(makeFragment);
    this._acked = new Set();
  }

  /** Fragmentos que ningún ACK ha confirmado todavía. */
  pending() {
    return this._fragments.filter((f) => !this._acked.has(f.index));
  }

  onAck(ack) {
    const a = makeAck(ack);
    if (messageKey(a.acked_type, a.device_id, a.seq) !== this._key || a.total !== this._total) {
      throw new FragmentError("el ACK es de otro mensaje");
    }
    for (const i of a.received) this._acked.add(i);
  }

  get done() {
    return this._acked.size === this._total;
  }
}
