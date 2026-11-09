"""Decide qué le pasa a cada trama según el perfil. Lo comparten la red simulada y el proxy."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from guadua.channel.profiles import ChannelProfile


@dataclass(frozen=True, slots=True)
class Verdict:
    delays: tuple[float, ...]  # vacío: perdida; dos valores: duplicada
    in_cut: bool  # se perdió por estar dentro de un corte
    cut_started: bool
    cut_ended: bool

    @property
    def lost(self) -> bool:
        return not self.delays

    @property
    def duplicated(self) -> bool:
        return len(self.delays) > 1


@dataclass(slots=True)
class FaultStats:
    sent: int = 0
    lost: int = 0
    lost_in_cut: int = 0
    duplicated: int = 0
    delivered: int = 0  # copias entregadas, contando duplicados

    @property
    def measured_loss(self) -> float:
        return self.lost / self.sent if self.sent else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "sent": self.sent,
            "lost": self.lost,
            "lost_in_cut": self.lost_in_cut,
            "duplicated": self.duplicated,
            "delivered": self.delivered,
            "measured_loss": round(self.measured_loss, 4),
        }


class FaultModel:
    """Con la misma semilla y la misma secuencia de tramas, decide siempre lo mismo.

    El perfil se puede cambiar en vivo; el estado de corte y los contadores se conservan.
    """

    def __init__(self, profile: ChannelProfile, rng: random.Random) -> None:
        self.profile = profile
        self.stats = FaultStats()
        self.in_cut = False
        self._rng = rng

    def decide(self) -> Verdict:
        p, rng = self.profile, self._rng
        was_in_cut = self.in_cut
        if self.in_cut:
            self.in_cut = rng.random() >= 1 / p.cut_length
        else:
            self.in_cut = rng.random() < p.cut_rate
        started, ended = self.in_cut and not was_in_cut, was_in_cut and not self.in_cut
        self.stats.sent += 1
        if self.in_cut or rng.random() < p.loss:
            self.stats.lost += 1
            self.stats.lost_in_cut += self.in_cut
            return Verdict((), self.in_cut, started, ended)
        copies = 2 if rng.random() < p.duplicate else 1
        self.stats.duplicated += copies - 1
        self.stats.delivered += copies
        delays = tuple(rng.uniform(p.delay_min, p.delay_max) for _ in range(copies))
        return Verdict(delays, False, started, ended)
