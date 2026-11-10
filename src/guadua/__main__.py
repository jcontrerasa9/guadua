"""Arranca un nodo: python -m guadua. La configuración sale del entorno (node/config.py)."""

from __future__ import annotations

import logging

from guadua import __version__
from guadua.channel.profiles import profile_by_name
from guadua.node.config import NodeConfig
from guadua.node.service import Node
from guadua.node.udp import UdpTransport
from guadua.providers.fallback import llm_from_env
from guadua.store.sqlite import SQLiteEventStore


def build_node(config: NodeConfig) -> Node:
    store = SQLiteEventStore(config.db_path)
    transport = None
    if config.peers:
        profile = profile_by_name(config.channel_profile) if config.channel_profile else None
        transport = UdpTransport(
            config.node_id,
            (config.udp_host, config.udp_port),
            config.peer_addrs,
            mtu=config.mtu,
            profile=profile,
            seed=config.channel_seed,
        )
    llm = llm_from_env()  # LLM_PROVIDER, GEMINI_*, OLLAMA_* del entorno; None sin ninguno
    logging.getLogger("guadua").info(
        "LLM de los agentes de triaje y recepción: %s",
        llm.name if llm else "ninguno (triaje: ambiguo → revisar; recepción: reglas)",
    )
    return Node(
        config.node_id,
        store,
        transport=transport,
        peers=config.peers,
        llm=llm,
        peer_timeout=config.peer_timeout,
        recepcion_llm=llm,
        question_kinds=frozenset(config.question_kinds),
    )


def main() -> None:
    import uvicorn

    from guadua.node.app import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    config = NodeConfig.from_env()
    logging.getLogger("guadua").info(
        "guadua %s · nodo %s · vecinos %s · base %s",
        __version__, config.node_id, ",".join(config.peers) or "-", config.db_path,
    )  # fmt: skip
    app = create_app(build_node(config), gossip_interval=config.gossip_interval)
    uvicorn.run(app, host=config.http_host, port=config.http_port)


if __name__ == "__main__":
    main()
