// Texto opcional del reporte, comprimido con diccionario. Equivale a src/guadua/protocol/text.py (N1).
// CONTRATO: ver docs/contratos.md y tests/fixtures/text_vectors.json.
//
// Va después de los 21 B de la cabecera y ocupa como máximo 100 B:
// - 0 B: sin texto.
// - 1 B de prefijo + datos. 4 bits altos: versión del diccionario (hoy 1).
//   4 bits bajos: modo. 0 = UTF-8 sin comprimir; 1 = deflate crudo con el diccionario de Manizales.
//
// Parámetros de compresión copiados de text.py: zlib.compressobj(9, DEFLATED, -15, zdict=...)
// → nivel 9, deflate crudo (sin encabezado ni Adler-32), ventana de 15 bits, memLevel 8 y
// estrategia por omisión (los valores por omisión de zlib, que text.py no cambia).
//
// El diccionario son los bytes exactos de data/diccionario_manizales.txt. Este módulo no lo lee:
// quien lo usa le pasa los bytes (en Node con fs; en el navegador con fetch).

import { Deflate, Inflate } from "../vendor/pako/pako.esm.mjs";

export const MAX_TEXT_BYTES = 100;
export const DICT_VERSION = 1;
/** Tope de bytes descomprimidos (text.py: decomp.decompress(data, MAX_TEXT_BYTES * 20)). */
export const MAX_INFLATED_BYTES = MAX_TEXT_BYTES * 20;

export const TextMode = Object.freeze({ PLANO: 0, DICCIONARIO: 1 });

const DEFLATE_OPTIONS = Object.freeze({
  raw: true,
  level: 9,
  windowBits: 15,
  memLevel: 8,
  strategy: 0,
});

export class TextError extends Error {}

function checkDictionary(dictionary) {
  if (!(dictionary instanceof Uint8Array) || dictionary.length === 0) {
    throw new TextError("se requiere el diccionario como Uint8Array no vacío");
  }
  if (dictionary.length > 32 * 1024) {
    throw new TextError(`el diccionario no cabe en la ventana de 32 KB: ${dictionary.length} B`);
  }
}

function compress(raw, dictionary) {
  const deflator = new Deflate({ ...DEFLATE_OPTIONS, dictionary });
  deflator.push(raw, true);
  if (deflator.err) throw new TextError(`deflate falló: ${deflator.msg || deflator.err}`);
  return deflator.result;
}

function decompress(data, dictionary) {
  // pako.inflate() no tiene tope de salida: se usa Inflate con onData y se corta en cuanto
  // lo decodificado supera MAX_INFLATED_BYTES (protección contra una bomba de descompresión).
  const inflator = new Inflate({ raw: true, windowBits: 15, dictionary, chunkSize: 256 });
  const chunks = [];
  let total = 0;
  inflator.onData = (chunk) => {
    total += chunk.length;
    if (total > MAX_INFLATED_BYTES) {
      throw new TextError(`el texto descomprimido supera ${MAX_INFLATED_BYTES} B`);
    }
    chunks.push(chunk.slice());
  };
  // Se reemplaza onEnd (que por omisión une los trozos) pero se conserva el código de error.
  inflator.onEnd = (status) => {
    inflator.err = status;
    inflator.msg = inflator.strm.msg;
  };

  try {
    inflator.push(data, true);
  } catch (err) {
    if (err instanceof TextError) throw err;
    throw new TextError(`deflate inválido: ${err.message}`);
  }
  if (inflator.err) {
    throw new TextError(`deflate inválido: ${inflator.msg || inflator.err}`);
  }
  // Sin el bloque final el flujo no termina (deflate truncado).
  if (!inflator.ended) {
    throw new TextError("deflate incompleto o con bytes de sobra");
  }
  // En modo crudo, lo que queda sin consumir tras el final son bytes de sobra.
  if (inflator.strm.avail_in > 0) {
    throw new TextError("deflate incompleto o con bytes de sobra");
  }

  const out = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.length;
  }
  return out;
}

/** Devuelve los bytes que van después de la cabecera (como máximo 100 B). */
export function encodeText(text, dictionary) {
  if (typeof text !== "string") throw new TextError(`se esperaba string: ${text}`);
  checkDictionary(dictionary);
  if (text === "") return new Uint8Array(0);
  // Python rechaza al codificar un surrogate suelto; TextEncoder lo cambiaría por U+FFFD.
  if (typeof text.isWellFormed === "function" && !text.isWellFormed()) {
    throw new TextError("el texto tiene caracteres UTF-16 sueltos");
  }

  const raw = new TextEncoder().encode(text);
  const packed = compress(raw, dictionary);
  const useDict = packed.length < raw.length;
  const mode = useDict ? TextMode.DICCIONARIO : TextMode.PLANO;
  const body = useDict ? packed : raw;

  const out = new Uint8Array(1 + body.length);
  out[0] = (DICT_VERSION << 4) | mode;
  out.set(body, 1);
  if (out.length > MAX_TEXT_BYTES) {
    throw new TextError(`el texto ocupa ${out.length} B; el máximo es ${MAX_TEXT_BYTES} B`);
  }
  return out;
}

/** Interpreta los bytes que siguen a la cabecera. Sin bytes, el texto es vacío. */
export function decodeText(data, dictionary) {
  if (!(data instanceof Uint8Array)) throw new TextError("se esperaba Uint8Array");
  checkDictionary(dictionary);
  if (data.length === 0) return "";
  if (data.length > MAX_TEXT_BYTES) {
    throw new TextError(`llegaron ${data.length} B de texto; el máximo es ${MAX_TEXT_BYTES} B`);
  }
  const version = data[0] >> 4;
  const mode = data[0] & 0x0f;
  if (version !== DICT_VERSION) {
    throw new TextError(`versión de diccionario no soportada: ${version}`);
  }

  let raw;
  if (mode === TextMode.PLANO) {
    raw = data.subarray(1);
  } else if (mode === TextMode.DICCIONARIO) {
    raw = decompress(data.subarray(1), dictionary);
  } else {
    throw new TextError(`modo de texto desconocido: ${mode}`);
  }
  if (raw.length === 0) throw new TextError("prefijo sin texto");

  try {
    // fatal: rechaza UTF-8 inválido como Python. ignoreBOM: conserva un U+FEFF inicial como Python.
    return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(raw);
  } catch {
    throw new TextError("el texto no es UTF-8 válido");
  }
}
