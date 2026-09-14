"""
Carrega config/modelos.toml: o modelo concreto de cada tier e o do gerador.

O nome do modelo vive so' no TOML; o codigo pede por TierModelo. A validacao roda
no import, entao um gerador que colide com a extracao impede qualquer execucao.
"""

from __future__ import annotations

import re
import tomllib
import warnings
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from src.schemas import Base, TierModelo

CAMINHO_PADRAO = Path(__file__).resolve().parents[1] / "config" / "modelos.toml"

TIERS_COM_MODELO = frozenset({TierModelo.PEQUENO, TierModelo.MEDIO, TierModelo.GRANDE})

# Nomes da OpenAI que nao comecam pela familia.
ALIAS_FAMILIA = {"o": "gpt", "chatgpt": "gpt"}

Provedor = Literal["anthropic", "openai"]
PrecoMilhao = Annotated[Decimal, Field(ge=0)]
EsforcoRaciocinio = Literal["none", "minimal", "low", "medium", "high"]


class SpecGerador(Base):
    provedor: Provedor
    modelo: str = Field(min_length=1)
    base_url: str | None = None
    # Parametro da API compativel com OpenAI. Modelo que pensa por padrao (Gemini 3)
    # gasta o max_tokens raciocinando e corta a resposta.
    reasoning_effort: EsforcoRaciocinio | None = None

    @model_validator(mode="after")
    def esforco_so_no_provedor_openai(self) -> "SpecGerador":
        if self.reasoning_effort is not None and self.provedor != "openai":
            raise ValueError("reasoning_effort so' vale para provedor = \"openai\"")
        return self


class SpecModelo(SpecGerador):
    preco_entrada_musd: PrecoMilhao
    preco_saida_musd: PrecoMilhao
    perimetro: Literal["interno", "externo"]


def _sem_organizacao(modelo: str) -> str:
    return modelo.rsplit("/", 1)[-1].lower()


def familia(modelo: str) -> str:
    """'meta-llama/Llama-3.1-8B' -> 'llama'; 'claude-sonnet-5' -> 'claude'."""
    prefixo = re.match(r"[a-z]+", _sem_organizacao(modelo))
    if prefixo is None:
        raise ValueError(f"nao da para inferir a familia do modelo {modelo!r}")
    return ALIAS_FAMILIA.get(prefixo.group(), prefixo.group())


class ConfigModelos(Base):
    data_consulta: date
    permitir_mesma_familia: bool = False
    gerador: SpecGerador
    tiers: dict[TierModelo, SpecModelo]

    @field_validator("tiers")
    @classmethod
    def todos_os_tiers(cls, tiers: dict[TierModelo, SpecModelo]) -> dict[TierModelo, SpecModelo]:
        if set(tiers) != TIERS_COM_MODELO:
            esperados = sorted(t.value for t in TIERS_COM_MODELO)
            raise ValueError(f"tiers devem ser exatamente {esperados}")
        return tiers

    @model_validator(mode="after")
    def gerador_distinto_da_extracao(self) -> "ConfigModelos":
        gerador = self.gerador.modelo
        # Colisao exata primeiro: o escape nunca a cobre, entao nao cabe warning antes.
        for tier, spec in self.tiers.items():
            if _sem_organizacao(gerador) == _sem_organizacao(spec.modelo):
                raise ValueError(
                    f"gerador e tier {tier.value} usam o mesmo modelo {spec.modelo!r}"
                )
        for tier, spec in self.tiers.items():
            if familia(gerador) != familia(spec.modelo):
                continue
            mensagem = (
                f"gerador {gerador!r} e tier {tier.value} ({spec.modelo!r}) sao da "
                f"familia {familia(gerador)!r}"
            )
            if not self.permitir_mesma_familia:
                raise ValueError(f"{mensagem}; permitir_mesma_familia = true aceita")
            warnings.warn(mensagem, stacklevel=2)
        return self


def carregar(caminho: Path = CAMINHO_PADRAO) -> ConfigModelos:
    with caminho.open("rb") as fh:
        # parse_float=Decimal: preco lido como float ja' chegaria arredondado.
        return ConfigModelos.model_validate(tomllib.load(fh, parse_float=Decimal))


CONFIG = carregar()


def modelo_do_tier(tier: TierModelo) -> SpecModelo:
    if tier not in CONFIG.tiers:
        raise ValueError(f"tier {tier.value} nao tem modelo")
    return CONFIG.tiers[tier]


def modelo_gerador() -> SpecGerador:
    return CONFIG.gerador
