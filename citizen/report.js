// Funciones puras para el armado del reporte ciudadano.
// CONTRATO: Usa exactamente citizen/protocol/header.js (docs/diseno.md §5.2).

import {
  encodeHeader,
  EPOCH_UNIX,
  Flag,
  HEADER_SIZE,
  Incident,
  MessageType,
  People,
} from "./protocol/header.js";

export const MAX_NOTE_BYTES = 100;

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

  // Decodifica cortando en el byte exacto; 'fatal: false' descarta cualquier fragmento de byte incompleto
  const slice = encoded.subarray(0, maxBytes);
  const decoder = new TextDecoder("utf-8", { fatal: false, ignoreBOM: true });
  return decoder.decode(slice);
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
 * @param {boolean} [params.smellsGasOrWires=false] - Checkbox gas o cables
 * @param {boolean} [params.notBreathing=false] - Checkbox alguien no respira
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
  smellsGasOrWires = false,
  notBreathing = false,
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
  if (smellsGasOrWires || incident === Incident.FUGA_GAS) flags |= Flag.RIESGO_ESCENA;
  if (notBreathing) flags |= Flag.NO_RESPIRA_O_SANGRADO;
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
 * Arma el paquete binario completo del reporte:
 * Cabecera de 21 B + texto UTF-8 opcional (máx. 100 B).
 *
 * @param {Object} params
 * @param {Object} params.headerFields - Objeto de campos de la cabecera
 * @param {string} [params.noteText=""] - Nota opcional
 * @param {boolean} [params.strict=false] - Si es true, lanza error si noteText > 100 bytes en vez de truncar
 * @returns {{ headerFields: Object, headerBytes: Uint8Array, textBytes: Uint8Array, fullBytes: Uint8Array, text: string }}
 */
export function buildReportBinary({ headerFields, noteText = "", strict = false }) {
  let text = typeof noteText === "string" ? noteText.trim() : "";
  const byteLen = measureUtf8Bytes(text);

  if (byteLen > MAX_NOTE_BYTES) {
    if (strict) {
      throw new Error(`El texto excede el límite de ${MAX_NOTE_BYTES} bytes UTF-8 (${byteLen} B)`);
    }
    text = truncateUtf8(text, MAX_NOTE_BYTES);
  }

  // Si la batería es crítica, no se envía texto según el diseño (§5.2)
  if (headerFields.flags & Flag.BATERIA_CRITICA) {
    text = "";
  }

  const headerBytes = encodeHeader(headerFields);
  const textBytes = new TextEncoder().encode(text);

  const fullBytes = new Uint8Array(HEADER_SIZE + textBytes.length);
  fullBytes.set(headerBytes, 0);
  fullBytes.set(textBytes, HEADER_SIZE);

  return {
    headerFields,
    headerBytes,
    textBytes,
    fullBytes,
    text,
  };
}
