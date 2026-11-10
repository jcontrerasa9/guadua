// Servidor de desarrollo simulado para la PWA ciudadana (sin dependencias).
// Implementa POST /api/messages respondiendo con MessageAck o ApiError (docs/contratos.md).
// Uso: node citizen/dev/mock-node.mjs [puerto]

import http from "node:http";

const PORT = Number(process.argv[2] || process.env.PORT || 8000);
const HEADER_SIZE = 21;
const seenReports = new Set();

const server = http.createServer((req, res) => {
  // Manejo de CORS
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");

  if (req.method === "OPTIONS") {
    res.writeHead(204);
    res.end();
    return;
  }

  const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);

  if (url.pathname === "/api/health" && req.method === "GET") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({
      node: "MOCK",
      version: "0.1.0-dev",
      peers_alive: [],
    }));
    return;
  }

  if (url.pathname === "/api/messages" && req.method === "POST") {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      const buffer = Buffer.concat(chunks);

      if (buffer.length < HEADER_SIZE) {
        res.writeHead(400, { "Content-Type": "application/json" });
        res.end(JSON.stringify({
          error: `se esperaban al menos 21 bytes, llegaron ${buffer.length}`,
        }));
        return;
      }

      // Decodificar device_id (bytes 1-4) y seq (bytes 5-8)
      const deviceId = buffer.readUInt32BE(1);
      const seq = buffer.readUInt32BE(5);
      const hexDevice = deviceId.toString(16).padStart(8, "0");
      const reportId = `${hexDevice}-${seq}`;

      const duplicate = seenReports.has(reportId);
      seenReports.add(reportId);

      // Respuesta 200 MessageAck
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({
        report_id: reportId,
        status: "recibido",
        duplicate,
      }));
    });
    return;
  }

  res.writeHead(404, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ error: `Ruta no encontrada: ${req.method} ${url.pathname}` }));
});

server.listen(PORT, () => {
  console.log(`Mock Node listo en http://localhost:${PORT}`);
  console.log(`Aceptando POST /api/messages (MessageAck de 21+ bytes)`);
});
