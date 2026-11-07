"""Punto de entrada provisional: solo identifica el nodo."""

import os

from guadua import __version__


def main() -> None:
    node_id = os.environ.get("SARD_NODE_ID", "A")
    print(f"guadua {__version__} · nodo {node_id} (sin funcionalidades todavía)")


if __name__ == "__main__":
    main()
