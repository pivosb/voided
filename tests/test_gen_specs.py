"""O gabarito sintetico concorda com as regras quando a extracao e' perfeita.

Divergencia aqui nao pode vir do LLM: ou o gerador rotulou errado, ou a regra
mudou sem o gerador acompanhar. Nos dois casos a metrica do pipeline mediria
ruido, entao o teste falha em vez de deixar a divergencia sumir no agregado.
"""

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from scripts.gen_specs import gerar_spec
from src.schemas import (
    ExtracaoContestacao,
    MotivoContestacao,
    RegraViolacao,
    SolicitacaoBruta,
)
from src.steps.validate import validar

N_SPECS = 300
RECEBIDA_BASE = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)

# O texto do cliente e' que cita o id falso; a spec so' declara a violacao.
ID_INEXISTENTE = "TX000000000"


def _solicitacao(spec: dict) -> SolicitacaoBruta:
    return SolicitacaoBruta(
        id_caso=spec["id_caso"],
        id_cliente_mascarado=spec["id_cliente_mascarado"],
        canal=spec["canal"],
        recebida_em=spec["recebida_em"],
        texto_cliente="Texto irrelevante para a validacao.",
        transacoes_periodo=spec["transacoes_periodo"],
        contestacoes_ultimos_12m=spec["contestacoes_ultimos_12m"],
    )


def _extracao_perfeita(verdade: dict) -> ExtracaoContestacao:
    motivo = MotivoContestacao(verdade["motivo"])
    ids = list(verdade["ids_contestadas"])
    if RegraViolacao.TRANSACAO_NAO_ENCONTRADA.value in verdade["violacoes_esperadas"]:
        ids.append(ID_INEXISTENTE)

    valor_alegado = verdade["valor_alegado"]
    return ExtracaoContestacao(
        motivo=motivo,
        ids_transacoes_citadas=ids,
        valor_alegado=Decimal(valor_alegado) if valor_alegado is not None else None,
        cliente_afirma_nao_autorizou=verdade["cliente_afirma_nao_autorizou"],
        cliente_tentou_contato_estabelecimento=verdade[
            "cliente_tentou_contato_estabelecimento"
        ],
        resumo_alegacao="Resumo irrelevante para a validacao.",
        confianca=0.3 if motivo is MotivoContestacao.INDETERMINADO else 0.9,
        justificativa="Extracao montada a partir do gabarito.",
    )


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

        resultado = validar(_solicitacao(spec), _extracao_perfeita(verdade))

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
