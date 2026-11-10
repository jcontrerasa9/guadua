// Codificador y decodificador de geohash para la PWA ciudadana.
// Complementa citizen/protocol/header.js (que solo convierte geohash <-> entero).

export const GEOHASH_ALPHABET = "0123456789bcdefghjkmnpqrstuvwxyz";
const BITS = [16, 8, 4, 2, 1];

/**
 * Codifica coordenadas (lat, lon) a un string geohash en base32 de longitud dada.
 *
 * @param {number} lat - Latitud (-90 a 90)
 * @param {number} lon - Longitud (-180 a 180)
 * @param {number} [precision=7] - Número de caracteres del geohash (por defecto 7)
 * @returns {string} Geohash de longitud precision
 */
export function encodeGeohash(lat, lon, precision = 7) {
  if (typeof lat !== "number" || typeof lon !== "number" || Number.isNaN(lat) || Number.isNaN(lon)) {
    throw new Error(`Coordenadas inválidas: lat=${lat}, lon=${lon}`);
  }
  if (lat < -90 || lat > 90) {
    throw new Error(`Latitud fuera de rango [-90, 90]: ${lat}`);
  }
  if (lon < -180 || lon > 180) {
    throw new Error(`Longitud fuera de rango [-180, 180]: ${lon}`);
  }

  const latInterval = [-90.0, 90.0];
  const lonInterval = [-180.0, 180.0];
  let isEven = true;
  let ch = 0;
  let bit = 0;
  let geohash = "";

  while (geohash.length < precision) {
    if (isEven) {
      const mid = (lonInterval[0] + lonInterval[1]) / 2.0;
      if (lon >= mid) {
        ch |= BITS[bit];
        lonInterval[0] = mid;
      } else {
        lonInterval[1] = mid;
      }
    } else {
      const mid = (latInterval[0] + latInterval[1]) / 2.0;
      if (lat >= mid) {
        ch |= BITS[bit];
        latInterval[0] = mid;
      } else {
        latInterval[1] = mid;
      }
    }

    isEven = !isEven;
    if (bit < 4) {
      bit++;
    } else {
      geohash += GEOHASH_ALPHABET[ch];
      bit = 0;
      ch = 0;
    }
  }

  return geohash;
}

/**
 * Decodifica un string geohash a { lat, lon }.
 *
 * @param {string} geohash
 * @returns {{ lat: number, lon: number }}
 */
export function decodeGeohash(geohash) {
  if (typeof geohash !== "string" || geohash.length === 0) {
    throw new Error(`geohash inválido: ${geohash}`);
  }

  const latInterval = [-90.0, 90.0];
  const lonInterval = [-180.0, 180.0];
  let isEven = true;

  for (const char of geohash.toLowerCase()) {
    const val = GEOHASH_ALPHABET.indexOf(char);
    if (val === -1) {
      throw new Error(`carácter de geohash inválido: '${char}'`);
    }

    for (const mask of BITS) {
      const bit = (val & mask) ? 1 : 0;
      if (isEven) {
        const mid = (lonInterval[0] + lonInterval[1]) / 2.0;
        if (bit === 1) {
          lonInterval[0] = mid;
        } else {
          lonInterval[1] = mid;
        }
      } else {
        const mid = (latInterval[0] + latInterval[1]) / 2.0;
        if (bit === 1) {
          latInterval[0] = mid;
        } else {
          latInterval[1] = mid;
        }
      }
      isEven = !isEven;
    }
  }

  return {
    lat: (latInterval[0] + latInterval[1]) / 2.0,
    lon: (lonInterval[0] + lonInterval[1]) / 2.0,
  };
}
