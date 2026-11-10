# pako (copia local, versión fija)

| Campo | Valor |
| :--- | :--- |
| Paquete | `pako` |
| Versión | **2.1.0** |
| Origen | registro de npm (`npm pack pako@2.1.0`) |
| SHA-1 del tarball `pako-2.1.0.tgz` | `266cc37f98c7d883545d11335c00fbd4062c9a86` (coincide con `dist.shasum` publicado en npm) |
| Licencia | MIT AND Zlib (ver `LICENSE`) |

## Archivos copiados

| Archivo | Origen en el paquete | SHA-256 |
| :--- | :--- | :--- |
| `pako.esm.mjs` | `package/dist/pako.esm.mjs` | `94d300905740b5f3d1200df0bc79348319d080bce8e6474ed852bda4c22690ee` |
| `LICENSE` | `package/LICENSE` | `a04665b3b2de56c66730c1f720f528175739e4104f79073614aa611da1e85539` |

## Reglas

- No está en ningún `package.json`: es una copia local, sin CDN, para que la PWA funcione sin Internet.
- No se edita. Para actualizar, se baja otra versión, se reemplazan ambos archivos y se actualiza esta tabla
  (y se corre `node --test "tests/js/*.test.mjs"`, que verifica los vectores compartidos con Python).
- La usa `citizen/protocol/text.js` (N16).
