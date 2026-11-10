// Funciones puras para el armado del mensaje "Estoy a salvo" (D2a).
// CONTRATO: Usa citizen/protocol/header.js (docs/diseno.md §5.2) con cabecera de 21 B
// y tipo de mensaje 4 (A_SALVO), y citizen/protocol/text.js para el nombre opcional.

import {
  encodeHeader,
  EPOCH_UNIX,
  Flag,
  HEADER_SIZE,
  Incident,
  MessageType,
  People,
} from "./protocol/header.js";
import { encodeText } from "./protocol/text.js";
import { truncateUtf8 } from "./report.js";

export const MAX_NAME_BYTES = 40;
export const SAFE_INCIDENT = Incident.OTRO; // 6: no es un siniestro
export const SAFE_PEOPLE = People.UNA; // 0: reporte individual (1 persona a salvo)
export const SAFE_FLAGS = Flag.OBSERVACION_DIRECTA; // 32: observación directa, GPS apagado (0)

/**
 * Calcula los segundos transcurridos desde EPOCH_UNIX para la hora del dispositivo.
 *
 * @param {number} [nowUnixSeconds=Date.now() / 1000]
 * @returns {number}
 */
export function getSafeDeviceTime(nowUnixSeconds = Date.now() / 1000) {
  const diff = Math.floor(nowUnixSeconds - EPOCH_UNIX);
  return Math.max(0, diff);
}

/**
 * Construye los campos de la cabecera para un mensaje "Estoy a salvo" (tipo 4).
 *
 * @param {Object} params
 * @param {number} params.deviceId - Identificador único de 32 bits del dispositivo
 * @param {number} params.seq - Consecutivo de 32 bits
 * @param {number} [params.deviceTime] - Segundos desde EPOCH_UNIX
 * @param {number} params.barrioCode - Código entero del barrio (>= 0)
 * @returns {Object} Campos compatibles con encodeHeader
 */
export function buildSafeHeaderFields({
  deviceId,
  seq,
  deviceTime,
  barrioCode,
}) {
  if (!Number.isInteger(barrioCode) || barrioCode < 0) {
    throw new Error(`Código de barrio inválido: ${barrioCode}`);
  }

  const dTime = deviceTime ?? getSafeDeviceTime();

  return {
    msg_type: MessageType.A_SALVO,
    device_id: deviceId,
    seq,
    device_time: dTime,
    incident: SAFE_INCIDENT,
    people: SAFE_PEOPLE,
    flags: SAFE_FLAGS,
    geohash: null,
    barrio: barrioCode,
  };
}

/**
 * Construye el paquete binario completo de "Estoy a salvo":
 * - Cabecera fija de 21 B (contrato protocol/header.js, GPS apagado, ubicación por barrio).
 * - Nombre opcional codificado con protocol/text.js (máx. 40 B UTF-8).
 *
 * Tamaños reales:
 * - Sin nombre: exactamente 21 bytes.
 * - Con nombre: 21 B + 1 B prefijo + bytes de texto comprimido/plano.
 *
 * @param {Object} params
 * @param {number} params.deviceId
 * @param {number} params.seq
 * @param {number} [params.deviceTime]
 * @param {number} params.barrioCode
 * @param {string} [params.name=""]
 * @param {Uint8Array | null} [params.dictionary=null]
 * @returns {{ headerFields: Object, headerBytes: Uint8Array, textBytes: Uint8Array, fullBytes: Uint8Array, name: string }}
 */
export function buildSafeBinary({
  deviceId,
  seq,
  deviceTime,
  barrioCode,
  name = "",
  dictionary = null,
}) {
  const headerFields = buildSafeHeaderFields({
    deviceId,
    seq,
    deviceTime,
    barrioCode,
  });

  const headerBytes = encodeHeader(headerFields);

  let cleanName = typeof name === "string" ? name.trim() : "";
  if (cleanName) {
    cleanName = truncateUtf8(cleanName, MAX_NAME_BYTES);
  }

  let textBytes = new Uint8Array(0);
  if (cleanName && dictionary) {
    textBytes = encodeText(cleanName, dictionary);
  }

  const fullBytes = new Uint8Array(HEADER_SIZE + textBytes.length);
  fullBytes.set(headerBytes, 0);
  if (textBytes.length > 0) {
    fullBytes.set(textBytes, HEADER_SIZE);
  }

  return {
    headerFields,
    headerBytes,
    textBytes,
    fullBytes,
    name: cleanName,
  };
}
