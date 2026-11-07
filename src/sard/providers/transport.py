"""Interfaz de transporte (canal simulado, HTTP, radio, LoRa, SMS). CONTRATO."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Transport(Protocol):
    mtu: int
    """Tamaño máximo de trama en bytes del perfil activo (p. ej., 140 en SMS)."""

    async def send(self, destination: str, frame: bytes) -> None:
        """Envía una trama de hasta `mtu` bytes. No garantiza entrega (para eso está el ACK)."""
        ...

    async def receive(self, timeout: float) -> tuple[str, bytes] | None:
        """Devuelve (origen, trama) o None si no llegó nada en `timeout` segundos."""
        ...
