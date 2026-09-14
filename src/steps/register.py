"""
Registro do caso resolvido automaticamente.

Deterministico, sem LLM: a resposta ao cliente sai de template. O protocolo deriva
do id_caso, entao reprocessar o mesmo caso gera o mesmo protocolo em vez de abrir
um segundo registro.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal

from src.schemas import RegistroCaso, ResultadoCalculo, SolicitacaoBruta, StatusExecucao

RESPOSTA = (
    "Recebemos sua contestação e ela foi aceita. O estorno de {valor} será lançado "
    "em até {prazo} dias úteis. Protocolo {protocolo}."
)


def gerar_protocolo(id_caso: str) -> str:
    return "CTS-" + hashlib.sha256(id_caso.encode("utf-8")).hexdigest()[:12].upper()


def registrar(
    solicitacao: SolicitacaoBruta,
    calculo: ResultadoCalculo,
    status: StatusExecucao,
    registrado_em: datetime,
) -> RegistroCaso:
    if status not in (StatusExecucao.AUTOMATICO, StatusExecucao.ESCALADO_MODELO):
        raise ValueError(f"registro automatico nao aceita status {status.value}")

    protocolo = gerar_protocolo(solicitacao.id_caso)
    return RegistroCaso(
        id_caso=solicitacao.id_caso,
        protocolo=protocolo,
        registrado_em=registrado_em,
        status=status,
        resposta_cliente=RESPOSTA.format(
            valor=_brl(calculo.valor_estorno),
            prazo=calculo.prazo_resposta_dias,
            protocolo=protocolo,
        ),
    )


def _brl(valor: Decimal) -> str:
    """Decimal('1079.70') -> 'R$ 1.079,70'."""
    return "R$ " + f"{valor:,.2f}".translate(str.maketrans(",.", ".,"))
