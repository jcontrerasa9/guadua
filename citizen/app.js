// Orquestación de la PWA Ciudadana - Guadua (SARD)
// Manejo de UI, geolocalización, persistencia en cola y registro de Service Worker.

import { encodeGeohash } from "./geohash.js";
import { createReportQueue, ReportStatus } from "./queue.js";
import {
  buildReportBinary,
  buildReportHeaderFields,
  INCIDENT_BUTTONS,
  MAX_NOTE_BYTES,
  measureUtf8Bytes,
  truncateUtf8,
} from "./report.js";

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

  // Contador de bytes UTF-8 con aviso
  if (noteInput && byteCounter) {
    noteInput.oninput = () => {
      let val = noteInput.value;
      const bytes = measureUtf8Bytes(val);

      if (bytes > MAX_NOTE_BYTES) {
        val = truncateUtf8(val, MAX_NOTE_BYTES);
        noteInput.value = val;
      }

      const currentBytes = measureUtf8Bytes(noteInput.value);
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

// --- Bloque 1: Botones de vida o muerte (stubs vacíos) ------------------------

function setupBloque1() {
  const btnSos = document.getElementById("btn-sos-escombros");
  const btnASalvo = document.getElementById("btn-a-salvo");

  if (btnSos) {
    btnSos.onclick = () => {
      // Stub vacío según contrato: pendiente (D1 / D2a)
      console.log("pendiente (D1 / D2a): activación de modo escombros");
      showToast("Modo escombros pendiente de implementación (D1)");
    };
  }

  if (btnASalvo) {
    btnASalvo.onclick = () => {
      // Stub vacío según contrato: pendiente (D1 / D2a)
      console.log("pendiente (D1 / D2a): reporte estoy a salvo");
      showToast("Estoy a salvo pendiente de implementación (D2a)");
    };
  }
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
      });

      const noteText = noteInput ? noteInput.value : "";
      const { fullBytes } = buildReportBinary({
        headerFields,
        noteText,
      });

      // Encolar y comenzar transmisión
      const item = await uiState.queue.enqueue({
        headerFields,
        fullBytes,
      });

      uiState.lastReportId = item.id;
      showToast("¡Reporte guardado! Transmitiendo auxilio...");
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

  if ("getBattery" in navigator) {
    navigator.getBattery().then((battery) => {
      function updateBattery() {
        const level = Math.round(battery.level * 100);
        if (batteryText) {
          batteryText.textContent = `🔋 ${level}%`;
        }
      }
      updateBattery();
      battery.addEventListener("levelchange", updateBattery);
    }).catch(() => {});
  }
}

// --- Inicialización Principal -------------------------------------------------

async function init() {
  registerServiceWorker();
  setupConnectivityAndBattery();
  setupBloque1();
  setupIncidentButtons();
  setupObservationAndFlags();
  setupLocationAndNote();
  await loadBarrios();

  // Inicializar cola de reportes con IndexedDB
  uiState.queue = createReportQueue({
    onStatusChange: (item) => {
      if (item.id === uiState.lastReportId) {
        updatePipelineUi(item.status);
      }
    },
  });

  setupBloque3();
  uiState.queue.start();
}

window.addEventListener("DOMContentLoaded", init);
