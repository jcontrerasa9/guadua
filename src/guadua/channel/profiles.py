"""Perfiles del canal simulado (docs/diseno.md §5.3).

Son parámetros de simulación, no mediciones de equipos reales: se ajustan sin tocar contratos.

Cada trama pasa por dos filtros:
1. Cortes (modelo de Gilbert-Elliott): en estado bueno, cada trama abre un corte con
   probabilidad `cut_rate`; en corte, cada trama lo cierra con probabilidad 1/`cut_length`.
   Mientras dura el corte se pierde todo.
2. Pérdida suelta: fuera del corte, cada trama se pierde con probabilidad `loss`.
Lo que pasa se retrasa entre `delay_min` y `delay_max` segundos (de ahí el desorden) y se
duplica con probabilidad `duplicate`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from typing import Any


class ChannelError(ValueError):
    """Parámetro del canal inválido."""


@dataclass(frozen=True, slots=True)
class ChannelProfile:
    name: str
    mtu: int  # bytes por trama
    loss: float  # probabilidad de perder una trama fuera de los cortes
    delay_min: float  # segundos
    delay_max: float  # segundos
    duplicate: float  # probabilidad de entregar una trama dos veces
    cut_rate: float = 0.0  # probabilidad por trama de que empiece un corte
    cut_length: float = 1.0  # tramas perdidas en promedio por corte

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ChannelError("el perfil necesita un nombre")
        if isinstance(self.mtu, bool) or not isinstance(self.mtu, int) or self.mtu < 1:
            raise ChannelError(f"mtu inválida: {self.mtu!r}")
        for name in ("loss", "duplicate", "cut_rate"):
            _probability(name, getattr(self, name))
        for name in ("delay_min", "delay_max", "cut_length"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
                raise ChannelError(f"{name} inválido: {value!r}")
        if self.delay_min > self.delay_max:
            raise ChannelError("delay_min no puede ser mayor que delay_max")
        if self.cut_length < 1:
            raise ChannelError("cut_length debe ser al menos 1")

    @property
    def cut_fraction(self) -> float:
        """Fracción de tramas que caen dentro de un corte (estado estacionario)."""
        exit_rate = 1 / self.cut_length
        return self.cut_rate / (self.cut_rate + exit_rate)

    @property
    def expected_loss(self) -> float:
        """Pérdida total esperada: cortes más pérdida suelta."""
        cut = self.cut_fraction
        return cut + (1 - cut) * self.loss

    def with_changes(self, **changes: Any) -> ChannelProfile:
        """Copia con parámetros cambiados. Rechaza nombres de campo desconocidos."""
        unknown = set(changes) - {f.name for f in fields(self)}
        if unknown:
            raise ChannelError(f"parámetros desconocidos: {sorted(unknown)}")
        return replace(self, **changes)

    def with_loss(self, loss: float) -> ChannelProfile:
        return self.with_changes(loss=loss)

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "expected_loss": round(self.expected_loss, 4)}


def _probability(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        raise ChannelError(f"{name} debe estar entre 0 y 1: {value!r}")


WIFI = ChannelProfile(
    "wifi", 1200, loss=0.05, delay_min=0.005, delay_max=0.05, duplicate=0.01,
    cut_rate=0.002, cut_length=25,
)  # fmt: skip
LORA = ChannelProfile(
    "lora", 200, loss=0.30, delay_min=0.2, delay_max=1.5, duplicate=0.0,
    cut_rate=0.01, cut_length=8,
)  # fmt: skip
SMS = ChannelProfile("sms", 140, loss=0.05, delay_min=2.0, delay_max=30.0, duplicate=0.02)
IDEAL = ChannelProfile("ideal", 1200, loss=0.0, delay_min=0.0, delay_max=0.0, duplicate=0.0)

PROFILES: dict[str, ChannelProfile] = {p.name: p for p in (WIFI, LORA, SMS, IDEAL)}


def profile_by_name(name: str) -> ChannelProfile:
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise ChannelError(f"perfil desconocido: {name!r}; hay {sorted(PROFILES)}") from exc
