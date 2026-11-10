// Módulo cliente de simulación y convergencia para el tablero del jurado (D9).
// Permite controlar perfiles del canal, inyectar ráfagas/radio, apagar nodos y
// verificar la convergencia de hashes entre los nodos A, B y C.

export const DEFAULT_NODE_HASHES = {
  A: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  B: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  C: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
};

export const DESYNC_NODE_HASH =
  "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1";

/**
 * Evalúa si los hashes de los nodos A, B y C han convergido.
 *
 * @param {Record<string, string>} hashesMap - Objeto con { [nodeId]: hashStr }
 * @param {string[]} [expectedNodes=["A", "B", "C"]] - Nodos que deben coincidir
 * @returns {{ converged: boolean, hashes: Record<string, string>, commonHash: string | null, message: string }}
 */
export function checkConvergence(hashesMap, expectedNodes = ["A", "B", "C"]) {
  if (!hashesMap || typeof hashesMap !== "object") {
    return {
      converged: false,
      hashes: {},
      commonHash: null,
      message: "Sin datos de hashes",
    };
  }

  const hashes = { ...hashesMap };
  const values = [];

  for (const nodeId of expectedNodes) {
    const val = hashes[nodeId];
    if (typeof val !== "string" || val.trim() === "") {
      return {
        converged: false,
        hashes,
        commonHash: null,
        message: `Falta hash para nodo ${nodeId}`,
      };
    }
    values.push(val.trim());
  }

  const first = values[0];
  const allMatch = values.every((v) => v === first);

  if (allMatch) {
    return {
      converged: true,
      hashes,
      commonHash: first,
      message: `3/3 nodos convergidos (${first.slice(0, 8)}…)`,
    };
  }

  return {
    converged: false,
    hashes,
    commonHash: null,
    message: "Divergencia entre nodos (sincronizando)",
  };
}

/**
 * Cliente de simulación en memoria para desarrollo, pruebas y modo offline.
 */
export class MockSimulationClient {
  constructor(initialState = {}) {
    this.profile = initialState.profile || "wifi";
    this.burstCount = initialState.burstCount || 0;
    this.radioClips = initialState.radioClips ? [...initialState.radioClips] : [];
    this.killedNodes = new Set(initialState.killedNodes || []);
    this.hashes = initialState.hashes
      ? { ...initialState.hashes }
      : { ...DEFAULT_NODE_HASHES };
    this.history = [];
  }

  async setProfile(profileName) {
    const valid = ["wifi", "radio_lora", "sms"];
    const normalized = String(profileName).toLowerCase();
    this.profile = valid.includes(normalized) ? normalized : profileName;
    this.history.push({
      action: "setProfile",
      profile: this.profile,
      timestamp: Date.now(),
    });
    return { ok: true, profile: this.profile };
  }

  async burst(n = 10) {
    const count = Number(n) || 10;
    this.burstCount += count;
    this.history.push({
      action: "burst",
      count,
      totalBursts: this.burstCount,
      timestamp: Date.now(),
    });
    return { ok: true, count, totalBursts: this.burstCount };
  }

  async injectRadio(clipId = "clip_colapso_dos_incidentes") {
    const clip = String(clipId);
    this.radioClips.push(clip);
    this.history.push({
      action: "injectRadio",
      clipId: clip,
      timestamp: Date.now(),
    });
    return { ok: true, clipId: clip };
  }

  async killNode(nodeId = "A") {
    const target = String(nodeId).toUpperCase();
    let isKilledNow = false;

    if (this.killedNodes.has(target)) {
      this.killedNodes.delete(target);
      isKilledNow = false;
    } else {
      this.killedNodes.add(target);
      isKilledNow = true;
    }

    this.history.push({
      action: isKilledNow ? "killNode" : "reviveNode",
      nodeId: target,
      killed: isKilledNow,
      timestamp: Date.now(),
    });

    return { ok: true, nodeId: target, killed: isKilledNow };
  }

  async getHashes() {
    const result = { ...this.hashes };
    for (const killed of this.killedNodes) {
      if (killed in result) {
        result[killed] = DESYNC_NODE_HASH;
      }
    }
    return result;
  }

  setMockHashes(hashesMap) {
    this.hashes = { ...hashesMap };
  }

  reset() {
    this.profile = "wifi";
    this.burstCount = 0;
    this.radioClips = [];
    this.killedNodes.clear();
    this.hashes = { ...DEFAULT_NODE_HASHES };
    this.history = [];
  }
}

/** Nombres de perfil del tablero → nombres del proxy de fallos (channel/profiles.py). */
export const PROXY_PROFILES = { wifi: "wifi", radio_lora: "lora", sms: "sms" };

/**
 * Cliente HTTP real de la barra: perfil y ráfaga en el proxy de fallos, radio y apagado
 * simulado en los nodos, hashes con GET /api/digest de cada nodo.
 */
export class HttpSimulationClient {
  constructor(options = {}) {
    this.proxyUrl = (options.proxyUrl || "").replace(/\/$/, "");
    this.nodeUrls = options.nodeUrls || {
      A: "http://localhost:8000",
      B: "http://localhost:8001",
      C: "http://localhost:8002",
    };
    this.fetchFn = options.fetchFn || (typeof fetch !== "undefined" ? fetch.bind(globalThis) : null);
    this.killedNodes = new Set();
  }

  async _call(url, options = {}) {
    if (!this.fetchFn) throw new Error("fetch no disponible");
    try {
      const res = await this.fetchFn(url, options);
      const isJson = res.headers?.get?.("content-type")?.includes("application/json");
      const data = isJson ? await res.json() : await res.text();
      return { ok: res.ok, status: res.status, data };
    } catch (err) {
      return { ok: false, status: 0, data: { error: `sin conexión con ${url}` } };
    }
  }

  _json(method, body) {
    return {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    };
  }

  async setProfile(profileName) {
    const profile = PROXY_PROFILES[profileName] || profileName;
    return this._call(`${this.proxyUrl}/_channel/profile`, this._json("PUT", { profile }));
  }

  /** Ráfaga de `n` reportes sintéticos por el proxy: data = { sent, delivered, lost }. */
  async burst(n = 10) {
    return this._call(`${this.proxyUrl}/_sim/burst`, this._json("POST", { count: n }));
  }

  /** D7: el nodo transcribe un clip de data/radio/. La radio no pasa por el canal celular:
   * va directo al primer nodo encendido. */
  async injectRadio(clipId = "clip_colapso_dos_incidentes") {
    const nodeId = Object.keys(this.nodeUrls).find((id) => !this.killedNodes.has(id)) || "A";
    const url = `${this.nodeUrls[nodeId]}/api/radio/demo/${encodeURIComponent(clipId)}`;
    return this._call(url, { method: "POST" });
  }

  /** Apaga o enciende (alterna) un nodo con POST /_sim/power. */
  async killNode(nodeId = "A") {
    const target = String(nodeId).toUpperCase();
    const turnOn = this.killedNodes.has(target);
    const res = await this._call(
      `${this.nodeUrls[target]}/_sim/power`,
      this._json("POST", { on: turnOn }),
    );
    if (res.ok) {
      if (res.data?.on === false) this.killedNodes.add(target);
      else this.killedNodes.delete(target);
    }
    return { ...res, nodeId: target, killed: this.killedNodes.has(target) };
  }

  /** Lee el estado de energía de los nodos (p. ej., al recargar el tablero con A apagado). */
  async syncPower() {
    for (const [nodeId, url] of Object.entries(this.nodeUrls)) {
      const res = await this._call(`${url}/_sim/power`);
      if (res.ok && res.data?.on === false) this.killedNodes.add(nodeId);
      else if (res.ok) this.killedNodes.delete(nodeId);
    }
    return new Set(this.killedNodes);
  }

  async getHashes() {
    const hashes = {};
    for (const [nodeId, url] of Object.entries(this.nodeUrls)) {
      const res = await this._call(`${url}/api/digest`);
      hashes[nodeId] = res.ok ? res.data?.digest || res.data?.hash || "" : "";
    }
    return hashes;
  }
}
