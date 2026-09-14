"""
O que conta como cobranca repetida. Fica fora de src/steps porque validate (a
duplicidade existe?) e compute (quanto devolver?) precisam da mesma definicao, e
etapas nao importam umas das outras.
"""

from __future__ import annotations

from datetime import timedelta

from src.schemas import Transacao

# Cobranca do mesmo estabelecimento e mesmo valor ate esta janela depois da anterior
# e' repeticao, nao compra nova.
JANELA_DUPLICIDADE_HORAS = 24


def grupos_repetidos(transacoes: list[Transacao]) -> list[list[Transacao]]:
    """Grupos de 2+ cobrancas iguais, cada uma ate a janela depois da anterior.

    Encadeado: tres cobrancas com 20h entre cada uma formam um grupo so', mesmo com
    40h entre a primeira e a ultima. Grupos em ordem cronologica.
    """
    janela = timedelta(hours=JANELA_DUPLICIDADE_HORAS)
    grupos: list[list[Transacao]] = []
    ultimo_por_chave: dict[tuple, list[Transacao]] = {}

    for transacao in sorted(transacoes, key=lambda t: t.data_hora):
        chave = (transacao.estabelecimento, transacao.valor)
        grupo = ultimo_por_chave.get(chave)
        if grupo is not None and transacao.data_hora - grupo[-1].data_hora <= janela:
            grupo.append(transacao)
        else:
            grupo = [transacao]
            ultimo_por_chave[chave] = grupo
            grupos.append(grupo)

    return [g for g in grupos if len(g) > 1]
