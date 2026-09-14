"""Texto sintetico: checagens contra o gabarito, ruido com seed e retry, sem rede."""

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import scripts.gen_texts as gen_texts
from scripts.build_golden import ID_INEXISTENTE
from scripts.gen_specs import gerar_spec
from scripts.gen_texts import (
    TextoGerado,
    Variacao,
    aplicar_ruido,
    contestadas,
    formatar_valor,
    gerar_caso,
    montar_pedido,
    problemas_do_texto,
    sortear_variacao,
)
from src.schemas import SolicitacaoBruta

RECEBIDA_BASE = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
LONGO = " Peço providências." * 60


def _spec(**filtro) -> dict:
    """Primeira spec do gerador que satisfaz o filtro (chaves de spec ou de verdade)."""
    rng = random.Random(11)
    for indice in range(1, 5000):
        spec = gerar_spec(rng, indice, RECEBIDA_BASE + timedelta(days=indice % 180))
        campos = spec | spec["verdade"]
        if all(
            v(campos[k]) if callable(v) else campos[k] == v for k, v in filtro.items()
        ):
            return spec
    raise AssertionError(f"gerador nao produziu spec com {filtro}")


def _texto_coerente(spec: dict) -> str:
    """Cita cada estabelecimento e o valor alegado, em formato brasileiro."""
    nomes = ", ".join(dict.fromkeys(t.estabelecimento for t in contestadas(spec)))
    alegado = formatar_valor(Decimal(spec["verdade"]["valor_alegado"]))
    return f"Contesto {nomes}, total {alegado}."


SEM_ID = {"violacoes_esperadas": lambda v: "transacao_nao_encontrada" not in v}


def test_texto_coerente_nao_tem_problema():
    spec = _spec(dificuldade="nenhuma", canal="app", motivo="nao_reconhecida", **SEM_ID)

    assert problemas_do_texto(spec, Variacao(tom="educado"), [_texto_coerente(spec)]) == []


def test_valor_em_formato_americano_tambem_conta():
    spec = _spec(dificuldade="nenhuma", canal="app", **SEM_ID)
    nomes = " ".join(t.estabelecimento for t in contestadas(spec))
    texto = f"{nomes} {Decimal(spec['verdade']['valor_alegado']):,.2f}"

    assert problemas_do_texto(spec, Variacao(tom="educado"), [texto]) == []


def test_texto_vago_nao_pode_citar_valor_nem_estabelecimento():
    spec = _spec(
        dificuldade="texto_vago",
        canal="app",
        ids_contestadas=lambda ids: len(ids) == 1,
        **SEM_ID,
    )
    transacao = contestadas(spec)[0]
    variacao = Variacao(tom="confuso")

    assert problemas_do_texto(spec, variacao, ["Tem uma cobrança estranha na fatura."]) == []
    com_valor = problemas_do_texto(spec, variacao, [f"Cobraram {transacao.valor + 3} a mais."])
    assert "texto vago nao pode citar valor" in com_valor
    if gen_texts._marcas(transacao.estabelecimento):
        com_nome = problemas_do_texto(spec, variacao, [transacao.estabelecimento.title()])
        assert "texto vago nao pode citar o estabelecimento" in com_nome


def test_pedido_de_texto_vago_nao_entrega_valor_nem_estabelecimento():
    spec = _spec(dificuldade="texto_vago")
    pedido = montar_pedido(spec, Variacao(tom="confuso"))

    for transacao in contestadas(spec):
        assert formatar_valor(transacao.valor) not in pedido
        assert transacao.estabelecimento not in pedido


def test_valor_nao_bate_exige_o_alegado_e_proibe_o_real():
    spec = _spec(dificuldade="valor_nao_bate", canal="app", **SEM_ID)
    variacao = Variacao(tom="irritado")
    real = formatar_valor(Decimal(spec["verdade"]["valor_total_contestado"]))

    assert problemas_do_texto(spec, variacao, [_texto_coerente(spec)]) == []
    assert "valor_nao_bate nao pode citar o valor real da fatura" in problemas_do_texto(
        spec, variacao, [_texto_coerente(spec) + f" Na fatura aparece {real}."]
    )
    assert real not in montar_pedido(spec, variacao)


def test_transacao_inexistente_exige_o_id_falso():
    spec = _spec(
        dificuldade="nenhuma",
        canal="app",
        violacoes_esperadas=lambda v: "transacao_nao_encontrada" in v,
    )
    variacao = Variacao(tom="educado")

    assert ID_INEXISTENTE in montar_pedido(spec, variacao)
    sem_id = problemas_do_texto(spec, variacao, [_texto_coerente(spec)])
    assert any("codigos de transacao" in p for p in sem_id)
    com_id = _texto_coerente(spec) + f" E o {ID_INEXISTENTE}."
    assert problemas_do_texto(spec, variacao, [com_id]) == []


def test_id_inventado_e_problema():
    spec = _spec(dificuldade="nenhuma", canal="app", **SEM_ID)

    problemas = problemas_do_texto(
        spec, Variacao(tom="educado"), [_texto_coerente(spec) + " Codigo TX123456789."]
    )

    assert any("codigos de transacao" in p for p in problemas)


def test_mistura_dois_assuntos_exige_o_pedido_extra():
    spec = _spec(dificuldade="mistura_dois_assuntos", canal="app", **SEM_ID)
    variacao = sortear_variacao(spec, random.Random(3))
    pedido_extra, palavra = variacao.assunto_extra

    assert pedido_extra in montar_pedido(spec, variacao)
    assert any("pedido nao relacionado" in p for p in problemas_do_texto(
        spec, variacao, [_texto_coerente(spec)]
    ))
    assert problemas_do_texto(spec, variacao, [_texto_coerente(spec) + f" E o {palavra}."]) == []


def test_relato_contraditorio_por_data_esconde_a_data_real():
    spec = _spec(dificuldade="relato_contraditorio")
    for semente in range(50):
        variacao = sortear_variacao(spec, random.Random(semente))
        if variacao.esconder_datas:
            break
    pedido = montar_pedido(spec, variacao)

    assert variacao.contradicao in pedido
    for transacao in contestadas(spec):
        assert f"{transacao.data_hora:%d/%m/%Y}" not in pedido


@pytest.mark.parametrize(
    "canal, mensagens, problema",
    [
        ("app", ["a" * 500], "app pede"),
        ("app", ["um", "dois"], "app pede"),
        ("chat", ["uma so'"], "chat pede"),
        ("ouvidoria", ["curta"], "ouvidoria pede"),
    ],
)
def test_registro_do_canal(canal, mensagens, problema):
    spec = _spec(canal=canal)

    problemas = problemas_do_texto(spec, Variacao(tom="educado"), mensagens)

    assert any(p.startswith(problema) for p in problemas)


def test_ruido_e_deterministico_e_preserva_valores_ids_e_quebras():
    texto = f"Não reconheço a compra de R$ 1.079,70 em 05/03/2026\ncódigo {ID_INEXISTENTE}"

    ruidoso = aplicar_ruido(texto, 1.0, random.Random(5))

    assert ruidoso == aplicar_ruido(texto, 1.0, random.Random(5))
    assert ruidoso != texto
    for intacto in ["R$ 1.079,70", "05/03/2026", ID_INEXISTENTE, "\n"]:
        assert intacto in ruidoso


def test_taxa_zero_nao_altera_o_texto():
    texto = "Transcrição da ligação, sem erro de digitação."

    assert aplicar_ruido(texto, 0.0, random.Random(5)) == texto


def _gerador_falso(monkeypatch, respostas: list[list[str]]):
    prompts = []

    def falso(sistema, usuario, schema):
        prompts.append(usuario)
        return TextoGerado(mensagens=respostas[len(prompts) - 1])

    monkeypatch.setattr(gen_texts, "chamar_gerador", falso)
    return prompts


def test_retry_repassa_os_problemas_e_monta_a_solicitacao(monkeypatch):
    spec = _spec(dificuldade="nenhuma", canal="ouvidoria", **SEM_ID)
    prompts = _gerador_falso(monkeypatch, [["curta"], [_texto_coerente(spec) + LONGO]])

    caso, tentativas = gerar_caso(spec, seed=1)

    assert tentativas == 2
    assert "rejeitada por: ouvidoria pede" in prompts[1]
    assert isinstance(caso, SolicitacaoBruta)
    assert caso.id_caso == spec["id_caso"]
    assert formatar_valor(Decimal(spec["verdade"]["valor_alegado"])) in caso.texto_cliente


def test_texto_incoerente_apos_todas_as_tentativas_falha(monkeypatch):
    spec = _spec(dificuldade="nenhuma", canal="ouvidoria", **SEM_ID)
    _gerador_falso(monkeypatch, [["curta"]] * gen_texts.MAX_TENTATIVAS)

    with pytest.raises(ValueError, match="incoerente com o gabarito"):
        gerar_caso(spec, seed=1)


def test_mesma_seed_gera_o_mesmo_caso(monkeypatch):
    spec = _spec(dificuldade="nenhuma", canal="chat", **SEM_ID)
    mensagens = [_texto_coerente(spec), "e ai", "alguem responde"]
    _gerador_falso(monkeypatch, [mensagens, mensagens])

    primeiro, _ = gerar_caso(spec, seed=7)
    segundo, _ = gerar_caso(spec, seed=7)

    assert primeiro.texto_cliente == segundo.texto_cliente


class _Relogio:
    def __init__(self):
        self.agora = 0.0
        self.dormidas = []

    def __call__(self):
        return self.agora

    def dormir(self, segundos):
        self.dormidas.append(segundos)
        self.agora += segundos


def test_ritmo_espaca_inicios_e_desconta_o_tempo_da_resposta():
    relogio = _Relogio()
    ritmo = gen_texts.Ritmo(4, relogio=relogio, dormir=relogio.dormir)

    ritmo.aguardar()          # primeira chamada nao espera
    relogio.agora += 5        # resposta levou 5s
    ritmo.aguardar()
    relogio.agora += 20       # resposta mais lenta que o intervalo
    ritmo.aguardar()

    assert relogio.dormidas == [10.0]


def test_ritmo_sem_rpm_nao_espera():
    relogio = _Relogio()
    ritmo = gen_texts.Ritmo(None, relogio=relogio, dormir=relogio.dormir)

    for _ in range(3):
        ritmo.aguardar()

    assert relogio.dormidas == []


def test_retry_tambem_passa_pelo_ritmo(monkeypatch):
    spec = _spec(dificuldade="nenhuma", canal="ouvidoria", **SEM_ID)
    _gerador_falso(monkeypatch, [["curta"], [_texto_coerente(spec) + LONGO]])
    relogio = _Relogio()
    ritmo = gen_texts.Ritmo(6, relogio=relogio, dormir=relogio.dormir)

    gerar_caso(spec, seed=1, ritmo=ritmo)

    assert relogio.dormidas == [10.0]
