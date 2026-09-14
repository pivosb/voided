"""
Grava o log de execucao: uma linha JSONL de EventoExecucao por caso processado.

Append-only. Reprocessar um caso acrescenta outra linha em vez de reescrever a
anterior: o log e' trilha de auditoria, e quem le decide qual execucao vale.
"""

from __future__ import annotations

from pathlib import Path

from src.schemas import EventoExecucao


def gravar(evento: EventoExecucao, caminho: Path) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("a", encoding="utf-8") as fh:
        fh.write(evento.model_dump_json() + "\n")
