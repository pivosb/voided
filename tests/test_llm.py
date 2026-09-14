"""Cliente LLM: modelo pelo tier, cache real/mock, retry com teto. Sem rede."""

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from tenacity import wait_none

from src import llm
from src.config import SpecGerador, modelo_do_tier, modelo_gerador
from src.schemas import Base, CustoChamada, TierModelo, Valor


class Resposta(Base):
    texto: str
    valor: Valor | None = None


class Payload(Base):
    id_caso: str
    texto: str


PAYLOAD = Payload(id_caso="CASO-0001", texto="oi")
OK = {"texto": "ok", "valor": 1079.7}


@pytest.fixture(autouse=True)
def isolado(tmp_path, monkeypatch):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "teste_v1.md").write_text("sistema v1", encoding="utf-8")
    (prompts / "teste_v2.md").write_text("sistema v2", encoding="utf-8")
    monkeypatch.setattr(llm, "PASTA_PROMPTS", prompts)
    monkeypatch.setattr(llm, "ESPERA", wait_none())
    llm.configurar("real", tmp_path / "cache.jsonl")
    yield
    llm.configurar("real")


def _cache():
    return llm._cache


def _provedor_falso(monkeypatch, *saidas, tokens=(1000, 200)):
    """Devolve as saidas em ordem (a ultima se repete); excecao e' levantada."""
    chamadas = []

    def falso(spec, sistema, usuario, schema):
        chamadas.append((spec, sistema, usuario))
        saida = saidas[min(len(chamadas), len(saidas)) - 1]
        if isinstance(saida, Exception):
            raise saida
        return saida, *tokens

    monkeypatch.setitem(llm._PROVEDORES, "anthropic", falso)
    monkeypatch.setitem(llm._PROVEDORES, "openai", falso)
    return chamadas


def test_modo_real_usa_o_modelo_do_tier_e_calcula_o_custo(monkeypatch):
    chamadas = _provedor_falso(monkeypatch, OK)
    spec = modelo_do_tier(TierModelo.GRANDE)

    resposta, custo = llm.complete_structured("teste_v1", PAYLOAD, Resposta, TierModelo.GRANDE)

    assert chamadas == [(spec, "sistema v1", PAYLOAD.model_dump_json())]
    assert resposta.texto == "ok"
    assert custo.modelo == spec.modelo
    esperado = (1000 * spec.preco_entrada_musd + 200 * spec.preco_saida_musd) / Decimal(10**6)
    assert custo.custo_usd == float(esperado)


def test_chamar_gerador_usa_o_modelo_do_gerador(monkeypatch):
    chamadas = _provedor_falso(monkeypatch, OK)

    llm.chamar_gerador("sistema", "usuario", Resposta)

    assert [spec for spec, _, _ in chamadas] == [modelo_gerador()]


@pytest.mark.parametrize("modo", ["real", "mock"])
def test_tier_humano_nao_chama_modelo(monkeypatch, modo):
    chamadas = _provedor_falso(monkeypatch, OK)
    llm.configurar(modo, _cache())

    with pytest.raises(ValueError, match="nao tem modelo"):
        llm.complete_structured("teste_v1", PAYLOAD, Resposta, TierModelo.HUMANO)
    assert chamadas == []


def test_mock_reproduz_resposta_e_custo_da_execucao_real(monkeypatch):
    chamadas = _provedor_falso(monkeypatch, OK)
    real = llm.complete_structured("teste_v1", PAYLOAD, Resposta, TierModelo.PEQUENO)

    llm.configurar("mock", _cache())
    mock = llm.complete_structured("teste_v1", PAYLOAD, Resposta, TierModelo.PEQUENO)

    assert len(chamadas) == 1
    assert mock == real
    assert mock[0].valor == Decimal("1079.70")


def test_resposta_fora_do_schema_e_gravada_e_o_mock_reproduz_a_falha(monkeypatch):
    _provedor_falso(monkeypatch, {"texto": "ok", "extra": 1})

    with pytest.raises(llm.FalhaSchema) as real:
        llm.complete_structured("teste_v1", PAYLOAD, Resposta, TierModelo.PEQUENO)
    assert isinstance(real.value.__cause__, ValidationError)
    assert real.value.custo.tokens_entrada == 1000
    assert len(_cache().read_text(encoding="utf-8").splitlines()) == 1

    llm.configurar("mock", _cache())
    with pytest.raises(llm.FalhaSchema) as mock:
        llm.complete_structured("teste_v1", PAYLOAD, Resposta, TierModelo.PEQUENO)
    assert mock.value.custo == real.value.custo


@pytest.mark.parametrize(
    "payload, versao, tier",
    [
        (Payload(id_caso="CASO-0002", texto="oi"), "teste_v1", TierModelo.PEQUENO),
        (PAYLOAD, "teste_v2", TierModelo.PEQUENO),
        (PAYLOAD, "teste_v1", TierModelo.MEDIO),
    ],
)
def test_mock_sem_a_chave_completa_levanta(monkeypatch, payload, versao, tier):
    chamadas = _provedor_falso(monkeypatch, OK)
    llm.complete_structured("teste_v1", PAYLOAD, Resposta, TierModelo.PEQUENO)

    llm.configurar("mock", _cache())
    with pytest.raises(llm.RespostaNaoGravada, match="nao tem resposta"):
        llm.complete_structured(versao, payload, Resposta, tier)
    assert len(chamadas) == 1


def test_mock_sem_arquivo_de_cache_levanta(tmp_path):
    llm.configurar("mock", tmp_path / "nao_existe.jsonl")

    with pytest.raises(llm.RespostaNaoGravada):
        llm.complete_structured("teste_v1", PAYLOAD, Resposta, TierModelo.PEQUENO)


def test_mock_usa_a_gravacao_mais_recente(monkeypatch):
    _provedor_falso(monkeypatch, {"texto": "antiga"}, {"texto": "nova"})
    for _ in range(2):
        llm.complete_structured("teste_v1", PAYLOAD, Resposta, TierModelo.PEQUENO)

    llm.configurar("mock", _cache())
    resposta, _ = llm.complete_structured("teste_v1", PAYLOAD, Resposta, TierModelo.PEQUENO)

    assert resposta.texto == "nova"


def test_mock_recusa_resposta_gravada_com_outro_modelo():
    entrada = llm.EntradaCache(
        id_caso=PAYLOAD.id_caso,
        prompt_version="teste_v1",
        tier=TierModelo.PEQUENO,
        resposta=OK,
        custo=CustoChamada(
            modelo="modelo-antigo", tokens_entrada=1, tokens_saida=1, custo_usd=0, latencia_ms=1
        ),
        gravado_em=datetime.now(timezone.utc),
    )
    _cache().write_text(entrada.model_dump_json() + "\n", encoding="utf-8")

    llm.configurar("mock", _cache())
    with pytest.raises(ValueError, match="modelo-antigo"):
        llm.complete_structured("teste_v1", PAYLOAD, Resposta, TierModelo.PEQUENO)


def test_erro_transitorio_e_repetido_uma_vez(monkeypatch):
    chamadas = _provedor_falso(monkeypatch, llm.ErroTransitorio("rede"), OK)

    resposta, _ = llm.complete_structured("teste_v1", PAYLOAD, Resposta, TierModelo.PEQUENO)

    assert resposta.texto == "ok"
    assert len(chamadas) == 2


def test_retry_para_no_teto_e_nao_grava(monkeypatch):
    chamadas = _provedor_falso(monkeypatch, llm.ErroTransitorio("rede"))

    with pytest.raises(llm.ErroTransitorio):
        llm.complete_structured("teste_v1", PAYLOAD, Resposta, TierModelo.PEQUENO)
    assert len(chamadas) == llm.MAX_TENTATIVAS == 2
    assert not _cache().exists()


def test_falha_de_schema_nao_e_repetida(monkeypatch):
    chamadas = _provedor_falso(monkeypatch, {"texto": 1})

    with pytest.raises(llm.FalhaSchema):
        llm.complete_structured("teste_v1", PAYLOAD, Resposta, TierModelo.PEQUENO)
    assert len(chamadas) == 1


def test_anthropic_desliga_retry_do_sdk_e_erro_de_conexao_vira_transitorio(monkeypatch):
    clientes = []

    class ErroConexao(Exception):
        pass

    class Cliente:
        def __init__(self, **kwargs):
            clientes.append(kwargs)
            self.messages = SimpleNamespace(create=self.create)

        def create(self, **kwargs):
            raise ErroConexao("caiu")

    sdk_falso = SimpleNamespace(
        Anthropic=Cliente,
        APIConnectionError=ErroConexao,
        RateLimitError=type("RateLimitError", (Exception,), {}),
        InternalServerError=type("InternalServerError", (Exception,), {}),
    )
    monkeypatch.setitem(sys.modules, "anthropic", sdk_falso)
    assert modelo_do_tier(TierModelo.MEDIO).provedor == "anthropic"

    with pytest.raises(llm.ErroTransitorio):
        llm.complete_structured("teste_v1", PAYLOAD, Resposta, TierModelo.MEDIO)
    assert len(clientes) == llm.MAX_TENTATIVAS
    assert all(c["max_retries"] == 0 for c in clientes)


@pytest.mark.parametrize("versao", ["../segredo", "Teste_v1", "teste", "teste_v1.md"])
def test_prompt_version_invalida_levanta_sem_chamar(monkeypatch, versao):
    chamadas = _provedor_falso(monkeypatch, OK)

    with pytest.raises(ValueError, match="prompt_version invalida"):
        llm.complete_structured(versao, PAYLOAD, Resposta, TierModelo.PEQUENO)
    assert chamadas == []


def test_payload_sem_id_caso_levanta(monkeypatch):
    class SemId(Base):
        texto: str

    _provedor_falso(monkeypatch, OK)

    with pytest.raises(ValueError, match="id_caso"):
        llm.complete_structured("teste_v1", SemId(texto="oi"), Resposta, TierModelo.PEQUENO)


def _sdk_openai_falso(monkeypatch, conteudo='{"texto": "ok"}', finish_reason="stop"):
    chamadas = []

    class Cliente:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            chamadas.append(kwargs)
            escolha = SimpleNamespace(
                finish_reason=finish_reason, message=SimpleNamespace(content=conteudo)
            )
            uso = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
            return SimpleNamespace(choices=[escolha], usage=uso)

    erro = type("Erro", (Exception,), {})
    sdk = SimpleNamespace(
        OpenAI=Cliente, APIConnectionError=erro, RateLimitError=erro, InternalServerError=erro
    )
    monkeypatch.setitem(sys.modules, "openai", sdk)
    return chamadas


@pytest.mark.parametrize("esforco", [None, "minimal"])
def test_reasoning_effort_so_vai_na_chamada_quando_configurado(monkeypatch, esforco):
    chamadas = _sdk_openai_falso(monkeypatch)
    spec = SpecGerador(
        provedor="openai", modelo="gerador-teste", base_url="http://x", reasoning_effort=esforco
    )
    monkeypatch.setattr(llm, "modelo_gerador", lambda: spec)

    llm.chamar_gerador("sistema", "usuario", Resposta)

    assert chamadas[0].get("reasoning_effort") == esforco
    assert chamadas[0]["temperature"] == 0


@pytest.mark.parametrize(
    "conteudo",
    [
        '{"texto": "ok"}\n```',
        '```json\n{"texto": "ok"}\n```',
        '  ```\n{"texto": "ok"}```  ',
    ],
)
def test_cerca_markdown_nas_bordas_e_removida(monkeypatch, conteudo):
    _sdk_openai_falso(monkeypatch, conteudo=conteudo)
    assert modelo_do_tier(TierModelo.PEQUENO).provedor == "openai"

    resposta, _ = llm.complete_structured("teste_v1", PAYLOAD, Resposta, TierModelo.PEQUENO)

    assert resposta.texto == "ok"


@pytest.mark.parametrize(
    "conteudo, finish_reason, motivo",
    [
        ('{"texto": "ok"}\nobrigado!', "stop", "nao e' JSON valido"),
        ('["ok"]', "stop", "nao e' um objeto"),
        (None, "stop", "nao e' JSON valido"),
        ('{"texto": "o', "length", "cortada em max_tokens"),
    ],
)
def test_resposta_que_nao_e_json_e_falha_de_schema_gravada_com_o_bruto(
    monkeypatch, conteudo, finish_reason, motivo
):
    _sdk_openai_falso(monkeypatch, conteudo=conteudo, finish_reason=finish_reason)

    with pytest.raises(llm.FalhaSchema) as real:
        llm.complete_structured("teste_v1", PAYLOAD, Resposta, TierModelo.PEQUENO)

    gravada = json.loads(_cache().read_text(encoding="utf-8"))
    assert motivo in gravada["resposta"]["_resposta_invalida"]
    assert gravada["resposta"]["_conteudo"] == conteudo
    assert real.value.custo.tokens_entrada == 10

    llm.configurar("mock", _cache())
    with pytest.raises(llm.FalhaSchema):
        llm.complete_structured("teste_v1", PAYLOAD, Resposta, TierModelo.PEQUENO)
