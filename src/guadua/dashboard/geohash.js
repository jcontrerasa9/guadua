// Decodificador de geohash a coordenadas geográficas (latitud, longitud).
// Complementa citizen/protocol/header.js (que solo convierte geohash <-> entero).

export const GEOHASH_ALPHABET = "0123456789bcdefghjkmnpqrstuvwxyz";

/**
 * Decodifica un string geohash a { lat, lon, minLat, maxLat, minLon, maxLon }.
 *
 * @param {string} geohash
 * @returns {{ lat: number, lon: number, minLat: number, maxLat: number, minLon: number, maxLon: number }}
 */
export function decodeGeohash(geohash) {
  if (typeof geohash !== "string" || geohash.length === 0) {
    throw new Error(`geohash inválido: se esperaba string no vacío, llegó ${geohash}`);
  }

  const latInterval = [-90.0, 90.0];
  const lonInterval = [-180.0, 180.0];
  let isEven = true;

  for (const char of geohash.toLowerCase()) {
    const val = GEOHASH_ALPHABET.indexOf(char);
    if (val === -1) {
      throw new Error(`carácter de geohash inválido: '${char}'`);
    }

    for (const mask of [16, 8, 4, 2, 1]) {
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

  const lat = (latInterval[0] + latInterval[1]) / 2.0;
  const lon = (lonInterval[0] + lonInterval[1]) / 2.0;

  return {
    lat,
    lon,
    minLat: latInterval[0],
    maxLat: latInterval[1],
    minLon: lonInterval[0],
    maxLon: lonInterval[1],
  };
}
