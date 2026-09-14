"""Registro: protocolo deterministico por id_caso e resposta de template."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.schemas import CanalEntrada, ResultadoCalculo, SolicitacaoBruta, StatusExecucao
from src.steps.register import gerar_protocolo, registrar

AGORA = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
SOLICITACAO = SolicitacaoBruta(
    id_caso="CASO-0001",
    id_cliente_mascarado="cli_teste",
    canal=CanalEntrada.APP,
    recebida_em=AGORA,
    texto_cliente="nao fiz",
)
CALCULO = ResultadoCalculo(
    valor_estorno=Decimal("1079.70"),
    provisao=Decimal("1079.70"),
    prazo_resposta_dias=5,
    faixa_risco="baixa",
)


def test_reprocessar_o_mesmo_caso_gera_o_mesmo_protocolo():
    assert gerar_protocolo("CASO-0001") == gerar_protocolo("CASO-0001")
    assert gerar_protocolo("CASO-0001") != gerar_protocolo("CASO-0002")


def test_resposta_traz_valor_em_reais_prazo_e_protocolo():
    registro = registrar(SOLICITACAO, CALCULO, StatusExecucao.AUTOMATICO, AGORA)

    assert "R$ 1.079,70" in registro.resposta_cliente
    assert "5 dias úteis" in registro.resposta_cliente
    assert registro.protocolo in registro.resposta_cliente
    assert registro.aprovado_por is None


@pytest.mark.parametrize(
    "status", [StatusExecucao.ESCALADO_HUMANO, StatusExecucao.ERRO, StatusExecucao.REJEITADO]
)
def test_registro_automatico_recusa_status_que_nao_e_automatico(status):
    with pytest.raises(ValueError, match="nao aceita status"):
        registrar(SOLICITACAO, CALCULO, status, AGORA)
