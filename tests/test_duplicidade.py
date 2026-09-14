"""Definicao de cobranca repetida, compartilhada por validate e compute."""

from datetime import datetime, timedelta
from decimal import Decimal

from src.duplicidade import JANELA_DUPLICIDADE_HORAS, grupos_repetidos
from src.schemas import Transacao

INICIO = datetime(2026, 5, 1, 10, 0)


def _t(id_transacao: str, horas: float, valor: str = "10.00", loja: str = "LOJA A") -> Transacao:
    return Transacao(
        id_transacao=id_transacao,
        data_hora=INICIO + timedelta(hours=horas),
        valor=Decimal(valor),
        estabelecimento=loja,
        mcc="5812",
        canal_transacao="online",
        pais="BR",
    )


def _ids(grupos):
    return [[t.id_transacao for t in g] for g in grupos]


def test_grupo_encadeia_pela_cobranca_anterior_e_ignora_a_ordem_de_entrada():
    janela = JANELA_DUPLICIDADE_HORAS
    transacoes = [_t("TX3", 2 * janela), _t("TX1", 0), _t("TX2", janela)]

    assert _ids(grupos_repetidos(transacoes)) == [["TX1", "TX2", "TX3"]]


def test_loja_ou_valor_diferente_nao_agrupa_e_avulsa_nao_vira_grupo():
    transacoes = [_t("TX1", 0), _t("TX2", 1, valor="10.01"), _t("TX3", 1, loja="LOJA B"), _t("TX4", 2)]

    assert _ids(grupos_repetidos(transacoes)) == [["TX1", "TX4"]]


def test_mesma_cobranca_depois_da_janela_abre_grupo_novo():
    transacoes = [_t("TX1", 0), _t("TX2", 1), _t("TX3", JANELA_DUPLICIDADE_HORAS + 2), _t("TX4", JANELA_DUPLICIDADE_HORAS + 3)]

    assert _ids(grupos_repetidos(transacoes)) == [["TX1", "TX2"], ["TX3", "TX4"]]
