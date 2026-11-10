// Service worker de la PWA (citizen/sw.js): red primero, copia guardada sin red, y todo
// módulo que carga la app guardado al instalar (si falta uno, la app no abre sin red).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join, normalize, relative } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const CITIZEN = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "citizen");
const SOURCE = readFileSync(join(CITIZEN, "sw.js"), "utf8");
const ORIGIN = "http://localhost:8000";

function precached() {
  const list = SOURCE.match(/ASSETS_TO_CACHE = \[([\s\S]*?)\]/)[1];
  return new Set([...list.matchAll(/"\.\/([^"]*)"/g)].map((m) => m[1]));
}

function importsOf(file, seen = new Set()) {
  if (seen.has(file)) return seen;
  seen.add(file);
  const code = readFileSync(join(CITIZEN, file), "utf8");
  for (const [, spec] of code.matchAll(/from\s+["'](\.{1,2}\/[^"']+)["']/g)) {
    importsOf(normalize(relative(CITIZEN, join(CITIZEN, dirname(file), spec))), seen);
  }
  return seen;
}

test("guarda al instalar todos los módulos que carga la app", () => {
  const assets = precached();
  for (const file of importsOf("app.js")) {
    assert.ok(assets.has(file), `falta ${file} en ASSETS_TO_CACHE de sw.js`);
  }
  assert.ok(assets.has("index.html"));
});

// --- Un service worker de mentira -----------------------------------------------------------

function load({ online }) {
  const store = new Map();
  const cache = {
    put: async (req, res) => store.set(key(req), res),
    match: async (req) => store.get(key(req)),
    addAll: async () => {},
  };
  const handlers = {};
  const self = {
    location: { origin: ORIGIN },
    addEventListener: (type, fn) => (handlers[type] = fn),
    skipWaiting: () => {},
    clients: { claim: () => {} },
  };
  const fetched = [];
  const context = {
    self,
    caches: { open: async () => cache, keys: async () => [] },
    fetch: async (req) => {
      fetched.push(key(req));
      if (!online) throw new TypeError("sin red");
      return { status: 200, type: "basic", body: `red:${key(req)}`, clone() { return this; } };
    },
    Response: { error: () => ({ error: true }) },
    URL,
    setTimeout,
    clearTimeout,
    Promise,
  };
  vm.runInNewContext(SOURCE, context);
  return { handlers, store, fetched };
}

function key(req) {
  const url = typeof req === "string" ? req : req.url;
  return new URL(url, `${ORIGIN}/ciudadano/`).pathname;
}

async function request(sw, path, mode = "no-cors") {
  let answer = null;
  sw.handlers.fetch({
    request: { url: ORIGIN + path, method: "GET", mode },
    respondWith: (p) => (answer = p),
  });
  return answer && (await answer);
}

test("con red usa siempre lo último y lo guarda", async () => {
  const sw = load({ online: true });
  sw.store.set("/ciudadano/app.js", { body: "vieja" });
  const res = await request(sw, "/ciudadano/app.js");
  assert.equal(res.body, "red:/ciudadano/app.js");
  assert.equal(sw.store.get("/ciudadano/app.js").body, "red:/ciudadano/app.js");
});

test("sin red usa la copia guardada, y al navegar index.html", async () => {
  const sw = load({ online: false });
  sw.store.set("/ciudadano/app.js", { body: "guardada" });
  sw.store.set("/ciudadano/index.html", { body: "inicio" });
  assert.equal((await request(sw, "/ciudadano/app.js")).body, "guardada");
  assert.equal((await request(sw, "/ciudadano/otra", "navigate")).body, "inicio");
  assert.deepEqual(await request(sw, "/ciudadano/nada.js"), { error: true });
});

test("no toca la API: las preguntas y reportes siempre van al nodo", async () => {
  const sw = load({ online: true });
  assert.equal(await request(sw, "/api/questions/a1b2c3d4"), null);
  assert.deepEqual(sw.fetched, []);
});
