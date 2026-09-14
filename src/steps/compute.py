"""
Calculo do estorno, da provisao, do prazo de resposta e da faixa de risco.

Aritmetica pura sobre um caso que a validacao ja aprovou: sem LLM, sem I/O, sem
relogio. Cada operacao deixa uma linha em `memoria_calculo`, suficiente para
refazer a conta a mao.

Politica de arredondamento
--------------------------
Todo valor monetario que sai deste modulo passa por `quantizar_centavos`:
quantize em 0.01 com ROUND_HALF_EVEN (arredondamento bancario, ABNT NBR 5891).
Meio centavo exato vai para o par -- 0.125 vira 0.12, 0.135 vira 0.14 --, entao
arredondar em volume nao acumula vies para nenhum lado.

Hoje o calculo so soma e subtrai valores que o schema `Valor` ja limita a duas
casas. Nessas operacoes o quantize nao arredonda nada: normaliza a escala
(Decimal("100") vira 100.00). Ele fica no caminho de todo valor para que uma
operacao futura que gere casas extras (juros, cambio, rateio) ja nasca sob a
mesma politica.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal

from src.duplicidade import grupos_repetidos
from src.schemas import (
    ExtracaoContestacao,
    MotivoContestacao,
    RegraViolacao,
    ResultadoCalculo,
    ResultadoValidacao,
    Severidade,
    SolicitacaoBruta,
    Transacao,
)

CENTAVO = Decimal("0.01")
ARREDONDAMENTO = ROUND_HALF_EVEN

# Acima disso o estorno nao automatiza: o caso vai para analise humana, que
# define a provisao final.
LIMITE_ESTORNO_AUTOMATICO = Decimal("1000.00")

ALERTAS_DE_RISCO_ALTO = frozenset(
    {
        RegraViolacao.CARTAO_PRESENTE_VS_NAO_RECONHECIMENTO,
        RegraViolacao.REINCIDENCIA_CONTESTACOES,
    }
)

PRAZO_RESPOSTA_DIAS_UTEIS = 5

FaixaRisco = Literal["baixa", "media", "alta"]


def quantizar_centavos(valor: Decimal) -> Decimal:
    """Unico ponto de arredondamento monetario do modulo."""
    return valor.quantize(CENTAVO, rounding=ARREDONDAMENTO)


def calcular(
    solicitacao: SolicitacaoBruta,
    extracao: ExtracaoContestacao,
    validacao: ResultadoValidacao,
) -> ResultadoCalculo:
    """Calcula o estorno de um caso elegivel e registra a memoria de calculo.

    Levanta ValueError quando a entrada nao sustenta um calculo: caso nao
    elegivel, transacao confirmada ausente do periodo, estorno parcial sem
    cobranca a maior, ou duplicidade sem cobranca repetida citada. Nesses casos
    nao ha numero defensavel para devolver.
    """
    if not validacao.elegivel:
        raise ValueError(
            f"Caso {solicitacao.id_caso} nao e' elegivel; calculo nao se aplica."
        )

    confirmadas = _transacoes_confirmadas(solicitacao, validacao)
    memoria = [f"Transacao {t.id_transacao}: {_brl(t.valor)}" for t in confirmadas]

    soma = quantizar_centavos(sum((t.valor for t in confirmadas), Decimal("0")))
    parcelas = " + ".join(_brl(t.valor) for t in confirmadas)
    memoria.append(f"Soma das confirmadas: {parcelas} = {_brl(soma)}")

    estorno, linha_estorno = _valor_estorno(solicitacao, extracao, confirmadas, soma)
    memoria.append(linha_estorno)

    # if, nao assert: python -O removeria a checagem, e esta e' a ultima barreira
    # antes de um efeito financeiro.
    if estorno > soma:
        raise ValueError(
            f"Estorno {_brl(estorno)} excede a soma das confirmadas {_brl(soma)}."
        )
    memoria.append(f"Verificacao: estorno {_brl(estorno)} <= soma {_brl(soma)}")

    acima_do_limite = estorno > LIMITE_ESTORNO_AUTOMATICO
    faixa, linha_faixa = _faixa_risco(estorno, acima_do_limite, validacao)
    memoria.append(linha_faixa)

    provisao = estorno
    linha_provisao = f"Provisao: 100% do estorno = {_brl(provisao)}"
    if acima_do_limite:
        linha_provisao += "; provisao final definida pelo analista"
    memoria.append(linha_provisao)

    memoria.append(f"Prazo de resposta: {PRAZO_RESPOSTA_DIAS_UTEIS} dias uteis")

    return ResultadoCalculo(
        valor_estorno=estorno,
        provisao=provisao,
        prazo_resposta_dias=PRAZO_RESPOSTA_DIAS_UTEIS,
        faixa_risco=faixa,
        memoria_calculo=memoria,
    )


def _transacoes_confirmadas(
    solicitacao: SolicitacaoBruta, validacao: ResultadoValidacao
) -> list[Transacao]:
    if not validacao.transacoes_confirmadas:
        raise ValueError("Validacao elegivel sem transacao confirmada.")

    por_id = {t.id_transacao: t for t in solicitacao.transacoes_periodo}
    ausentes = [i for i in validacao.transacoes_confirmadas if i not in por_id]
    if ausentes:
        raise ValueError(
            f"Transacoes confirmadas ausentes do periodo: {', '.join(ausentes)}."
        )

    return [por_id[i] for i in validacao.transacoes_confirmadas]


def _valor_estorno(
    solicitacao: SolicitacaoBruta,
    extracao: ExtracaoContestacao,
    confirmadas: list[Transacao],
    soma: Decimal,
) -> tuple[Decimal, str]:
    if extracao.motivo is MotivoContestacao.DUPLICIDADE:
        return _estorno_duplicidade(solicitacao, confirmadas)

    if extracao.motivo is not MotivoContestacao.VALOR_DIVERGENTE:
        return soma, f"Estorno integral: {_brl(soma)}"

    if extracao.valor_alegado is None:
        raise ValueError(
            "Motivo valor_divergente sem valor_alegado: nao ha base para estorno parcial."
        )

    devido = quantizar_centavos(extracao.valor_alegado)
    if devido >= soma:
        raise ValueError(
            f"Valor devido {_brl(devido)} nao e' menor que o cobrado {_brl(soma)}: "
            "nao ha cobranca a maior para estornar."
        )

    estorno = quantizar_centavos(soma - devido)
    return estorno, (
        f"Estorno parcial: cobrado {_brl(soma)} - devido {_brl(devido)} = {_brl(estorno)}"
    )


def _estorno_duplicidade(
    solicitacao: SolicitacaoBruta, confirmadas: list[Transacao]
) -> tuple[Decimal, str]:
    """Devolve so' o excedente: de cada grupo de cobrancas iguais, uma fica.

    O grupo vem do periodo inteiro, nao so' das citadas: cliente que cita apenas a
    segunda cobranca de um par recebe essa de volta. Citada sem cobranca igual no
    periodo nao e' repeticao e nao entra.
    """
    citadas = {t.id_transacao for t in confirmadas}
    estorno = Decimal("0")
    partes: list[str] = []

    for grupo in grupos_repetidos(solicitacao.transacoes_periodo):
        n_citadas = sum(t.id_transacao in citadas for t in grupo)
        devolvidas = min(n_citadas, len(grupo) - 1)
        if devolvidas == 0:
            continue
        valor = quantizar_centavos(grupo[0].valor * devolvidas)
        estorno += valor
        ids = ", ".join(t.id_transacao for t in grupo)
        partes.append(
            f"{ids}: {len(grupo)} cobrancas de {_brl(grupo[0].valor)}, "
            f"{n_citadas} citada(s), {devolvidas} devolvida(s) = {_brl(valor)}"
        )

    if estorno == 0:
        raise ValueError(
            "Duplicidade sem cobranca repetida entre as confirmadas: nao ha excedente para estornar."
        )

    estorno = quantizar_centavos(estorno)
    return estorno, (
        f"Estorno de duplicidade (uma cobranca de cada grupo fica): {'; '.join(partes)}; "
        f"total {_brl(estorno)}"
    )


def _faixa_risco(
    estorno: Decimal, acima_do_limite: bool, validacao: ResultadoValidacao
) -> tuple[FaixaRisco, str]:
    alertas = [
        v.regra for v in validacao.violacoes if v.severidade is Severidade.ALERTA
    ]

    motivos_alta = [
        f"alerta {r.value}" for r in alertas if r in ALERTAS_DE_RISCO_ALTO
    ]
    if acima_do_limite:
        motivos_alta.insert(
            0,
            f"estorno {_brl(estorno)} > limite automatico "
            f"{_brl(LIMITE_ESTORNO_AUTOMATICO)}",
        )

    if motivos_alta:
        return "alta", (
            f"Faixa alta: {'; '.join(motivos_alta)}; segue para analise humana"
        )

    if alertas:
        return "media", f"Faixa media: alerta {', '.join(r.value for r in alertas)}"

    return "baixa", (
        "Faixa baixa: sem alerta e estorno dentro do limite de "
        f"{_brl(LIMITE_ESTORNO_AUTOMATICO)}"
    )


def _brl(valor: Decimal) -> str:
    return f"R$ {quantizar_centavos(valor)}"
