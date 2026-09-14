"""Politica de roteamento: entrada, escalada por schema/confianca, teto e faixa de risco."""

import pytest

from src.router import ESCADA, LIMIAR_CONFIANCA, MAX_ESCALADAS, decidir
from src.schemas import TierModelo


def test_entrada_comeca_no_tier_pequeno():
    decisao = decidir(0, False, None, None)

    assert (decisao.tier, decisao.tentativa) == (TierModelo.PEQUENO, 1)
    assert "entrada" in decisao.motivo_decisao


def test_confianca_no_limiar_e_aceita_no_tier_atual():
    decisao = decidir(2, False, LIMIAR_CONFIANCA, None)

    assert (decisao.tier, decisao.tentativa) == (TierModelo.MEDIO, 2)
    assert "segue para validacao" in decisao.motivo_decisao


@pytest.mark.parametrize("tentativa, proximo", [(1, TierModelo.MEDIO), (2, TierModelo.GRANDE)])
def test_falha_de_schema_sobe_um_degrau(tentativa, proximo):
    decisao = decidir(tentativa, True, None, None)

    assert (decisao.tier, decisao.tentativa) == (proximo, tentativa + 1)
    assert "fora do schema" in decisao.motivo_decisao


def test_confianca_abaixo_do_limiar_sobe_e_o_motivo_cita_os_numeros():
    decisao = decidir(1, False, 0.55, None)

    assert decisao.tier is TierModelo.MEDIO
    assert "0.55" in decisao.motivo_decisao
    assert f"{LIMIAR_CONFIANCA:.2f}" in decisao.motivo_decisao


@pytest.mark.parametrize("falha_schema, confianca", [(True, None), (False, 0.1)])
def test_escaladas_esgotadas_vao_para_humano(falha_schema, confianca):
    decisao = decidir(1 + MAX_ESCALADAS, falha_schema, confianca, None)

    assert decisao.tier is TierModelo.HUMANO
    assert "esgotadas" in decisao.motivo_decisao


def test_escada_cobre_todas_as_tentativas():
    assert len(ESCADA) >= 1 + MAX_ESCALADAS


def test_faixa_alta_vai_para_humano():
    decisao = decidir(1, False, 0.9, "alta")

    assert decisao.tier is TierModelo.HUMANO
    assert "faixa de risco alta" in decisao.motivo_decisao


@pytest.mark.parametrize("faixa", ["baixa", "media"])
def test_faixa_baixa_ou_media_segue_automatico(faixa):
    decisao = decidir(2, False, 0.9, faixa)

    assert (decisao.tier, decisao.tentativa) == (TierModelo.MEDIO, 2)
    assert "registro automatico" in decisao.motivo_decisao


def test_extracao_valida_sem_confianca_e_erro_de_uso():
    with pytest.raises(ValueError, match="confianca"):
        decidir(1, False, None, None)


@pytest.mark.parametrize("tentativa", [-1, 2 + MAX_ESCALADAS])
def test_tentativa_fora_da_faixa_e_erro_de_uso(tentativa):
    with pytest.raises(ValueError, match="fora de"):
        decidir(tentativa, True, None, None)
