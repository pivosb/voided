"""Log de execucao: append de uma linha por evento, criando o diretorio."""

from datetime import datetime, timezone

from src.schemas import EventoExecucao, StatusExecucao
from src.telemetry import gravar


def _evento(id_caso: str) -> EventoExecucao:
    agora = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
    return EventoExecucao(
        id_caso=id_caso,
        versao_pipeline="0.1.0",
        versao_prompt="extract_v1",
        iniciado_em=agora,
        finalizado_em=agora,
        status=StatusExecucao.ERRO,
        erro="extracao: RuntimeError: teste",
    )


def test_cria_diretorio_e_acrescenta_uma_linha_por_evento(tmp_path):
    caminho = tmp_path / "saida" / "aninhada" / "eventos.jsonl"
    eventos = [_evento("CASO-0001"), _evento("CASO-0002")]

    for evento in eventos:
        gravar(evento, caminho)

    linhas = caminho.read_text(encoding="utf-8").splitlines()
    assert [EventoExecucao.model_validate_json(l) for l in linhas] == eventos


def test_nao_sobrescreve_execucao_anterior(tmp_path):
    caminho = tmp_path / "eventos.jsonl"

    gravar(_evento("CASO-0001"), caminho)
    gravar(_evento("CASO-0001"), caminho)

    assert len(caminho.read_text(encoding="utf-8").splitlines()) == 2
