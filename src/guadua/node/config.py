"""Configuración del nodo, leída del entorno. Nunca usa `.env` (regla 8 de AGENTS.md).

Variables (todas opcionales; sin ninguna, arranca un nodo "A" solo, en memoria):
    SARD_NODE_ID          nombre del nodo                         A
    SARD_PEERS            vecinos de gossip, separados por coma   (ninguno)
    SARD_PEER_ADDRS       dirección UDP de cada vecino            B=node-b:9000,C=node-c:9000
    SARD_DB               archivo SQLite; ":memory:" sin disco    :memory:
    SARD_HTTP_HOST        dónde escucha la API                    127.0.0.1
    SARD_HTTP_PORT                                                 8000
    SARD_UDP_HOST         dónde escucha el gossip                 0.0.0.0
    SARD_UDP_PORT                                                  9000
    SARD_MTU              trama del gossip en bytes               200 (como LoRa)
    SARD_CHANNEL_PROFILE  fallos del canal: wifi, lora, sms…      (ninguno)
    SARD_CHANNEL_SEED     semilla de los fallos                   0
    SARD_GOSSIP_INTERVAL  segundos entre rondas                   2.0
    SARD_PEER_TIMEOUT     segundos sin latidos → vecino caído     10.0
    SARD_QUESTION_KINDS   tipos de pregunta que muestra la PWA    si_no
                          (si_no, opciones, texto; separados por coma)
    SARD_VIGIA_INTERVAL   segundos entre rondas del vigía (D6)    30
    SARD_VIGIA_CRITICO_S  sin atender → alerta, crítico o alto    300
    SARD_VIGIA_OLVIDO_S   sin atender → solicitud, los demás      900
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

QUESTION_KINDS = frozenset({"si_no", "opciones", "texto"})  # agents/recepcion.ALL_KINDS


class ConfigError(ValueError):
    """Variable de entorno inválida."""


@dataclass(frozen=True, slots=True)
class NodeConfig:
    node_id: str = "A"
    peers: tuple[str, ...] = ()
    peer_addrs: dict[str, tuple[str, int]] = field(default_factory=dict)
    db_path: str = ":memory:"
    http_host: str = "127.0.0.1"
    http_port: int = 8000
    udp_host: str = "0.0.0.0"
    udp_port: int = 9000
    mtu: int = 200
    channel_profile: str | None = None
    channel_seed: int = 0
    gossip_interval: float = 2.0
    peer_timeout: float = 10.0
    question_kinds: tuple[str, ...] = ("si_no",)
    vigia_interval: float = 30.0
    vigia_critico_s: float = 300.0
    vigia_olvido_s: float = 900.0

    def __post_init__(self) -> None:
        if not self.node_id.isalnum():
            raise ConfigError(f"SARD_NODE_ID debe ser alfanumérico: {self.node_id!r}")
        if self.node_id in self.peers:
            raise ConfigError("un nodo no puede ser su propio vecino")
        missing = [p for p in self.peers if p not in self.peer_addrs]
        if self.peer_addrs and missing:
            raise ConfigError(f"faltan direcciones en SARD_PEER_ADDRS para {missing}")
        bad = set(self.question_kinds) - QUESTION_KINDS
        if self.vigia_interval <= 0 or self.vigia_critico_s < 0 or self.vigia_olvido_s < 0:
            raise ConfigError("SARD_VIGIA_*: el intervalo va > 0 y los umbrales ≥ 0")
        if not self.question_kinds or bad:
            raise ConfigError(
                f"SARD_QUESTION_KINDS inválido: {sorted(bad) or 'vacío'} "
                f"(opciones: {sorted(QUESTION_KINDS)})"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> NodeConfig:
        env = os.environ if env is None else env

        def number(name: str, default: str, kind: type) -> int | float:
            raw = env.get(name, default)
            try:
                return kind(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} inválido: {raw!r}") from exc

        peers = tuple(p.strip() for p in env.get("SARD_PEERS", "").split(",") if p.strip())
        return cls(
            node_id=env.get("SARD_NODE_ID", "A"),
            peers=peers,
            peer_addrs=_parse_addrs(env.get("SARD_PEER_ADDRS", "")),
            db_path=env.get("SARD_DB", ":memory:"),
            http_host=env.get("SARD_HTTP_HOST", "127.0.0.1"),
            http_port=int(number("SARD_HTTP_PORT", "8000", int)),
            udp_host=env.get("SARD_UDP_HOST", "0.0.0.0"),
            udp_port=int(number("SARD_UDP_PORT", "9000", int)),
            mtu=int(number("SARD_MTU", "200", int)),
            channel_profile=env.get("SARD_CHANNEL_PROFILE") or None,
            channel_seed=int(number("SARD_CHANNEL_SEED", "0", int)),
            gossip_interval=float(number("SARD_GOSSIP_INTERVAL", "2.0", float)),
            peer_timeout=float(number("SARD_PEER_TIMEOUT", "10.0", float)),
            question_kinds=tuple(
                k.strip() for k in env.get("SARD_QUESTION_KINDS", "si_no").split(",") if k.strip()
            ),
            vigia_interval=float(number("SARD_VIGIA_INTERVAL", "30", float)),
            vigia_critico_s=float(number("SARD_VIGIA_CRITICO_S", "300", float)),
            vigia_olvido_s=float(number("SARD_VIGIA_OLVIDO_S", "900", float)),
        )


def _parse_addrs(raw: str) -> dict[str, tuple[str, int]]:
    addrs: dict[str, tuple[str, int]] = {}
    for item in filter(None, (part.strip() for part in raw.split(","))):
        try:
            name, address = item.split("=", 1)
            host, port = address.rsplit(":", 1)
            addrs[name.strip()] = (host.strip(), int(port))
        except ValueError as exc:
            raise ConfigError(
                f"SARD_PEER_ADDRS: se esperaba NODO=host:puerto, llegó {item!r}"
            ) from exc
    return addrs
