// Orquestación de la PWA Ciudadana - Guadua (SARD)
// Manejo de UI, geolocalización, persistencia en cola y registro de Service Worker.

import {
  buildDebrisSosBinary,
  createAcousticBeacon,
  createTapDetector,
  createWakeLockManager,
  EXIT_HOLD_DURATION_MS,
  PROVISIONAL_ADVICE_TEXT,
  RAPID_TAP_COUNT,
  RAPID_TAP_WINDOW_MS,
} from "./debris.js";
import { encodeGeohash } from "./geohash.js";
import { createReportQueue, ReportStatus } from "./queue.js";
import {
  buildReportBinary,
  buildReportHeaderFields,
  encodedNoteBytes,
  fitNote,
  INCIDENT_BUTTONS,
  MAX_NOTE_BYTES,
  measureUtf8Bytes,
  truncateUtf8,
} from "./report.js";
import { buildSafeBinary, MAX_NAME_BYTES } from "./safe.js";
import { createQuestionManager } from "./questions.js";
import { setupBatteryMonitor } from "./battery.js";

// Estado de la interfaz
const uiState = {
  selectedIncident: null,
  isDirectObservation: true,
  smellsGasOrWires: false,
  notBreathing: false,
  gpsActive: false,
  geohash: null,
  selectedBarrioCode: null,
  barriosList: [],
  queue: null,
  lastReportId: null,
  dictionary: null, // bytes de diccionario_manizales.txt para comprimir la nota
  inDebrisMode: false,
  beacon: null,
  wakeLock: null,
  tapDetector: null,
  questionManager: null,
  batteryLevel: null,
  isCriticalBattery: false,
  isCharging: false,
};

// --- Registro de Service Worker para funcionamiento 100% offline -------------

function registerServiceWorker() {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker
      .register("./sw.js")
      .then((reg) => console.log("Service Worker registrado con scope:", reg.scope))
      .catch((err) => console.warn("Fallo al registrar Service Worker:", err));
  }
}

// --- Carga del diccionario de texto (N1) -------------------------------------
// Sin él la nota no se puede codificar y el reporte sale sin nota (nunca deja de salir).

async function loadDictionary() {
  try {
    const res = await fetch("./diccionario_manizales.txt");
    if (res.ok) {
      uiState.dictionary = new Uint8Array(await res.arrayBuffer());
    }
  } catch (err) {
    console.warn("No se pudo cargar el diccionario; los reportes saldrán sin nota:", err);
  }
}

// --- Carga del catálogo de barrios -------------------------------------------

async function loadBarrios() {
  const select = document.getElementById("barrio-select");
  if (!select) return;

  try {
    const res = await fetch("./barrios.json");
    if (res.ok) {
      uiState.barriosList = await res.json();
    }
  } catch (err) {
    console.warn("No se pudo cargar barrios.json, usando catálogo fallback:", err);
    uiState.barriosList = [
      { code: 1, name: "El Cable" },
      { code: 2, name: "Av. Santander" },
      { code: 3, name: "Chipre" },
      { code: 4, name: "Centro" },
      { code: 5, name: "Palermo" },
      { code: 6, name: "Milán" },
    ];
  }

  select.innerHTML = `<option value="" disabled selected>Seleccione el barrio...</option>`;
  for (const b of uiState.barriosList) {
    const opt = document.createElement("option");
    opt.value = b.code;
    opt.textContent = b.name;
    select.appendChild(opt);
  }

  select.onchange = () => {
    uiState.selectedBarrioCode = Number(select.value);
    if (uiState.queue?.store?.saveLastBarrio) {
      uiState.queue.store.saveLastBarrio(uiState.selectedBarrioCode).catch(() => {});
    }
  };
}

// --- Selección de Incidentes (Paso 1) ------------------------------------------

function setupIncidentButtons() {
  const buttons = document.querySelectorAll(".incident-btn");
  buttons.forEach((btn) => {
    btn.onclick = () => {
      buttons.forEach((b) => b.classList.remove("selected"));
      btn.classList.add("selected");
      uiState.selectedIncident = btn.dataset.id;
    };
  });
}

// --- Observación y Banderas (Paso 2) ------------------------------------------

function setupObservationAndFlags() {
  const radioDirecta = document.querySelector('input[name="observation"][value="directa"]');
  const radioContaron = document.querySelector('input[name="observation"][value="segunda_mano"]');
  const checkGas = document.getElementById("check-gas-cables");
  const checkNoRespira = document.getElementById("check-no-respira");

  if (radioDirecta) {
    radioDirecta.onchange = () => {
      uiState.isDirectObservation = true;
    };
  }
  if (radioContaron) {
    radioContaron.onchange = () => {
      uiState.isDirectObservation = false;
    };
  }
  if (checkGas) {
    checkGas.onchange = () => {
      uiState.smellsGasOrWires = checkGas.checked;
    };
  }
  if (checkNoRespira) {
    checkNoRespira.onchange = () => {
      uiState.notBreathing = checkNoRespira.checked;
    };
  }
}

// --- Ubicación (GPS vs Barrio) y Nota (Paso 3) --------------------------------

function setupLocationAndNote() {
  const btnGps = document.getElementById("btn-toggle-gps");
  const gpsStatus = document.getElementById("gps-status-text");
  const barrioContainer = document.getElementById("barrio-container");
  const noteInput = document.getElementById("note-input");
  const byteCounter = document.getElementById("byte-counter");

  if (btnGps) {
    btnGps.onclick = () => {
      if (!uiState.gpsActive) {
        if (!navigator.geolocation) {
          showToast("Tu dispositivo no soporta geolocalización GPS.");
          return;
        }

        gpsStatus.textContent = "Obteniendo coordenadas...";
        navigator.geolocation.getCurrentPosition(
          (pos) => {
            const lat = pos.coords.latitude;
            const lon = pos.coords.longitude;
            try {
              uiState.geohash = encodeGeohash(lat, lon, 7);
              uiState.gpsActive = true;
              btnGps.classList.add("active");
              btnGps.textContent = "📍 GPS activo";
              gpsStatus.textContent = `GPS: ${uiState.geohash}`;
              if (barrioContainer) barrioContainer.style.opacity = "0.4";
              if (uiState.queue?.store?.saveLastLocation) {
                uiState.queue.store.saveLastLocation({
                  geohash: uiState.geohash,
                  lat,
                  lon,
                  timestamp: Date.now(),
                }).catch(() => {});
              }
            } catch (err) {
              gpsStatus.textContent = "Error al codificar GPS";
              console.warn(err);
            }
          },
          (err) => {
            gpsStatus.textContent = "No se pudo obtener GPS (usando barrio)";
            showToast("No se pudo obtener la ubicación GPS.");
            console.warn(err);
          },
          { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 },
        );
      } else {
        // Desactivar GPS
        uiState.gpsActive = false;
        uiState.geohash = null;
        btnGps.classList.remove("active");
        btnGps.textContent = "📍 Usar mi GPS";
        gpsStatus.textContent = "Sin GPS (usando barrio)";
        if (barrioContainer) barrioContainer.style.opacity = "1";
      }
    };
  }

  // Contador de los bytes que de verdad se enviarán (nota ya comprimida), con aviso
  if (noteInput && byteCounter) {
    noteInput.oninput = () => {
      if (!uiState.dictionary) {
        byteCounter.textContent = "Sin diccionario: la nota no se enviará";
        byteCounter.className = "byte-counter danger";
        return;
      }
      if (encodedNoteBytes(noteInput.value, uiState.dictionary) === null) {
        noteInput.value = fitNote(noteInput.value, uiState.dictionary);
      }

      const currentBytes = encodedNoteBytes(noteInput.value, uiState.dictionary) ?? 0;
      byteCounter.textContent = `${currentBytes} / ${MAX_NOTE_BYTES} bytes`;

      byteCounter.className = "byte-counter";
      if (currentBytes >= 80 && currentBytes < MAX_NOTE_BYTES) {
        byteCounter.classList.add("warning");
      } else if (currentBytes >= MAX_NOTE_BYTES) {
        byteCounter.classList.add("danger");
      }
    };
  }
}

// --- Modo Escombros (D1) -----------------------------------------------------

async function enterDebrisMode() {
  if (uiState.inDebrisMode) return;
  uiState.inDebrisMode = true;

  // 1. Iniciar la baliza acústica de inmediato dentro del gesto del usuario
  if (!uiState.beacon) {
    uiState.beacon = createAcousticBeacon();
  }
  uiState.beacon.start();

  // 2. Solicitar WakeLock para mantener la pantalla encendida
  // Limitación declarada en docs/diseno.md §6.1.5:
  // En la PWA, la pantalla queda encendida con fondo negro porque una página web no puede apagar la pantalla
  // y el audio se suspende si el teléfono se bloquea.
  if (!uiState.wakeLock) {
    uiState.wakeLock = createWakeLockManager();
  }
  uiState.wakeLock.acquire().catch(() => {});

  // 3. Mostrar overlay de modo escombros
  const overlay = document.getElementById("debris-overlay");
  const adviceEl = document.getElementById("debris-advice-text");
  if (adviceEl) {
    adviceEl.textContent = PROVISIONAL_ADVICE_TEXT;
  }
  if (overlay) {
    overlay.classList.remove("hidden");
  }

  // 4. Armar y encolar el SOS escombros de 21 bytes con prioridad
  try {
    const store = uiState.queue.store;
    const deviceId = await store.getDeviceId();
    const seq = await store.getNextSeq();

    let lastLocation = null;
    if (typeof store.getLastLocation === "function") {
      lastLocation = await store.getLastLocation();
    }
    if (!lastLocation && uiState.geohash) {
      lastLocation = { geohash: uiState.geohash };
    }

    let lastBarrio = null;
    if (typeof store.getLastBarrio === "function") {
      lastBarrio = await store.getLastBarrio();
    }
    const fallbackBarrio = lastBarrio || uiState.selectedBarrioCode || 1;

    const { headerFields, fullBytes } = buildDebrisSosBinary({
      deviceId,
      seq,
      lastLocation,
      fallbackBarrioCode: fallbackBarrio,
    });

    const item = await uiState.queue.enqueue({
      headerFields,
      fullBytes,
      priority: 1, // Prioridad máxima en la cola
    });

    uiState.lastReportId = item.id;
  } catch (err) {
    console.error("Error al armar o encolar SOS escombros:", err);
  }
}

function exitDebrisMode() {
  if (!uiState.inDebrisMode) return;
  uiState.inDebrisMode = false;

  // 1. Detener baliza acústica
  if (uiState.beacon) {
    uiState.beacon.stop();
  }

  // 2. Liberar WakeLock
  if (uiState.wakeLock) {
    uiState.wakeLock.release().catch(() => {});
  }

  // 3. Ocultar overlay
  const overlay = document.getElementById("debris-overlay");
  if (overlay) {
    overlay.classList.add("hidden");
  }

  showToast("Saliste del modo escombros.");
}

function setupDebrisExitButton() {
  const exitBtn = document.getElementById("btn-debris-exit");
  const progressBar = document.getElementById("debris-exit-progress");
  if (!exitBtn) return;

  let holdTimer = null;
  let startTime = 0;
  let animFrame = null;

  function updateProgress() {
    if (!holdTimer) return;
    const elapsed = Date.now() - startTime;
    const pct = Math.min(100, (elapsed / EXIT_HOLD_DURATION_MS) * 100);
    if (progressBar) progressBar.style.width = `${pct}%`;
    if (elapsed < EXIT_HOLD_DURATION_MS) {
      animFrame = requestAnimationFrame(updateProgress);
    }
  }

  function startHold(e) {
    if (e) e.stopPropagation();
    if (!uiState.inDebrisMode) return;

    startTime = Date.now();
    if (progressBar) progressBar.style.width = "0%";
    animFrame = requestAnimationFrame(updateProgress);

    holdTimer = setTimeout(() => {
      cancelHold();
      exitDebrisMode();
    }, EXIT_HOLD_DURATION_MS);
  }

  function cancelHold() {
    if (holdTimer) {
      clearTimeout(holdTimer);
      holdTimer = null;
    }
    if (animFrame) {
      cancelAnimationFrame(animFrame);
      animFrame = null;
    }
    if (progressBar) progressBar.style.width = "0%";
  }

  exitBtn.addEventListener("pointerdown", startHold);
  exitBtn.addEventListener("pointerup", cancelHold);
  exitBtn.addEventListener("pointerleave", cancelHold);
  exitBtn.addEventListener("pointercancel", cancelHold);
}

function setupRapidTapDetector() {
  uiState.tapDetector = createTapDetector({
    targetCount: RAPID_TAP_COUNT,
    windowMs: RAPID_TAP_WINDOW_MS,
    onTrigger: () => {
      if (!uiState.inDebrisMode) {
        enterDebrisMode();
      }
    },
  });

  window.addEventListener(
    "pointerdown",
    (e) => {
      // Ignorar toques en el botón de salida si ya está en modo escombros
      if (uiState.inDebrisMode) {
        return;
      }
      uiState.tapDetector.registerTap();
    },
    { passive: true },
  );
}

// --- Bloque 1: Botones de vida o muerte ---------------------------------------

function setupSafeModal() {
  const btnASalvo = document.getElementById("btn-a-salvo");
  const safeOverlay = document.getElementById("safe-overlay");
  const safeBarrioSelect = document.getElementById("safe-barrio-select");
  const safeNameInput = document.getElementById("safe-name-input");
  const safeNameCounter = document.getElementById("safe-name-counter");
  const btnCancel = document.getElementById("btn-safe-cancel");
  const btnSubmit = document.getElementById("btn-safe-submit");

  if (!btnASalvo || !safeOverlay) return;

  function populateSafeBarrios() {
    if (!safeBarrioSelect) return;
    if (safeBarrioSelect.options.length <= 1 && uiState.barriosList.length > 0) {
      safeBarrioSelect.innerHTML = `<option value="" disabled selected>Seleccione el barrio...</option>`;
      for (const b of uiState.barriosList) {
        const opt = document.createElement("option");
        opt.value = b.code;
        opt.textContent = b.name;
        safeBarrioSelect.appendChild(opt);
      }
    }
    if (uiState.selectedBarrioCode) {
      safeBarrioSelect.value = String(uiState.selectedBarrioCode);
    } else if (uiState.queue?.store?.getLastBarrio) {
      uiState.queue.store.getLastBarrio().then((code) => {
        if (code) safeBarrioSelect.value = String(code);
      }).catch(() => {});
    }
  }

  btnASalvo.onclick = () => {
    populateSafeBarrios();
    if (safeNameInput) {
      safeNameInput.value = "";
    }
    if (safeNameCounter) {
      safeNameCounter.textContent = `0 / ${MAX_NAME_BYTES} bytes`;
      safeNameCounter.className = "byte-counter";
    }
    safeOverlay.classList.remove("hidden");
  };

  if (btnCancel) {
    btnCancel.onclick = () => {
      safeOverlay.classList.add("hidden");
    };
  }

  if (safeNameInput && safeNameCounter) {
    safeNameInput.oninput = () => {
      let val = safeNameInput.value;
      if (measureUtf8Bytes(val) > MAX_NAME_BYTES) {
        val = truncateUtf8(val, MAX_NAME_BYTES);
        safeNameInput.value = val;
      }
      const bytes = measureUtf8Bytes(val);
      safeNameCounter.textContent = `${bytes} / ${MAX_NAME_BYTES} bytes`;
      safeNameCounter.className = "byte-counter";
      if (bytes >= 35 && bytes < MAX_NAME_BYTES) {
        safeNameCounter.classList.add("warning");
      } else if (bytes >= MAX_NAME_BYTES) {
        safeNameCounter.classList.add("danger");
      }
    };
  }

  if (btnSubmit) {
    btnSubmit.onclick = async () => {
      const barrioCode = Number(safeBarrioSelect?.value);
      if (!barrioCode || isNaN(barrioCode) || barrioCode <= 0) {
        showToast("Por favor selecciona un barrio.");
        if (safeBarrioSelect) safeBarrioSelect.focus();
        return;
      }

      const rawName = safeNameInput ? safeNameInput.value.trim() : "";

      try {
        const store = uiState.queue.store;
        const deviceId = await store.getDeviceId();
        const seq = await store.getNextSeq();

        const { headerFields, fullBytes } = buildSafeBinary({
          deviceId,
          seq,
          barrioCode,
          name: rawName,
          dictionary: uiState.dictionary,
        });

        if (store.saveLastBarrio) {
          await store.saveLastBarrio(barrioCode).catch(() => {});
        }

        const item = await uiState.queue.enqueue({
          headerFields,
          fullBytes,
          priority: 0,
        });

        uiState.lastReportId = item.id;
        updatePipelineUi(ReportStatus.GUARDADO);

        safeOverlay.classList.add("hidden");
        showToast("Mensaje 'Estoy a salvo' guardado y enviando...");
      } catch (err) {
        console.error("Error al armar o encolar Estoy a salvo:", err);
        showToast("Error al enviar el mensaje 'Estoy a salvo'.");
      }
    };
  }
}

function setupBloque1() {
  const btnSos = document.getElementById("btn-sos-escombros");

  if (btnSos) {
    btnSos.onclick = () => {
      enterDebrisMode();
    };
  }

  setupSafeModal();
}

// --- Bloque 3: Envío del reporte y Pipeline de Estados ------------------------

function updatePipelineUi(status) {
  const stepGuardado = document.getElementById("step-guardado");
  const stepEnviando = document.getElementById("step-enviando");
  const stepRecibido = document.getElementById("step-recibido");

  if (!stepGuardado || !stepEnviando || !stepRecibido) return;

  stepGuardado.className = "status-step";
  stepEnviando.className = "status-step";
  stepRecibido.className = "status-step";

  if (status === ReportStatus.GUARDADO) {
    stepGuardado.classList.add("active");
  } else if (status === ReportStatus.ENVIANDO) {
    stepGuardado.classList.add("completed");
    stepEnviando.classList.add("active");
  } else if (status === ReportStatus.RECIBIDO) {
    stepGuardado.classList.add("completed");
    stepEnviando.classList.add("completed");
    stepRecibido.classList.add("completed");
  }
}

function setupBloque3() {
  const btnPedirAyuda = document.getElementById("btn-pedir-ayuda");
  const noteInput = document.getElementById("note-input");

  if (!btnPedirAyuda) return;

  btnPedirAyuda.onclick = async () => {
    // 1. Validar que seleccionó incidente
    if (!uiState.selectedIncident) {
      showToast("Por favor seleccione qué pasa (Paso 1).");
      return;
    }

    // 2. Validar ubicación: GPS o Barrio
    if (!uiState.gpsActive && !uiState.selectedBarrioCode) {
      showToast("Por favor active el GPS o seleccione su barrio (Paso 3).");
      return;
    }

    try {
      const store = uiState.queue.store;
      const deviceId = await store.getDeviceId();
      const seq = await store.getNextSeq();

      const headerFields = buildReportHeaderFields({
        deviceId,
        seq,
        incidentKey: uiState.selectedIncident,
        isDirectObservation: uiState.isDirectObservation,
        smellsGasOrWires: uiState.smellsGasOrWires,
        notBreathing: uiState.notBreathing,
        geohash: uiState.geohash,
        barrioCode: uiState.selectedBarrioCode,
        criticalBattery: uiState.isCriticalBattery,
      });

      const noteText = noteInput ? noteInput.value : "";
      const { fullBytes, noteDropped } = buildReportBinary({
        headerFields,
        noteText,
        dictionary: uiState.dictionary,
      });
      if (noteDropped) {
        console.warn("Reporte enviado sin nota: no se cargó el diccionario.");
      }

      // Encolar y comenzar transmisión
      const item = await uiState.queue.enqueue({
        headerFields,
        fullBytes,
      });

      uiState.lastReportId = item.id;
      const successToast = uiState.isCriticalBattery
        ? "¡Reporte guardado! Modo batería crítica (21 B). Transmitiendo auxilio..."
        : "¡Reporte guardado! Transmitiendo auxilio...";
      showToast(successToast);
    } catch (err) {
      console.error("Error al armar o encolar el reporte:", err);
      showToast("Ocurrió un error al preparar el reporte.");
    }
  };
}

// --- Notificaciones Toast ----------------------------------------------------

function showToast(message) {
  const existing = document.querySelector(".app-toast");
  if (existing) existing.remove();

  const toast = document.createElement("div");
  toast.className = "app-toast";
  toast.textContent = message;
  document.body.appendChild(toast);

  setTimeout(() => {
    if (toast.parentNode) toast.remove();
  }, 4000);
}

// --- Monitoreo de Conexión y Batería -------------------------------------------

function setupConnectivityAndBattery() {
  const connText = document.getElementById("conn-text");
  const connIcon = document.querySelector("#conn-status span");
  const batteryContainer = document.getElementById("battery-status");
  const batteryText = document.getElementById("battery-text");

  function updateOnlineStatus() {
    if (navigator.onLine) {
      if (connText) connText.textContent = "Conexión detectada";
      if (connIcon) connIcon.textContent = "🟢";
    } else {
      if (connText) connText.textContent = "Listo sin internet";
      if (connIcon) connIcon.textContent = "🟠";
    }
  }

  window.addEventListener("online", updateOnlineStatus);
  window.addEventListener("offline", updateOnlineStatus);
  updateOnlineStatus();

  setupBatteryMonitor({
    onBatteryChange: (status) => {
      uiState.batteryLevel = status.levelPercent;
      uiState.isCriticalBattery = status.isCritical;
      uiState.isCharging = status.isCharging;

      if (batteryText) {
        batteryText.textContent = status.text;
      }
      if (batteryContainer) {
        batteryContainer.classList.toggle("battery-critical", status.isCritical);
        batteryContainer.classList.toggle("battery-charging", status.isCharging);
        if (status.isCritical) {
          batteryContainer.setAttribute(
            "title",
            `Batería crítica (${status.levelPercent}%). Modo ahorro activado (reportes compactos de 21 B).`,
          );
        } else if (status.isCharging) {
          batteryContainer.setAttribute("title", `Dispositivo cargando (${status.levelPercent}%).`);
        } else if (status.levelPercent !== null) {
          batteryContainer.setAttribute("title", `Nivel de batería: ${status.levelPercent}%.`);
        } else {
          batteryContainer.setAttribute("title", "Nivel de batería no disponible en este navegador.");
        }
      }
    },
  });
}

// --- Preguntas de seguimiento (D5b) -------------------------------------------

function setupQuestionsCard() {
  const cardEl = document.getElementById("question-card");
  const textEl = document.getElementById("question-text");
  const btnNo = document.getElementById("btn-question-no");
  const btnYes = document.getElementById("btn-question-yes");

  if (!cardEl || !uiState.queue) return;

  uiState.questionManager = createQuestionManager({
    store: uiState.queue.store,
    queue: uiState.queue,
    getDeviceIdFn: () => uiState.queue.store.getDeviceId(),
    onQuestionChange: (q) => {
      if (q && q.text) {
        if (textEl) textEl.textContent = q.text;
        cardEl.classList.remove("hidden");
      } else {
        cardEl.classList.add("hidden");
      }
    },
  });

  if (btnNo) {
    btnNo.onclick = async () => {
      try {
        const res = await uiState.questionManager.answerCurrentQuestion(false);
        if (res?.enqueuedItem) {
          uiState.lastReportId = res.enqueuedItem.id;
          updatePipelineUi(ReportStatus.GUARDADO);
          showToast("Respuesta 'No' guardada y enviando...");
        }
      } catch (err) {
        console.error("Error al responder pregunta:", err);
      }
    };
  }

  if (btnYes) {
    btnYes.onclick = async () => {
      try {
        const res = await uiState.questionManager.answerCurrentQuestion(true);
        if (res?.enqueuedItem) {
          uiState.lastReportId = res.enqueuedItem.id;
          updatePipelineUi(ReportStatus.GUARDADO);
          showToast("Respuesta 'Sí' guardada y enviando...");
        }
      } catch (err) {
        console.error("Error al responder pregunta:", err);
      }
    };
  }

  // Consultar preguntas si hay red
  if (navigator.onLine) {
    uiState.questionManager.pollQuestions().catch(() => {});
  }

  window.addEventListener("online", () => {
    if (uiState.questionManager) {
      uiState.questionManager.pollQuestions().catch(() => {});
    }
  });

  // Sondeo periódico cada 15s si hay red
  setInterval(() => {
    if (navigator.onLine && uiState.questionManager) {
      uiState.questionManager.pollQuestions().catch(() => {});
    }
  }, 15000);
}

// --- Inicialización Principal -------------------------------------------------

async function init() {
  registerServiceWorker();
  setupConnectivityAndBattery();
  setupBloque1();
  setupDebrisExitButton();
  setupRapidTapDetector();
  setupIncidentButtons();
  setupObservationAndFlags();
  setupLocationAndNote();
  await Promise.all([loadBarrios(), loadDictionary()]);

  // Inicializar cola de reportes con IndexedDB
  uiState.queue = createReportQueue({
    onStatusChange: (item) => {
      if (item.id === uiState.lastReportId) {
        updatePipelineUi(item.status);
      }
    },
  });

  setupQuestionsCard();
  setupBloque3();
  uiState.queue.start();
}

window.addEventListener("DOMContentLoaded", init);
