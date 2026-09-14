"""Extracao: payload minimo, prompt v1 em sincronia com o schema, replay pelo mock."""

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src import llm
from src.schemas import (
    CanalEntrada,
    ExtracaoContestacao,
    MotivoContestacao,
    SolicitacaoBruta,
    TierModelo,
    Transacao,
)
from src.steps.extract import PROMPT_VERSION, VERSOES_PROMPT, extrair

SOLICITACAO = SolicitacaoBruta(
    id_caso="CASO-0042",
    id_cliente_mascarado="CLI-SEGREDO-01",
    canal=CanalEntrada.CHAT,
    recebida_em=datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc),
    texto_cliente="nao fiz essa compra de R$ 1.079,70 na loja xpto",
    transacoes_periodo=[
        Transacao(
            id_transacao="TX000000101",
            data_hora=datetime(2026, 3, 5, 10, 0, tzinfo=timezone.utc),
            valor=Decimal("1079.70"),
            estabelecimento="LOJA XPTO",
            mcc="5311",
            canal_transacao="online",
            pais="BR",
        ),
        Transacao(
            id_transacao="TX000000102",
            data_hora=datetime(2026, 3, 6, 18, 30, tzinfo=timezone.utc),
            valor=Decimal("45.00"),
            estabelecimento="PADARIA CENTRAL",
            mcc="5462",
            canal_transacao="presencial",
            pais="BR",
            ja_estornada=True,
        ),
    ],
    contestacoes_ultimos_12m=7,
)

EXTRACAO = {
    "motivo": "nao_reconhecida",
    "ids_transacoes_citadas": ["TX000000101"],
    "valor_alegado": 1079.7,
    "cliente_afirma_nao_autorizou": True,
    "cliente_tentou_contato_estabelecimento": None,
    "resumo_alegacao": "Cliente nao reconhece compra online na LOJA XPTO.",
    "confianca": 0.9,
    "justificativa": "Afirma que nao fez a compra; estabelecimento e valor batem com TX000000101.",
}


@pytest.fixture(autouse=True)
def cache_isolado(tmp_path):
    llm.configurar("real", tmp_path / "cache.jsonl")
    yield
    llm.configurar("real")


def _provedor_falso(monkeypatch):
    usuarios = []

    def falso(spec, sistema, usuario, schema):
        usuarios.append(usuario)
        return EXTRACAO, 900, 150

    monkeypatch.setitem(llm._PROVEDORES, "anthropic", falso)
    monkeypatch.setitem(llm._PROVEDORES, "openai", falso)
    return usuarios


def test_mock_devolve_a_extracao_gravada_pela_execucao_real(monkeypatch):
    usuarios = _provedor_falso(monkeypatch)
    real, custo_real = extrair(SOLICITACAO, TierModelo.PEQUENO)

    llm.configurar("mock", llm._cache)
    mock, custo_mock = extrair(SOLICITACAO, TierModelo.PEQUENO)

    assert len(usuarios) == 1
    assert isinstance(mock, ExtracaoContestacao)
    assert (mock, custo_mock) == (real, custo_real)
    assert mock.valor_alegado == Decimal("1079.70")
    gravada = json.loads(llm._cache.read_text(encoding="utf-8"))
    assert (gravada["id_caso"], gravada["prompt_version"], gravada["tier"]) == (
        "CASO-0042",
        PROMPT_VERSION,
        "pequeno",
    )


def test_payload_leva_so_o_necessario_para_a_extracao(monkeypatch):
    usuarios = _provedor_falso(monkeypatch)

    extrair(SOLICITACAO, TierModelo.PEQUENO)

    payload = json.loads(usuarios[0])
    assert set(payload) == {"id_caso", "canal", "recebida_em", "texto_cliente", "transacoes"}
    assert [t["id_transacao"] for t in payload["transacoes"]] == ["TX000000101", "TX000000102"]
    for transacao in payload["transacoes"]:
        assert set(transacao) == {
            "id_transacao",
            "data_hora",
            "valor",
            "estabelecimento",
            "canal_transacao",
        }
    assert "CLI-SEGREDO-01" not in usuarios[0]


@pytest.mark.parametrize("versao", VERSOES_PROMPT)
def test_prompt_cita_todos_os_motivos_e_campos_do_schema(versao):
    prompt = (llm.PASTA_PROMPTS / f"{versao}.md").read_text(encoding="utf-8")

    for motivo in MotivoContestacao:
        assert f"`{motivo.value}`" in prompt
    for campo in ExtracaoContestacao.model_fields:
        assert f"`{campo}`" in prompt


def test_versoes_registradas_sao_exatamente_os_prompts_de_extracao_em_disco():
    em_disco = sorted(p.stem for p in llm.PASTA_PROMPTS.glob("extract_v*.md"))

    assert sorted(VERSOES_PROMPT) == em_disco
    assert PROMPT_VERSION == "extract_v2"


def test_versao_desconhecida_falha_antes_de_chamar_o_modelo(monkeypatch):
    usuarios = _provedor_falso(monkeypatch)

    with pytest.raises(ValueError, match="prompt de extracao desconhecido"):
        extrair(SOLICITACAO, TierModelo.PEQUENO, "extract_v9")
    assert usuarios == []
