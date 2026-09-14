"""
Cliente LLM unico do projeto.

O pipeline chama so' `complete_structured`: prompt versionado, payload, schema e
TierModelo. O modelo concreto sai de config/modelos.toml via src/config.py;
nenhuma funcao aceita nome de modelo. Toda resposta passa pelo schema pedido: o
que nao satisfaz levanta FalhaSchema (com o custo da chamada), nunca vira
"quase certo".

Modos (`configurar`):
  real  chama o provedor e grava a resposta bruta em data/llm_cache.jsonl ANTES
        de validar, para o mock reproduzir tambem as falhas de schema.
  mock  devolve resposta e custo gravados para (id_caso, prompt_version, tier).
        Chave ausente e' erro: o mock nao inventa resposta.

Resposta que nao chega a ser JSON (texto solto, cortada em max_tokens, sem a tool)
tambem e' falha de schema: vira um dict que nenhum schema aceita, com o bruto dentro,
e segue o mesmo caminho -- gravada, FalhaSchema, sobe de tier.

Temperatura 0 so' no provedor openai. Claude 4.7+ rejeita temperature; ali a saida
estruturada vem da tool forcada e a reprodutibilidade vem do cache.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, TypeVar

from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.config import SpecGerador, SpecModelo, modelo_do_tier, modelo_gerador
from src.schemas import Base, CustoChamada, TierModelo

load_dotenv()

CACHE_PADRAO = Path(__file__).resolve().parents[1] / "data" / "llm_cache.jsonl"
PASTA_PROMPTS = Path(__file__).resolve().parent / "prompts"
VERSAO_PROMPT = re.compile(r"[a-z]+_v\d+")

MAX_TOKENS = 4000

# Alguns modelos embrulham o JSON num bloco markdown mesmo com response_format.
# So' a cerca nas bordas sai; o conteudo continua passando inteiro pelo schema.
CERCA_ABERTURA = re.compile(r"\A```[A-Za-z]*\s*")
CERCA_FECHAMENTO = re.compile(r"\s*```\Z")
MAX_TENTATIVAS = 2
ESPERA = wait_exponential(multiplier=1, max=8)

T = TypeVar("T", bound=BaseModel)

# (dict da resposta, tokens de entrada, tokens de saida)
Resposta = tuple[dict, int, int]
Chave = tuple[str, str, TierModelo]


class ErroTransitorio(Exception):
    """Rede, limite de taxa ou 5xx do provedor: a unica falha que vale repetir."""


class RespostaNaoGravada(LookupError):
    """Modo mock sem resposta gravada para a chave pedida."""


class FalhaSchema(Exception):
    """Resposta fora do schema. Carrega o custo: a chamada foi paga mesmo sem
    resultado aproveitavel, e custo nao registrado some das metricas."""

    def __init__(self, custo: CustoChamada, erro: ValidationError) -> None:
        super().__init__(f"{custo.modelo} respondeu fora do schema: {erro}")
        self.custo = custo


class EntradaCache(Base):
    id_caso: str
    prompt_version: str
    tier: TierModelo
    resposta: dict[str, Any]  # bruta, antes da validacao pelo schema
    custo: CustoChamada
    gravado_em: datetime


_modo: Literal["real", "mock"] = "real"
_cache: Path = CACHE_PADRAO
_indice: dict[Chave, EntradaCache] | None = None


def configurar(modo: Literal["real", "mock"], cache: Path = CACHE_PADRAO) -> None:
    global _modo, _cache, _indice
    if modo not in ("real", "mock"):
        raise ValueError(f"modo desconhecido: {modo!r}")
    _modo, _cache, _indice = modo, cache, None


def complete_structured(
    prompt_version: str, payload: BaseModel, schema: type[T], tier: TierModelo
) -> tuple[T, CustoChamada]:
    spec = modelo_do_tier(tier)
    sistema = _ler_prompt(prompt_version)
    id_caso = getattr(payload, "id_caso", None)
    if not isinstance(id_caso, str):
        raise ValueError("payload precisa de id_caso: e' parte da chave do cache")
    chave = (id_caso, prompt_version, tier)

    if _modo == "mock":
        entrada = _entrada_gravada(chave, spec)
    else:
        entrada = _chamar_e_gravar(chave, spec, sistema, payload.model_dump_json(), schema)
    try:
        objeto = schema.model_validate(entrada.resposta)
    except ValidationError as exc:
        raise FalhaSchema(entrada.custo, exc) from exc
    return objeto, entrada.custo


def chamar_gerador(sistema: str, usuario: str, schema: type[T]) -> T:
    """So' para scripts de dados sinteticos; o pipeline usa `complete_structured`."""
    bruto, _, _ = _chamar_provedor(modelo_gerador(), sistema, usuario, schema)
    return schema.model_validate(bruto)


def _ler_prompt(prompt_version: str) -> str:
    if not VERSAO_PROMPT.fullmatch(prompt_version):
        raise ValueError(f"prompt_version invalida: {prompt_version!r}")
    return (PASTA_PROMPTS / f"{prompt_version}.md").read_text(encoding="utf-8")


def _chamar_e_gravar(
    chave: Chave, spec: SpecModelo, sistema: str, usuario: str, schema: type[BaseModel]
) -> EntradaCache:
    inicio = time.perf_counter()
    resposta, tokens_entrada, tokens_saida = _chamar_provedor(spec, sistema, usuario, schema)
    latencia_ms = int((time.perf_counter() - inicio) * 1000)

    custo = (
        tokens_entrada * spec.preco_entrada_musd + tokens_saida * spec.preco_saida_musd
    ) / Decimal(1_000_000)
    id_caso, prompt_version, tier = chave
    entrada = EntradaCache(
        id_caso=id_caso,
        prompt_version=prompt_version,
        tier=tier,
        resposta=resposta,
        custo=CustoChamada(
            modelo=spec.modelo,
            tokens_entrada=tokens_entrada,
            tokens_saida=tokens_saida,
            custo_usd=float(custo),
            latencia_ms=latencia_ms,
        ),
        gravado_em=datetime.now(timezone.utc),
    )
    _cache.parent.mkdir(parents=True, exist_ok=True)
    with _cache.open("a", encoding="utf-8") as fh:
        fh.write(entrada.model_dump_json() + "\n")
    return entrada


def _entrada_gravada(chave: Chave, spec: SpecModelo) -> EntradaCache:
    global _indice
    if _indice is None:
        _indice = _carregar_indice(_cache)

    id_caso, prompt_version, tier = chave
    entrada = _indice.get(chave)
    if entrada is None:
        raise RespostaNaoGravada(
            f"{_cache} nao tem resposta para id_caso={id_caso} "
            f"prompt_version={prompt_version} tier={tier.value}"
        )
    # A chave nao inclui o modelo: sem isto, trocar o modelo no TOML atribuiria a ele
    # resposta e custo do modelo antigo.
    if entrada.custo.modelo != spec.modelo:
        raise ValueError(
            f"resposta de {id_caso} no tier {tier.value} foi gravada com "
            f"{entrada.custo.modelo!r}, mas o config usa {spec.modelo!r}"
        )
    return entrada


def _carregar_indice(caminho: Path) -> dict[Chave, EntradaCache]:
    indice: dict[Chave, EntradaCache] = {}
    if not caminho.exists():
        return indice
    with caminho.open(encoding="utf-8") as fh:
        for linha in fh:
            if linha.strip():
                entrada = EntradaCache.model_validate_json(linha)
                # Append-only: a gravacao mais recente da chave e' a vigente.
                indice[(entrada.id_caso, entrada.prompt_version, entrada.tier)] = entrada
    return indice


def _chamar_provedor(
    spec: SpecModelo | SpecGerador, sistema: str, usuario: str, schema: type[BaseModel]
) -> Resposta:
    tentativas = Retrying(
        stop=stop_after_attempt(MAX_TENTATIVAS),
        wait=ESPERA,
        retry=retry_if_exception_type(ErroTransitorio),
        reraise=True,
    )
    return tentativas(_PROVEDORES[spec.provedor], spec, sistema, usuario, schema)


def _anthropic(
    spec: SpecModelo | SpecGerador, sistema: str, usuario: str, schema: type[BaseModel]
) -> Resposta:
    import anthropic

    # max_retries=0: o SDK retentaria por conta propria e furaria MAX_TENTATIVAS.
    cliente = anthropic.Anthropic(base_url=spec.base_url, max_retries=0)
    try:
        # Claude 4.7+ rejeita temperature; a saida estruturada vem da tool forcada,
        # que exige thinking desligado.
        resposta = cliente.messages.create(
            model=spec.modelo,
            max_tokens=MAX_TOKENS,
            system=sistema,
            messages=[{"role": "user", "content": usuario}],
            tools=[
                {
                    "name": schema.__name__,
                    "description": "Registra a resposta no formato exigido.",
                    "input_schema": schema.model_json_schema(),
                }
            ],
            tool_choice={"type": "tool", "name": schema.__name__},
            thinking={"type": "disabled"},
        )
    except (
        anthropic.APIConnectionError,
        anthropic.RateLimitError,
        anthropic.InternalServerError,
    ) as exc:
        raise ErroTransitorio(f"{spec.modelo}: {exc}") from exc

    tokens = (resposta.usage.input_tokens, resposta.usage.output_tokens)
    blocos = [b for b in resposta.content if b.type == "tool_use"]
    if resposta.stop_reason == "max_tokens":
        bruto = json.dumps(blocos[0].input, ensure_ascii=False) if blocos else None
        return _resposta_invalida(_motivo_cortada(), bruto), *tokens
    if not blocos:
        texto = "".join(getattr(b, "text", "") for b in resposta.content)
        return _resposta_invalida(f"nao devolveu a tool {schema.__name__}", texto), *tokens
    return blocos[0].input, *tokens


def _openai(
    spec: SpecModelo | SpecGerador, sistema: str, usuario: str, schema: type[BaseModel]
) -> Resposta:
    import openai

    # Servidor local compativel ignora a chave, mas o SDK exige uma.
    chave = os.environ.get("OPENAI_API_KEY") or ("local" if spec.base_url else None)
    cliente = openai.OpenAI(api_key=chave, base_url=spec.base_url, max_retries=0)
    opcionais = {}
    if spec.reasoning_effort is not None:
        opcionais["reasoning_effort"] = spec.reasoning_effort
    try:
        resposta = cliente.chat.completions.create(
            model=spec.modelo,
            max_tokens=MAX_TOKENS,
            temperature=0,
            messages=[
                {"role": "system", "content": sistema},
                {"role": "user", "content": usuario},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()},
            },
            **opcionais,
        )
    except (openai.APIConnectionError, openai.RateLimitError, openai.InternalServerError) as exc:
        raise ErroTransitorio(f"{spec.modelo}: {exc}") from exc

    uso = resposta.usage
    tokens = (uso.prompt_tokens if uso else 0, uso.completion_tokens if uso else 0)
    escolha = resposta.choices[0]
    if escolha.finish_reason == "length":
        return _resposta_invalida(_motivo_cortada(), escolha.message.content), *tokens
    return _interpretar_json(escolha.message.content), *tokens


def _interpretar_json(conteudo: str | None) -> dict:
    texto = CERCA_FECHAMENTO.sub("", CERCA_ABERTURA.sub("", (conteudo or "").strip()))
    try:
        objeto = json.loads(texto)
    except json.JSONDecodeError as exc:
        return _resposta_invalida(f"nao e' JSON valido: {exc}", conteudo)
    if not isinstance(objeto, dict):
        return _resposta_invalida("JSON nao e' um objeto", conteudo)
    return objeto


def _resposta_invalida(motivo: str, conteudo: str | None) -> dict:
    """Chave que nenhum schema aceita (extra="forbid"): falha na validacao como
    qualquer resposta fora do schema, e o bruto fica no cache para auditoria."""
    return {"_resposta_invalida": motivo, "_conteudo": conteudo}


def _motivo_cortada() -> str:
    return (
        f"cortada em max_tokens={MAX_TOKENS}; se o modelo raciocina por padrao, "
        "reduza reasoning_effort em config/modelos.toml"
    )


_PROVEDORES: dict[str, Callable[..., Resposta]] = {
    "anthropic": _anthropic,
    "openai": _openai,
}
