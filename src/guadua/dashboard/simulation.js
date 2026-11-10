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
      message: `3/3 Nodos convergidos (${first.slice(0, 8)}…)`,
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

/**
 * Cliente HTTP real para llamar a las rutas cuando Jacobo las defina.
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
  }

  async setProfile(profileName) {
    if (!this.fetchFn) throw new Error("fetch no disponible");
    const res = await this.fetchFn(`${this.proxyUrl}/_channel/profile`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile: profileName }),
    });
    const isJson = res.headers?.get?.("content-type")?.includes("application/json");
    const data = isJson ? await res.json() : await res.text();
    return { ok: res.ok, status: res.status, data };
  }

  async burst(n = 10) {
    if (!this.fetchFn) throw new Error("fetch no disponible");
    const res = await this.fetchFn(`${this.proxyUrl}/_sim/burst`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ count: n }),
    });
    const isJson = res.headers?.get?.("content-type")?.includes("application/json");
    const data = isJson ? await res.json() : await res.text();
    return { ok: res.ok, status: res.status, data };
  }

  async injectRadio(clipId = "clip_colapso_dos_incidentes") {
    if (!this.fetchFn) throw new Error("fetch no disponible");
    const res = await this.fetchFn(`${this.proxyUrl}/_sim/radio`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clip_id: clipId }),
    });
    const isJson = res.headers?.get?.("content-type")?.includes("application/json");
    const data = isJson ? await res.json() : await res.text();
    return { ok: res.ok, status: res.status, data };
  }

  async killNode(nodeId = "A") {
    if (!this.fetchFn) throw new Error("fetch no disponible");
    const res = await this.fetchFn(`${this.proxyUrl}/_sim/kill`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node: nodeId }),
    });
    const isJson = res.headers?.get?.("content-type")?.includes("application/json");
    const data = isJson ? await res.json() : await res.text();
    return { ok: res.ok, status: res.status, data };
  }

  async getHashes() {
    if (!this.fetchFn) throw new Error("fetch no disponible");
    const hashes = {};
    for (const [nodeId, url] of Object.entries(this.nodeUrls)) {
      try {
        const res = await this.fetchFn(`${url}/api/digest`);
        if (res.ok) {
          const data = await res.json();
          hashes[nodeId] = data.digest || data.hash || "";
        } else {
          hashes[nodeId] = "";
        }
      } catch {
        hashes[nodeId] = "";
      }
    }
    return hashes;
  }
}
