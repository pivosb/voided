"""
Politica de roteamento entre tiers.

Funcao pura: recebe o resultado da ultima etapa e devolve a proxima rota, com o
motivo em texto para a trilha de auditoria. O orquestrador grava toda decisao em
EventoExecucao.rotas, inclusive as que so' confirmam o tier atual.

Politica:
  1. A extracao comeca no tier pequeno.
  2. Saida fora do schema ou confianca abaixo do limiar sobe um degrau na escada.
     Depois de MAX_ESCALADAS subidas, o caso vai para a fila humana.
  3. Com extracao aceita e calculo feito, faixa de risco alta vai para humano;
     baixa e media seguem para registro automatico.
"""

from __future__ import annotations

from typing import Literal

from src.schemas import DecisaoRoteamento, TierModelo

ESCADA = (TierModelo.PEQUENO, TierModelo.MEDIO, TierModelo.GRANDE)
MAX_ESCALADAS = 2

# Provisorio: o limiar so' e' defensavel depois de calibrar a confianca
# (notebooks/analise.ipynb). Igual ao limiar conta como aceito.
LIMIAR_CONFIANCA = 0.70


def decidir(
    tentativa: int,
    falha_schema: bool,
    confianca: float | None,
    faixa_risco: Literal["baixa", "media", "alta"] | None,
) -> DecisaoRoteamento:
    """`tentativa` e' quantas extracoes ja foram feitas (0 = caso acabou de chegar).

    `confianca` e' a da ultima extracao; None so' quando ela falhou no schema ou
    ainda nao houve extracao. `faixa_risco` so' vem preenchida depois do calculo.
    """
    if not 0 <= tentativa <= 1 + MAX_ESCALADAS:
        raise ValueError(f"tentativa {tentativa} fora de 0..{1 + MAX_ESCALADAS}")

    if tentativa == 0:
        return DecisaoRoteamento(
            tier=ESCADA[0],
            motivo_decisao=f"entrada: extracao comeca no tier {ESCADA[0].value}",
            tentativa=1,
        )

    atual = ESCADA[tentativa - 1]

    if falha_schema:
        problema = f"saida fora do schema no tier {atual.value}"
    elif confianca is None:
        raise ValueError("extracao sem falha de schema precisa informar a confianca")
    elif confianca < LIMIAR_CONFIANCA:
        problema = (
            f"confianca {confianca:.2f} abaixo do limiar {LIMIAR_CONFIANCA:.2f} "
            f"no tier {atual.value}"
        )
    else:
        problema = None

    if problema is not None:
        if tentativa > MAX_ESCALADAS:
            return DecisaoRoteamento(
                tier=TierModelo.HUMANO,
                motivo_decisao=f"{problema}; {MAX_ESCALADAS} escaladas esgotadas, fila humana",
                tentativa=tentativa,
            )
        proximo = ESCADA[tentativa]
        return DecisaoRoteamento(
            tier=proximo,
            motivo_decisao=f"{problema}; sobe para {proximo.value}",
            tentativa=tentativa + 1,
        )

    if faixa_risco is None:
        return DecisaoRoteamento(
            tier=atual,
            motivo_decisao=(
                f"confianca {confianca:.2f} >= limiar {LIMIAR_CONFIANCA:.2f}: "
                "extracao aceita, segue para validacao"
            ),
            tentativa=tentativa,
        )

    if faixa_risco == "alta":
        return DecisaoRoteamento(
            tier=TierModelo.HUMANO,
            motivo_decisao="faixa de risco alta: estorno exige analise humana",
            tentativa=tentativa,
        )

    return DecisaoRoteamento(
        tier=atual,
        motivo_decisao=f"faixa de risco {faixa_risco}: registro automatico",
        tentativa=tentativa,
    )
