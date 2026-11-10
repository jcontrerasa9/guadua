// Pruebas unitarias para la barra de simulación del jurado y convergencia (D9).
// Se ejecutan con: node --test "tests/js/*.test.mjs"

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  setupSimulationBar,
  state as appState,
  updateHashesUi,
} from "../../src/guadua/dashboard/app.js";
import {
  checkConvergence,
  DEFAULT_NODE_HASHES,
  DESYNC_NODE_HASH,
  HttpSimulationClient,
  MockSimulationClient,
} from "../../src/guadua/dashboard/simulation.js";

// Helper para crear un entorno DOM mínimo simulado para Node.js
function setupMockDom() {
  const elements = new Map();

  function makeElement(id) {
    const classes = new Set();
    const el = {
      id,
      textContent: "",
      title: "",
      get className() {
        return Array.from(classes).join(" ");
      },
      set className(val) {
        classes.clear();
        if (val) {
          for (const c of String(val).split(/\s+/).filter(Boolean)) {
            classes.add(c);
          }
        }
      },
      classList: {
        add(c) {
          classes.add(c);
        },
        remove(c) {
          classes.delete(c);
        },
        toggle(c, force) {
          if (force === undefined) {
            if (classes.has(c)) classes.delete(c);
            else classes.add(c);
          } else if (force) {
            classes.add(c);
          } else {
            classes.delete(c);
          }
        },
        contains(c) {
          return classes.has(c);
        },
      },
      listeners: {},
      addEventListener(evt, fn) {
        if (!this.listeners[evt]) this.listeners[evt] = [];
        this.listeners[evt].push(fn);
      },
      async click() {
        if (this.listeners.click) {
          for (const fn of this.listeners.click) {
            await fn({ target: el });
          }
        }
      },
    };
    elements.set(id, el);
    return el;
  }

  const ids = [
    "sim-convergence-badge",
    "btn-sim-profile-wifi",
    "btn-sim-profile-lora",
    "btn-sim-profile-sms",
    "btn-sim-burst",
    "btn-sim-radio",
    "btn-sim-kill-a",
    "btn-sim-refresh-hashes",
    "sim-hash-box-a",
    "sim-hash-box-b",
    "sim-hash-box-c",
    "sim-hash-a",
    "sim-hash-b",
    "sim-hash-c",
    "sim-status-msg",
  ];

  for (const id of ids) {
    makeElement(id);
  }

  const mockDoc = {
    getElementById(id) {
      return elements.get(id) || null;
    },
    elements,
  };

  globalThis.document = mockDoc;
  return mockDoc;
}

// --- 1. Pruebas de la lógica pura de convergencia (checkConvergence) ---------

test("checkConvergence: hashes iguales en los 3 nodos se marcan como convergidos", () => {
  const hashes = {
    A: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    B: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    C: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  };

  const result = checkConvergence(hashes);
  assert.equal(result.converged, true);
  assert.equal(result.commonHash, hashes.A);
  assert.match(result.message, /3\/3 Nodos convergidos/);
});

test("checkConvergence: hashes distintos se marcan como no convergidos", () => {
  const hashes = {
    A: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    B: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    C: "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff", // nodo C desincronizado
  };

  const result = checkConvergence(hashes);
  assert.equal(result.converged, false);
  assert.equal(result.commonHash, null);
  assert.match(result.message, /Divergencia entre nodos/);
});

test("checkConvergence: detecta nodo faltante o hash vacío", () => {
  const missingOne = {
    A: "e3b0c442",
    B: "e3b0c442",
    C: "",
  };
  const resEmpty = checkConvergence(missingOne);
  assert.equal(resEmpty.converged, false);
  assert.match(resEmpty.message, /Falta hash para nodo C/);

  const incomplete = { A: "e3b0c442", B: "e3b0c442" };
  const resIncomplete = checkConvergence(incomplete);
  assert.equal(resIncomplete.converged, false);
  assert.match(resIncomplete.message, /Falta hash para nodo C/);

  const resNull = checkConvergence(null);
  assert.equal(resNull.converged, false);
});

// --- 2. Pruebas de MockSimulationClient ---------------------------------------

test("MockSimulationClient: setProfile cambia perfil y lo registra", async () => {
  const client = new MockSimulationClient();
  assert.equal(client.profile, "wifi");

  const res = await client.setProfile("sms");
  assert.equal(res.ok, true);
  assert.equal(res.profile, "sms");
  assert.equal(client.profile, "sms");

  const lastLog = client.history.at(-1);
  assert.equal(lastLog.action, "setProfile");
  assert.equal(lastLog.profile, "sms");
});

test("MockSimulationClient: burst acumula ráfagas de reportes sintéticos", async () => {
  const client = new MockSimulationClient();
  assert.equal(client.burstCount, 0);

  const res1 = await client.burst(10);
  assert.equal(res1.ok, true);
  assert.equal(res1.count, 10);
  assert.equal(client.burstCount, 10);

  const res2 = await client.burst(5);
  assert.equal(res2.ok, true);
  assert.equal(res2.count, 5);
  assert.equal(client.burstCount, 15);
});

test("MockSimulationClient: injectRadio registra clips de audio inyectados", async () => {
  const client = new MockSimulationClient();
  assert.equal(client.radioClips.length, 0);

  const res = await client.injectRadio("clip_colapso_dos_incidentes");
  assert.equal(res.ok, true);
  assert.equal(res.clipId, "clip_colapso_dos_incidentes");
  assert.deepEqual(client.radioClips, ["clip_colapso_dos_incidentes"]);
});

test("MockSimulationClient: killNode conmuta caída de nodo y altera hash", async () => {
  const client = new MockSimulationClient();

  // Estado inicial: convergente
  const initialHashes = await client.getHashes();
  assert.equal(checkConvergence(initialHashes).converged, true);

  // Apagar nodo A
  const killRes = await client.killNode("A");
  assert.equal(killRes.ok, true);
  assert.equal(killRes.nodeId, "A");
  assert.equal(killRes.killed, true);
  assert.ok(client.killedNodes.has("A"));

  // Hashes ahora divergen
  const divergedHashes = await client.getHashes();
  assert.equal(divergedHashes.A, DESYNC_NODE_HASH);
  assert.equal(checkConvergence(divergedHashes).converged, false);

  // Revivir nodo A
  const reviveRes = await client.killNode("A");
  assert.equal(reviveRes.ok, true);
  assert.equal(reviveRes.killed, false);
  assert.equal(client.killedNodes.has("A"), false);

  // Hashes vuelven a converger
  const restoredHashes = await client.getHashes();
  assert.equal(checkConvergence(restoredHashes).converged, true);
});

// --- 3. Pruebas de integración UI de la barra de simulación ------------------

test("UI barra de simulación: cada botón llama a su función y actualiza el tablero", async () => {
  setupMockDom();
  const mockClient = new MockSimulationClient();

  setupSimulationBar(mockClient);
  await updateHashesUi();

  // 1. Estado inicial de convergencia
  const badge = document.getElementById("sim-convergence-badge");
  assert.ok(badge);
  assert.match(badge.textContent, /3\/3 Nodos convergidos/i);
  assert.equal(badge.classList.contains("diverged"), false);

  // 2. Probar botón de perfil (Radio / LoRa)
  const loraBtn = document.getElementById("btn-sim-profile-lora");
  await loraBtn.click();
  assert.equal(mockClient.profile, "radio_lora");
  assert.equal(loraBtn.classList.contains("active"), true);
  const statusMsg = document.getElementById("sim-status-msg");
  assert.match(statusMsg.textContent, /RADIO_LORA/);

  // 3. Probar botón de ráfaga
  const burstBtn = document.getElementById("btn-sim-burst");
  await burstBtn.click();
  assert.equal(mockClient.burstCount, 10);
  assert.match(statusMsg.textContent, /Ráfaga de 10/);

  // 4. Probar botón de radio
  const radioBtn = document.getElementById("btn-sim-radio");
  await radioBtn.click();
  assert.equal(mockClient.radioClips.length, 1);
  assert.match(statusMsg.textContent, /Clip de radio/);

  // 5. Probar botón de apagar nodo A
  const killBtn = document.getElementById("btn-sim-kill-a");
  await killBtn.click();
  assert.equal(mockClient.killedNodes.has("A"), true);
  assert.equal(killBtn.textContent, "❤️ Revivir nodo A");
  assert.equal(killBtn.classList.contains("active-killed"), true);

  // Verificar que los hashes ahora marcan divergencia en el badge
  assert.match(badge.textContent, /Divergencia/);
  assert.equal(badge.classList.contains("diverged"), true);

  const hashBoxA = document.getElementById("sim-hash-box-a");
  assert.equal(hashBoxA.classList.contains("killed"), true);

  // 6. Probar botón de revivir nodo A
  await killBtn.click();
  assert.equal(mockClient.killedNodes.has("A"), false);
  assert.equal(killBtn.textContent, "💀 Apagar nodo A");
  assert.equal(killBtn.classList.contains("active-killed"), false);

  // El badge vuelve a marcar convergencia
  assert.match(badge.textContent, /3\/3 Nodos convergidos/i);
  assert.equal(badge.classList.contains("diverged"), false);
  assert.equal(hashBoxA.classList.contains("killed"), false);

  // 7. Probar botón de refrescar hashes
  const refreshBtn = document.getElementById("btn-sim-refresh-hashes");
  await refreshBtn.click();
  assert.equal(statusMsg.textContent, "Hashes actualizados.");
});

// --- 4. Pruebas de HttpSimulationClient ---------------------------------------

test("HttpSimulationClient despacha las llamadas HTTP con los formatos previstos", async () => {
  const requests = [];
  const mockFetch = async (url, options = {}) => {
    requests.push({ url, options });
    return {
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => {
        if (url.includes("/api/digest")) {
          return { digest: "hash-1234" };
        }
        return { ok: true };
      },
    };
  };

  const client = new HttpSimulationClient({
    proxyUrl: "http://proxy.local",
    nodeUrls: {
      A: "http://node-a.local",
      B: "http://node-b.local",
      C: "http://node-c.local",
    },
    fetchFn: mockFetch,
  });

  await client.setProfile("sms");
  assert.equal(requests.at(-1).url, "http://proxy.local/_channel/profile");
  assert.equal(requests.at(-1).options.method, "PUT");
  assert.deepEqual(JSON.parse(requests.at(-1).options.body), { profile: "sms" });

  await client.burst(15);
  assert.equal(requests.at(-1).url, "http://proxy.local/_sim/burst");
  assert.deepEqual(JSON.parse(requests.at(-1).options.body), { count: 15 });

  await client.injectRadio("clip_1");
  assert.equal(requests.at(-1).url, "http://proxy.local/_sim/radio");
  assert.deepEqual(JSON.parse(requests.at(-1).options.body), { clip_id: "clip_1" });

  await client.killNode("A");
  assert.equal(requests.at(-1).url, "http://proxy.local/_sim/kill");
  assert.deepEqual(JSON.parse(requests.at(-1).options.body), { node: "A" });

  const hashes = await client.getHashes();
  assert.deepEqual(hashes, {
    A: "hash-1234",
    B: "hash-1234",
    C: "hash-1234",
  });
});
