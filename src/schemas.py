"""
Contratos de dados do pipeline de contestacao.

Decisao de projeto: toda fronteira entre etapas e' um modelo Pydantic validado.
A saida do LLM nunca circula como texto livre ou dict solto -- ou ela satisfaz
o schema, ou a etapa falha e o caso e' roteado para cima. Isso e' o que torna
o pipeline auditavel: cada objeto abaixo vira uma linha no log de execucao.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Confianca sempre no intervalo [0, 1]. Usada como chave de roteamento, entao
# precisa ser calibrada (ver notebooks/analise.ipynb) antes de virar limiar.
Confianca = Annotated[float, Field(ge=0.0, le=1.0)]

# Valores monetarios em Decimal, nunca float. Erro de centavo em estorno vira
# divergencia contabil.
Valor = Annotated[Decimal, Field(ge=0, decimal_places=2)]


class Base(BaseModel):
    """Config comum: imutavel, sem campo extra silencioso."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- #
# Enums de dominio
# --------------------------------------------------------------------------- #


class MotivoContestacao(str, Enum):
    NAO_RECONHECIDA = "nao_reconhecida"
    DUPLICIDADE = "duplicidade"
    VALOR_DIVERGENTE = "valor_divergente"
    SERVICO_NAO_PRESTADO = "servico_nao_prestado"
    CANCELAMENTO_NAO_PROCESSADO = "cancelamento_nao_processado"
    SUSPEITA_FRAUDE = "suspeita_fraude"
    INDETERMINADO = "indeterminado"  # forca escalada, nunca decide sozinho


class CanalEntrada(str, Enum):
    APP = "app"
    CHAT = "chat"
    TELEFONE = "telefone"
    OUVIDORIA = "ouvidoria"


class TierModelo(str, Enum):
    """Faixas de custo/capacidade. O nome concreto do modelo fica em config,
    nao no codigo, para trocar sem tocar na logica."""

    PEQUENO = "pequeno"      # aberto, hospedado internamente, alto volume
    MEDIO = "medio"          # API de producao, casos ambiguos
    GRANDE = "grande"        # fronteira, so excecao
    HUMANO = "humano"        # fila de analise


class StatusExecucao(str, Enum):
    AUTOMATICO = "automatico"
    ESCALADO_MODELO = "escalado_modelo"
    ESCALADO_HUMANO = "escalado_humano"
    REJEITADO = "rejeitado"
    ERRO = "erro"


class Severidade(str, Enum):
    BLOQUEANTE = "bloqueante"  # impede automacao
    ALERTA = "alerta"          # registra, nao impede
    INFO = "info"


# --------------------------------------------------------------------------- #
# Etapa 1 -- Coleta
# --------------------------------------------------------------------------- #

CanalTransacao = Literal["presencial", "online", "recorrente", "saque"]

class Transacao(Base):
    """Dado estruturado vindo da API interna de cartoes. Nunca passa pelo LLM
    sem mascaramento (ver src/pii.py)."""

    id_transacao: str
    data_hora: datetime
    valor: Valor
    estabelecimento: str
    mcc: str = Field(pattern=r"^\d{4}$", description="Merchant Category Code")
    canal_transacao: CanalTransacao
    pais: str = Field(min_length=2, max_length=2)
    ja_estornada: bool = False


class SolicitacaoBruta(Base):
    """Entrada do pipeline: o que o cliente disse + o contexto da conta."""

    id_caso: str
    id_cliente_mascarado: str = Field(
        description="Pseudonimo estavel. O mapa de reversao vive fora do pipeline."
    )
    canal: CanalEntrada
    recebida_em: datetime
    texto_cliente: str = Field(min_length=1, max_length=8000)
    transacoes_periodo: list[Transacao] = Field(default_factory=list)
    contestacoes_ultimos_12m: int = Field(ge=0, default=0)

    @field_validator("texto_cliente")
    @classmethod
    def sem_pii_obvia(cls, v: str) -> str:
        # Guarda-corpo barato: CPF cru nunca deveria chegar ate aqui.
        import re

        if re.search(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b", v):
            raise ValueError("texto_cliente contem CPF nao mascarado")
        return v


# --------------------------------------------------------------------------- #
# Etapa 2 -- Extracao (unica etapa LLM-first)
# --------------------------------------------------------------------------- #


class ExtracaoContestacao(Base):
    """Saida estruturada do LLM. Tudo que o modelo produz esta' aqui dentro,
    e nada alem disso influencia as etapas seguintes."""

    motivo: MotivoContestacao
    ids_transacoes_citadas: list[str] = Field(default_factory=list)
    valor_alegado: Valor | None = None
    cliente_afirma_nao_autorizou: bool
    cliente_tentou_contato_estabelecimento: bool | None = None
    resumo_alegacao: str = Field(max_length=400)
    confianca: Confianca
    justificativa: str = Field(
        max_length=600,
        description="Por que o modelo classificou assim. Vai para a trilha de auditoria.",
    )

    @model_validator(mode="after")
    def indeterminado_nao_tem_confianca_alta(self) -> "ExtracaoContestacao":
        if self.motivo is MotivoContestacao.INDETERMINADO and self.confianca > 0.5:
            raise ValueError("motivo indeterminado nao pode reportar confianca alta")
        return self


# --------------------------------------------------------------------------- #
# Etapa 3 -- Validacao (deterministica)
# --------------------------------------------------------------------------- #


class Violacao(Base):
    regra: str
    severidade: Severidade
    detalhe: str


class ResultadoValidacao(Base):
    elegivel: bool
    violacoes: list[Violacao] = Field(default_factory=list)
    transacoes_confirmadas: list[str] = Field(default_factory=list)

    @property
    def tem_bloqueante(self) -> bool:
        return any(v.severidade is Severidade.BLOQUEANTE for v in self.violacoes)


# --------------------------------------------------------------------------- #
# Etapa 4 -- Calculo (aritmetica pura, 100% testavel)
# --------------------------------------------------------------------------- #


class ResultadoCalculo(Base):
    valor_estorno: Valor
    provisao: Valor
    prazo_resposta_dias: int = Field(ge=1, le=90)
    faixa_risco: Literal["baixa", "media", "alta"]
    memoria_calculo: list[str] = Field(
        default_factory=list,
        description="Passo a passo legivel. Exigencia de auditoria, nao enfeite.",
    )


# --------------------------------------------------------------------------- #
# Etapa 5 -- Registro
# --------------------------------------------------------------------------- #


class RegistroCaso(Base):
    id_caso: str
    protocolo: str
    registrado_em: datetime
    status: StatusExecucao
    resposta_cliente: str
    aprovado_por: str | None = Field(
        default=None, description="None quando automatico; matricula quando humano."
    )


# --------------------------------------------------------------------------- #
# Roteamento
# --------------------------------------------------------------------------- #


class DecisaoRoteamento(Base):
    tier: TierModelo
    motivo_decisao: str
    tentativa: int = Field(ge=1, le=3)


class CustoChamada(Base):
    modelo: str
    tokens_entrada: int = Field(ge=0)
    tokens_saida: int = Field(ge=0)
    tokens_cache: int = Field(ge=0, default=0)
    custo_usd: float = Field(ge=0)
    latencia_ms: int = Field(ge=0)


# --------------------------------------------------------------------------- #
# Telemetria -- a linha do JSONL
# --------------------------------------------------------------------------- #


class EventoExecucao(Base):
    """Uma linha por caso processado. Este objeto e' simultaneamente:
      (a) a fonte das metricas,
      (b) o dataset bruto de fine-tuning/destilacao,
      (c) a evidencia de auditoria.
    Qualquer campo que voce nao gravar aqui, voce nao tera' depois."""

    id_caso: str
    versao_pipeline: str
    versao_prompt: str
    iniciado_em: datetime
    finalizado_em: datetime
    status: StatusExecucao

    extracao: ExtracaoContestacao | None = None
    validacao: ResultadoValidacao | None = None
    calculo: ResultadoCalculo | None = None
    registro: RegistroCaso | None = None

    rotas: list[DecisaoRoteamento] = Field(default_factory=list)
    custos: list[CustoChamada] = Field(default_factory=list)
    erro: str | None = None

    @property
    def custo_total_usd(self) -> float:
        return sum(c.custo_usd for c in self.custos)

    @property
    def latencia_total_ms(self) -> int:
        return int((self.finalizado_em - self.iniciado_em).total_seconds() * 1000)


# --------------------------------------------------------------------------- #
# Conjunto dourado -- o rotulo humano contra o qual tudo e' medido
# --------------------------------------------------------------------------- #


class CasoDourado(Base):
    """Caso historico ja' decidido por analista. Split temporal, nao aleatorio."""

    id_caso: str
    decidido_em: date
    motivo_humano: MotivoContestacao
    elegivel_humano: bool
    valor_estorno_humano: Valor
    observacao_analista: str | None = None
