// Modo escombros y baliza acústica (~3000 Hz) - Guadua (SARD)
// CONTRATO y diseño: ver docs/diseno.md §3.1, §5.2 y §6.1.
// No modifica protocol/header.js.

import {
  encodeHeader,
  EPOCH_UNIX,
  Flag,
  HEADER_SIZE,
  Incident,
  MessageType,
  People,
} from "./protocol/header.js";

// --- Constantes con nombre ---------------------------------------------------

export const RAPID_TAP_COUNT = 5;
export const RAPID_TAP_WINDOW_MS = 2500;

export const BEACON_FREQUENCY_HZ = 3000;
export const BEACON_PULSE_DURATION_MS = 250;
export const BEACON_PAUSE_DURATION_MS = 750;

export const EXIT_HOLD_DURATION_MS = 3000;

// TEXTO PROVISIONAL: validar con recomendaciones oficiales de autoprotección
export const PROVISIONAL_ADVICE_TEXT =
  "No enciendas fuego ni fósforos. Cubre tu nariz y boca. Si escuchas rescatistas o perros cerca, golpea una tubería o muro para que te ubiquen.";

/**
 * Calcula los segundos transcurridos desde EPOCH_UNIX para la hora del dispositivo.
 *
 * @param {number} [nowUnixSeconds=Date.now() / 1000]
 * @returns {number}
 */
export function getDebrisDeviceTime(nowUnixSeconds = Date.now() / 1000) {
  const diff = Math.floor(nowUnixSeconds - EPOCH_UNIX);
  return Math.max(0, diff);
}

// --- Detector puro de toques rápidos ------------------------------------------

/**
 * Crea un detector de toques rápidos en una ventana deslizante de tiempo.
 *
 * @param {Object} [options]
 * @param {number} [options.targetCount=RAPID_TAP_COUNT] - Cantidad de toques requeridos
 * @param {number} [options.windowMs=RAPID_TAP_WINDOW_MS] - Ventana de tiempo máxima
 * @param {() => void} [options.onTrigger] - Callback al detectar la ráfaga
 * @returns {Object}
 */
export function createTapDetector({
  targetCount = RAPID_TAP_COUNT,
  windowMs = RAPID_TAP_WINDOW_MS,
  onTrigger = null,
} = {}) {
  let tapTimestamps = [];

  function registerTap(now = Date.now()) {
    // Filtrar toques fuera de la ventana
    tapTimestamps = tapTimestamps.filter((t) => t >= now - windowMs);
    tapTimestamps.push(now);

    if (tapTimestamps.length >= targetCount) {
      tapTimestamps = []; // Reiniciar tras disparo
      if (typeof onTrigger === "function") {
        onTrigger();
      }
      return true;
    }

    return false;
  }

  function reset() {
    tapTimestamps = [];
  }

  function getTapCount() {
    return tapTimestamps.length;
  }

  return {
    registerTap,
    reset,
    getTapCount,
    targetCount,
    windowMs,
  };
}

// --- Armado del binario SOS escombros (exactamente 21 bytes) ------------------

/**
 * Construye el paquete binario de SOS escombros (tipo 1).
 *
 * Reglas (docs/diseno.md §5.2 y §6.1):
 * - Tamaño: exactamente 21 bytes (sin texto).
 * - msg_type: MessageType.SOS_ESCOMBROS (1).
 * - incident: Incident.ATRAPADOS (0).
 * - people: People.UNA (0).
 * - flags: OBSERVACION_DIRECTA | BATERIA_CRITICA | ATRAPADOS (+ GPS si hay geohash).
 * - Ubicación: Si lastLocation tiene un geohash válido (7 caracteres), se activa Flag.GPS
 *   y se asigna dicho geohash. Si no, se usa el barrio guardado (o fallback) sin Flag.GPS.
 *
 * @param {Object} params
 * @param {number} params.deviceId - ID numérico de 32 bits del dispositivo
 * @param {number} params.seq - Consecutivo de 32 bits
 * @param {number} [params.deviceTime] - Segundos desde EPOCH_UNIX
 * @param {Object | null} [params.lastLocation=null] - { geohash?: string, barrio?: number }
 * @param {number} [params.fallbackBarrioCode=1] - Código de barrio de respaldo
 * @returns {{ headerFields: Object, fullBytes: Uint8Array }}
 */
export function buildDebrisSosBinary({
  deviceId,
  seq,
  deviceTime = null,
  lastLocation = null,
  fallbackBarrioCode = 1,
}) {
  const dTime = deviceTime ?? getDebrisDeviceTime();

  let hasGps = false;
  let geohash = null;
  let barrio = null;

  if (lastLocation && typeof lastLocation.geohash === "string" && lastLocation.geohash.length === 7) {
    hasGps = true;
    geohash = lastLocation.geohash.toLowerCase();
  } else if (lastLocation && Number.isInteger(lastLocation.barrio)) {
    barrio = lastLocation.barrio;
  } else {
    barrio = fallbackBarrioCode;
  }

  let flags = Flag.OBSERVACION_DIRECTA | Flag.BATERIA_CRITICA | Flag.ATRAPADOS;
  if (hasGps) {
    flags |= Flag.GPS;
  }

  const headerFields = {
    msg_type: MessageType.SOS_ESCOMBROS,
    device_id: deviceId,
    seq,
    device_time: dTime,
    incident: Incident.ATRAPADOS,
    people: People.UNA,
    flags,
    geohash: hasGps ? geohash : null,
    barrio: hasGps ? null : barrio,
  };

  const fullBytes = encodeHeader(headerFields);

  if (fullBytes.length !== HEADER_SIZE) {
    throw new Error(`El SOS escombros debe medir exactamente ${HEADER_SIZE} B, mide ${fullBytes.length}`);
  }

  return {
    headerFields,
    fullBytes,
  };
}

// --- Baliza acústica (Web Audio API a ~3000 Hz) --------------------------------

/**
 * Administrador de la baliza acústica con pulsos periódicos.
 * Arranca únicamente con un gesto del usuario (exigido por políticas de audio de navegadores).
 *
 * @param {Object} [options]
 * @param {any} [options.audioContextClass] - Clase AudioContext o mock inyectable
 * @param {number} [options.frequency=BEACON_FREQUENCY_HZ] - Frecuencia del tono
 * @param {number} [options.pulseDurationMs=BEACON_PULSE_DURATION_MS]
 * @param {number} [options.pauseDurationMs=BEACON_PAUSE_DURATION_MS]
 * @returns {Object}
 */
export function createAcousticBeacon({
  audioContextClass = (typeof window !== "undefined" ? (window.AudioContext || window.webkitAudioContext) : null),
  frequency = BEACON_FREQUENCY_HZ,
  pulseDurationMs = BEACON_PULSE_DURATION_MS,
  pauseDurationMs = BEACON_PAUSE_DURATION_MS,
} = {}) {
  let audioCtx = null;
  let timerId = null;
  let active = false;
  const cycleMs = pulseDurationMs + pauseDurationMs;

  function _playSinglePulse() {
    if (!active || !audioCtx) return;

    try {
      if (audioCtx.state === "suspended") {
        audioCtx.resume().catch(() => {});
      }

      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();

      osc.type = "sine";
      osc.frequency.setValueAtTime(frequency, audioCtx.currentTime);

      // Envolvente de volumen para evitar chasquidos (clicks)
      const now = audioCtx.currentTime;
      const durationSec = pulseDurationMs / 1000;
      gain.gain.setValueAtTime(0, now);
      gain.gain.linearRampToValueAtTime(0.8, now + 0.015);
      gain.gain.setValueAtTime(0.8, now + durationSec - 0.015);
      gain.gain.linearRampToValueAtTime(0, now + durationSec);

      osc.connect(gain);
      gain.connect(audioCtx.destination);

      osc.start(now);
      osc.stop(now + durationSec);
    } catch (err) {
      console.warn("Error emitiendo pulso de baliza:", err);
    }
  }

  function start() {
    if (active) return;
    active = true;

    if (audioContextClass) {
      try {
        if (!audioCtx || audioCtx.state === "closed") {
          audioCtx = new audioContextClass();
        }
        if (audioCtx.state === "suspended") {
          audioCtx.resume().catch(() => {});
        }
      } catch (err) {
        console.warn("No se pudo iniciar AudioContext:", err);
      }
    }

    // Emitir el primer pulso inmediatamente
    _playSinglePulse();

    // Programar repetición periódica
    timerId = setInterval(() => {
      _playSinglePulse();
    }, cycleMs);
  }

  function stop() {
    active = false;
    if (timerId) {
      clearInterval(timerId);
      timerId = null;
    }
    if (audioCtx && audioCtx.state !== "closed") {
      try {
        audioCtx.close().catch(() => {});
      } catch {}
      audioCtx = null;
    }
  }

  function isRunning() {
    return active;
  }

  return {
    start,
    stop,
    isRunning,
    frequency,
    pulseDurationMs,
    pauseDurationMs,
    cycleMs,
  };
}

// --- Gestión de WakeLock de pantalla ------------------------------------------

/**
 * Control del bloqueo de apagado de pantalla (WakeLock).
 *
 * Limitación técnica declarada en docs/diseno.md §6.1.5:
 * Una PWA no puede apagar la pantalla ni mantener el audio si el teléfono se bloquea.
 * Por ello, la pantalla permanece encendida con fondo negro y se solicita wakeLock si está disponible.
 *
 * @param {Object} [options]
 * @param {any} [options.navigatorRef] - Referencia al objeto navigator
 * @returns {Object}
 */
export function createWakeLockManager({
  navigatorRef = (typeof navigator !== "undefined" ? navigator : null),
} = {}) {
  let sentinel = null;
  let requested = false;

  async function acquire() {
    requested = true;
    if (!navigatorRef || !navigatorRef.wakeLock) return false;

    try {
      sentinel = await navigatorRef.wakeLock.request("screen");
      sentinel.addEventListener("release", () => {
        sentinel = null;
      });
      return true;
    } catch (err) {
      console.warn("WakeLock no disponible o denegado:", err);
      return false;
    }
  }

  async function release() {
    requested = false;
    if (sentinel) {
      try {
        await sentinel.release();
      } catch {}
      sentinel = null;
    }
  }

  function isAcquired() {
    return Boolean(sentinel && !sentinel.released);
  }

  return {
    acquire,
    release,
    isAcquired,
  };
}
