// Monitoreo de batería y cálculo de estado crítico para la PWA de Guadua (SARD)
// Diseño: docs/diseno.md §3.1 (punto 6) y §6.6 (batería crítica envía solo cabecera de 21 B).

export const CRITICAL_BATTERY_THRESHOLD = 15; // 15% de batería o menos se considera crítico

/**
 * Calcula el estado de batería a partir del nivel y estado de carga.
 *
 * @param {Object} params
 * @param {number | null} [params.level=null] - Nivel entre 0.0 y 1.0 (o porcentaje 0-100)
 * @param {boolean} [params.charging=false] - Si el dispositivo está conectado a la corriente
 * @returns {{ levelPercent: number | null, isCritical: boolean, isCharging: boolean, icon: string, text: string }}
 */
export function computeBatteryStatus({ level = null, charging = false } = {}) {
  if (level === null || level === undefined || Number.isNaN(Number(level))) {
    return {
      levelPercent: null,
      isCritical: false,
      isCharging: false,
      icon: "🔋",
      text: "🔋 --",
    };
  }

  const num = Number(level);
  const levelPercent = Math.max(
    0,
    Math.min(100, Math.round(num <= 1.0 && num >= 0 ? num * 100 : num)),
  );
  const isCharging = Boolean(charging);
  const isCritical = !isCharging && levelPercent <= CRITICAL_BATTERY_THRESHOLD;

  let icon = "🔋";
  if (isCharging) {
    icon = "⚡";
  } else if (isCritical) {
    icon = "🪫";
  }

  return {
    levelPercent,
    isCritical,
    isCharging,
    icon,
    text: `${icon} ${levelPercent}%`,
  };
}

/**
 * Analiza parámetros de URL para permitir simular niveles de batería
 * en pruebas del jurado o navegadores sin Battery Status API (iOS, Firefox).
 *
 * Ejemplos: `?battery=12`, `?bateria=8`, `?battery=charging`
 *
 * @param {string} [searchString=""]
 * @returns {{ level: number, charging: boolean } | null}
 */
export function parseSimulatedBattery(searchString = "") {
  if (!searchString || typeof searchString !== "string") {
    return null;
  }

  try {
    const params = new URLSearchParams(searchString.startsWith("?") ? searchString : `?${searchString}`);
    const rawVal = params.get("battery") ?? params.get("bateria");
    if (!rawVal) return null;

    const charging = params.get("charging") === "1" || params.get("charging") === "true";
    const num = parseFloat(rawVal);
    if (!Number.isNaN(num)) {
      return { level: num, charging };
    }
  } catch {
    // Si la cadena no es válida, continuar sin simulación
  }

  return null;
}

/**
 * Configura el monitoreo de batería:
 * 1. Prioriza simulación si se pasa en URL (para pruebas y navegadores sin API como Safari/Firefox).
 * 2. Si existe la API nativa `navigator.getBattery`, se suscribe a los cambios.
 * 3. Si no hay soporte ni simulación, reporta `null` ("🔋 --") honestamente.
 *
 * @param {Object} options
 * @param {(status: ReturnType<typeof computeBatteryStatus>) => void} options.onBatteryChange
 * @param {(() => Promise<any>) | null} [options.getBatteryFn=null]
 * @param {string} [options.search=""]
 * @returns {(() => void)} Función de limpieza (desuscripción)
 */
export function setupBatteryMonitor({ onBatteryChange, getBatteryFn = null, search = "" }) {
  if (typeof onBatteryChange !== "function") {
    return () => {};
  }

  // 1. Simulación por parámetro de URL
  const searchVal = search || (typeof window !== "undefined" && window.location ? window.location.search : "");
  const simulated = parseSimulatedBattery(searchVal);
  if (simulated) {
    const status = computeBatteryStatus(simulated);
    onBatteryChange(status);
    return () => {};
  }

  // 2. API nativa navigator.getBattery
  const resolver =
    getBatteryFn ??
    (typeof navigator !== "undefined" && typeof navigator.getBattery === "function"
      ? () => navigator.getBattery()
      : null);

  if (!resolver) {
    onBatteryChange(computeBatteryStatus({ level: null }));
    return () => {};
  }

  let active = true;
  let batteryRef = null;
  let updateListener = null;

  resolver()
    .then((battery) => {
      if (!active || !battery) return;
      batteryRef = battery;

      updateListener = () => {
        if (!active) return;
        const status = computeBatteryStatus({
          level: battery.level,
          charging: battery.charging,
        });
        onBatteryChange(status);
      };

      updateListener();
      if (typeof battery.addEventListener === "function") {
        battery.addEventListener("levelchange", updateListener);
        battery.addEventListener("chargingchange", updateListener);
      }
    })
    .catch(() => {
      if (active) {
        onBatteryChange(computeBatteryStatus({ level: null }));
      }
    });

  return () => {
    active = false;
    if (batteryRef && typeof batteryRef.removeEventListener === "function" && updateListener) {
      batteryRef.removeEventListener("levelchange", updateListener);
      batteryRef.removeEventListener("chargingchange", updateListener);
    }
  };
}
