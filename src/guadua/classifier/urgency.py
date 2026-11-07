"""Niveles de urgencia y regla del piso. CONTRATO: el piso nunca baja."""

from enum import IntEnum


class Urgency(IntEnum):
    BAJA = 1
    MEDIA = 2
    ALTA = 3
    CRITICA = 4


def apply_floor(floor: Urgency, proposed: Urgency) -> Urgency:
    """Única forma permitida de combinar una urgencia nueva con el piso: solo sube."""
    return max(Urgency(floor), Urgency(proposed))
