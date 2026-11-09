// Cola local de reportes con persistencia y reintentos automáticos.
// Soporta IndexedDB para el navegador y almacenamiento inyectable en memoria para node --test.

export const ReportStatus = Object.freeze({
  GUARDADO: "guardado",
  ENVIANDO: "enviando",
  RECIBIDO: "recibido",
});

export const RETRY_CONFIG = Object.freeze({
  BASE_DELAY_MS: 1000,
  MAX_DELAY_MS: 30000,
  DEFAULT_POLL_INTERVAL_MS: 5000,
});

/**
 * Calcula el tiempo de espera exponencial con tope y jitter.
 *
 * @param {number} attempt - Número de reintentos fallidos previos (0, 1, 2...)
 * @param {Object} [config]
 * @param {number} [config.baseDelayMs=1000]
 * @param {number} [config.maxDelayMs=30000]
 * @param {number} [config.jitter=0.1] - Factor de aleatoriedad (0 = sin jitter)
 * @param {() => number} [config.randomFn=Math.random]
 * @returns {number} Milisegundos para el siguiente reintento
 */
export function calculateBackoffDelay(
  attempt,
  {
    baseDelayMs = RETRY_CONFIG.BASE_DELAY_MS,
    maxDelayMs = RETRY_CONFIG.MAX_DELAY_MS,
    jitter = 0.1,
    randomFn = Math.random,
  } = {},
) {
  const exp = Math.min(maxDelayMs, baseDelayMs * Math.pow(2, Math.max(0, attempt)));
  if (jitter <= 0) return exp;
  const randFactor = 1 + (randomFn() * 2 - 1) * jitter;
  return Math.max(baseDelayMs, Math.round(exp * randFactor));
}

/**
 * Almacenamiento en memoria para pruebas unitarias sin navegador.
 */
export class MemoryReportStore {
  constructor() {
    this.items = new Map();
    this.meta = new Map();
  }

  async get(id) {
    return this.items.get(id) || null;
  }

  async put(item) {
    this.items.set(item.id, { ...item });
  }

  async getAll() {
    return Array.from(this.items.values());
  }

  async getPending() {
    return Array.from(this.items.values()).filter(
      (item) => item.status !== ReportStatus.RECIBIDO,
    );
  }

  async getDeviceId() {
    if (!this.meta.has("device_id")) {
      const randId = Math.floor(Math.random() * 0xffffffff);
      this.meta.set("device_id", randId);
    }
    return this.meta.get("device_id");
  }

  async getNextSeq() {
    const current = this.meta.get("seq") || 0;
    const next = current + 1;
    this.meta.set("seq", next);
    return next;
  }

  async saveLastLocation(location) {
    this.meta.set("last_location", location ? { ...location } : null);
  }

  async getLastLocation() {
    return this.meta.get("last_location") || null;
  }

  async saveLastBarrio(barrioCode) {
    this.meta.set("last_barrio", barrioCode);
  }

  async getLastBarrio() {
    return this.meta.get("last_barrio") ?? null;
  }
}

/**
 * Almacenamiento IndexedDB para el navegador en la PWA.
 */
export class IndexedDbReportStore {
  constructor(dbName = "guadua_citizen_db") {
    this.dbName = dbName;
    this.dbPromise = null;
  }

  async _getDb() {
    if (this.dbPromise) return this.dbPromise;

    if (typeof indexedDB === "undefined") {
      throw new Error("IndexedDB no está disponible en este entorno");
    }

    this.dbPromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(this.dbName, 1);
      request.onupgradeneeded = (e) => {
        const db = e.target.result;
        if (!db.objectStoreNames.contains("reports")) {
          db.createObjectStore("reports", { keyPath: "id" });
        }
        if (!db.objectStoreNames.contains("meta")) {
          db.createObjectStore("meta", { keyPath: "key" });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });

    return this.dbPromise;
  }

  async get(id) {
    const db = await this._getDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction("reports", "readonly");
      const store = tx.objectStore("reports");
      const req = store.get(id);
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => reject(req.error);
    });
  }

  async put(item) {
    const db = await this._getDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction("reports", "readwrite");
      const store = tx.objectStore("reports");
      const req = store.put(item);
      req.onsuccess = () => resolve();
      req.onerror = () => reject(req.error);
    });
  }

  async getAll() {
    const db = await this._getDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction("reports", "readonly");
      const store = tx.objectStore("reports");
      const req = store.getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
    });
  }

  async getPending() {
    const all = await this.getAll();
    return all.filter((item) => item.status !== ReportStatus.RECIBIDO);
  }

  async getDeviceId() {
    const db = await this._getDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction("meta", "readwrite");
      const store = tx.objectStore("meta");
      const req = store.get("device_id");
      req.onsuccess = () => {
        if (req.result && typeof req.result.value === "number") {
          resolve(req.result.value);
        } else {
          // Generar entero de 32 bits positivo (1 a 0xFFFFFFFF)
          const newId = (Math.floor(Math.random() * 0x7fffffff) + 1) >>> 0;
          store.put({ key: "device_id", value: newId });
          resolve(newId);
        }
      };
      req.onerror = () => reject(req.error);
    });
  }

  async getNextSeq() {
    const db = await this._getDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction("meta", "readwrite");
      const store = tx.objectStore("meta");
      const req = store.get("seq");
      req.onsuccess = () => {
        const current = (req.result && typeof req.result.value === "number") ? req.result.value : 0;
        const next = current + 1;
        store.put({ key: "seq", value: next });
        resolve(next);
      };
      req.onerror = () => reject(req.error);
    });
  }

  async saveLastLocation(location) {
    const db = await this._getDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction("meta", "readwrite");
      const store = tx.objectStore("meta");
      const req = store.put({ key: "last_location", value: location });
      req.onsuccess = () => resolve();
      req.onerror = () => reject(req.error);
    });
  }

  async getLastLocation() {
    const db = await this._getDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction("meta", "readonly");
      const store = tx.objectStore("meta");
      const req = store.get("last_location");
      req.onsuccess = () => resolve(req.result ? req.result.value : null);
      req.onerror = () => reject(req.error);
    });
  }

  async saveLastBarrio(barrioCode) {
    const db = await this._getDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction("meta", "readwrite");
      const store = tx.objectStore("meta");
      const req = store.put({ key: "last_barrio", value: barrioCode });
      req.onsuccess = () => resolve();
      req.onerror = () => reject(req.error);
    });
  }

  async getLastBarrio() {
    const db = await this._getDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction("meta", "readonly");
      const store = tx.objectStore("meta");
      const req = store.get("last_barrio");
      req.onsuccess = () => resolve(req.result ? req.result.value : null);
      req.onerror = () => reject(req.error);
    });
  }
}

/**
 * Procesa la cola de reportes pendientes.
 *
 * @param {Object} options
 * @param {MemoryReportStore | IndexedDbReportStore} options.store
 * @param {(url: string, opts: any) => Promise<any>} options.fetchFn
 * @param {() => number} [options.clockFn=Date.now]
 * @param {string} [options.endpoint="/api/messages"]
 * @param {(item: Object) => void} [options.onItemUpdated]
 * @returns {Promise<Array<Object>>} Lista de items procesados con su estado
 */
export async function processQueue({
  store,
  fetchFn,
  clockFn = Date.now,
  endpoint = "/api/messages",
  onItemUpdated = null,
}) {
  const pending = await store.getPending();
  const now = clockFn();
  const processed = [];

  // Los de mayor prioridad (SOS escombros) se procesan primero; a igual prioridad, el más viejo primero
  pending.sort((a, b) => {
    const prioA = Number(a?.priority ?? 0);
    const prioB = Number(b?.priority ?? 0);
    if (prioA !== prioB) return prioB - prioA;
    return Number(a?.createdAt ?? 0) - Number(b?.createdAt ?? 0);
  });

  for (const item of pending) {
    // Si ya está recibido, no se reenvía nunca
    if (item.status === ReportStatus.RECIBIDO) continue;

    // Si aún no vence el tiempo de reintento exponencial, esperar
    if (item.nextRetry && now < item.nextRetry) {
      continue;
    }

    // Marcar como enviando
    item.status = ReportStatus.ENVIANDO;
    await store.put(item);
    if (onItemUpdated) onItemUpdated(item);

    try {
      const res = await fetchFn(endpoint, {
        method: "POST",
        body: item.rawBytes,
        headers: {
          "Content-Type": "application/octet-stream",
        },
      });

      if (res && res.status === 200) {
        const ack = typeof res.json === "function" ? await res.json() : res.data;
        // 200 con recibido o duplicate: true cuenta como Recibido
        item.status = ReportStatus.RECIBIDO;
        item.ack = ack || { status: "recibido", duplicate: false };
        item.sentAt = clockFn();
      } else {
        // Falló la respuesta del servidor
        item.status = ReportStatus.GUARDADO;
        item.attempts = (item.attempts || 0) + 1;
        const delay = calculateBackoffDelay(item.attempts);
        item.nextRetry = clockFn() + delay;
      }
    } catch {
      // Error de red / sin conexión
      item.status = ReportStatus.GUARDADO;
      item.attempts = (item.attempts || 0) + 1;
      const delay = calculateBackoffDelay(item.attempts);
      item.nextRetry = clockFn() + delay;
    }

    await store.put(item);
    if (onItemUpdated) onItemUpdated(item);
    processed.push(item);
  }

  return processed;
}

/**
 * Crea una instancia de la cola conectada al almacenamiento y eventos de red.
 */
export function createReportQueue({
  store = (typeof indexedDB !== "undefined" ? new IndexedDbReportStore() : new MemoryReportStore()),
  fetchFn = (typeof fetch !== "undefined" ? fetch : null),
  clockFn = Date.now,
  endpoint = "/api/messages",
  onStatusChange = null,
} = {}) {
  let isRunning = false;
  let timerId = null;

  async function enqueue({ headerFields, fullBytes, priority = null }) {
    const hexDevice = Number(headerFields.device_id).toString(16).padStart(8, "0");
    const id = `${hexDevice}-${headerFields.seq}`;
    const itemPriority = priority !== null
      ? priority
      : (headerFields?.msg_type === 1 ? 1 : 0);

    const item = {
      id,
      deviceId: headerFields.device_id,
      seq: headerFields.seq,
      status: ReportStatus.GUARDADO,
      createdAt: clockFn(),
      attempts: 0,
      nextRetry: 0,
      rawBytes: fullBytes,
      headerFields,
      priority: itemPriority,
      ack: null,
    };

    await store.put(item);
    if (onStatusChange) onStatusChange(item);

    // Intentar envío inmediato
    await triggerProcess();
    return item;
  }

  async function triggerProcess() {
    if (isRunning) return;
    isRunning = true;
    try {
      await processQueue({
        store,
        fetchFn,
        clockFn,
        endpoint,
        onItemUpdated: onStatusChange,
      });
    } finally {
      isRunning = false;
    }
  }

  function start(pollIntervalMs = RETRY_CONFIG.DEFAULT_POLL_INTERVAL_MS) {
    if (typeof window !== "undefined") {
      window.addEventListener("online", () => triggerProcess());
    }
    if (!timerId) {
      timerId = setInterval(() => triggerProcess(), pollIntervalMs);
    }
  }

  function stop() {
    if (timerId) {
      clearInterval(timerId);
      timerId = null;
    }
  }

  return {
    store,
    enqueue,
    triggerProcess,
    start,
    stop,
  };
}
