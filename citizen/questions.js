// Manejador y funciones puras para preguntas de seguimiento (D5b).
// CONTRATO y diseño: ver docs/diseno.md §3.1, §6.4, docs/contratos.md y src/guadua/node/api.py.

import {
  encodeHeader,
  EPOCH_UNIX,
  Flag,
  HEADER_SIZE,
  Incident,
  MessageType,
  People,
} from "./protocol/header.js";

/**
 * Formatea un identificador numérico de dispositivo como cadena hex de 8 caracteres.
 * Coincide con la ruta GET /api/questions/{device_id}.
 *
 * @param {number} deviceId
 * @returns {string}
 */
export function formatDeviceIdHex(deviceId) {
  return Number(deviceId >>> 0)
    .toString(16)
    .padStart(8, "0");
}

/**
 * Función aislada y pura para construir el mensaje binario de respuesta (tipo 7).
 *
 * ATENCIÓN: El contrato general no especifica la estructura interna de los bytes
 * de la respuesta tras la cabecera de 21 B. Esta función se mantiene separada y
 * modular para ajustarse de inmediato al formato acordado con Jacobo.
 *
 * @param {string} question_id - Identificador de la pregunta (ej. "q-1")
 * @param {boolean} answer - True para Sí, False para No
 * @param {Object} context
 * @param {number} context.deviceId - ID del dispositivo
 * @param {number} context.seq - Consecutivo
 * @param {number} [context.deviceTime] - Segundos desde EPOCH_UNIX
 * @param {number} [context.barrioCode=0] - Código del barrio
 * @returns {{ headerFields: Object, headerBytes: Uint8Array, payloadBytes: Uint8Array, fullBytes: Uint8Array, question_id: string, answer: boolean }}
 */
export function armarRespuesta(
  question_id,
  answer,
  { deviceId, seq, deviceTime, barrioCode = 0 } = {},
) {
  if (typeof question_id !== "string" || !question_id.trim()) {
    throw new Error(`question_id inválido: ${question_id}`);
  }

  const dTime =
    deviceTime ?? Math.max(0, Math.floor(Date.now() / 1000 - EPOCH_UNIX));

  const headerFields = {
    msg_type: MessageType.RESPUESTA, // 7
    device_id: deviceId,
    seq,
    device_time: dTime,
    incident: Incident.OTRO,
    people: People.UNA,
    flags: Flag.OBSERVACION_DIRECTA,
    geohash: null,
    barrio: barrioCode || 0,
  };

  const headerBytes = encodeHeader(headerFields);

  // Payload modular de respuesta: [1 byte booleano (1=Sí, 0=No), bytes UTF-8 de question_id]
  const qIdBytes = new TextEncoder().encode(question_id.trim());
  const payloadBytes = new Uint8Array(1 + qIdBytes.length);
  payloadBytes[0] = answer ? 1 : 0;
  payloadBytes.set(qIdBytes, 1);

  const fullBytes = new Uint8Array(HEADER_SIZE + payloadBytes.length);
  fullBytes.set(headerBytes, 0);
  fullBytes.set(payloadBytes, HEADER_SIZE);

  return {
    headerFields,
    headerBytes,
    payloadBytes,
    fullBytes,
    question_id: question_id.trim(),
    answer: Boolean(answer),
  };
}

/**
 * Clave de una pregunta respondida. El question_id es el id del evento en el nodo (A:11) y
 * se repite si el nodo arranca con la base vacía; el report_id lleva el consecutivo de este
 * dispositivo y no se repite. Juntos identifican la pregunta sin chocar con las de antes.
 *
 * @param {{ question_id: string, report_id?: string }} q
 * @returns {string}
 */
export function answeredKey(q) {
  return `${q?.report_id ?? ""}|${q?.question_id ?? ""}`;
}

/**
 * Selecciona como máximo 1 pregunta pendiente no respondida.
 *
 * @param {Array<{ question_id: string, report_id: string, text: string }>} questions
 * @param {Set<string>} answeredIdsSet - claves de answeredKey()
 * @returns {Object | null}
 */
export function pickNextQuestion(questions = [], answeredIdsSet = new Set()) {
  if (!Array.isArray(questions)) return null;
  for (const q of questions) {
    if (q && q.question_id && !answeredIdsSet.has(answeredKey(q))) {
      return q;
    }
  }
  return null;
}

/**
 * Gestor del ciclo de vida de preguntas de seguimiento.
 *
 * @param {Object} options
 * @param {Object} options.store - ReportStore con getAnsweredQuestionIds y markQuestionAnswered
 * @param {Object} options.queue - ReportQueue con enqueue
 * @param {(url: string, opts?: any) => Promise<any>} [options.fetchFn=fetch]
 * @param {() => Promise<number>} options.getDeviceIdFn - Retorna deviceId de 32 bits
 * @param {(question: Object | null) => void} [options.onQuestionChange] - Callback para la UI
 * @param {string} [options.endpointBase="/api/questions"]
 */
export function createQuestionManager({
  store,
  queue,
  fetchFn = typeof fetch !== "undefined" ? fetch : null,
  getDeviceIdFn,
  onQuestionChange = null,
  endpointBase = "/api/questions",
} = {}) {
  let currentQuestion = null;

  async function pollQuestions() {
    if (!fetchFn || !getDeviceIdFn) return null;

    try {
      const deviceId = await getDeviceIdFn();
      const hexDevice = formatDeviceIdHex(deviceId);
      const url = `${endpointBase.replace(/\/$/, "")}/${hexDevice}`;

      const res = await fetchFn(url, { method: "GET" });
      if (!res || (typeof res.ok === "boolean" && !res.ok) || res.status !== 200) {
        return currentQuestion;
      }

      const data = typeof res.json === "function" ? await res.json() : res.data;
      const questions = Array.isArray(data?.questions) ? data.questions : [];

      const answeredSet = store.getAnsweredQuestionIds
        ? await store.getAnsweredQuestionIds()
        : new Set();

      const nextQ = pickNextQuestion(questions, answeredSet);
      currentQuestion = nextQ;
      if (onQuestionChange) onQuestionChange(currentQuestion);
      return currentQuestion;
    } catch (err) {
      // Sin conexión o fallo de red: no altera el estado actual
      return currentQuestion;
    }
  }

  async function answerCurrentQuestion(answerBool) {
    if (!currentQuestion) return null;

    const q = currentQuestion;
    const answeredId = q.question_id;

    // 1. Marcar como respondida inmediatamente para que la UI la oculte y no reaparezca
    if (store.markQuestionAnswered) {
      await store.markQuestionAnswered(answeredKey(q));
    }
    currentQuestion = null;
    if (onQuestionChange) onQuestionChange(null);

    // 2. Armar la respuesta binaria
    const deviceId = await getDeviceIdFn();
    const seq = store.getNextSeq ? await store.getNextSeq() : 1;
    const barrioCode = store.getLastBarrio ? (await store.getLastBarrio()) || 0 : 0;

    const { headerFields, fullBytes } = armarRespuesta(answeredId, Boolean(answerBool), {
      deviceId,
      seq,
      barrioCode,
    });

    // 3. Encolar en la cola de N13 (se envía cuando haya red)
    let enqueuedItem = null;
    if (queue && typeof queue.enqueue === "function") {
      enqueuedItem = await queue.enqueue({
        headerFields,
        fullBytes,
        priority: 0,
      });
    }

    return {
      enqueuedItem,
      question: q,
      answer: Boolean(answerBool),
    };
  }

  function getCurrentQuestion() {
    return currentQuestion;
  }

  return {
    pollQuestions,
    answerCurrentQuestion,
    getCurrentQuestion,
  };
}
