"""O gerador de texto sintetico nao pode colidir com os modelos da extracao."""

from decimal import Decimal

import pytest

from src.config import CONFIG, carregar, familia, modelo_do_tier
from src.schemas import TierModelo


def _toml(tmp_path, gerador: str, permitir: bool = False, pequeno: str = "qwen3:8b"):
    caminho = tmp_path / "modelos.toml"
    caminho.write_text(
        f"""
data_consulta = "2026-09-13"
permitir_mesma_familia = {str(permitir).lower()}

[gerador]
provedor = "openai"
modelo = "{gerador}"

[tiers.pequeno]
provedor = "openai"
modelo = "{pequeno}"
base_url = "http://localhost:11434/v1"
preco_entrada_musd = 0.0
preco_saida_musd = 0.0
perimetro = "interno"

[tiers.medio]
provedor = "anthropic"
modelo = "claude-sonnet-5"
preco_entrada_musd = 2.0
preco_saida_musd = 10.0
perimetro = "externo"

[tiers.grande]
provedor = "anthropic"
modelo = "claude-opus-5"
preco_entrada_musd = 5.0
preco_saida_musd = 25.0
perimetro = "externo"
""",
        encoding="utf-8",
    )
    return caminho


def test_config_do_repo_carrega():
    assert CONFIG.gerador.modelo
    assert modelo_do_tier(TierModelo.GRANDE).preco_saida_musd == Decimal("25.0")


def test_tier_humano_nao_tem_modelo():
    with pytest.raises(ValueError, match="nao tem modelo"):
        modelo_do_tier(TierModelo.HUMANO)


@pytest.mark.parametrize(
    "modelo, esperada",
    [
        ("claude-sonnet-5", "claude"),
        ("gpt-4.1-mini", "gpt"),
        ("o4-mini", "gpt"),
        ("qwen3:8b", "qwen"),
        ("meta-llama/Llama-3.1-8B-Instruct", "llama"),
    ],
)
def test_familia(modelo, esperada):
    assert familia(modelo) == esperada


def test_gerador_distinto_carrega_sem_warning(tmp_path, recwarn):
    config = carregar(_toml(tmp_path, gerador="gpt-4.1-mini"))

    assert config.gerador.modelo == "gpt-4.1-mini"
    assert len(recwarn) == 0


def test_colisao_exata_falha_mesmo_com_escape(tmp_path, recwarn):
    with pytest.raises(ValueError, match="mesmo modelo"):
        carregar(_toml(tmp_path, gerador="claude-opus-5", permitir=True))
    assert len(recwarn) == 0


def test_colisao_exata_ignora_organizacao_e_caixa(tmp_path):
    with pytest.raises(ValueError, match="mesmo modelo"):
        carregar(_toml(tmp_path, gerador="Qwen/Qwen3:8B"))


def test_mesma_familia_falha(tmp_path):
    with pytest.raises(ValueError, match="familia 'claude'"):
        carregar(_toml(tmp_path, gerador="claude-haiku-4-5"))


def test_mesma_familia_com_escape_carrega_com_warning(tmp_path):
    with pytest.warns(UserWarning, match="familia 'claude'"):
        config = carregar(_toml(tmp_path, gerador="claude-haiku-4-5", permitir=True))

    assert config.gerador.modelo == "claude-haiku-4-5"


def test_tier_faltando_falha(tmp_path):
    caminho = _toml(tmp_path, gerador="gpt-4.1-mini")
    texto = caminho.read_text(encoding="utf-8")
    caminho.write_text(texto.split("[tiers.grande]")[0], encoding="utf-8")

    with pytest.raises(ValueError, match="tiers devem ser"):
        carregar(caminho)


def test_reasoning_effort_aceito_no_provedor_openai(tmp_path):
    caminho = _toml(tmp_path, gerador="gpt-4.1-mini")
    texto = caminho.read_text(encoding="utf-8")
    caminho.write_text(
        texto.replace('modelo = "gpt-4.1-mini"', 'modelo = "gpt-4.1-mini"\nreasoning_effort = "minimal"'),
        encoding="utf-8",
    )

    assert carregar(caminho).gerador.reasoning_effort == "minimal"


def test_reasoning_effort_recusado_fora_do_provedor_openai(tmp_path):
    caminho = _toml(tmp_path, gerador="gpt-4.1-mini")
    texto = caminho.read_text(encoding="utf-8")
    caminho.write_text(
        texto.replace('modelo = "claude-opus-5"', 'modelo = "claude-opus-5"\nreasoning_effort = "low"'),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="reasoning_effort so' vale"):
        carregar(caminho)
