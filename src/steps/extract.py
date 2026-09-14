"""
Extracao estruturada da contestacao a partir do texto do cliente.

Unica etapa LLM-first. O modelo recebe o texto e o extrato do periodo e devolve
ExtracaoContestacao; elegibilidade e valor ficam com validate e compute. FalhaSchema,
RespostaNaoGravada e ErroTransitorio sobem sem tratamento: subir de tier e' decisao
do roteador.
"""

from __future__ import annotations

from datetime import datetime

from src import llm
from src.schemas import (
    Base,
    CanalEntrada,
    CanalTransacao,
    CustoChamada,
    ExtracaoContestacao,
    SolicitacaoBruta,
    TierModelo,
    Valor,
)

PROMPT_VERSION = "extract_v1"


class TransacaoNoPrompt(Base):
    """So' o que casa relato com extrato. ja_estornada fica de fora para o modelo
    nao julgar elegibilidade."""

    id_transacao: str
    data_hora: datetime
    valor: Valor
    estabelecimento: str
    canal_transacao: CanalTransacao


class EntradaExtracao(Base):
    id_caso: str
    canal: CanalEntrada
    recebida_em: datetime
    texto_cliente: str
    transacoes: list[TransacaoNoPrompt]


def extrair(
    solicitacao: SolicitacaoBruta, tier: TierModelo
) -> tuple[ExtracaoContestacao, CustoChamada]:
    campos = set(TransacaoNoPrompt.model_fields)
    payload = EntradaExtracao(
        id_caso=solicitacao.id_caso,
        canal=solicitacao.canal,
        recebida_em=solicitacao.recebida_em,
        texto_cliente=solicitacao.texto_cliente,
        transacoes=[
            TransacaoNoPrompt.model_validate(t.model_dump(include=campos))
            for t in solicitacao.transacoes_periodo
        ],
    )
    return llm.complete_structured(PROMPT_VERSION, payload, ExtracaoContestacao, tier)
