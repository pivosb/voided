"""
Gera as especificacoes dos casos sinteticos -- a VERDADE, antes de existir texto.

Decisao de projeto: o rotulo e' gerado primeiro e o texto do cliente depois
(scripts/gen_texts.py), nunca o contrario. Assim a verdade e' construida, nao
inferida, e nenhum modelo de linguagem participa da definicao do gabarito.

Saida: data/case_specs.jsonl  (uma spec por linha)
Uso:   python scripts/gen_specs.py [--n 120] [--seed 20260913]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from src.schemas import (  # noqa: E402
    CanalEntrada,
    MotivoContestacao,
    RegraViolacao,
    Transacao,
)
from src.steps.validate import PRAZO_CONTESTACAO_DIAS  # noqa: E402

# --------------------------------------------------------------------------- #
# Tabela de estratificacao -- esta tabela vai colada no README.
# Prevalencia deliberadamente ARTIFICIAL: casos dificeis sobreamostrados porque
# sao eles que produzem informacao. A acuracia agregada aqui NAO e' a acuracia
# de producao; reportar por classe e reponderar pela prevalencia real.
# --------------------------------------------------------------------------- #

DIST_MOTIVO: dict[MotivoContestacao, float] = {
    MotivoContestacao.NAO_RECONHECIDA: 0.30,
    MotivoContestacao.DUPLICIDADE: 0.15,
    MotivoContestacao.VALOR_DIVERGENTE: 0.15,
    MotivoContestacao.SERVICO_NAO_PRESTADO: 0.15,
    MotivoContestacao.CANCELAMENTO_NAO_PROCESSADO: 0.10,
    MotivoContestacao.SUSPEITA_FRAUDE: 0.10,
    MotivoContestacao.INDETERMINADO: 0.05,
}

# Violacoes de elegibilidade injetadas por cima do motivo, ~20% dos casos.
DIST_VIOLACAO: dict[RegraViolacao | None, float] = {
    None: 0.80,
    RegraViolacao.PRAZO_EXPIRADO: 0.07,
    RegraViolacao.TRANSACAO_JA_ESTORNADA: 0.05,
    RegraViolacao.REINCIDENCIA_CONTESTACOES: 0.04,
    RegraViolacao.TRANSACAO_NAO_ENCONTRADA: 0.04,
}

# Registram sem bloquear: o caso continua elegivel.
VIOLACOES_ALERTA = frozenset(
    {
        RegraViolacao.REINCIDENCIA_CONTESTACOES,
        RegraViolacao.VALOR_ALEGADO_DIVERGENTE,
        RegraViolacao.CARTAO_PRESENTE_VS_NAO_RECONHECIMENTO,
    }
)

# Dificuldades textuais, sorteadas de forma independente. Sao elas que tornam
# a calibracao da confianca mensuravel -- sem isso, tudo vira caso facil.
DIST_DIFICULDADE: dict[str, float] = {
    "nenhuma": 0.55,
    "texto_vago": 0.12,            # "tem umas coisas erradas na fatura"
    "relato_contraditorio": 0.10,  # texto diverge dos dados da transacao
    "valor_nao_bate": 0.08,        # valor citado nao casa com nenhuma transacao
    "multiplas_transacoes": 0.10,  # contesta 3+ de uma vez
    "mistura_dois_assuntos": 0.05,  # reclamacao + pedido nao relacionado
}

DIST_CANAL: dict[CanalEntrada, float] = {
    CanalEntrada.APP: 0.45,
    CanalEntrada.CHAT: 0.30,
    CanalEntrada.TELEFONE: 0.15,
    CanalEntrada.OUVIDORIA: 0.10,
}

# (nome, mcc, canal_transacao, faixa de valor)
ESTABELECIMENTOS = [
    ("MERCADO SAO JOAO", "5411", "presencial", (25, 480)),
    ("POSTO IPIRANGA VL MARIANA", "5541", "presencial", (80, 350)),
    ("DROGARIA SP", "5912", "presencial", (18, 240)),
    ("RESTAURANTE OSAKA", "5812", "presencial", (45, 390)),
    ("LOJA VIRTUAL TECHBOX", "5732", "online", (150, 4200)),
    ("STREAMING GLOBALPLAY", "5815", "recorrente", (19, 70)),
    ("ACADEMIA MOVE", "7997", "recorrente", (89, 260)),
    ("PASSAGENS AEREAS VOEJA", "4511", "online", (380, 3800)),
    ("HOTEL COSTA AZUL", "7011", "online", (240, 2600)),
    ("MARKETPLACE ZAPSHOP", "5999", "online", (35, 1500)),
    ("SAQUE ATM 24H", "6011", "saque", (100, 1200)),
    ("CURSO ONLINE EDUPRO", "8299", "online", (120, 2400)),
]

PAISES = ["BR"] * 18 + ["US", "PT"]  # minoria internacional, casos de fraude


def escolher(rng: random.Random, dist: dict) -> object:
    """Sorteio ponderado deterministico."""
    return rng.choices(list(dist.keys()), weights=list(dist.values()), k=1)[0]


def dinheiro(rng: random.Random, minimo: float, maximo: float) -> Decimal:
    return Decimal(f"{rng.uniform(minimo, maximo):.2f}")


def gerar_transacoes(
    rng: random.Random, recebida_em: datetime, n: int
) -> list[Transacao]:
    """Extrato do periodo. Inclui transacoes legitimas alem das contestadas,
    para que a extracao precise de fato escolher quais sao citadas."""
    transacoes: list[Transacao] = []
    for i in range(n):
        nome, mcc, canal_tx, (lo, hi) = rng.choice(ESTABELECIMENTOS)
        # -1 porque as horas subtraidas abaixo podem recuar a data em um dia; prazo
        # vencido so' entra por injecao explicita em gerar_spec.
        dias_atras = rng.randint(1, PRAZO_CONTESTACAO_DIAS - 1)
        transacoes.append(
            Transacao(
                id_transacao=f"TX{rng.randrange(10**8, 10**9)}",
                data_hora=recebida_em - timedelta(days=dias_atras, hours=rng.randint(0, 23)),
                valor=dinheiro(rng, lo, hi),
                estabelecimento=nome,
                mcc=mcc,
                canal_transacao=canal_tx,
                pais=rng.choice(PAISES),
                ja_estornada=False,
            )
        )
    return sorted(transacoes, key=lambda t: t.data_hora)


def aplicar_motivo(
    rng: random.Random,
    motivo: MotivoContestacao,
    transacoes: list[Transacao],
    recebida_em: datetime,
) -> list[Transacao]:
    """Reescreve o extrato para que ele seja COERENTE com o motivo real.
    Duplicidade precisa de duas transacoes iguais; valor divergente precisa de
    um valor redondo por perto. Sem isso, o gabarito seria inverificavel."""
    if motivo is MotivoContestacao.DUPLICIDADE:
        base = rng.choice(transacoes)
        gemea = base.model_copy(
            update={
                "id_transacao": f"TX{rng.randrange(10**8, 10**9)}",
                "data_hora": base.data_hora + timedelta(minutes=rng.randint(2, 45)),
            }
        )
        transacoes = sorted(transacoes + [gemea], key=lambda t: t.data_hora)
    elif motivo is MotivoContestacao.SUSPEITA_FRAUDE:
        # rajada: 3 transacoes online, mesmo dia, uma delas fora do pais
        nome, mcc, _, (lo, hi) = ESTABELECIMENTOS[9]
        dia = recebida_em - timedelta(days=rng.randint(1, 20))
        rajada = [
            Transacao(
                id_transacao=f"TX{rng.randrange(10**8, 10**9)}",
                data_hora=dia + timedelta(minutes=12 * k),
                valor=dinheiro(rng, lo, hi),
                estabelecimento=nome,
                mcc=mcc,
                canal_transacao="online",
                pais="US" if k == 2 else "BR",
            )
            for k in range(3)
        ]
        transacoes = sorted(transacoes + rajada, key=lambda t: t.data_hora)
    return transacoes


def selecionar_contestadas(
    rng: random.Random,
    motivo: MotivoContestacao,
    transacoes: list[Transacao],
    dificuldade: str,
) -> list[Transacao]:
    if motivo is MotivoContestacao.DUPLICIDADE:
        chaves = [(t.valor, t.estabelecimento) for t in transacoes]
        dup = [t for t in transacoes if chaves.count((t.valor, t.estabelecimento)) > 1]
        return dup[:2] if len(dup) >= 2 else [transacoes[-1]]
    if motivo is MotivoContestacao.SUSPEITA_FRAUDE:
        online = [t for t in transacoes if t.canal_transacao == "online"]
        return online[-3:] if len(online) >= 3 else transacoes[-2:]
    if dificuldade == "multiplas_transacoes":
        return rng.sample(transacoes, k=min(3, len(transacoes)))
    return [rng.choice(transacoes)]


def gerar_spec(rng: random.Random, indice: int, recebida_em: datetime) -> dict:
    motivo = escolher(rng, DIST_MOTIVO)
    violacao = escolher(rng, DIST_VIOLACAO)
    dificuldade = escolher(rng, DIST_DIFICULDADE)
    canal = escolher(rng, DIST_CANAL)

    transacoes = gerar_transacoes(rng, recebida_em, n=rng.randint(6, 14))
    transacoes = aplicar_motivo(rng, motivo, transacoes, recebida_em)
    contestadas = selecionar_contestadas(rng, motivo, transacoes, dificuldade)

    contestacoes_12m = rng.choice([0, 0, 0, 1, 1, 2])
    violacoes_esperadas: list[RegraViolacao] = []

    if violacao is RegraViolacao.PRAZO_EXPIRADO:
        # Recua todas as contestadas pelo mesmo delta: a mais recente passa do
        # prazo, entao todas passam, e o par de duplicidade continua um par.
        atraso = timedelta(
            days=rng.randint(PRAZO_CONTESTACAO_DIAS + 1, PRAZO_CONTESTACAO_DIAS + 40)
        )
        recuo = max(t.data_hora for t in contestadas) - (recebida_em - atraso)
        antigas = {
            t.id_transacao: t.model_copy(update={"data_hora": t.data_hora - recuo})
            for t in contestadas
        }
        transacoes = [antigas.get(t.id_transacao, t) for t in transacoes]
        contestadas = [antigas[t.id_transacao] for t in contestadas]
        violacoes_esperadas.append(RegraViolacao.PRAZO_EXPIRADO)
    elif violacao is RegraViolacao.TRANSACAO_JA_ESTORNADA:
        alvo = contestadas[0]
        estornada = alvo.model_copy(update={"ja_estornada": True})
        transacoes = [estornada if t.id_transacao == alvo.id_transacao else t for t in transacoes]
        contestadas = [estornada if t.id_transacao == alvo.id_transacao else t for t in contestadas]
        violacoes_esperadas.append(RegraViolacao.TRANSACAO_JA_ESTORNADA)
    elif violacao is RegraViolacao.REINCIDENCIA_CONTESTACOES:
        contestacoes_12m = rng.randint(6, 11)
        violacoes_esperadas.append(RegraViolacao.REINCIDENCIA_CONTESTACOES)
    elif violacao is RegraViolacao.TRANSACAO_NAO_ENCONTRADA:
        violacoes_esperadas.append(RegraViolacao.TRANSACAO_NAO_ENCONTRADA)

    total_contestado = sum((t.valor for t in contestadas), Decimal("0.00"))
    if dificuldade == "valor_nao_bate":
        valor_alegado = (total_contestado + dinheiro(rng, 12, 90)).quantize(Decimal("0.01"))
    elif dificuldade == "texto_vago":
        # Vem antes de valor_divergente: texto vago nao cita valor, entao nao ha
        # valor alegado para extrair, nem base para estorno parcial.
        valor_alegado = None
    elif motivo is MotivoContestacao.VALOR_DIVERGENTE:
        valor_alegado = (total_contestado - dinheiro(rng, 5, 60)).quantize(Decimal("0.01"))
        valor_alegado = max(valor_alegado, Decimal("1.00"))
    else:
        valor_alegado = total_contestado

    cliente_afirma_nao_autorizou = motivo in (
        MotivoContestacao.NAO_RECONHECIDA,
        MotivoContestacao.SUSPEITA_FRAUDE,
    )

    if motivo is MotivoContestacao.INDETERMINADO:
        violacoes_esperadas.append(RegraViolacao.MOTIVO_INDETERMINADO)
    if motivo is MotivoContestacao.VALOR_DIVERGENTE and (
        dificuldade == "valor_nao_bate" or valor_alegado is None
    ):
        violacoes_esperadas.append(RegraViolacao.ESTORNO_PARCIAL_SEM_BASE)
    elif dificuldade == "valor_nao_bate":
        violacoes_esperadas.append(RegraViolacao.VALOR_ALEGADO_DIVERGENTE)
    if cliente_afirma_nao_autorizou and any(
        t.canal_transacao == "presencial" for t in contestadas
    ):
        violacoes_esperadas.append(RegraViolacao.CARTAO_PRESENTE_VS_NAO_RECONHECIMENTO)

    return {
        "id_caso": f"CASO-{indice:04d}",
        "id_cliente_mascarado": f"cli_{rng.randrange(16**6):06x}",
        "canal": canal.value,
        "recebida_em": recebida_em.isoformat(),
        "contestacoes_ultimos_12m": contestacoes_12m,
        "transacoes_periodo": [json.loads(t.model_dump_json()) for t in transacoes],
        # ---- gabarito ----
        "verdade": {
            "motivo": motivo.value,
            "ids_contestadas": [t.id_transacao for t in contestadas],
            "valor_alegado": str(valor_alegado) if valor_alegado is not None else None,
            "valor_total_contestado": str(total_contestado),
            "cliente_afirma_nao_autorizou": cliente_afirma_nao_autorizou,
            "cliente_tentou_contato_estabelecimento": rng.random() < 0.45
            if motivo
            in (
                MotivoContestacao.SERVICO_NAO_PRESTADO,
                MotivoContestacao.CANCELAMENTO_NAO_PROCESSADO,
            )
            else None,
            "violacoes_esperadas": [v.value for v in violacoes_esperadas],
            "elegivel_esperado": all(v in VIOLACOES_ALERTA for v in violacoes_esperadas),
        },
        # ---- metadados de avaliacao ----
        "dificuldade": dificuldade,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--seed", type=int, default=20260913)
    ap.add_argument("--n-eval", type=int, default=40, help="casos mais recentes = avaliacao")
    ap.add_argument("--out", type=Path, default=RAIZ / "data" / "case_specs.jsonl")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    base = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)

    # Datas espalhadas em ~180 dias: o split e' TEMPORAL, nao aleatorio.
    datas = sorted(
        base + timedelta(days=rng.randint(0, 180), minutes=rng.randint(0, 600))
        for _ in range(args.n)
    )
    specs = [gerar_spec(rng, i + 1, d) for i, d in enumerate(datas)]

    corte = len(specs) - args.n_eval
    for i, s in enumerate(specs):
        s["split"] = "dev" if i < corte else "eval"

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for s in specs:
            fh.write(json.dumps(s, ensure_ascii=False) + "\n")

    motivos = Counter(s["verdade"]["motivo"] for s in specs)
    dificuldades = Counter(s["dificuldade"] for s in specs)
    violacoes = Counter(
        v for s in specs for v in (s["verdade"]["violacoes_esperadas"] or ["nenhuma"])
    )
    print(f"{len(specs)} specs -> {args.out}  (seed={args.seed})")
    print(f"  split: dev={corte}  eval={args.n_eval}  (corte temporal)")
    print(f"  motivo:     {dict(motivos)}")
    print(f"  violacao:   {dict(violacoes)}")
    print(f"  dificuldade:{dict(dificuldades)}")


if __name__ == "__main__":
    main()