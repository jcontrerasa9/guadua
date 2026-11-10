// Panel de radio VHF del tablero (D7): clips, su transcripción y el respaldo manual.
// Funciones puras para armar el HTML; app.js las usa y hace las llamadas a la API:
//   GET  /api/radio                            lista de clips (el más reciente primero)
//   GET  /api/radio/{audio_id}/audio           el audio, si este nodo lo tiene
//   POST /api/radio                            subir un clip (cuerpo: el audio)
//   POST /api/radio/demo/{clip_id}             clip grabado por el equipo (data/radio/)
//   POST /api/radio/{audio_id}/transcription   el operador escribe o corrige el texto

export const DEMO_CLIP = "clip_colapso_dos_incidentes";

const AUDIO_BY_EXTENSION = {
  ogg: "audio/ogg", opus: "audio/ogg", oga: "audio/ogg", wav: "audio/wav", mp3: "audio/mpeg",
  m4a: "audio/mp4", mp4: "audio/mp4", aac: "audio/aac", webm: "audio/webm", flac: "audio/flac",
  "3gp": "audio/3gpp", amr: "audio/amr",
};

/**
 * El Content-Type para subir un archivo. Por la extensión primero: los navegadores a veces
 * dan "application/ogg" o nada a una nota de voz (WhatsApp).
 */
export function audioContentType(file) {
  const ext = String(file?.name || "").split(".").pop().toLowerCase();
  if (AUDIO_BY_EXTENSION[ext]) return AUDIO_BY_EXTENSION[ext];
  const type = String(file?.type || "");
  return type.startsWith("audio/") ? type : "application/octet-stream";
}

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

/** Firma de la lista: si no cambia, no se vuelve a dibujar (no corta el audio ni lo que se escribe). */
export function radioSignature(clips) {
  return JSON.stringify(
    (clips || []).map((c) => [c.audio_id, c.status, c.transcription_id, c.report_ids, c.note]),
  );
}

/** "Whisper (small) · 82 %", "operador comandante-1", "pendiente". */
export function radioSourceLabel(clip) {
  if (clip.status !== "transcrito") return "pendiente";
  if (clip.source === "operador") return `corregido por ${clip.operator || "operador"}`;
  const confidence =
    typeof clip.confidence === "number" ? ` · ${Math.round(clip.confidence * 100)} %` : "";
  return `${clip.model || "Whisper"}${confidence}`;
}

/** HTML de un clip. `nodeBase` sirve el audio; `draft` es lo que el operador lleva escrito. */
export function radioClipHtml(clip, nodeBase, draft = null) {
  const id = escapeHtml(clip.audio_id);
  const audio = clip.has_audio
    ? `<audio controls preload="none" src="${escapeHtml(nodeBase)}/api/radio/${id}/audio"></audio>`
    : `<span class="radio-muted">Audio guardado en otro nodo</span>`;
  const text = clip.text
    ? `<p class="radio-text">«${escapeHtml(clip.text)}»</p>`
    : `<p class="radio-text radio-pending">Sin transcripción: ${escapeHtml(clip.note || "escriba el texto")}</p>`;
  const reports = (clip.report_ids || []).length
    ? `<span>${clip.report_ids.length} reporte(s): ${clip.report_ids.map(escapeHtml).join(", ")}` +
      `${clip.split_by ? ` · separado por ${escapeHtml(clip.split_by)}` : ""}</span>`
    : "";
  const value = draft ?? clip.text ?? "";
  return `
    <div class="radio-clip ${clip.status === "pendiente" ? "pending" : ""}" data-audio-id="${id}">
      <div class="radio-clip-head">
        <strong>📻 ${escapeHtml(clip.name || clip.audio_id)}</strong>
        <span class="radio-muted">${escapeHtml(radioSourceLabel(clip))}${clip.corrections ? ` · ${clip.corrections} corrección(es)` : ""}</span>
      </div>
      ${audio}
      ${text}
      <div class="radio-meta">${reports}</div>
      <details class="radio-fix" ${clip.status === "pendiente" ? "open" : ""}>
        <summary>${clip.status === "pendiente" ? "Escribir el texto" : "Corregir el texto"}</summary>
        <textarea class="radio-fix-text" rows="3" data-audio-id="${id}">${escapeHtml(value)}</textarea>
        <button class="btn-secondary-sm radio-fix-save" data-audio-id="${id}">Guardar (crea reportes nuevos)</button>
      </details>
    </div>`;
}

export function radioListHtml(clips, nodeBase, drafts = {}) {
  if (!clips || clips.length === 0) {
    return `<p class="radio-muted">No hay clips de radio todavía.</p>`;
  }
  return clips.map((c) => radioClipHtml(c, nodeBase, drafts[c.audio_id] ?? null)).join("");
}
