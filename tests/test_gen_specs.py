"""O gabarito sintetico concorda com as regras quando a extracao e' perfeita.

Divergencia aqui nao pode vir do LLM: ou o gerador rotulou errado, ou a regra
mudou sem o gerador acompanhar. Nos dois casos a metrica do pipeline mediria
ruido, entao o teste falha em vez de deixar a divergencia sumir no agregado.
"""

import random
from datetime import datetime, timedelta, timezone

import pytest

from scripts.build_golden import montar_extracao_perfeita, montar_solicitacao
from scripts.gen_specs import gerar_spec
from src.steps.validate import validar

N_SPECS = 300
RECEBIDA_BASE = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("seed", [20260913, 1, 2])
def test_gabarito_concorda_com_a_validacao(seed):
    rng = random.Random(seed)
    divergencias = []

    for indice in range(1, N_SPECS + 1):
        recebida_em = RECEBIDA_BASE + timedelta(
            days=rng.randint(0, 180), minutes=rng.randint(0, 600)
        )
        spec = gerar_spec(rng, indice, recebida_em)
        verdade = spec["verdade"]

        resultado = validar(montar_solicitacao(spec), montar_extracao_perfeita(verdade))

        emitidas = sorted({v.regra.value for v in resultado.violacoes})
        esperadas = sorted(set(verdade["violacoes_esperadas"]))
        if emitidas != esperadas or resultado.elegivel != verdade["elegivel_esperado"]:
            divergencias.append(
                f"{spec['id_caso']} motivo={verdade['motivo']} "
                f"dificuldade={spec['dificuldade']}: "
                f"esperadas={esperadas} emitidas={emitidas} "
                f"elegivel esperado={verdade['elegivel_esperado']} "
                f"obtido={resultado.elegivel}"
            )

    assert divergencias == []
