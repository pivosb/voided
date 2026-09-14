"""CLI: selecao por split/limite, resumo final e preenchimento do cache por tier."""

import json
import sys
from datetime import datetime, timedelta, timezone

import pytest
from tenacity import wait_none

import run
from src import llm
from src.schemas import (
    CanalEntrada,
    CustoChamada,
    EventoExecucao,
    SolicitacaoBruta,
    StatusExecucao,
    TierModelo,
)
from src.steps.extract import PROMPT_VERSION

AGORA = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)


def _caso(id_caso: str) -> SolicitacaoBruta:
    return SolicitacaoBruta(
        id_caso=id_caso,
        id_cliente_mascarado="cli_teste",
        canal=CanalEntrada.APP,
        recebida_em=AGORA,
        texto_cliente="nao fiz",
    )


CASOS = [_caso(f"CASO-000{i}") for i in range(1, 5)]
SPLITS = {"CASO-0001": "dev", "CASO-0002": "eval", "CASO-0003": "dev", "CASO-0004": "eval"}


@pytest.mark.parametrize(
    "split, limite, esperados",
    [
        ("todos", None, ["CASO-0001", "CASO-0002", "CASO-0003", "CASO-0004"]),
        ("dev", None, ["CASO-0001", "CASO-0003"]),
        ("eval", 1, ["CASO-0002"]),
        ("todos", 2, ["CASO-0001", "CASO-0002"]),
    ],
)
def test_selecionar_filtra_split_antes_do_limite(split, limite, esperados):
    selecionados = run.selecionar(CASOS, split, SPLITS, limite)

    assert [c.id_caso for c in selecionados] == esperados


def test_caso_sem_split_no_gabarito_falha():
    with pytest.raises(RuntimeError, match="CASO-0004"):
        run.selecionar(CASOS, "dev", {"CASO-0001": "dev"}, None)


@pytest.mark.parametrize(
    "p, esperado", [(50, 30), (95, 100), (100, 100), (0, 10)]
)
def test_percentil_nearest_rank(p, esperado):
    assert run.percentil([100, 10, 30, 20, 40], p) == esperado


def _evento(status: StatusExecucao, custos_usd: list[float], latencias: list[int]) -> EventoExecucao:
    return EventoExecucao(
        id_caso="CASO-0001",
        versao_pipeline="0.1.0",
        versao_prompt="extract_v1",
        iniciado_em=AGORA,
        finalizado_em=AGORA + timedelta(milliseconds=5),
        status=status,
        custos=[
            CustoChamada(
                modelo="m", tokens_entrada=1, tokens_saida=1, custo_usd=c, latencia_ms=l
            )
            for c, l in zip(custos_usd, latencias)
        ],
    )


def test_resumo_conta_status_soma_custo_sem_erro_de_float_e_usa_latencia_gravada():
    eventos = [
        _evento(StatusExecucao.AUTOMATICO, [0.1], [100]),
        _evento(StatusExecucao.ESCALADO_MODELO, [0.2, 0.1], [300, 400]),
        _evento(StatusExecucao.ERRO, [], []),
    ]

    linhas = run.resumo(eventos)

    assert "  automatico: 1" in linhas
    assert "  escalado_humano: 0" in linhas
    assert "  erro: 1" in linhas
    assert "  custo total: US$ 0.400000" in linhas
    assert "  latencia LLM por caso: p50 100 ms, p95 700 ms" in linhas


# --------------------------------------------------------------------------- #
# --record: preenchimento do cache
# --------------------------------------------------------------------------- #

# Confianca baixa de proposito: o roteador escalaria, o --record nao pode escalar.
EXTRACAO_BAIXA = {
    "motivo": "nao_reconhecida",
    "ids_transacoes_citadas": [],
    "cliente_afirma_nao_autorizou": True,
    "resumo_alegacao": "Nao reconhece.",
    "confianca": 0.1,
    "justificativa": "Teste.",
}


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "ESPERA", wait_none())
    llm.configurar("real", tmp_path / "cache.jsonl")
    yield tmp_path / "cache.jsonl"
    llm.configurar("real")


def _provedor(monkeypatch, responder):
    chamadas = []

    def falso(spec, sistema, usuario, schema):
        id_caso = json.loads(usuario)["id_caso"]
        chamadas.append((spec.modelo, id_caso))
        return responder(spec, id_caso), 100, 10

    monkeypatch.setitem(llm._PROVEDORES, "anthropic", falso)
    monkeypatch.setitem(llm._PROVEDORES, "openai", falso)
    return chamadas


def _chaves(cache):
    linhas = [json.loads(l) for l in cache.read_text(encoding="utf-8").splitlines()]
    return [(l["id_caso"], l["prompt_version"], l["tier"]) for l in linhas]


def test_tier_forcado_grava_todos_os_casos_nele_sem_escalar(monkeypatch, cache):
    chamadas = _provedor(monkeypatch, lambda spec, id_caso: EXTRACAO_BAIXA)

    gravacao = run.preencher_cache(CASOS[:2], [TierModelo.MEDIO])

    assert _chaves(cache) == [
        ("CASO-0001", PROMPT_VERSION, "medio"),
        ("CASO-0002", PROMPT_VERSION, "medio"),
    ]
    assert len(chamadas) == 2
    assert gravacao.gravadas == {TierModelo.MEDIO: 2}


def test_todos_executa_os_tres_tiers_em_sequencia(monkeypatch, cache):
    _provedor(monkeypatch, lambda spec, id_caso: EXTRACAO_BAIXA)

    gravacao = run.preencher_cache(CASOS[:2], run.TIERS_RECORD["todos"])

    assert [(tier, id_caso) for id_caso, _, tier in _chaves(cache)] == [
        ("pequeno", "CASO-0001"),
        ("pequeno", "CASO-0002"),
        ("medio", "CASO-0001"),
        ("medio", "CASO-0002"),
        ("grande", "CASO-0001"),
        ("grande", "CASO-0002"),
    ]
    assert gravacao.gravadas == {t: 2 for t in run.TIERS_RECORD["todos"]}


def test_fora_do_schema_conta_como_gravada_e_falha_sem_gravacao_nao_para_o_lote(
    monkeypatch, cache
):
    def responder(spec, id_caso):
        if id_caso == "CASO-0001":
            raise llm.ErroTransitorio("rede")
        if id_caso == "CASO-0002":
            return EXTRACAO_BAIXA | {"extra": 1}
        return EXTRACAO_BAIXA

    _provedor(monkeypatch, responder)

    gravacao = run.preencher_cache(CASOS[:3], [TierModelo.PEQUENO])

    assert [id_caso for id_caso, _, _ in _chaves(cache)] == ["CASO-0002", "CASO-0003"]
    assert gravacao.gravadas == {TierModelo.PEQUENO: 2}
    assert gravacao.fora_do_schema == {TierModelo.PEQUENO: 1}
    assert len(gravacao.falhas) == 1 and "CASO-0001: ErroTransitorio" in gravacao.falhas[0]
    linhas = run.resumo_gravacao(gravacao, [TierModelo.PEQUENO])
    assert "  pequeno: 2 gravadas (1 fora do schema)" in linhas
    assert "  falhas sem gravacao: 1" in linhas


@pytest.mark.parametrize(
    "argumentos, mensagem",
    [
        (["--mock", "--tier", "medio"], "--tier so' vale com --record"),
        (["--record", "--out", "x.jsonl"], "--out so' vale com --mock"),
    ],
)
def test_flags_incompativeis_com_o_modo_falham(monkeypatch, capsys, argumentos, mensagem):
    monkeypatch.setattr(sys, "argv", ["run.py", *argumentos])

    with pytest.raises(SystemExit) as saida:
        run.main()

    assert saida.value.code == 2
    assert mensagem in capsys.readouterr().err


def test_record_sem_tier_usa_pequeno(monkeypatch, tmp_path, cache):
    casos = tmp_path / "casos.jsonl"
    casos.write_text(CASOS[0].model_dump_json() + "\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["run.py", "--record", "--casos", str(casos)])
    monkeypatch.setattr(llm, "CACHE_PADRAO", cache)
    _provedor(monkeypatch, lambda spec, id_caso: EXTRACAO_BAIXA)

    run.main()

    assert _chaves(cache) == [("CASO-0001", PROMPT_VERSION, "pequeno")]


def test_record_com_prompt_antigo_grava_na_chave_dessa_versao(monkeypatch, cache):
    _provedor(monkeypatch, lambda spec, id_caso: EXTRACAO_BAIXA)

    run.preencher_cache(CASOS[:1], [TierModelo.PEQUENO], "extract_v1")

    assert _chaves(cache) == [("CASO-0001", "extract_v1", "pequeno")]
    assert run.respostas_gravadas(cache, "extract_v1") == 1
    assert run.respostas_gravadas(cache, PROMPT_VERSION) == 0


def test_mock_sem_respostas_da_versao_pedida_para_antes_de_processar(
    monkeypatch, tmp_path, cache
):
    casos = tmp_path / "casos.jsonl"
    casos.write_text(CASOS[0].model_dump_json() + "\n", encoding="utf-8")
    _provedor(monkeypatch, lambda spec, id_caso: EXTRACAO_BAIXA)
    run.preencher_cache(CASOS[:1], [TierModelo.PEQUENO], "extract_v1")
    monkeypatch.setattr(llm, "CACHE_PADRAO", cache)
    monkeypatch.setattr(
        sys, "argv", ["run.py", "--mock", "--casos", str(casos), "--prompt", "extract_v2"]
    )

    with pytest.raises(SystemExit) as saida:
        run.main()

    assert "nao tem respostas de extract_v2" in str(saida.value.code)
