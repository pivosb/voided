"""
Validacao deterministica de elegibilidade da contestacao.

Recebe o dado duro da transacao e o que o modelo extraiu do texto do cliente, e
responde se o caso pode seguir para o calculo automatico. Nenhuma chamada de LLM
entra aqui: o modelo propoe, este modulo decide.

Nenhuma regra interrompe as demais. Todas rodam e todas as violacoes vao para o
resultado, porque o log de execucao e' a fonte das metricas -- violacao nao
registrada e' violacao perdida.
"""

from __future__ import annotations

from decimal import Decimal

from src.duplicidade import JANELA_DUPLICIDADE_HORAS, grupos_repetidos
from src.schemas import (
    ExtracaoContestacao,
    MotivoContestacao,
    RegraViolacao,
    ResultadoValidacao,
    Severidade,
    SolicitacaoBruta,
    Transacao,
    Violacao,
)

# Janela da bandeira para abertura de contestacao, contada em dias de calendario.
PRAZO_CONTESTACAO_DIAS = 120

# Acima disso o historico do cliente vira sinal de risco para o calculo.
LIMITE_CONTESTACOES_12M = 3

MOTIVOS_DE_NAO_AUTORIZACAO = frozenset(
    {MotivoContestacao.NAO_RECONHECIDA, MotivoContestacao.SUSPEITA_FRAUDE}
)


def validar(
    solicitacao: SolicitacaoBruta, extracao: ExtracaoContestacao
) -> ResultadoValidacao:
    """Aplica as regras de elegibilidade e devolve o veredito auditavel.

    O "agora" e' `solicitacao.recebida_em`, entao a funcao e' pura: mesma
    entrada, mesmo resultado, sempre.
    """
    confirmadas, violacoes = _resolver_transacoes(solicitacao, extracao)

    violacoes += _regra_prazo(solicitacao, confirmadas)
    violacoes += _regra_ja_estornada(confirmadas)
    violacoes += _regra_duplicidade(solicitacao, extracao, confirmadas)
    violacoes += _regra_valor_alegado(extracao, confirmadas)
    violacoes += _regra_estorno_parcial_sem_base(extracao, confirmadas)
    violacoes += _regra_motivo_indeterminado(extracao)
    violacoes += _regra_cartao_presente(extracao, confirmadas)
    violacoes += _regra_reincidencia(solicitacao)

    tem_bloqueante = any(v.severidade is Severidade.BLOQUEANTE for v in violacoes)

    return ResultadoValidacao(
        elegivel=not tem_bloqueante and bool(confirmadas),
        violacoes=violacoes,
        transacoes_confirmadas=[t.id_transacao for t in confirmadas],
    )


def _resolver_transacoes(
    solicitacao: SolicitacaoBruta, extracao: ExtracaoContestacao
) -> tuple[list[Transacao], list[Violacao]]:
    """Cruza os ids citados pelo modelo com as transacoes vindas da base."""
    if not extracao.ids_transacoes_citadas:
        return [], [
            Violacao(
                regra=RegraViolacao.SEM_TRANSACAO_CITADA,
                severidade=Severidade.BLOQUEANTE,
                detalhe="A extracao nao citou nenhuma transacao; nao ha o que contestar.",
            )
        ]

    por_id = {t.id_transacao: t for t in solicitacao.transacoes_periodo}
    confirmadas: list[Transacao] = []
    violacoes: list[Violacao] = []

    # dict.fromkeys remove id repetido preservando a ordem: citar duas vezes a
    # mesma transacao dobraria o valor do estorno la' na frente.
    for id_citado in dict.fromkeys(extracao.ids_transacoes_citadas):
        transacao = por_id.get(id_citado)
        if transacao is None:
            violacoes.append(
                Violacao(
                    regra=RegraViolacao.TRANSACAO_NAO_ENCONTRADA,
                    severidade=Severidade.BLOQUEANTE,
                    detalhe=f"Transacao {id_citado} nao consta no periodo consultado.",
                )
            )
        else:
            confirmadas.append(transacao)

    return confirmadas, violacoes


def _regra_prazo(
    solicitacao: SolicitacaoBruta, confirmadas: list[Transacao]
) -> list[Violacao]:
    violacoes: list[Violacao] = []

    for transacao in confirmadas:
        # Subtracao entre .date(), nao entre datetime: o dia da transacao e'
        # periodo de graca inteiro, entao a hora da compra nao consome prazo.
        dias = (solicitacao.recebida_em.date() - transacao.data_hora.date()).days

        if dias > PRAZO_CONTESTACAO_DIAS:
            violacoes.append(
                Violacao(
                    regra=RegraViolacao.PRAZO_EXPIRADO,
                    severidade=Severidade.BLOQUEANTE,
                    detalhe=(
                        f"Transacao {transacao.id_transacao}: {dias} dias decorridos, "
                        f"limite de {PRAZO_CONTESTACAO_DIAS}."
                    ),
                )
            )
        elif dias < 0:
            violacoes.append(
                Violacao(
                    regra=RegraViolacao.DATA_TRANSACAO_FUTURA,
                    severidade=Severidade.ALERTA,
                    detalhe=(
                        f"Transacao {transacao.id_transacao} datada {abs(dias)} dia(s) "
                        "apos a solicitacao."
                    ),
                )
            )

    return violacoes


def _regra_ja_estornada(confirmadas: list[Transacao]) -> list[Violacao]:
    return [
        Violacao(
            regra=RegraViolacao.TRANSACAO_JA_ESTORNADA,
            severidade=Severidade.BLOQUEANTE,
            detalhe=f"Transacao {t.id_transacao} ja possui estorno registrado.",
        )
        for t in confirmadas
        if t.ja_estornada
    ]


def _regra_duplicidade(
    solicitacao: SolicitacaoBruta,
    extracao: ExtracaoContestacao,
    confirmadas: list[Transacao],
) -> list[Violacao]:
    if extracao.motivo is not MotivoContestacao.DUPLICIDADE or not confirmadas:
        return []

    # A transacao citada tem de estar num grupo repetido. Par em outro ponto do
    # periodo nao basta: o calculo so' devolve cobranca repetida que foi citada.
    grupos = grupos_repetidos(solicitacao.transacoes_periodo)
    repetidas = {t.id_transacao for grupo in grupos for t in grupo}
    if any(t.id_transacao in repetidas for t in confirmadas):
        return []

    return [
        Violacao(
            regra=RegraViolacao.DUPLICIDADE_NAO_CONFIRMADA,
            severidade=Severidade.BLOQUEANTE,
            detalhe=(
                "Cliente alega cobranca em duplicidade, mas nenhuma transacao citada "
                "tem outra de mesmo estabelecimento e mesmo valor em ate "
                f"{JANELA_DUPLICIDADE_HORAS}h no periodo."
            ),
        )
    ]


def _regra_valor_alegado(
    extracao: ExtracaoContestacao, confirmadas: list[Transacao]
) -> list[Violacao]:
    if extracao.valor_alegado is None or not confirmadas:
        return []

    if extracao.motivo is MotivoContestacao.VALOR_DIVERGENTE:
        return []

    total = sum((t.valor for t in confirmadas), Decimal("0"))
    if extracao.valor_alegado == total:
        return []

    # Alerta, nao bloqueio: o estorno sai da soma das confirmadas, entao o valor
    # citado errado nao altera o dinheiro devolvido.
    return [
        Violacao(
            regra=RegraViolacao.VALOR_ALEGADO_DIVERGENTE,
            severidade=Severidade.ALERTA,
            detalhe=(
                f"Cliente alega {extracao.valor_alegado}, transacoes confirmadas "
                f"somam {total}."
            ),
        )
    ]


def _regra_estorno_parcial_sem_base(
    extracao: ExtracaoContestacao, confirmadas: list[Transacao]
) -> list[Violacao]:
    if extracao.motivo is not MotivoContestacao.VALOR_DIVERGENTE or not confirmadas:
        return []

    if extracao.valor_alegado is None:
        detalhe = (
            "Motivo valor_divergente sem valor alegado: nao ha base para estorno parcial."
        )
    else:
        cobrado = sum((t.valor for t in confirmadas), Decimal("0"))
        if extracao.valor_alegado < cobrado:
            return []
        detalhe = (
            f"Valor devido {extracao.valor_alegado} nao e' menor que o cobrado "
            f"{cobrado}: nao ha cobranca a maior."
        )

    return [
        Violacao(
            regra=RegraViolacao.ESTORNO_PARCIAL_SEM_BASE,
            severidade=Severidade.BLOQUEANTE,
            detalhe=detalhe,
        )
    ]


def _regra_motivo_indeterminado(extracao: ExtracaoContestacao) -> list[Violacao]:
    if extracao.motivo is not MotivoContestacao.INDETERMINADO:
        return []

    return [
        Violacao(
            regra=RegraViolacao.MOTIVO_INDETERMINADO,
            severidade=Severidade.BLOQUEANTE,
            detalhe="Motivo nao determinado na extracao; caso exige analise humana.",
        )
    ]


def _regra_cartao_presente(
    extracao: ExtracaoContestacao, confirmadas: list[Transacao]
) -> list[Violacao]:
    if extracao.motivo not in MOTIVOS_DE_NAO_AUTORIZACAO:
        return []

    return [
        Violacao(
            regra=RegraViolacao.CARTAO_PRESENTE_VS_NAO_RECONHECIMENTO,
            severidade=Severidade.ALERTA,
            detalhe=(
                f"Transacao {t.id_transacao} foi presencial, o que contradiz o relato "
                "de nao autorizacao."
            ),
        )
        for t in confirmadas
        if t.canal_transacao == "presencial"
    ]


def _regra_reincidencia(solicitacao: SolicitacaoBruta) -> list[Violacao]:
    if solicitacao.contestacoes_ultimos_12m <= LIMITE_CONTESTACOES_12M:
        return []

    return [
        Violacao(
            regra=RegraViolacao.REINCIDENCIA_CONTESTACOES,
            severidade=Severidade.ALERTA,
            detalhe=(
                f"{solicitacao.contestacoes_ultimos_12m} contestacoes nos ultimos 12 "
                f"meses, acima do limite de {LIMITE_CONTESTACOES_12M}."
            ),
        )
    ]
