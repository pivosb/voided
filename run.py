"""
Executa o pipeline sobre os casos sinteticos, ou preenche o cache do LLM.

Uso:
    python run.py --mock   [--limit N] [--split dev|eval|todos] [--out ...]
    python run.py --record [--tier pequeno|medio|grande|todos] [--limit N] [--split ...]

--mock   roda a maquina de estados reproduzindo data/llm_cache.jsonl, sem chave e
         sem rede, e grava um EventoExecucao por caso.
--record chama os provedores de config/modelos.toml e so' grava o cache: toda
         extracao vai para o tier pedido, sem roteador e sem evento. Com
         --tier todos o cache cobre qualquer caminho do roteador, o que permite
         avaliar politicas de roteamento offline. So' com o padrao (pequeno), um
         --mock depois da ERRO nos casos que o roteador escalaria.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from src import llm, telemetry
from src.llm import FalhaSchema
from src.orchestrator import processar_caso
from src.schemas import EventoExecucao, SolicitacaoBruta, StatusExecucao, TierModelo
from src.steps.extract import extrair

RAIZ = Path(__file__).resolve().parent
CASOS_PADRAO = RAIZ / "data" / "synthetic_cases.jsonl"
SPECS_PADRAO = RAIZ / "data" / "case_specs.jsonl"
OUT_PADRAO = RAIZ / "outputs" / "eventos.jsonl"

TIERS_RECORD = {
    "pequeno": [TierModelo.PEQUENO],
    "medio": [TierModelo.MEDIO],
    "grande": [TierModelo.GRANDE],
    "todos": [TierModelo.PEQUENO, TierModelo.MEDIO, TierModelo.GRANDE],
}


def carregar_casos(caminho: Path) -> list[SolicitacaoBruta]:
    casos = []
    with caminho.open(encoding="utf-8") as fh:
        for numero, linha in enumerate(fh, start=1):
            if not linha.strip():
                continue
            try:
                casos.append(SolicitacaoBruta.model_validate_json(linha))
            except ValueError as exc:
                raise RuntimeError(f"{caminho}:{numero}: {exc}") from exc
    return casos


def carregar_splits(caminho: Path) -> dict[str, str]:
    """id_caso -> split. O split vive no gabarito, nao na SolicitacaoBruta."""
    with caminho.open(encoding="utf-8") as fh:
        return {
            spec["id_caso"]: spec["split"]
            for spec in (json.loads(linha) for linha in fh if linha.strip())
        }


def selecionar(
    casos: list[SolicitacaoBruta], split: str, splits: dict[str, str], limite: int | None
) -> list[SolicitacaoBruta]:
    if split != "todos":
        sem_split = [c.id_caso for c in casos if c.id_caso not in splits]
        if sem_split:
            raise RuntimeError(f"casos sem split no gabarito: {', '.join(sem_split[:5])}")
        casos = [c for c in casos if splits[c.id_caso] == split]
    return casos if limite is None else casos[:limite]


def percentil(valores: list[int], p: float) -> int:
    """Nearest-rank: sempre um valor observado, sem interpolacao."""
    if not valores:
        raise ValueError("percentil de lista vazia")
    ordenados = sorted(valores)
    return ordenados[max(math.ceil(p / 100 * len(ordenados)), 1) - 1]


def resumo(eventos: list[EventoExecucao]) -> list[str]:
    contagem = Counter(e.status for e in eventos)
    custo = sum((Decimal(str(c.custo_usd)) for e in eventos for c in e.custos), Decimal("0"))
    # Em --mock a latencia de parede nao mede nada; a das chamadas gravadas reproduz
    # a execucao real.
    llm_ms = [sum(c.latencia_ms for c in e.custos) for e in eventos]
    parede_ms = [e.latencia_total_ms for e in eventos]

    linhas = [f"  {s.value}: {contagem[s]}" for s in StatusExecucao]
    linhas.append(f"  custo total: US$ {custo:.6f}")
    if eventos:
        linhas.append(
            f"  latencia LLM por caso: p50 {percentil(llm_ms, 50)} ms, "
            f"p95 {percentil(llm_ms, 95)} ms"
        )
        linhas.append(
            f"  latencia de parede por caso: p50 {percentil(parede_ms, 50)} ms, "
            f"p95 {percentil(parede_ms, 95)} ms"
        )
    return linhas


@dataclass
class Gravacao:
    gravadas: Counter[TierModelo] = field(default_factory=Counter)
    fora_do_schema: Counter[TierModelo] = field(default_factory=Counter)
    falhas: list[str] = field(default_factory=list)


def preencher_cache(casos: list[SolicitacaoBruta], tiers: list[TierModelo]) -> Gravacao:
    """Extrai cada caso em cada tier, na ordem dos tiers, sem roteador.

    O que fica e' o cache: resposta fora do schema tambem foi gravada pelo llm e
    conta como gravada. Falha sem gravacao (rede, provedor) nao interrompe o lote.
    """
    gravacao = Gravacao()
    total = len(casos) * len(tiers)
    numero = 0
    for tier in tiers:
        for solicitacao in casos:
            numero += 1
            try:
                extrair(solicitacao, tier)
                resultado = "gravada"
            except FalhaSchema:
                gravacao.fora_do_schema[tier] += 1
                resultado = "gravada, fora do schema"
            except Exception as exc:  # noqa: BLE001 -- um caso nao derruba o lote
                gravacao.falhas.append(
                    f"{tier.value} {solicitacao.id_caso}: {type(exc).__name__}: {exc}"
                )
                print(f"[{numero}/{total}] {tier.value} {solicitacao.id_caso} FALHA", flush=True)
                continue
            gravacao.gravadas[tier] += 1
            print(f"[{numero}/{total}] {tier.value} {solicitacao.id_caso} {resultado}", flush=True)
    return gravacao


def resumo_gravacao(gravacao: Gravacao, tiers: list[TierModelo]) -> list[str]:
    linhas = [
        f"  {t.value}: {gravacao.gravadas[t]} gravadas "
        f"({gravacao.fora_do_schema[t]} fora do schema)"
        for t in tiers
    ]
    linhas.append(f"  falhas sem gravacao: {len(gravacao.falhas)}")
    linhas += [f"    {f}" for f in gravacao.falhas]
    return linhas


def main() -> None:
    ap = argparse.ArgumentParser()
    modo = ap.add_mutually_exclusive_group(required=True)
    modo.add_argument("--mock", action="store_true", help="reproduz data/llm_cache.jsonl")
    modo.add_argument("--record", action="store_true", help="chama a API e grava o cache")
    ap.add_argument("--tier", choices=list(TIERS_RECORD), default=None,
                    help="so' com --record; padrao pequeno")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--split", choices=["dev", "eval", "todos"], default="todos")
    ap.add_argument("--out", type=Path, default=None, help=f"so' com --mock; padrao {OUT_PADRAO}")
    ap.add_argument("--casos", type=Path, default=CASOS_PADRAO)
    args = ap.parse_args()

    if args.mock and args.tier is not None:
        ap.error("--tier so' vale com --record: no --mock o tier sai do roteador")
    if args.record and args.out is not None:
        ap.error("--out so' vale com --mock: --record grava so' o cache, sem eventos")
    if args.limit is not None and args.limit < 1:
        ap.error("--limit precisa ser >= 1")
    if not args.casos.exists():
        sys.exit(f"{args.casos} nao existe; gere com python scripts/gen_texts.py")
    if args.mock and not llm.CACHE_PADRAO.exists():
        sys.exit(f"{llm.CACHE_PADRAO} nao existe; grave com python run.py --record --tier todos")

    llm.configurar("mock" if args.mock else "real", llm.CACHE_PADRAO)
    splits = carregar_splits(SPECS_PADRAO) if args.split != "todos" else {}
    casos = selecionar(carregar_casos(args.casos), args.split, splits, args.limit)

    if args.record:
        tiers = TIERS_RECORD[args.tier or "pequeno"]
        gravacao = preencher_cache(casos, tiers)
        print(f"\n{len(casos)} casos (record, split {args.split}) -> {llm.CACHE_PADRAO}")
        print("\n".join(resumo_gravacao(gravacao, tiers)))
        return

    out = args.out or OUT_PADRAO
    eventos = []
    for numero, solicitacao in enumerate(casos, start=1):
        evento = processar_caso(solicitacao)
        telemetry.gravar(evento, out)
        eventos.append(evento)
        print(f"[{numero}/{len(casos)}] {evento.id_caso} {evento.status.value}", flush=True)

    print(f"\n{len(eventos)} casos (mock, split {args.split}) -> {out}")
    print("\n".join(resumo(eventos)))


if __name__ == "__main__":
    main()
