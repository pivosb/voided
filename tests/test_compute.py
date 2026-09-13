"""Testes do calculo: arredondamento, estorno parcial, multiplas transacoes e o teto."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from src.schemas import (
    CanalEntrada,
    ExtracaoContestacao,
    MotivoContestacao,
    RegraViolacao,
    ResultadoCalculo,
    ResultadoValidacao,
    Severidade,
    SolicitacaoBruta,
    Transacao,
    Violacao,
    CanalTransacao,
)
from src.steps.compute import (
    ALERTAS_DE_RISCO_ALTO,
    PRAZO_RESPOSTA_DIAS_UTEIS,
    calcular,
    quantizar_centavos,
)

RECEBIDA_EM = datetime(2026, 6, 1, 12, 0)


def _transacao(
    id_transacao: str = "TX1",
    valor: str = "100.00",
    canal_transacao: CanalTransacao = "online",
) -> Transacao:
    return Transacao(
        id_transacao=id_transacao,
        data_hora=RECEBIDA_EM - timedelta(days=10),
        valor=Decimal(valor),
        estabelecimento="LOJA A",
        mcc="5812",
        canal_transacao=canal_transacao,
        pais="BR",
    )


def _transacoes(*valores: str) -> list[Transacao]:
    return [_transacao(f"TX{i}", valor) for i, valor in enumerate(valores, start=1)]


def _solicitacao(
    transacoes: list[Transacao],
    canal: CanalEntrada = CanalEntrada.APP,
    contestacoes_ultimos_12m: int = 0,
) -> SolicitacaoBruta:
    return SolicitacaoBruta(
        id_caso="CASO-1",
        id_cliente_mascarado="CLI-0001",
        canal=canal,
        recebida_em=RECEBIDA_EM,
        texto_cliente="Nao reconheco essa compra.",
        transacoes_periodo=transacoes,
        contestacoes_ultimos_12m=contestacoes_ultimos_12m,
    )


def _extracao(
    ids: list[str],
    motivo: MotivoContestacao = MotivoContestacao.NAO_RECONHECIDA,
    valor_alegado: str | None = None,
) -> ExtracaoContestacao:
    return ExtracaoContestacao(
        motivo=motivo,
        ids_transacoes_citadas=ids,
        valor_alegado=Decimal(valor_alegado) if valor_alegado is not None else None,
        cliente_afirma_nao_autorizou=True,
        resumo_alegacao="Cliente contesta a cobranca.",
        confianca=0.9,
        justificativa="Texto explicito.",
    )


def _validacao(
    ids: list[str], elegivel: bool = True, alertas: tuple[RegraViolacao, ...] = ()
) -> ResultadoValidacao:
    return ResultadoValidacao(
        elegivel=elegivel,
        violacoes=[
            Violacao(regra=r, severidade=Severidade.ALERTA, detalhe="teste")
            for r in alertas
        ],
        transacoes_confirmadas=ids,
    )


def _calcular(
    transacoes: list[Transacao],
    motivo: MotivoContestacao = MotivoContestacao.NAO_RECONHECIDA,
    valor_alegado: str | None = None,
    alertas: tuple[RegraViolacao, ...] = (),
    canal: CanalEntrada = CanalEntrada.APP,
) -> ResultadoCalculo:
    ids = [t.id_transacao for t in transacoes]
    return calcular(
        _solicitacao(transacoes, canal=canal),
        _extracao(ids, motivo=motivo, valor_alegado=valor_alegado),
        _validacao(ids, alertas=alertas),
    )


# --- arredondamento -------------------------------------------------------- #


@pytest.mark.parametrize(
    "bruto, esperado",
    [("0.125", "0.12"), ("0.135", "0.14"), ("0.126", "0.13"), ("0.124", "0.12")],
)
def test_meio_centavo_arredonda_para_o_par(bruto, esperado):
    assert str(quantizar_centavos(Decimal(bruto))) == esperado


def test_valor_sem_casas_decimais_sai_com_duas_casas():
    resultado = _calcular([_transacao(valor="100")])

    assert str(resultado.valor_estorno) == "100.00"
    assert str(resultado.provisao) == "100.00"


# --- estorno parcial ------------------------------------------------------- #


def test_estorno_parcial_devolve_o_cobrado_a_maior():
    resultado = _calcular(
        _transacoes("100.00"),
        motivo=MotivoContestacao.VALOR_DIVERGENTE,
        valor_alegado="80.00",
    )

    assert resultado.valor_estorno == Decimal("20.00")
    assert resultado.memoria_calculo == [
        "Transacao TX1: R$ 100.00",
        "Soma das confirmadas: R$ 100.00 = R$ 100.00",
        "Estorno parcial: cobrado R$ 100.00 - devido R$ 80.00 = R$ 20.00",
        "Verificacao: estorno R$ 20.00 <= soma R$ 100.00",
        "Faixa baixa: sem alerta e estorno dentro do limite de R$ 1000.00",
        "Provisao: 100% do estorno = R$ 20.00",
        "Prazo de resposta: 5 dias uteis",
    ]


def test_estorno_parcial_sobre_multiplas_transacoes_usa_a_soma():
    resultado = _calcular(
        _transacoes("100.00", "50.00"),
        motivo=MotivoContestacao.VALOR_DIVERGENTE,
        valor_alegado="120.00",
    )

    assert resultado.valor_estorno == Decimal("30.00")


@pytest.mark.parametrize("valor_alegado", ["150.00", "150.01"])
def test_estorno_parcial_sem_cobranca_a_maior_falha(valor_alegado):
    with pytest.raises(ValueError, match="cobranca a maior"):
        _calcular(
            _transacoes("100.00", "50.00"),
            motivo=MotivoContestacao.VALOR_DIVERGENTE,
            valor_alegado=valor_alegado,
        )


def test_estorno_parcial_sem_valor_alegado_falha():
    with pytest.raises(ValueError, match="sem valor_alegado"):
        _calcular(_transacoes("100.00"), motivo=MotivoContestacao.VALOR_DIVERGENTE)


# --- multiplas transacoes -------------------------------------------------- #


def test_estorno_integral_soma_todas_as_confirmadas():
    resultado = _calcular(_transacoes("100.00", "49.90", "0.10"))

    assert resultado.valor_estorno == Decimal("150.00")
    assert resultado.memoria_calculo[:4] == [
        "Transacao TX1: R$ 100.00",
        "Transacao TX2: R$ 49.90",
        "Transacao TX3: R$ 0.10",
        "Soma das confirmadas: R$ 100.00 + R$ 49.90 + R$ 0.10 = R$ 150.00",
    ]


# --- invariante ------------------------------------------------------------ #


@pytest.mark.parametrize(
    "motivo, valores, valor_alegado",
    [
        (MotivoContestacao.NAO_RECONHECIDA, ("0.01",), None),
        (MotivoContestacao.DUPLICIDADE, ("33.33", "33.33"), None),
        (MotivoContestacao.SUSPEITA_FRAUDE, ("999.99", "0.01", "1234.56"), None),
        (MotivoContestacao.SERVICO_NAO_PRESTADO, ("0.05", "0.05", "0.05"), None),
        (MotivoContestacao.CANCELAMENTO_NAO_PROCESSADO, ("4999.99",), None),
        (MotivoContestacao.VALOR_DIVERGENTE, ("100.00",), "99.99"),
        (MotivoContestacao.VALOR_DIVERGENTE, ("0.10", "0.20"), "0.01"),
        (MotivoContestacao.VALOR_DIVERGENTE, ("19.99", "19.99", "19.99"), "0.00"),
    ],
)
def test_estorno_nunca_excede_a_soma_das_confirmadas(motivo, valores, valor_alegado):
    transacoes = _transacoes(*valores)

    resultado = _calcular(transacoes, motivo=motivo, valor_alegado=valor_alegado)

    soma = sum((t.valor for t in transacoes), Decimal("0"))
    assert Decimal("0") < resultado.valor_estorno <= soma
    assert resultado.provisao == resultado.valor_estorno


# --- faixa de risco, provisao e prazo -------------------------------------- #


@pytest.mark.parametrize("valor, faixa", [("1000.00", "baixa"), ("1000.01", "alta")])
def test_limite_do_estorno_automatico(valor, faixa):
    resultado = _calcular(_transacoes(valor))

    assert resultado.faixa_risco == faixa


def test_estorno_acima_do_limite_registra_escalada_e_provisao_do_analista():
    resultado = _calcular(_transacoes("1000.01"))

    assert resultado.provisao == Decimal("1000.01")
    assert (
        "Faixa alta: estorno R$ 1000.01 > limite automatico R$ 1000.00; "
        "segue para analise humana"
    ) in resultado.memoria_calculo
    assert (
        "Provisao: 100% do estorno = R$ 1000.01; provisao final definida pelo analista"
    ) in resultado.memoria_calculo


@pytest.mark.parametrize("alerta", sorted(ALERTAS_DE_RISCO_ALTO))
def test_alerta_de_risco_alto_eleva_a_faixa_mesmo_com_valor_baixo(alerta):
    resultado = _calcular(_transacoes("10.00"), alertas=(alerta,))

    assert resultado.faixa_risco == "alta"
    assert resultado.memoria_calculo[-3] == (
        f"Faixa alta: alerta {alerta.value}; segue para analise humana"
    )


def test_demais_alertas_levam_a_faixa_media():
    alerta = RegraViolacao.DATA_TRANSACAO_FUTURA

    resultado = _calcular(_transacoes("10.00"), alertas=(alerta,))

    assert resultado.faixa_risco == "media"
    assert resultado.memoria_calculo[-3] == f"Faixa media: alerta {alerta.value}"


@pytest.mark.parametrize("canal", list(CanalEntrada))
def test_prazo_de_resposta_e_o_mesmo_em_qualquer_canal(canal):
    resultado = _calcular(_transacoes("100.00"), canal=canal)

    assert resultado.prazo_resposta_dias == PRAZO_RESPOSTA_DIAS_UTEIS == 5


# --- guardas --------------------------------------------------------------- #


def test_caso_nao_elegivel_nao_e_calculado():
    transacoes = _transacoes("100.00")

    with pytest.raises(ValueError, match="nao e' elegivel"):
        calcular(
            _solicitacao(transacoes),
            _extracao(["TX1"]),
            _validacao(["TX1"], elegivel=False),
        )


def test_transacao_confirmada_ausente_do_periodo_falha():
    with pytest.raises(ValueError, match="TX9"):
        calcular(
            _solicitacao(_transacoes("100.00")),
            _extracao(["TX9"]),
            _validacao(["TX9"]),
        )
