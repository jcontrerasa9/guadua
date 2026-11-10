// Pruebas unitarias para la tarjeta Sí / No y preguntas de seguimiento en la PWA (D5b).
// Se ejecutan con: node --test "tests/js/*.test.mjs"

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  decodeHeader,
  HEADER_SIZE,
  Incident,
  MessageType,
  People,
} from "../../citizen/protocol/header.js";
import {
  answeredKey,
  armarRespuesta,
  createQuestionManager,
  formatDeviceIdHex,
  pickNextQuestion,
} from "../../citizen/questions.js";
import {
  createReportQueue,
  MemoryReportStore,
  processQueue,
  ReportStatus,
} from "../../citizen/queue.js";

// --- 1. Funciones puras: formato de device_id y armado de respuesta -----------

test("formatDeviceIdHex formatea a 8 dígitos hexadecimales en minúsculas", () => {
  assert.equal(formatDeviceIdHex(0xa1b2c3d4), "a1b2c3d4");
  assert.equal(formatDeviceIdHex(1), "00000001");
  assert.equal(formatDeviceIdHex(0), "00000000");
  assert.equal(formatDeviceIdHex(0xffffffff), "ffffffff");
});

test("armarRespuesta construye mensaje con cabecera de 21 B de tipo 7 (RESPUESTA)", () => {
  const res = armarRespuesta("q-1", true, {
    deviceId: 0xa1b2c3d4,
    seq: 5,
    deviceTime: 1200,
    barrioCode: 3,
  });

  assert.equal(res.question_id, "q-1");
  assert.equal(res.answer, true);
  assert.ok(res.fullBytes.length >= HEADER_SIZE + 2); // 21 B cabecera + payload

  // Decodificación de la cabecera estándar de 21 B
  const header = decodeHeader(res.fullBytes.subarray(0, HEADER_SIZE));
  assert.equal(header.msg_type, MessageType.RESPUESTA); // 7
  assert.equal(header.device_id, 0xa1b2c3d4);
  assert.equal(header.seq, 5);
  assert.equal(header.device_time, 1200);
  assert.equal(header.incident, Incident.OTRO);
  assert.equal(header.people, People.UNA);
  assert.equal(header.barrio, 3);
  assert.equal(header.geohash, null);

  // Verificación del payload modular: byte 21 booleano (1=Sí), seguido de question_id en UTF-8
  const answerByte = res.fullBytes[HEADER_SIZE];
  assert.equal(answerByte, 1);
  const qIdStr = new TextDecoder().decode(res.fullBytes.subarray(HEADER_SIZE + 1));
  assert.equal(qIdStr, "q-1");
});

test("armarRespuesta con false genera byte booleano 0 (No)", () => {
  const res = armarRespuesta("q-99", false, {
    deviceId: 1,
    seq: 1,
  });
  assert.equal(res.answer, false);
  assert.equal(res.fullBytes[HEADER_SIZE], 0);
  const qIdStr = new TextDecoder().decode(res.fullBytes.subarray(HEADER_SIZE + 1));
  assert.equal(qIdStr, "q-99");
});

test("pickNextQuestion selecciona máximo 1 pregunta pendiente y filtra las respondidas", () => {
  const questions = [
    { question_id: "q-1", text: "¿Huele a gas?" },
    { question_id: "q-2", text: "¿Cuántas personas atrapadas?" },
  ];

  // Ninguna respondida: entrega la primera
  const next1 = pickNextQuestion(questions, new Set());
  assert.equal(next1.question_id, "q-1");

  // q-1 ya respondida: entrega q-2
  const next2 = pickNextQuestion(questions, new Set([answeredKey(questions[0])]));
  assert.equal(next2.question_id, "q-2");

  // Todas respondidas: entrega null
  const next3 = pickNextQuestion(questions, new Set(questions.map(answeredKey)));
  assert.equal(next3, null);
});

test("una pregunta nueva con un id ya usado (nodo reiniciado) no se descarta", () => {
  // Antes de reiniciar el nodo se respondió A:11 del reporte 007caa53-3; después del
  // reinicio, A:11 es otra pregunta, de otro reporte del mismo dispositivo.
  const answered = new Set([answeredKey({ question_id: "A:11", report_id: "007caa53-3" })]);
  const nueva = { question_id: "A:11", report_id: "007caa53-52", text: "¿Alguien no respira?" };
  assert.equal(pickNextQuestion([nueva], answered), nueva);
  // La misma pregunta del mismo reporte sí queda como respondida
  assert.equal(pickNextQuestion([{ ...nueva, report_id: "007caa53-3" }], answered), null);
});

// --- 2. Pruebas de integración del gestor de preguntas -----------------------

test("una pregunta aparece al consultar GET /api/questions/{device_id}", async () => {
  const store = new MemoryReportStore();
  const queue = createReportQueue({ store });
  store.meta.set("device_id", 0xa1b2c3d4);

  const mockQuestions = [
    {
      question_id: "q-1",
      report_id: "a1b2c3d4-7",
      text: "¿Huele a gas?",
    },
  ];

  let requestedUrl = null;
  const mockFetch = async (url) => {
    requestedUrl = url;
    return {
      ok: true,
      status: 200,
      json: async () => ({ questions: mockQuestions }),
    };
  };

  let notifiedQuestion = null;
  const manager = createQuestionManager({
    store,
    queue,
    fetchFn: mockFetch,
    getDeviceIdFn: async () => store.getDeviceId(),
    onQuestionChange: (q) => {
      notifiedQuestion = q;
    },
  });

  const q = await manager.pollQuestions();
  assert.equal(requestedUrl, "/api/questions/a1b2c3d4");
  assert.ok(q);
  assert.equal(q.question_id, "q-1");
  assert.equal(q.text, "¿Huele a gas?");
  assert.deepEqual(notifiedQuestion, q);
});

test("responder la pregunta la encola en la cola de N13 y desaparece de la UI", async () => {
  const store = new MemoryReportStore();
  let dispatchedPayload = null;

  const mockFetch = async (url, opts) => {
    if (url.startsWith("/api/questions")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          questions: [{ question_id: "q-10", text: "¿Hay fuego visible?" }],
        }),
      };
    }
    if (url === "/api/messages") {
      dispatchedPayload = new Uint8Array(opts.body);
      return {
        status: 200,
        json: async () => ({ status: "recibido", duplicate: false }),
      };
    }
    throw new Error(`Ruta inesperada: ${url}`);
  };

  const queue = createReportQueue({
    store,
    fetchFn: mockFetch,
  });
  store.meta.set("device_id", 0x12345678);

  const notifications = [];
  const manager = createQuestionManager({
    store,
    queue,
    fetchFn: mockFetch,
    getDeviceIdFn: async () => store.getDeviceId(),
    onQuestionChange: (q) => notifications.push(q),
  });

  // 1. Sondeo inicial: aparece la pregunta
  await manager.pollQuestions();
  assert.equal(notifications.length, 1);
  assert.equal(notifications[0].question_id, "q-10");

  // 2. Responder "Sí" (true)
  const answerResult = await manager.answerCurrentQuestion(true);
  assert.equal(answerResult.question.question_id, "q-10");
  assert.equal(answerResult.answer, true);

  // La UI fue notificada con null (la tarjeta desaparece)
  assert.equal(notifications.length, 2);
  assert.equal(notifications[1], null);
  assert.equal(manager.getCurrentQuestion(), null);

  // La pregunta quedó marcada como respondida en el almacenamiento
  const answeredSet = await store.getAnsweredQuestionIds();
  assert.ok(answeredSet.has(answeredKey({ question_id: "q-10" })));

  // La respuesta entró a la cola y se despachó al endpoint /api/messages
  assert.ok(dispatchedPayload);
  const decodedHeader = decodeHeader(dispatchedPayload.subarray(0, HEADER_SIZE));
  assert.equal(decodedHeader.msg_type, MessageType.RESPUESTA);
});

test("no se vuelve a mostrar una pregunta ya respondida", async () => {
  const store = new MemoryReportStore();
  const queue = createReportQueue({ store });
  store.meta.set("device_id", 0x99887766);

  const mockQuestions = [
    { question_id: "q-1", text: "¿Huele a gas?" },
  ];

  const mockFetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({ questions: mockQuestions }),
  });

  const notifications = [];
  const manager = createQuestionManager({
    store,
    queue,
    fetchFn: mockFetch,
    getDeviceIdFn: async () => store.getDeviceId(),
    onQuestionChange: (q) => notifications.push(q),
  });

  // 1. Sondeo inicial y respuesta
  await manager.pollQuestions();
  assert.equal(manager.getCurrentQuestion().question_id, "q-1");
  await manager.answerCurrentQuestion(false); // responde No
  assert.equal(manager.getCurrentQuestion(), null);

  // 2. Sondeos posteriores no vuelven a mostrar q-1
  const polledAgain = await manager.pollQuestions();
  assert.equal(polledAgain, null);
  assert.equal(manager.getCurrentQuestion(), null);

  // 3. Si llega una nueva pregunta q-2, esa sí se muestra
  mockQuestions.push({ question_id: "q-2", text: "¿Ves humo denso?" });
  const polledNew = await manager.pollQuestions();
  assert.ok(polledNew);
  assert.equal(polledNew.question_id, "q-2");
});

test("la respuesta enviada sin red queda Guardada y sale al volver la conexión", async () => {
  const store = new MemoryReportStore();
  let networkOnline = false;
  let sentPayload = null;

  const dynamicFetch = async (url, opts) => {
    if (url.startsWith("/api/questions")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          questions: [{ question_id: "q-offline", text: "¿Hay heridos cerca?" }],
        }),
      };
    }
    if (url === "/api/messages") {
      if (!networkOnline) {
        throw new Error("Red no disponible (offline)");
      }
      sentPayload = new Uint8Array(opts.body);
      return {
        status: 200,
        json: async () => ({ status: "recibido", duplicate: false }),
      };
    }
    throw new Error(`Ruta desconocida: ${url}`);
  };

  const queue = createReportQueue({
    store,
    fetchFn: dynamicFetch,
    clockFn: () => 1000,
  });
  store.meta.set("device_id", 0x44332211);

  const manager = createQuestionManager({
    store,
    queue,
    fetchFn: dynamicFetch,
    getDeviceIdFn: async () => store.getDeviceId(),
  });

  // 1. Cargar pregunta
  await manager.pollQuestions();
  assert.equal(manager.getCurrentQuestion().question_id, "q-offline");

  // 2. Responder SIN red (networkOnline = false)
  const { enqueuedItem } = await manager.answerCurrentQuestion(true);
  assert.ok(enqueuedItem);

  // En la cola debe quedar en estado GUARDADO debido al fallo de red
  const itemInStore = await store.get(enqueuedItem.id);
  assert.equal(itemInStore.status, ReportStatus.GUARDADO);
  assert.equal(sentPayload, null);

  // 3. Vuelve la conexión (networkOnline = true)
  networkOnline = true;

  // Al procesar la cola con tiempo posterior al backoff, la respuesta sale y finaliza en RECIBIDO
  const processed = await processQueue({
    store,
    fetchFn: dynamicFetch,
    clockFn: () => 10000,
  });

  assert.equal(processed.length, 1);
  assert.equal(processed[0].status, ReportStatus.RECIBIDO);
  assert.ok(sentPayload);

  const decoded = decodeHeader(sentPayload.subarray(0, HEADER_SIZE));
  assert.equal(decoded.msg_type, MessageType.RESPUESTA);

  const finalItem = await store.get(enqueuedItem.id);
  assert.equal(finalItem.status, ReportStatus.RECIBIDO);
});
