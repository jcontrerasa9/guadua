// Cabecera binaria de 21 B. CONTRATO: ver docs/diseno.md §5.2 (Contrato de la cabecera).
// Debe producir los mismos bytes que src/sard/protocol/header.py
// (tests/fixtures/header_vectors.json).

export const HEADER_SIZE = 21;
export const VERSION = 1;
export const EPOCH_UNIX = 1767225600; // 2026-01-01T00:00:00Z
export const GEOHASH_ALPHABET = "0123456789bcdefghjkmnpqrstuvwxyz";
export const GEOHASH_LEN = 7;
const MAX_UINT32 = 0xffffffff;
const MAX_UINT40 = 2 ** 40 - 1;
const TWO_32 = 2 ** 32;

export const MessageType = Object.freeze({
  REPORTE: 0, SOS_ESCOMBROS: 1, FRAGMENTO: 2, ACK: 3, A_SALVO: 4,
  LOTE_EVENTOS: 5, PREGUNTA: 6, RESPUESTA: 7, FRAGMENTO_FOTO: 8,
});
export const Incident = Object.freeze({
  ATRAPADOS: 0, COLAPSO: 1, INCENDIO: 2, FUGA_GAS: 3, HERIDO: 4, VIA_BLOQUEADA: 5, OTRO: 6,
});
export const People = Object.freeze({
  UNA: 0, DE_2_A_5: 1, DE_6_A_20: 2, MAS_DE_20: 3, NO_SE: 4,
});
export const Flag = Object.freeze({
  ATRAPADOS: 1 << 0, NO_RESPIRA_O_SANGRADO: 1 << 1, INCENDIO: 1 << 2, RIESGO_ESCENA: 1 << 3,
  NINOS_O_MAYORES: 1 << 4, OBSERVACION_DIRECTA: 1 << 5, GPS: 1 << 6, BATERIA_CRITICA: 1 << 7,
});

export class HeaderError extends Error {}

function checkUint(name, value, max) {
  if (!Number.isInteger(value) || value < 0 || value > max) {
    throw new HeaderError(`${name} fuera de rango: ${value}`);
  }
}

function checkEnum(name, value, values) {
  if (!Object.values(values).includes(value)) {
    throw new HeaderError(`${name} desconocido: ${value}`);
  }
}

function geohashToInt(geohash) {
  if (typeof geohash !== "string" || geohash.length !== GEOHASH_LEN) {
    throw new HeaderError(`geohash debe tener ${GEOHASH_LEN} caracteres: ${geohash}`);
  }
  let value = 0;
  for (const char of geohash) {
    const index = GEOHASH_ALPHABET.indexOf(char);
    if (index < 0) throw new HeaderError(`carácter de geohash inválido: ${char}`);
    value = value * 32 + index; // aritmética, no operadores de bits (35 bits > 32)
  }
  return value;
}

function intToGeohash(value) {
  let out = "";
  for (let i = 0; i < GEOHASH_LEN; i++) {
    out = GEOHASH_ALPHABET[value % 32] + out;
    value = Math.floor(value / 32);
  }
  return out;
}

/** Valida y normaliza: devuelve un objeto con geohash y barrio (uno de los dos en null). */
export function validateHeader(h) {
  const header = { geohash: null, barrio: null, ...h };
  checkEnum("msg_type", header.msg_type, MessageType);
  checkEnum("incident", header.incident, Incident);
  checkEnum("people", header.people, People);
  checkUint("flags", header.flags, 0xff);
  checkUint("device_id", header.device_id, MAX_UINT32);
  checkUint("seq", header.seq, MAX_UINT32);
  checkUint("device_time", header.device_time, MAX_UINT32);
  if (header.flags & Flag.GPS) {
    if (header.barrio !== null) throw new HeaderError("con GPS no se envía barrio");
    geohashToInt(header.geohash);
  } else {
    if (header.geohash !== null) throw new HeaderError("sin la bandera GPS no se envía geohash");
    if (header.barrio === null) throw new HeaderError("sin GPS se requiere el código de barrio");
    checkUint("barrio", header.barrio, MAX_UINT40);
  }
  return header;
}

/** Devuelve un Uint8Array de exactamente 21 bytes. */
export function encodeHeader(h) {
  const header = validateHeader(h);
  const bytes = new Uint8Array(HEADER_SIZE);
  const view = new DataView(bytes.buffer);
  view.setUint8(0, (VERSION << 4) | header.msg_type);
  view.setUint32(1, header.device_id); // DataView usa big-endian por defecto
  view.setUint32(5, header.seq);
  view.setUint32(9, header.device_time);
  view.setUint8(13, header.incident);
  view.setUint8(14, header.people);
  view.setUint8(15, header.flags);
  const location = header.flags & Flag.GPS ? geohashToInt(header.geohash) * 32 : header.barrio;
  view.setUint8(16, Math.floor(location / TWO_32));
  view.setUint32(17, location % TWO_32);
  return bytes;
}

/** Lee los primeros 21 bytes. El texto opcional que siga no se interpreta aquí. */
export function decodeHeader(bytes) {
  if (bytes.length < HEADER_SIZE) {
    throw new HeaderError(`se esperaban al menos ${HEADER_SIZE} bytes, llegaron ${bytes.length}`);
  }
  const view = new DataView(bytes.buffer, bytes.byteOffset, HEADER_SIZE);
  const first = view.getUint8(0);
  if (first >> 4 !== VERSION) throw new HeaderError(`versión no soportada: ${first >> 4}`);
  const flags = view.getUint8(15);
  const location = view.getUint8(16) * TWO_32 + view.getUint32(17);
  let geohash = null;
  let barrio = null;
  if (flags & Flag.GPS) {
    if (location % 32 !== 0) throw new HeaderError("los 5 bits finales del geohash deben ser cero");
    geohash = intToGeohash(location / 32);
  } else {
    barrio = location;
  }
  return validateHeader({
    msg_type: first & 0x0f,
    device_id: view.getUint32(1),
    seq: view.getUint32(5),
    device_time: view.getUint32(9),
    incident: view.getUint8(13),
    people: view.getUint8(14),
    flags,
    geohash,
    barrio,
  });
}
