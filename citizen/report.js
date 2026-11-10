// Funciones puras para el armado del reporte ciudadano.
// CONTRATO: Usa exactamente citizen/protocol/header.js (docs/diseno.md §5.2) y
// citizen/protocol/text.js para la nota (prefijo + texto comprimido, docs/contratos.md).

import {
  encodeHeader,
  EPOCH_UNIX,
  Flag,
  HEADER_SIZE,
  Incident,
  MessageType,
  People,
} from "./protocol/header.js";
import { encodeText, MAX_TEXT_BYTES } from "./protocol/text.js";

export const MAX_NOTE_BYTES = MAX_TEXT_BYTES; // ya codificada: prefijo + texto comprimido

export const INCIDENT_BUTTONS = Object.freeze({
  colapso: { id: "colapso", label: "Derrumbe", incident: Incident.COLAPSO, icon: "🏚️" },
  incendio: { id: "incendio", label: "Incendio", incident: Incident.INCENDIO, icon: "🔥" },
  fuga_gas: { id: "fuga_gas", label: "Fuga de gas", incident: Incident.FUGA_GAS, icon: "⚠️" },
  herido: { id: "herido", label: "Herido", incident: Incident.HERIDO, icon: "🩸" },
  via_bloqueada: { id: "via_bloqueada", label: "Vía cerrada", incident: Incident.VIA_BLOQUEADA, icon: "🚧" },
  atrapados: { id: "atrapados", label: "Atrapados", incident: Incident.ATRAPADOS, icon: "🆘" },
});

/**
 * Mide el tamaño en bytes UTF-8 de una cadena.
 *
 * @param {string} str
 * @returns {number}
 */
export function measureUtf8Bytes(str) {
  if (typeof str !== "string") return 0;
  return new TextEncoder().encode(str).length;
}

/**
 * Corta una cadena de forma segura en el límite de caracteres UTF-8 sin romper bytes.
 *
 * @param {string} str
 * @param {number} [maxBytes=MAX_NOTE_BYTES]
 * @returns {string}
 */
export function truncateUtf8(str, maxBytes = MAX_NOTE_BYTES) {
  if (typeof str !== "string") return "";
  const encoder = new TextEncoder();
  const encoded = encoder.encode(str);
  if (encoded.length <= maxBytes) return str;

  // Retrocede hasta el inicio de un carácter: cortar a la mitad de una "ñ" metería el
  // carácter de reemplazo "�" (3 B) y el resultado se pasaría del límite.
  let end = maxBytes;
  while (end > 0 && (encoded[end] & 0xc0) === 0x80) end -= 1;
  return new TextDecoder("utf-8", { ignoreBOM: true }).decode(encoded.subarray(0, end));
}

/**
 * Calcula los segundos transcurridos desde EPOCH_UNIX para la hora del dispositivo.
 *
 * @param {number} [nowUnixSeconds=Date.now() / 1000]
 * @returns {number}
 */
export function getDeviceTime(nowUnixSeconds = Date.now() / 1000) {
  const diff = Math.floor(nowUnixSeconds - EPOCH_UNIX);
  return Math.max(0, diff);
}

/**
 * Construye el objeto de campos para encodeHeader() a partir de las entradas de la PWA.
 *
 * @param {Object} params
 * @param {number} params.deviceId - Identificador único de 32 bits del dispositivo
 * @param {number} params.seq - Consecutivo de 32 bits
 * @param {number} [params.deviceTime] - Segundos desde EPOCH_UNIX (calculado si no se pasa)
 * @param {string | number} params.incidentKey - Clave de INCIDENT_BUTTONS o valor de Incident
 * @param {boolean} [params.isDirectObservation=true] - Si "Sí, lo veo" está activo
 * @param {number} [params.people=People.NO_SE] - Rango de personas (enum People)
 * @param {string | null} [params.geohash=null] - Geohash de 7 chars si hay GPS
 * @param {number | null} [params.barrioCode=null] - Código de barrio si no hay GPS
 * @param {boolean} [params.criticalBattery=false] - Bandera de batería crítica
 * @returns {Object} Campos compatibles con validateHeader / encodeHeader
 */
export function buildReportHeaderFields({
  deviceId,
  seq,
  deviceTime,
  incidentKey,
  isDirectObservation = true,
  people = People.NO_SE,
  geohash = null,
  barrioCode = null,
  criticalBattery = false,
}) {
  let incident;
  if (typeof incidentKey === "string" && INCIDENT_BUTTONS[incidentKey]) {
    incident = INCIDENT_BUTTONS[incidentKey].incident;
  } else if (Number.isInteger(incidentKey) && Object.values(Incident).includes(incidentKey)) {
    incident = incidentKey;
  } else {
    throw new Error(`Tipo de incidente desconocido: ${incidentKey}`);
  }

  let flags = 0;
  if (isDirectObservation) flags |= Flag.OBSERVACION_DIRECTA;
  // El riesgo para rescatistas y las señales vitales las calcula el nodo (reglas, risk.py
  // y preguntas de seguimiento): la PWA solo marca lo que se sabe por el tipo de incidente.
  if (incident === Incident.FUGA_GAS) flags |= Flag.RIESGO_ESCENA;
  if (incident === Incident.ATRAPADOS) flags |= Flag.ATRAPADOS;
  if (incident === Incident.INCENDIO) flags |= Flag.INCENDIO;
  if (criticalBattery) flags |= Flag.BATERIA_CRITICA;

  const hasGps = typeof geohash === "string" && geohash.length === 7;
  if (hasGps) {
    flags |= Flag.GPS;
  }

  const dTime = deviceTime ?? getDeviceTime();

  return {
    msg_type: MessageType.REPORTE,
    device_id: deviceId,
    seq,
    device_time: dTime,
    incident,
    people,
    flags,
    geohash: hasGps ? geohash.toLowerCase() : null,
    barrio: hasGps ? null : barrioCode,
  };
}

/**
 * Bytes que ocupará la nota ya codificada (prefijo + texto comprimido), o null si no cabe
 * en el límite o no hay diccionario.
 *
 * @param {string} text
 * @param {Uint8Array | null} dictionary - bytes de diccionario_manizales.txt
 * @returns {number | null}
 */
export function encodedNoteBytes(text, dictionary) {
  if (!text) return 0;
  if (!dictionary) return null;
  try {
    return encodeText(text, dictionary).length;
  } catch {
    return null;
  }
}

/**
 * Recorta la nota, letra por letra y sin romper tildes ni emojis, hasta que ya codificada
 * quepa en `maxBytes`. Como el texto se comprime, casi siempre caben más de 100 letras.
 *
 * @param {string} text
 * @param {Uint8Array} dictionary
 * @param {number} [maxBytes=MAX_NOTE_BYTES]
 * @returns {string}
 */
export function fitNote(text, dictionary, maxBytes = MAX_NOTE_BYTES) {
  if (typeof text !== "string" || !text || !dictionary) return "";
  const fits = (candidate) => {
    const size = encodedNoteBytes(candidate, dictionary);
    return size !== null && size <= maxBytes;
  };
  if (fits(text)) return text;
  const chars = Array.from(text); // puntos de código: no parte un emoji ni una tilde
  let lo = 0;
  let hi = chars.length;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (fits(chars.slice(0, mid).join(""))) lo = mid;
    else hi = mid - 1;
  }
  // La compresión no crece exactamente con la longitud: se asegura que el resultado quepa.
  while (lo > 0 && !fits(chars.slice(0, lo).join(""))) lo -= 1;
  return chars.slice(0, lo).join("");
}

/**
 * Arma el paquete binario completo del reporte:
 * Cabecera de 21 B + nota opcional codificada con protocol/text.js (máx. 100 B).
 *
 * Sin diccionario, el reporte sale sin la nota: el reporte es lo vital, la nota es opcional.
 *
 * @param {Object} params
 * @param {Object} params.headerFields - Objeto de campos de la cabecera
 * @param {string} [params.noteText=""] - Nota opcional
 * @param {boolean} [params.strict=false] - Si es true, lanza error si la nota codificada no cabe en vez de recortarla
 * @param {Uint8Array | null} [params.dictionary=null] - bytes de diccionario_manizales.txt
 * @returns {{ headerFields: Object, headerBytes: Uint8Array, textBytes: Uint8Array, fullBytes: Uint8Array, text: string, noteDropped: boolean }}
 */
export function buildReportBinary({ headerFields, noteText = "", strict = false, dictionary = null }) {
  let text = typeof noteText === "string" ? noteText.trim() : "";
  let noteDropped = false;

  // Si la batería es crítica, no se envía texto según el diseño (§5.2)
  if (headerFields.flags & Flag.BATERIA_CRITICA) {
    text = "";
  }

  if (text && !dictionary) {
    noteDropped = true;
    text = "";
  }

  if (text && encodedNoteBytes(text, dictionary) === null) {
    if (strict) {
      throw new Error(`El texto excede el límite de ${MAX_NOTE_BYTES} bytes ya codificado`);
    }
    text = fitNote(text, dictionary);
  }

  const headerBytes = encodeHeader(headerFields);
  const textBytes = text ? encodeText(text, dictionary) : new Uint8Array(0);

  const fullBytes = new Uint8Array(HEADER_SIZE + textBytes.length);
  fullBytes.set(headerBytes, 0);
  fullBytes.set(textBytes, HEADER_SIZE);

  return {
    headerFields,
    headerBytes,
    textBytes,
    fullBytes,
    text,
    noteDropped,
  };
}
