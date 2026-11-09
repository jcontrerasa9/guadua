// Pruebas unitarias para el modo escombros y baliza acústica (D1).
// Se ejecutan con: node --test "tests/js/*.test.mjs"

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  BEACON_FREQUENCY_HZ,
  BEACON_PAUSE_DURATION_MS,
  BEACON_PULSE_DURATION_MS,
  buildDebrisSosBinary,
  createAcousticBeacon,
  createTapDetector,
  EXIT_HOLD_DURATION_MS,
  PROVISIONAL_ADVICE_TEXT,
  RAPID_TAP_COUNT,
  RAPID_TAP_WINDOW_MS,
} from "../../citizen/debris.js";
import { decodeHeader, Flag, HEADER_SIZE, Incident, MessageType, People } from "../../citizen/protocol/header.js";
import {
  createReportQueue,
  MemoryReportStore,
  processQueue,
  ReportStatus,
} from "../../citizen/queue.js";

// --- Mock de Web Audio API para verificar la baliza en Node -------------------

class MockAudioContext {
  constructor() {
    this.state = "suspended";
    this.currentTime = 10.0;
    this.oscillators = [];
    this.gains = [];
    this.destination = { type: "speaker" };
  }

  async resume() {
    this.state = "running";
  }

  async close() {
    this.state = "closed";
  }

  createOscillator() {
    const osc = {
      type: "sine",
      frequency: {
        value: 0,
        setValueAtTime: (val, time) => {
          osc.frequency.value = val;
          osc.frequency.time = time;
        },
      },
      startTime: null,
      stopTime: null,
      connect: (node) => {
        osc.target = node;
      },
      start: (t) => {
        osc.startTime = t;
      },
      stop: (t) => {
        osc.stopTime = t;
      },
    };
    this.oscillators.push(osc);
    return osc;
  }

  createGain() {
    const gain = {
      gain: {
        value: 1,
        setValueAtTime: () => {},
        linearRampToValueAtTime: () => {},
      },
      connect: (node) => {
        gain.target = node;
      },
    };
    this.gains.push(gain);
    return gain;
  }
}

// --- Pruebas del detector de toques rápidos -----------------------------------

test("5 toques dentro de la ventana activan el detector", () => {
  let triggered = false;
  const detector = createTapDetector({
    targetCount: 5,
    windowMs: 2500,
    onTrigger: () => {
      triggered = true;
    },
  });

  // Tiempos espaciados dentro de la ventana de 2500 ms (t = 0, 300, 600, 900, 1200)
  assert.equal(detector.registerTap(1000), false);
  assert.equal(detector.registerTap(1300), false);
  assert.equal(detector.registerTap(1600), false);
  assert.equal(detector.registerTap(1900), false);
  assert.equal(detector.registerTap(2200), true);

  assert.equal(triggered, true);
  // Tras activarse, el contador se reinicia a 0
  assert.equal(detector.getTapCount(), 0);
});

test("4 toques dentro de la ventana NO activan el detector", () => {
  let triggered = false;
  const detector = createTapDetector({
    targetCount: 5,
    windowMs: 2500,
    onTrigger: () => {
      triggered = true;
    },
  });

  assert.equal(detector.registerTap(1000), false);
  assert.equal(detector.registerTap(1400), false);
  assert.equal(detector.registerTap(1800), false);
  assert.equal(detector.registerTap(2200), false);

  assert.equal(triggered, false);
  assert.equal(detector.getTapCount(), 4);
});

test("toques lentos fuera de la ventana de tiempo NO activan el detector", () => {
  let triggered = false;
  const detector = createTapDetector({
    targetCount: 5,
    windowMs: 2500,
    onTrigger: () => {
      triggered = true;
    },
  });

  // Cada toque espaciado por 1000 ms: al 5to toque, los primeros ya cayeron fuera de la ventana de 2500 ms
  assert.equal(detector.registerTap(1000), false); // ventana: [1000]
  assert.equal(detector.registerTap(2000), false); // ventana: [1000, 2000]
  assert.equal(detector.registerTap(3000), false); // ventana: [1000, 2000, 3000]
  assert.equal(detector.registerTap(4000), false); // t=4000: ventana es >= 1500 -> [2000, 3000, 4000] (3 toques)
  assert.equal(detector.registerTap(5000), false); // t=5000: ventana es >= 2500 -> [3000, 4000, 5000] (3 toques)
  assert.equal(detector.registerTap(6000), false); // t=6000: ventana es >= 3500 -> [4000, 5000, 6000] (3 toques)

  assert.equal(triggered, false);
  assert.equal(detector.getTapCount(), 3);
});

// --- Pruebas del mensaje SOS escombros (exactamente 21 bytes y tipo 1) ----------

test("el SOS mide exactamente 21 bytes y decodeHeader lo reconoce como tipo 1", () => {
  const deviceId = 0xa1b2c3d4;
  const seq = 7;
  const deviceTime = 86400;

  const { fullBytes, headerFields } = buildDebrisSosBinary({
    deviceId,
    seq,
    deviceTime,
    lastLocation: { geohash: "d2fkcdc" },
  });

  // Longitud estricta
  assert.equal(fullBytes.length, HEADER_SIZE);
  assert.equal(fullBytes.length, 21);

  // Decodificación contractual con header.js
  const decoded = decodeHeader(fullBytes);
  assert.equal(decoded.msg_type, MessageType.SOS_ESCOMBROS);
  assert.equal(decoded.msg_type, 1);
  assert.equal(decoded.device_id, deviceId);
  assert.equal(decoded.seq, seq);
  assert.equal(decoded.device_time, deviceTime);
  assert.equal(decoded.incident, Incident.ATRAPADOS);
  assert.equal(decoded.people, People.UNA);

  // Banderas obligatorias: observación directa, batería crítica, atrapados y GPS
  assert.ok(decoded.flags & Flag.OBSERVACION_DIRECTA);
  assert.ok(decoded.flags & Flag.BATERIA_CRITICA);
  assert.ok(decoded.flags & Flag.ATRAPADOS);
  assert.ok(decoded.flags & Flag.GPS);
  assert.equal(decoded.geohash, "d2fkcdc");
  assert.equal(decoded.barrio, null);
});

test("la última ubicación conocida se usa si existe y, si no, el barrio", () => {
  const deviceId = 0x12345678;
  const seq = 1;

  // 1. Con última ubicación conocida por GPS (geohash)
  const sosConGps = buildDebrisSosBinary({
    deviceId,
    seq,
    lastLocation: { geohash: "d2fkcdc", lat: 5.069, lon: -75.518 },
    fallbackBarrioCode: 4,
  });

  const decGps = decodeHeader(sosConGps.fullBytes);
  assert.equal(decGps.geohash, "d2fkcdc");
  assert.equal(decGps.barrio, null);
  assert.ok(decGps.flags & Flag.GPS);

  // 2. Sin GPS pero con barrio guardado en lastLocation
  const sosConBarrio = buildDebrisSosBinary({
    deviceId,
    seq,
    lastLocation: { barrio: 3 }, // Chipre
    fallbackBarrioCode: 1,
  });

  const decBarrio = decodeHeader(sosConBarrio.fullBytes);
  assert.equal(decBarrio.geohash, null);
  assert.equal(decBarrio.barrio, 3);
  assert.equal(decBarrio.flags & Flag.GPS, 0);

  // 3. Sin ningún lastLocation (usa el fallback de barrio)
  const sosFallback = buildDebrisSosBinary({
    deviceId,
    seq,
    lastLocation: null,
    fallbackBarrioCode: 6, // Milán
  });

  const decFallback = decodeHeader(sosFallback.fullBytes);
  assert.equal(decFallback.geohash, null);
  assert.equal(decFallback.barrio, 6);
  assert.equal(decFallback.flags & Flag.GPS, 0);
});

// --- Pruebas de la baliza acústica y patrón periódico -------------------------

test("el patrón de pulsos de la baliza acústica es el esperado (~3000 Hz, duración y pausa)", async () => {
  assert.equal(BEACON_FREQUENCY_HZ, 3000);
  assert.equal(BEACON_PULSE_DURATION_MS, 250);
  assert.equal(BEACON_PAUSE_DURATION_MS, 750);

  const beacon = createAcousticBeacon({
    audioContextClass: MockAudioContext,
    frequency: 3000,
    pulseDurationMs: 250,
    pauseDurationMs: 750,
  });

  assert.equal(beacon.isRunning(), false);
  beacon.start();
  assert.equal(beacon.isRunning(), true);

  // Esperar un ciclo breve
  await new Promise((resolve) => setTimeout(resolve, 50));

  beacon.stop();
  assert.equal(beacon.isRunning(), false);
});

// --- Pruebas de prioridad del SOS en la cola ----------------------------------

test("el SOS entra a la misma cola y sale primero por prioridad", async () => {
  const store = new MemoryReportStore();
  const dispatchOrder = [];

  const mockFetch = async (url, opts) => {
    // Leer el primer byte del cuerpo para saber qué mensaje se envió
    const view = new DataView(opts.body.buffer, opts.body.byteOffset);
    const msgType = view.getUint8(0) & 0x0f;
    dispatchOrder.push(msgType);
    return {
      status: 200,
      json: async () => ({ status: "recibido", duplicate: false }),
    };
  };

  // 1. Encolar reporte ordinario (msg_type: 0, prioridad 0) a t=100
  const normalReport = new Uint8Array(21);
  normalReport[0] = (1 << 4) | MessageType.REPORTE;
  await store.put({
    id: "device-1",
    status: ReportStatus.GUARDADO,
    createdAt: 100,
    rawBytes: normalReport,
    headerFields: { msg_type: MessageType.REPORTE },
    priority: 0,
  });

  // 2. Encolar SOS escombros más tarde a t=200 con prioridad alta (priority 1)
  const { fullBytes: sosBytes, headerFields: sosFields } = buildDebrisSosBinary({
    deviceId: 99,
    seq: 1,
    fallbackBarrioCode: 1,
  });
  await store.put({
    id: "device-2",
    status: ReportStatus.GUARDADO,
    createdAt: 200,
    rawBytes: sosBytes,
    headerFields: sosFields,
    priority: 1,
  });

  // 3. Procesar cola
  await processQueue({
    store,
    fetchFn: mockFetch,
    clockFn: () => 300,
  });

  // El SOS (tipo 1) debe haberse despachado antes que el reporte normal (tipo 0)
  assert.deepEqual(dispatchOrder, [MessageType.SOS_ESCOMBROS, MessageType.REPORTE]);
});

// --- Pruebas de texto provisional de autoprotección ---------------------------

test("el consejo breve de autoprotección está presente como texto provisional", () => {
  assert.ok(typeof PROVISIONAL_ADVICE_TEXT === "string");
  assert.ok(PROVISIONAL_ADVICE_TEXT.length > 20);
  assert.ok(PROVISIONAL_ADVICE_TEXT.includes("fuego"));
  assert.ok(PROVISIONAL_ADVICE_TEXT.includes("pared") || PROVISIONAL_ADVICE_TEXT.includes("muro") || PROVISIONAL_ADVICE_TEXT.includes("tubo"));
});
