"""Maquina de estados: caminhos ate cada status, sem LLM (extrair falso por tier)."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src import orchestrator
from src.llm import FalhaSchema, RespostaNaoGravada
from src.schemas import (
    CanalEntrada,
    CustoChamada,
    ExtracaoContestacao,
    MotivoContestacao,
    SolicitacaoBruta,
    StatusExecucao,
    TierModelo,
    Transacao,
)

RECEBIDA = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)


def _transacao(id_transacao: str, valor: str) -> Transacao:
    return Transacao(
        id_transacao=id_transacao,
        data_hora=datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc),
        valor=Decimal(valor),
        estabelecimento=f"LOJA {id_transacao}",
        mcc="5311",
        canal_transacao="online",
        pais="BR",
    )


SOLICITACAO = SolicitacaoBruta(
    id_caso="CASO-0007",
    id_cliente_mascarado="cli_teste",
    canal=CanalEntrada.APP,
    recebida_em=RECEBIDA,
    texto_cliente="nao fiz essa compra",
    transacoes_periodo=[_transacao("TX000000001", "100.00"), _transacao("TX000000002", "1500.00")],
)


def _extracao(ids=("TX000000001",), confianca=0.9) -> ExtracaoContestacao:
    return ExtracaoContestacao(
        motivo=MotivoContestacao.NAO_RECONHECIDA,
        ids_transacoes_citadas=list(ids),
        cliente_afirma_nao_autorizou=True,
        resumo_alegacao="Nao reconhece a compra.",
        confianca=confianca,
        justificativa="Afirma que nao fez.",
    )


def _custo(tier: TierModelo) -> CustoChamada:
    return CustoChamada(
        modelo=tier.value, tokens_entrada=100, tokens_saida=20, custo_usd=0.001, latencia_ms=50
    )


def _falha_schema(tier: TierModelo) -> FalhaSchema:
    try:
        ExtracaoContestacao.model_validate({})
    except ValidationError as exc:
        return FalhaSchema(_custo(tier), exc)


def _extrator_falso(monkeypatch, respostas: dict[TierModelo, object]):
    """Resposta por tier: ExtracaoContestacao devolve, excecao levanta."""
    tiers = []

    def falso(solicitacao, tier):
        tiers.append(tier)
        resposta = respostas[tier]
        if isinstance(resposta, Exception):
            raise resposta
        return resposta, _custo(tier)

    monkeypatch.setattr(orchestrator, "extrair", falso)
    return tiers


def test_caminho_feliz_e_automatico(monkeypatch):
    tiers = _extrator_falso(monkeypatch, {TierModelo.PEQUENO: _extracao()})

    evento = orchestrator.processar_caso(SOLICITACAO)

    assert evento.status is StatusExecucao.AUTOMATICO
    assert tiers == [TierModelo.PEQUENO]
    assert evento.calculo.valor_estorno == Decimal("100.00")
    assert evento.registro.status is StatusExecucao.AUTOMATICO
    assert evento.registro.protocolo.startswith("CTS-")
    assert [r.tier for r in evento.rotas] == [TierModelo.PEQUENO] * 3
    assert len(evento.custos) == 1
    assert evento.erro is None
    assert evento.versao_prompt == "extract_v1"


def test_falha_de_schema_sobe_de_tier_e_guarda_o_custo_da_falha(monkeypatch):
    tiers = _extrator_falso(
        monkeypatch,
        {TierModelo.PEQUENO: _falha_schema(TierModelo.PEQUENO), TierModelo.MEDIO: _extracao()},
    )

    evento = orchestrator.processar_caso(SOLICITACAO)

    assert evento.status is StatusExecucao.ESCALADO_MODELO
    assert evento.registro.status is StatusExecucao.ESCALADO_MODELO
    assert tiers == [TierModelo.PEQUENO, TierModelo.MEDIO]
    assert [c.modelo for c in evento.custos] == ["pequeno", "medio"]
    assert "fora do schema" in evento.rotas[1].motivo_decisao


def test_confianca_baixa_em_todos_os_tiers_vai_para_humano_sem_validar(monkeypatch):
    baixa = _extracao(confianca=0.4)
    tiers = _extrator_falso(
        monkeypatch,
        {TierModelo.PEQUENO: baixa, TierModelo.MEDIO: baixa, TierModelo.GRANDE: baixa},
    )

    evento = orchestrator.processar_caso(SOLICITACAO)

    assert evento.status is StatusExecucao.ESCALADO_HUMANO
    assert tiers == [TierModelo.PEQUENO, TierModelo.MEDIO, TierModelo.GRANDE]
    assert evento.rotas[-1].tier is TierModelo.HUMANO
    assert evento.extracao == baixa
    assert evento.validacao is None and evento.calculo is None
    assert len(evento.custos) == 3


def test_violacao_bloqueante_vai_para_humano_sem_calcular(monkeypatch):
    _extrator_falso(monkeypatch, {TierModelo.PEQUENO: _extracao(ids=["TX999999999"])})

    evento = orchestrator.processar_caso(SOLICITACAO)

    assert evento.status is StatusExecucao.ESCALADO_HUMANO
    assert evento.validacao.tem_bloqueante
    assert evento.calculo is None and evento.registro is None


def test_faixa_de_risco_alta_vai_para_humano_sem_registrar(monkeypatch):
    _extrator_falso(monkeypatch, {TierModelo.PEQUENO: _extracao(ids=["TX000000002"])})

    evento = orchestrator.processar_caso(SOLICITACAO)

    assert evento.status is StatusExecucao.ESCALADO_HUMANO
    assert evento.calculo.faixa_risco == "alta"
    assert evento.registro is None
    assert "faixa de risco alta" in evento.rotas[-1].motivo_decisao


@pytest.mark.parametrize(
    "excecao", [RespostaNaoGravada("sem chave"), RuntimeError("provedor caiu")]
)
def test_excecao_nao_escapa_e_vira_erro(monkeypatch, excecao):
    _extrator_falso(monkeypatch, {TierModelo.PEQUENO: excecao})

    evento = orchestrator.processar_caso(SOLICITACAO)

    assert evento.status is StatusExecucao.ERRO
    assert evento.erro.startswith(f"extracao: {type(excecao).__name__}")
    assert [r.tier for r in evento.rotas] == [TierModelo.PEQUENO]
    assert evento.custos == []


def test_erro_depois_da_extracao_preserva_o_que_ja_foi_produzido(monkeypatch):
    _extrator_falso(monkeypatch, {TierModelo.PEQUENO: _extracao()})

    def quebra(*args):
        raise ValueError("calculo impossivel")

    monkeypatch.setattr(orchestrator, "calcular", quebra)

    evento = orchestrator.processar_caso(SOLICITACAO)

    assert evento.status is StatusExecucao.ERRO
    assert evento.erro == "calculo: ValueError: calculo impossivel"
    assert evento.extracao is not None and evento.validacao is not None
    assert len(evento.custos) == 1
