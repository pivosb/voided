"""Testes das regras de elegibilidade. Um caso por regra, mais as bordas."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from src.schemas import (
    CanalEntrada,
    ExtracaoContestacao,
    MotivoContestacao,
    RegraViolacao,
    ResultadoValidacao,
    Severidade,
    SolicitacaoBruta,
    CanalTransacao,
    Transacao,
)
from src.steps.validate import (
    JANELA_DUPLICIDADE_HORAS,
    LIMITE_CONTESTACOES_12M,
    PRAZO_CONTESTACAO_DIAS,
    validar,
)

RECEBIDA_EM = datetime(2026, 6, 1, 12, 0)


def _transacao(
    id_transacao: str = "TX1",
    data_hora: datetime | None = None,
    valor: str = "100.00",
    estabelecimento: str = "LOJA A",
    canal_transacao: CanalTransacao = "online",
    ja_estornada: bool = False,
) -> Transacao:
    return Transacao(
        id_transacao=id_transacao,
        data_hora=data_hora if data_hora is not None else RECEBIDA_EM - timedelta(days=10),
        valor=Decimal(valor),
        estabelecimento=estabelecimento,
        mcc="5812",
        canal_transacao=canal_transacao,
        pais="BR",
        ja_estornada=ja_estornada,
    )


def _solicitacao(
    transacoes: list[Transacao] | None = None,
    recebida_em: datetime = RECEBIDA_EM,
    contestacoes_ultimos_12m: int = 0,
) -> SolicitacaoBruta:
    return SolicitacaoBruta(
        id_caso="CASO-1",
        id_cliente_mascarado="CLI-0001",
        canal=CanalEntrada.APP,
        recebida_em=recebida_em,
        texto_cliente="Nao reconheco essa compra.",
        transacoes_periodo=transacoes if transacoes is not None else [_transacao()],
        contestacoes_ultimos_12m=contestacoes_ultimos_12m,
    )


def _extracao(
    motivo: MotivoContestacao = MotivoContestacao.NAO_RECONHECIDA,
    ids_transacoes_citadas: tuple[str, ...] = ("TX1",),
    valor_alegado: Decimal | None = None,
    confianca: float = 0.9,
) -> ExtracaoContestacao:
    return ExtracaoContestacao(
        motivo=motivo,
        ids_transacoes_citadas=list(ids_transacoes_citadas),
        valor_alegado=valor_alegado,
        cliente_afirma_nao_autorizou=True,
        resumo_alegacao="Cliente nao reconhece a compra.",
        confianca=confianca,
        justificativa="Texto explicito de nao reconhecimento.",
    )


def _regras(resultado: ResultadoValidacao) -> set[RegraViolacao]:
    return {v.regra for v in resultado.violacoes}


def test_caso_sem_violacao_e_elegivel():
    resultado = validar(_solicitacao(), _extracao())

    assert resultado.elegivel
    assert resultado.violacoes == []
    assert resultado.transacoes_confirmadas == ["TX1"]


def test_ultimo_dia_do_prazo_ainda_e_elegivel():
    data_hora = RECEBIDA_EM - timedelta(days=PRAZO_CONTESTACAO_DIAS)

    resultado = validar(_solicitacao([_transacao(data_hora=data_hora)]), _extracao())

    assert resultado.elegivel


def test_um_dia_apos_o_prazo_bloqueia():
    data_hora = RECEBIDA_EM - timedelta(days=PRAZO_CONTESTACAO_DIAS + 1)

    resultado = validar(_solicitacao([_transacao(data_hora=data_hora)]), _extracao())

    assert not resultado.elegivel
    assert _regras(resultado) == {RegraViolacao.PRAZO_EXPIRADO}


def test_hora_do_dia_nao_consome_prazo():
    # A borda que a subtracao de datetime erraria: 120 dias e 13 horas ainda e'
    # o dia 120 do prazo.
    data_hora = datetime(2026, 2, 1, 10, 0)
    recebida_em = (data_hora + timedelta(days=PRAZO_CONTESTACAO_DIAS)).replace(hour=23)

    resultado = validar(
        _solicitacao([_transacao(data_hora=data_hora)], recebida_em=recebida_em),
        _extracao(),
    )

    assert resultado.elegivel


def test_primeira_hora_do_dia_seguinte_ao_prazo_bloqueia():
    data_hora = datetime(2026, 2, 1, 10, 0)
    recebida_em = (data_hora + timedelta(days=PRAZO_CONTESTACAO_DIAS + 1)).replace(
        hour=0, minute=1
    )

    resultado = validar(
        _solicitacao([_transacao(data_hora=data_hora)], recebida_em=recebida_em),
        _extracao(),
    )

    assert not resultado.elegivel
    assert _regras(resultado) == {RegraViolacao.PRAZO_EXPIRADO}


def test_dia_da_transacao_e_periodo_de_graca():
    resultado = validar(
        _solicitacao([_transacao(data_hora=RECEBIDA_EM - timedelta(hours=2))]),
        _extracao(),
    )

    assert resultado.elegivel


def test_data_futura_alerta_sem_bloquear():
    data_hora = RECEBIDA_EM + timedelta(days=1)

    resultado = validar(_solicitacao([_transacao(data_hora=data_hora)]), _extracao())

    assert resultado.elegivel
    assert _regras(resultado) == {RegraViolacao.DATA_TRANSACAO_FUTURA}
    assert resultado.violacoes[0].severidade is Severidade.ALERTA


def test_transacao_ja_estornada_bloqueia():
    resultado = validar(_solicitacao([_transacao(ja_estornada=True)]), _extracao())

    assert not resultado.elegivel
    assert _regras(resultado) == {RegraViolacao.TRANSACAO_JA_ESTORNADA}


def test_duplicidade_com_par_no_periodo_e_elegivel():
    primeira = _transacao(id_transacao="TX1")
    segunda = _transacao(
        id_transacao="TX2", data_hora=primeira.data_hora + timedelta(hours=1)
    )

    resultado = validar(
        _solicitacao([primeira, segunda]),
        _extracao(motivo=MotivoContestacao.DUPLICIDADE),
    )

    assert resultado.elegivel


def test_duplicidade_sem_par_bloqueia():
    resultado = validar(
        _solicitacao(), _extracao(motivo=MotivoContestacao.DUPLICIDADE)
    )

    assert not resultado.elegivel
    assert _regras(resultado) == {RegraViolacao.DUPLICIDADE_NAO_CONFIRMADA}


def test_par_duplicado_no_limite_da_janela_conta():
    primeira = _transacao(id_transacao="TX1")
    segunda = _transacao(
        id_transacao="TX2",
        data_hora=primeira.data_hora + timedelta(hours=JANELA_DUPLICIDADE_HORAS),
    )

    resultado = validar(
        _solicitacao([primeira, segunda]),
        _extracao(motivo=MotivoContestacao.DUPLICIDADE),
    )

    assert resultado.elegivel


def test_par_duplicado_fora_da_janela_nao_conta():
    primeira = _transacao(id_transacao="TX1")
    segunda = _transacao(
        id_transacao="TX2",
        data_hora=primeira.data_hora + timedelta(hours=JANELA_DUPLICIDADE_HORAS + 1),
    )

    resultado = validar(
        _solicitacao([primeira, segunda]),
        _extracao(motivo=MotivoContestacao.DUPLICIDADE),
    )

    assert not resultado.elegivel
    assert _regras(resultado) == {RegraViolacao.DUPLICIDADE_NAO_CONFIRMADA}


def test_id_citado_fora_do_periodo_bloqueia():
    resultado = validar(_solicitacao(), _extracao(ids_transacoes_citadas=("TX9",)))

    assert not resultado.elegivel
    assert resultado.transacoes_confirmadas == []
    assert _regras(resultado) == {RegraViolacao.TRANSACAO_NAO_ENCONTRADA}
    assert "TX9" in resultado.violacoes[0].detalhe


def test_sem_id_citado_bloqueia():
    resultado = validar(_solicitacao(), _extracao(ids_transacoes_citadas=()))

    assert not resultado.elegivel
    assert _regras(resultado) == {RegraViolacao.SEM_TRANSACAO_CITADA}


def test_id_citado_repetido_nao_dobra_a_confirmacao():
    resultado = validar(_solicitacao(), _extracao(ids_transacoes_citadas=("TX1", "TX1")))

    assert resultado.transacoes_confirmadas == ["TX1"]


def test_valor_alegado_diferente_alerta_sem_bloquear():
    resultado = validar(_solicitacao(), _extracao(valor_alegado=Decimal("250.00")))

    assert resultado.elegivel
    assert _regras(resultado) == {RegraViolacao.VALOR_ALEGADO_DIVERGENTE}
    assert resultado.violacoes[0].severidade is Severidade.ALERTA


def test_valor_alegado_igual_a_soma_nao_alerta():
    resultado = validar(_solicitacao(), _extracao(valor_alegado=Decimal("100.00")))

    assert resultado.violacoes == []


@pytest.mark.parametrize("valor_devido", ["80.00", "99.99"])
def test_valor_divergente_com_devido_menor_que_o_cobrado_e_elegivel(valor_devido):
    resultado = validar(
        _solicitacao(),
        _extracao(
            motivo=MotivoContestacao.VALOR_DIVERGENTE,
            valor_alegado=Decimal(valor_devido),
        ),
    )

    assert resultado.elegivel
    assert resultado.violacoes == []


@pytest.mark.parametrize("valor_devido", ["100.00", "100.01", None])
def test_valor_divergente_sem_cobranca_a_maior_bloqueia(valor_devido):
    resultado = validar(
        _solicitacao(),
        _extracao(
            motivo=MotivoContestacao.VALOR_DIVERGENTE,
            valor_alegado=Decimal(valor_devido) if valor_devido is not None else None,
        ),
    )

    assert not resultado.elegivel
    assert _regras(resultado) == {RegraViolacao.ESTORNO_PARCIAL_SEM_BASE}


def test_motivo_indeterminado_bloqueia():
    resultado = validar(
        _solicitacao(),
        _extracao(motivo=MotivoContestacao.INDETERMINADO, confianca=0.3),
    )

    assert not resultado.elegivel
    assert _regras(resultado) == {RegraViolacao.MOTIVO_INDETERMINADO}


def test_transacao_presencial_contra_relato_de_nao_reconhecimento_alerta():
    resultado = validar(
        _solicitacao([_transacao(canal_transacao="presencial")]), _extracao()
    )

    assert resultado.elegivel
    assert _regras(resultado) == {RegraViolacao.CARTAO_PRESENTE_VS_NAO_RECONHECIMENTO}


def test_reincidencia_alerta_sem_bloquear():
    resultado = validar(
        _solicitacao(contestacoes_ultimos_12m=LIMITE_CONTESTACOES_12M + 1), _extracao()
    )

    assert resultado.elegivel
    assert _regras(resultado) == {RegraViolacao.REINCIDENCIA_CONTESTACOES}


def test_violacoes_se_acumulam():
    data_hora = RECEBIDA_EM - timedelta(days=PRAZO_CONTESTACAO_DIAS + 1)

    resultado = validar(
        _solicitacao([_transacao(data_hora=data_hora, ja_estornada=True)]), _extracao()
    )

    assert not resultado.elegivel
    assert _regras(resultado) == {
        RegraViolacao.PRAZO_EXPIRADO,
        RegraViolacao.TRANSACAO_JA_ESTORNADA,
    }
