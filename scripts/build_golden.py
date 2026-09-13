"""
Gera o conjunto dourado: o resultado esperado de cada caso sintetico.

Aplica src/steps/validate.py e src/steps/compute.py sobre a extracao perfeita
montada do gabarito da spec. O rotulo de dinheiro sai das mesmas regras
deterministicas do pipeline, sem LLM.

Entrada: data/case_specs.jsonl  (scripts/gen_specs.py)
Saida:   data/golden_set.jsonl  (um CasoDourado por linha)
Uso:     python scripts/build_golden.py [--specs ...] [--out ...]
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from src.schemas import (  # noqa: E402
    CasoDourado,
    ExtracaoContestacao,
    MotivoContestacao,
    RegraViolacao,
    SolicitacaoBruta,
)
from src.steps.compute import calcular  # noqa: E402
from src.steps.validate import validar  # noqa: E402

# O texto do cliente e' que cita o id falso; a spec so' declara a violacao.
ID_INEXISTENTE = "TX000000000"


def montar_solicitacao(spec: dict) -> SolicitacaoBruta:
    return SolicitacaoBruta(
        id_caso=spec["id_caso"],
        id_cliente_mascarado=spec["id_cliente_mascarado"],
        canal=spec["canal"],
        recebida_em=spec["recebida_em"],
        texto_cliente="Texto irrelevante para a validacao.",
        transacoes_periodo=spec["transacoes_periodo"],
        contestacoes_ultimos_12m=spec["contestacoes_ultimos_12m"],
    )


def montar_extracao_perfeita(verdade: dict) -> ExtracaoContestacao:
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


def caso_dourado(spec: dict) -> CasoDourado:
    """Levanta ValueError se a validacao contradiz o gabarito da spec."""
    verdade = spec["verdade"]
    solicitacao = montar_solicitacao(spec)
    extracao = montar_extracao_perfeita(verdade)

    validacao = validar(solicitacao, extracao)

    # Spec gerada por versao antiga do gerador: gravar o rotulo contradiria o
    # proprio gabarito, entao falha em vez de escolher um dos dois.
    emitidas = sorted({v.regra.value for v in validacao.violacoes})
    esperadas = sorted(set(verdade["violacoes_esperadas"]))
    if emitidas != esperadas or validacao.elegivel != verdade["elegivel_esperado"]:
        raise ValueError(
            f"validacao diverge do gabarito: esperadas={esperadas} "
            f"elegivel={verdade['elegivel_esperado']}; emitidas={emitidas} "
            f"elegivel={validacao.elegivel}"
        )

    calculo = calcular(solicitacao, extracao, validacao) if validacao.elegivel else None

    return CasoDourado(
        id_caso=solicitacao.id_caso,
        decidido_em=solicitacao.recebida_em.date(),
        motivo_humano=extracao.motivo,
        elegivel_humano=validacao.elegivel,
        valor_estorno_humano=calculo.valor_estorno if calculo else Decimal("0.00"),
        violacoes_humano=list(dict.fromkeys(v.regra for v in validacao.violacoes)),
        faixa_risco_humano=calculo.faixa_risco if calculo else None,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--specs", type=Path, default=RAIZ / "data" / "case_specs.jsonl")
    ap.add_argument("--out", type=Path, default=RAIZ / "data" / "golden_set.jsonl")
    args = ap.parse_args()

    casos: list[CasoDourado] = []
    with args.specs.open(encoding="utf-8") as fh:
        for numero, linha in enumerate(fh, start=1):
            id_caso = "?"
            try:
                spec = json.loads(linha)
                id_caso = spec["id_caso"]
                casos.append(caso_dourado(spec))
            except Exception as exc:
                raise RuntimeError(f"{args.specs}:{numero} ({id_caso}): {exc}") from exc

    if not casos:
        raise RuntimeError(f"{args.specs} nao tem nenhuma spec.")

    # So' grava depois de tudo processar: nunca fica golden set parcial em disco.
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for caso in casos:
            fh.write(caso.model_dump_json() + "\n")

    elegiveis = sum(c.elegivel_humano for c in casos)
    total = sum((c.valor_estorno_humano for c in casos), Decimal("0.00"))
    print(f"{len(casos)} casos -> {args.out}")
    print(f"  elegiveis: {elegiveis} de {len(casos)}")
    print(f"  estorno total esperado: R$ {total}")


if __name__ == "__main__":
    main()
