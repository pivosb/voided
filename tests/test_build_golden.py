"""O caso dourado reproduz validate + compute sobre a extracao perfeita."""

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from scripts.build_golden import caso_dourado, montar_extracao_perfeita, montar_solicitacao
from scripts.gen_specs import gerar_spec
from src.steps.compute import calcular
from src.steps.validate import validar

RECEBIDA_BASE = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)


def _primeira_spec(elegivel: bool) -> dict:
    rng = random.Random(7)
    for indice in range(1, 200):
        spec = gerar_spec(rng, indice, RECEBIDA_BASE + timedelta(days=indice))
        if spec["verdade"]["elegivel_esperado"] is elegivel:
            return spec
    raise AssertionError("gerador nao produziu a spec pedida")


def test_caso_elegivel_traz_o_estorno_e_a_faixa_do_calculo():
    spec = _primeira_spec(elegivel=True)
    solicitacao = montar_solicitacao(spec)
    extracao = montar_extracao_perfeita(spec["verdade"])
    calculo = calcular(solicitacao, extracao, validar(solicitacao, extracao))

    caso = caso_dourado(spec)

    assert caso.elegivel_humano
    assert caso.valor_estorno_humano == calculo.valor_estorno
    assert caso.faixa_risco_humano == calculo.faixa_risco


def test_caso_nao_elegivel_tem_estorno_zero_e_sem_faixa():
    spec = _primeira_spec(elegivel=False)

    caso = caso_dourado(spec)

    assert not caso.elegivel_humano
    assert caso.valor_estorno_humano == Decimal("0.00")
    assert caso.faixa_risco_humano is None
    assert sorted(r.value for r in caso.violacoes_humano) == sorted(
        spec["verdade"]["violacoes_esperadas"]
    )


def test_gabarito_divergente_da_validacao_falha():
    spec = _primeira_spec(elegivel=True)
    spec["verdade"]["elegivel_esperado"] = False

    with pytest.raises(ValueError, match="diverge do gabarito"):
        caso_dourado(spec)
