"""
Orquestrador: maquina de estados que leva uma solicitacao ate um EventoExecucao.

    EXTRACAO --(falha de schema / confianca baixa, ate MAX_ESCALADAS)--> EXTRACAO
    EXTRACAO --(escaladas esgotadas)--> FIM [escalado_humano]
    EXTRACAO --(aceita)--> VALIDACAO
    VALIDACAO --(nao elegivel: violacao bloqueante)--> FIM [escalado_humano]
    VALIDACAO --(elegivel)--> CALCULO
    CALCULO --(faixa alta)--> FIM [escalado_humano]
    CALCULO --(faixa baixa/media)--> REGISTRO --> FIM [automatico | escalado_modelo]

Quem escolhe tier e decide escalar e' src/router.py; toda decisao dele vira uma
rota no evento. Nenhuma excecao sai de `processar_caso`: qualquer falha nao prevista
vira status ERRO, com o estado em que ocorreu e o que ja foi produzido ate ali.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from src import router
from src.llm import FalhaSchema
from src.schemas import (
    CustoChamada,
    DecisaoRoteamento,
    EventoExecucao,
    ExtracaoContestacao,
    RegistroCaso,
    ResultadoCalculo,
    ResultadoValidacao,
    SolicitacaoBruta,
    StatusExecucao,
    TierModelo,
)
from src.steps.compute import calcular
from src.steps.extract import PROMPT_VERSION, extrair
from src.steps.register import registrar
from src.steps.validate import validar

VERSAO_PIPELINE = "0.1.0"


class Estado(str, Enum):
    EXTRACAO = "extracao"
    VALIDACAO = "validacao"
    CALCULO = "calculo"
    REGISTRO = "registro"
    FIM = "fim"


@dataclass
class _Caso:
    solicitacao: SolicitacaoBruta
    tentativas: int = 0
    rotas: list[DecisaoRoteamento] = field(default_factory=list)
    custos: list[CustoChamada] = field(default_factory=list)
    extracao: ExtracaoContestacao | None = None
    validacao: ResultadoValidacao | None = None
    calculo: ResultadoCalculo | None = None
    registro: RegistroCaso | None = None
    status: StatusExecucao | None = None
    erro: str | None = None


def processar_caso(solicitacao: SolicitacaoBruta) -> EventoExecucao:
    iniciado_em = _agora()
    caso = _Caso(solicitacao)
    estado = Estado.EXTRACAO

    try:
        caso.rotas.append(router.decidir(0, False, None, None))
        while estado is not Estado.FIM:
            estado = _TRANSICOES[estado](caso)
    except Exception as exc:  # noqa: BLE001 -- contrato: nenhuma excecao escapa
        caso.status = StatusExecucao.ERRO
        caso.erro = f"{estado.value}: {type(exc).__name__}: {exc}"

    return EventoExecucao(
        id_caso=solicitacao.id_caso,
        versao_pipeline=VERSAO_PIPELINE,
        versao_prompt=PROMPT_VERSION,
        iniciado_em=iniciado_em,
        finalizado_em=_agora(),
        status=caso.status,
        extracao=caso.extracao,
        validacao=caso.validacao,
        calculo=caso.calculo,
        registro=caso.registro,
        rotas=caso.rotas,
        custos=caso.custos,
        erro=caso.erro,
    )


def _extracao(caso: _Caso) -> Estado:
    tier = caso.rotas[-1].tier
    caso.tentativas += 1
    try:
        extracao, custo = extrair(caso.solicitacao, tier)
    except FalhaSchema as exc:
        caso.custos.append(exc.custo)
        falha_schema, confianca = True, None
    else:
        caso.custos.append(custo)
        # Guarda a ultima extracao valida mesmo se escalar: e' evidencia para o analista.
        caso.extracao = extracao
        falha_schema, confianca = False, extracao.confianca

    decisao = router.decidir(caso.tentativas, falha_schema, confianca, None)
    caso.rotas.append(decisao)

    if decisao.tier is TierModelo.HUMANO:
        caso.status = StatusExecucao.ESCALADO_HUMANO
        return Estado.FIM
    if decisao.tentativa > caso.tentativas:
        return Estado.EXTRACAO
    return Estado.VALIDACAO


def _validacao(caso: _Caso) -> Estado:
    caso.validacao = validar(caso.solicitacao, caso.extracao)
    # Nao elegivel implica violacao bloqueante (sem transacao confirmada tambem e').
    if not caso.validacao.elegivel:
        caso.status = StatusExecucao.ESCALADO_HUMANO
        return Estado.FIM
    return Estado.CALCULO


def _calculo(caso: _Caso) -> Estado:
    caso.calculo = calcular(caso.solicitacao, caso.extracao, caso.validacao)
    decisao = router.decidir(
        caso.tentativas, False, caso.extracao.confianca, caso.calculo.faixa_risco
    )
    caso.rotas.append(decisao)

    if decisao.tier is TierModelo.HUMANO:
        caso.status = StatusExecucao.ESCALADO_HUMANO
        return Estado.FIM
    return Estado.REGISTRO


def _registro(caso: _Caso) -> Estado:
    status = (
        StatusExecucao.AUTOMATICO if caso.tentativas == 1 else StatusExecucao.ESCALADO_MODELO
    )
    caso.registro = registrar(caso.solicitacao, caso.calculo, status, _agora())
    caso.status = status
    return Estado.FIM


_TRANSICOES: dict[Estado, Callable[[_Caso], Estado]] = {
    Estado.EXTRACAO: _extracao,
    Estado.VALIDACAO: _validacao,
    Estado.CALCULO: _calculo,
    Estado.REGISTRO: _registro,
}


def _agora() -> datetime:
    return datetime.now(timezone.utc)
