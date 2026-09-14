"""
Gera o texto do cliente de cada caso sintetico, coerente com o gabarito da spec.

O modelo so' redige. Tudo que precisa ser reproduzivel sai de Python com seed:
tom, contradicao, pedido extra e o ruido de digitacao. Depois de gerado, o texto
passa por checagens deterministicas contra o gabarito; se falhar, o modelo recebe
os problemas e tenta de novo, ate' MAX_TENTATIVAS.

O gerador vem de config/modelos.toml [gerador], que src/config.py impede de ser
da mesma familia dos modelos da extracao.

Entrada: data/case_specs.jsonl       (scripts/gen_specs.py)
Saida:   data/synthetic_cases.jsonl  (uma SolicitacaoBruta por linha)
Uso:     python scripts/gen_texts.py [--specs ...] [--out ...] [--seed 20260913] [--rpm N]
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

from pydantic import Field

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from scripts.build_golden import ID_INEXISTENTE, montar_solicitacao  # noqa: E402
from src.config import modelo_gerador  # noqa: E402
from src.llm import chamar_gerador  # noqa: E402
from src.schemas import (  # noqa: E402
    Base,
    CanalEntrada,
    MotivoContestacao,
    RegraViolacao,
    SolicitacaoBruta,
    Transacao,
)

MAX_TENTATIVAS = 3
MAX_CARACTERES_APP = 400
MIN_MENSAGENS_CHAT = 3
MIN_CARACTERES_OUVIDORIA = 800

# Transcricao de ligacao nao tem erro de digitacao; ouvidoria e' revisada.
TAXA_RUIDO = {
    CanalEntrada.APP: 0.04,
    CanalEntrada.CHAT: 0.06,
    CanalEntrada.TELEFONE: 0.0,
    CanalEntrada.OUVIDORIA: 0.01,
}

REGISTRO_CANAL = {
    CanalEntrada.APP: (
        "Mensagem digitada no app do banco: curta (1 a 3 frases, no máximo "
        f"{MAX_CARACTERES_APP} caracteres), informal e direta. Devolva uma única mensagem."
    ),
    CanalEntrada.CHAT: (
        f"Chat com atendente: de {MIN_MENSAGENS_CHAT} a 6 mensagens curtas enviadas em "
        "sequência, com frases quebradas entre uma mensagem e a seguinte, informal. "
        "Cada mensagem é um item da lista."
    ),
    CanalEntrada.TELEFONE: (
        "Transcrição da fala do cliente numa ligação: repete informações, hesita "
        "(é..., tipo, né, então), se corrige no meio da frase e volta ao assunto. "
        "Devolva uma única mensagem."
    ),
    CanalEntrada.OUVIDORIA: (
        "Reclamação formal registrada na ouvidoria: longa (3 a 5 parágrafos, pelo menos "
        f"{MIN_CARACTERES_OUVIDORIA} caracteres), linguagem formal, fatos em ordem "
        "cronológica, menciona que já tentou resolver com o banco e pede providência. "
        "Devolva uma única mensagem."
    ),
}

DESCRICAO_MOTIVO = {
    MotivoContestacao.NAO_RECONHECIDA: "não reconhece a compra e afirma que não a autorizou",
    MotivoContestacao.DUPLICIDADE: "foi cobrado duas vezes pela mesma compra",
    MotivoContestacao.VALOR_DIVERGENTE: (
        "reconhece a compra, mas foi cobrado acima do valor combinado"
    ),
    MotivoContestacao.SERVICO_NAO_PRESTADO: (
        "pagou, mas o produto ou serviço não foi entregue"
    ),
    MotivoContestacao.CANCELAMENTO_NAO_PROCESSADO: (
        "cancelou a compra ou a assinatura, mas a cobrança veio mesmo assim"
    ),
    MotivoContestacao.SUSPEITA_FRAUDE: (
        "acha que o cartão foi clonado, porque apareceram compras que não fez"
    ),
    MotivoContestacao.INDETERMINADO: (
        "está insatisfeito com a cobrança, mas não deixa claro qual é o problema: não "
        "diz que não autorizou, nem que foi cobrado em dobro, nem que não recebeu"
    ),
}

TONS = ["irritado", "preocupado", "educado", "apressado", "confuso"]

CONTRADICAO_CANAL = {
    "presencial": "diz que essa compra foi feita pela internet",
    "online": "diz que essa compra foi feita na loja física",
    "recorrente": "diz que foi uma compra única, não uma assinatura",
    "saque": "diz que foi uma compra, não um saque",
}

# (pedido, palavra que tem de aparecer no texto)
ASSUNTOS_EXTRA = [
    ("pedir aumento do limite do cartão", "limite"),
    ("pedir a segunda via do boleto da fatura", "boleto"),
    ("mudar a data de vencimento da fatura", "vencimento"),
    ("cancelar o seguro do cartão", "seguro"),
    ("perguntar sobre os pontos do programa de fidelidade", "pontos"),
]

# Palavras de categoria: "uma compra no posto" nao identifica o estabelecimento.
GENERICOS = frozenset(
    {
        "mercado", "posto", "drogaria", "restaurante", "loja", "virtual", "streaming",
        "academia", "passagens", "aereas", "hotel", "marketplace", "saque", "curso",
        "online",
    }
)

SISTEMA = """Você escreve mensagens sintéticas de clientes de um banco brasileiro que \
contestam cobranças no cartão de crédito. Elas servem para testar um sistema de triagem.

Regras:
- Escreva só o que o cliente diz, em português do Brasil, sem narração nem rótulos.
- Nunca inclua dado pessoal: nome, CPF, telefone, e-mail, endereço ou número de cartão.
- Valores sempre em algarismos, no formato R$ 1.234,56.
- Não cite código de transação, a menos que o pedido mande citar um código específico.
- Siga os fatos do pedido; não invente outras cobranças nem outros valores.
- Escreva sem erros de digitação: o ruído é aplicado depois."""


class TextoGerado(Base):
    mensagens: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)


@dataclass(frozen=True)
class Variacao:
    tom: str
    contradicao: str | None = None
    esconder_datas: bool = False
    assunto_extra: tuple[str, str] | None = None


def formatar_valor(valor: Decimal) -> str:
    return "R$ " + f"{valor:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def normalizar(texto: str) -> str:
    decomposto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in decomposto if not unicodedata.combining(c)).lower()


def contestadas(spec: dict) -> list[Transacao]:
    por_id = {t.id_transacao: t for t in montar_solicitacao(spec).transacoes_periodo}
    return [por_id[i] for i in spec["verdade"]["ids_contestadas"]]


def cita_id_inexistente(spec: dict) -> bool:
    return RegraViolacao.TRANSACAO_NAO_ENCONTRADA.value in spec["verdade"]["violacoes_esperadas"]


def sortear_variacao(spec: dict, rng: random.Random) -> Variacao:
    tom = rng.choice(TONS)

    if spec["dificuldade"] == "relato_contraditorio":
        alvo = contestadas(spec)[0]
        if rng.random() < 0.5:
            desvio = timedelta(days=rng.randint(8, 25))
            data_falsa = alvo.data_hora.date() - desvio
            return Variacao(
                tom=tom,
                contradicao=(
                    f"afirma que a compra em {alvo.estabelecimento} foi no dia "
                    f"{data_falsa:%d/%m/%Y}, data que não é a da fatura"
                ),
                esconder_datas=True,
            )
        return Variacao(tom=tom, contradicao=CONTRADICAO_CANAL[alvo.canal_transacao])

    if spec["dificuldade"] == "mistura_dois_assuntos":
        return Variacao(tom=tom, assunto_extra=rng.choice(ASSUNTOS_EXTRA))

    return Variacao(tom=tom)


def montar_pedido(spec: dict, variacao: Variacao) -> str:
    verdade = spec["verdade"]
    canal = CanalEntrada(spec["canal"])
    motivo = MotivoContestacao(verdade["motivo"])
    dificuldade = spec["dificuldade"]
    transacoes = contestadas(spec)

    linhas = [
        f"Canal: {REGISTRO_CANAL[canal]}",
        f"Tom do cliente: {variacao.tom}.",
        f"Situação: o cliente {DESCRICAO_MOTIVO[motivo]}.",
    ]

    if dificuldade == "texto_vago":
        tipos = ", ".join(sorted({t.canal_transacao for t in transacoes}))
        linhas.append(
            f"Texto vago: o cliente fala de {len(transacoes)} cobrança(s) na fatura "
            f"(tipo: {tipos}) sem citar valor, nome do estabelecimento nem data exata."
        )
    else:
        linhas.append("Cobranças contestadas, como aparecem na fatura:")
        for t in transacoes:
            partes = [t.estabelecimento]
            if not variacao.esconder_datas:
                partes.append(f"{t.data_hora:%d/%m/%Y}")
            # Em valor_nao_bate o modelo nem ve' o valor real, para nao cita-lo.
            if dificuldade != "valor_nao_bate":
                partes.append(formatar_valor(t.valor))
            partes.append(f"compra {t.canal_transacao}")
            if t.pais != "BR":
                partes.append(f"no exterior ({t.pais})")
            linhas.append("- " + ", ".join(partes))

        alegado = formatar_valor(Decimal(verdade["valor_alegado"]))
        if motivo is MotivoContestacao.VALOR_DIVERGENTE:
            linhas.append(f"O cliente diz que o valor combinado era {alegado} e cita esse valor.")
        elif dificuldade == "valor_nao_bate":
            linhas.append(
                f"O cliente diz que o total contestado é {alegado}, valor que não bate "
                "com a fatura."
            )
        else:
            linhas.append(f"O cliente cita o total contestado: {alegado}.")

    if variacao.contradicao:
        linhas.append(
            f"Relato contraditório: o cliente {variacao.contradicao}. Não mencione o dado "
            "correto."
        )
    if cita_id_inexistente(spec):
        linhas.append(
            f"O cliente também cita o código {ID_INEXISTENTE}, dizendo que é de outra "
            "cobrança que quer contestar, sem dizer o valor dela."
        )
    if variacao.assunto_extra:
        linhas.append(
            f"No meio do relato, o cliente também pede para {variacao.assunto_extra[0]}, "
            "pedido sem relação com a contestação."
        )

    if not verdade["cliente_afirma_nao_autorizou"]:
        linhas.append("O cliente não diz que deixou de autorizar a compra.")
    contato = verdade["cliente_tentou_contato_estabelecimento"]
    if contato is True:
        linhas.append("O cliente conta que já tentou resolver com o estabelecimento, sem sucesso.")
    elif contato is False:
        linhas.append("O cliente diz que não procurou o estabelecimento.")
    else:
        linhas.append("Não fale se o cliente procurou ou não o estabelecimento.")

    return "\n".join(linhas)


def _numeros(texto: str) -> set[Decimal]:
    """Valores citados, em formato brasileiro ou americano. Partes de data ficam de fora."""
    numeros = set()
    for token in re.findall(r"(?<![\d/.,])\d(?:[\d.,]*\d)?(?![\d/])", texto):
        decimal = None
        if "," in token and "." in token:
            decimal = "," if token.rfind(",") > token.rfind(".") else "."
        elif "," in token or "." in token:
            separador = "," if "," in token else "."
            if len(token.rsplit(separador, 1)[1]) == 2:
                decimal = separador
        for separador in {",", "."} - {decimal}:
            token = token.replace(separador, "")
        if decimal:
            token = token.replace(decimal, ".")
        try:
            numeros.add(Decimal(token))
        except InvalidOperation:
            continue
    return numeros


def _marcas(estabelecimento: str) -> set[str]:
    palavras = re.findall(r"[a-z0-9]+", normalizar(estabelecimento))
    return {p for p in palavras if len(p) >= 4 and p not in GENERICOS}


def problemas_do_texto(spec: dict, variacao: Variacao, mensagens: list[str]) -> list[str]:
    """Checagens deterministicas do texto contra o gabarito. Lista vazia = coerente."""
    verdade = spec["verdade"]
    canal = CanalEntrada(spec["canal"])
    dificuldade = spec["dificuldade"]
    transacoes = contestadas(spec)

    texto = "\n".join(mensagens)
    normal = normalizar(texto)
    palavras = set(re.findall(r"[a-z0-9]+", normal))
    numeros = _numeros(texto)
    problemas: list[str] = []

    if canal is CanalEntrada.APP and (len(mensagens) != 1 or len(texto) > MAX_CARACTERES_APP):
        problemas.append(f"app pede uma unica mensagem de ate {MAX_CARACTERES_APP} caracteres")
    if canal is CanalEntrada.CHAT and len(mensagens) < MIN_MENSAGENS_CHAT:
        problemas.append(f"chat pede pelo menos {MIN_MENSAGENS_CHAT} mensagens")
    if canal is CanalEntrada.OUVIDORIA and len(texto) < MIN_CARACTERES_OUVIDORIA:
        problemas.append(f"ouvidoria pede pelo menos {MIN_CARACTERES_OUVIDORIA} caracteres")

    # Id inventado viraria transacao_nao_encontrada fora do gabarito.
    citados = {i.upper() for i in re.findall(r"tx\d+", normal)}
    esperados = {ID_INEXISTENTE} if cita_id_inexistente(spec) else set()
    if citados != esperados:
        problemas.append(
            f"codigos de transacao citados {sorted(citados)}, esperados {sorted(esperados)}"
        )

    if dificuldade == "texto_vago":
        perto_de_um_valor = any(
            abs(n - t.valor) <= max(t.valor * Decimal("0.1"), Decimal("1"))
            for n in numeros
            for t in transacoes
        )
        if "r$" in normal or "reais" in palavras or perto_de_um_valor:
            problemas.append("texto vago nao pode citar valor")
        if any(_marcas(t.estabelecimento) & palavras for t in transacoes):
            problemas.append("texto vago nao pode citar o estabelecimento")
    else:
        for estabelecimento in dict.fromkeys(t.estabelecimento for t in transacoes):
            marcas = _marcas(estabelecimento)
            if marcas and not marcas & palavras:
                problemas.append(f"nao cita o estabelecimento {estabelecimento}")
        alegado = Decimal(verdade["valor_alegado"])
        if alegado not in numeros:
            problemas.append(f"nao cita o valor {formatar_valor(alegado)}")
        if dificuldade == "valor_nao_bate":
            reais = {t.valor for t in transacoes} | {Decimal(verdade["valor_total_contestado"])}
            if reais & numeros:
                problemas.append("valor_nao_bate nao pode citar o valor real da fatura")

    if variacao.assunto_extra and variacao.assunto_extra[1] not in palavras:
        problemas.append(f"falta o pedido nao relacionado: {variacao.assunto_extra[0]}")

    return problemas


def _troca(palavra: str, rng: random.Random) -> str:
    i = rng.randrange(len(palavra) - 1)
    return palavra[:i] + palavra[i + 1] + palavra[i] + palavra[i + 2 :]


def _omite(palavra: str, rng: random.Random) -> str:
    i = rng.randrange(len(palavra))
    return palavra[:i] + palavra[i + 1 :]


def _duplica(palavra: str, rng: random.Random) -> str:
    i = rng.randrange(len(palavra))
    return palavra[:i] + palavra[i] + palavra[i:]


def _sem_acento(palavra: str, rng: random.Random) -> str:
    decomposto = unicodedata.normalize("NFKD", palavra)
    sem = "".join(c for c in decomposto if not unicodedata.combining(c))
    return sem if sem != palavra else _omite(palavra, rng)


ERROS_DE_DIGITACAO = [_troca, _omite, _duplica, _sem_acento]


def aplicar_ruido(texto: str, taxa: float, rng: random.Random) -> str:
    """Erro de digitacao em palavras so' de letras: valor, data e codigo de
    transacao ficam intactos, porque o gabarito depende deles."""

    def corromper(m: re.Match[str]) -> str:
        if rng.random() >= taxa:
            return m.group()
        return rng.choice(ERROS_DE_DIGITACAO)(m.group(), rng)

    return re.sub(r"\b[^\W\d_]{3,}\b", corromper, texto)


class Ritmo:
    """Espaca o inicio das chamadas para caber no limite de requisicoes por minuto."""

    def __init__(
        self,
        rpm: float | None,
        relogio: Callable[[], float] = time.monotonic,
        dormir: Callable[[float], None] = time.sleep,
    ) -> None:
        if rpm is not None and rpm <= 0:
            raise ValueError("rpm precisa ser positivo")
        self.intervalo_s = 60 / rpm if rpm else 0.0
        self._relogio = relogio
        self._dormir = dormir
        self._ultima: float | None = None

    def aguardar(self) -> None:
        if self._ultima is not None:
            # Desconta o tempo da propria resposta: so' dorme o que falta.
            falta = self.intervalo_s - (self._relogio() - self._ultima)
            if falta > 0:
                self._dormir(falta)
        self._ultima = self._relogio()


def gerar_caso(
    spec: dict, seed: int, ritmo: Ritmo | None = None
) -> tuple[SolicitacaoBruta, int]:
    """Levanta ValueError se o texto nao fica coerente em MAX_TENTATIVAS."""
    # Seed por caso: reordenar ou filtrar specs nao muda o texto dos outros.
    rng = random.Random(f"{seed}:{spec['id_caso']}")
    variacao = sortear_variacao(spec, rng)
    pedido = montar_pedido(spec, variacao)

    problemas: list[str] = []
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        prompt = pedido
        if problemas:
            prompt += "\n\nA versão anterior foi rejeitada por: " + "; ".join(problemas) + "."
        if ritmo is not None:
            ritmo.aguardar()
        gerado = chamar_gerador(SISTEMA, prompt, TextoGerado)
        problemas = problemas_do_texto(spec, variacao, gerado.mensagens)
        if not problemas:
            break
    else:
        raise ValueError(
            f"texto incoerente com o gabarito apos {MAX_TENTATIVAS} tentativas: {problemas}"
        )

    canal = CanalEntrada(spec["canal"])
    texto = aplicar_ruido("\n".join(gerado.mensagens), TAXA_RUIDO[canal], rng)
    # model_validate, nao model_copy: o validador de CPF tem de rodar no texto final.
    dados = montar_solicitacao(spec).model_dump() | {"texto_cliente": texto}
    return SolicitacaoBruta.model_validate(dados), tentativa


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--specs", type=Path, default=RAIZ / "data" / "case_specs.jsonl")
    ap.add_argument("--out", type=Path, default=RAIZ / "data" / "synthetic_cases.jsonl")
    ap.add_argument("--seed", type=int, default=20260913)
    ap.add_argument(
        "--rpm", type=float, default=None,
        help="limite de requisicoes por minuto do provedor (conta cada tentativa)",
    )
    args = ap.parse_args()
    if args.rpm is not None and args.rpm <= 0:
        ap.error("--rpm precisa ser positivo")
    ritmo = Ritmo(args.rpm)

    gerador = modelo_gerador()
    print(f"gerador: {gerador.provedor}/{gerador.modelo}")
    if args.rpm:
        print(f"ritmo: no maximo {args.rpm:g} chamadas/min ({ritmo.intervalo_s:.1f}s entre inicios)")

    casos: list[SolicitacaoBruta] = []
    tentativas_extras = 0
    with args.specs.open(encoding="utf-8") as fh:
        for numero, linha in enumerate(fh, start=1):
            id_caso = "?"
            try:
                spec = json.loads(linha)
                id_caso = spec["id_caso"]
                caso, tentativas = gerar_caso(spec, args.seed, ritmo)
            except Exception as exc:
                raise RuntimeError(f"{args.specs}:{numero} ({id_caso}): {exc}") from exc
            casos.append(caso)
            tentativas_extras += tentativas - 1
            print(f"  {id_caso} {caso.canal.value} ({tentativas} tentativa(s))", flush=True)

    if not casos:
        raise RuntimeError(f"{args.specs} nao tem nenhuma spec.")

    # So' grava depois de tudo processar: nunca fica arquivo parcial em disco.
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for caso in casos:
            fh.write(caso.model_dump_json() + "\n")

    canais = Counter(c.canal.value for c in casos)
    print(f"{len(casos)} casos -> {args.out}  (seed={args.seed})")
    print(f"  canal: {dict(canais)}")
    print(f"  tentativas extras: {tentativas_extras}")


if __name__ == "__main__":
    main()
