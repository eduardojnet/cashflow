"""Caso de uso `RegistrarLancamento` sem infraestrutura (002-T3).

A transação real é coberta em tests/integracao/test_unidade_de_trabalho_livro.py;
aqui fica o que não depende de banco: hash do conteúdo e validação da chave.
"""

import asyncio

import pytest
from lancamentos.aplicacao import (
    ChaveIdempotenciaInvalida,
    RegistrarLancamento,
    RegistrarLancamentoCmd,
    RespostaRegistro,
)
from lancamentos.dominio import ErroDominio


class UnidadeDeTrabalhoEspia:
    def __init__(self) -> None:
        self.chamadas: list[tuple[RegistrarLancamentoCmd, str, str]] = []

    async def registrar(
        self, cmd: RegistrarLancamentoCmd, chave: str, cliente: str
    ) -> RespostaRegistro:
        self.chamadas.append((cmd, chave, cliente))
        return RespostaRegistro(status_http=201, corpo={"posicao": 1})


def _cmd(**campos: object) -> RegistrarLancamentoCmd:
    dados: dict[str, object] = {
        "tipo": "CREDITO",
        "valor": "10.50",
        "data_lancamento": "2026-11-06",
        "descricao": "venda",
    }
    dados.update(campos)
    return RegistrarLancamentoCmd.de(**dados)


def test_mesmo_conteudo_gera_o_mesmo_hash_mesmo_com_id_diferente() -> None:
    primeiro, segundo = _cmd(), _cmd()

    assert primeiro.lancamento.id != segundo.lancamento.id
    assert primeiro.hash_requisicao == segundo.hash_requisicao


def test_valor_com_escrita_diferente_e_o_mesmo_conteudo() -> None:
    assert _cmd(valor="10.5").hash_requisicao == _cmd(valor="10.50").hash_requisicao


@pytest.mark.parametrize(
    "campos",
    [
        {"tipo": "DEBITO"},
        {"valor": "10.51"},
        {"data_lancamento": "2026-11-05"},
        {"descricao": "outra"},
        {"descricao": None},
    ],
    ids=["tipo", "valor", "data", "descricao", "sem-descricao"],
)
def test_qualquer_campo_diferente_muda_o_hash(campos: dict[str, object]) -> None:
    assert _cmd(**campos).hash_requisicao != _cmd().hash_requisicao


def test_comando_invalido_e_rejeitado_pelo_dominio() -> None:
    with pytest.raises(ErroDominio):
        _cmd(valor="0")


def test_caso_de_uso_delega_a_unidade_de_trabalho() -> None:
    unidade = UnidadeDeTrabalhoEspia()
    cmd = _cmd()

    resposta = asyncio.run(
        RegistrarLancamento(unidade).executar(cmd, "chave-1", "cliente-1")
    )

    assert resposta.status_http == 201
    assert unidade.chamadas == [(cmd, "chave-1", "cliente-1")]


@pytest.mark.parametrize(
    ("chave", "cliente"),
    [("", "cliente-1"), ("   ", "cliente-1"), ("chave-1", ""), ("chave-1", " ")],
    ids=["chave-vazia", "chave-em-branco", "cliente-vazio", "cliente-em-branco"],
)
def test_chave_ou_cliente_ausente_e_rejeitado_sem_registro(
    chave: str, cliente: str
) -> None:
    unidade = UnidadeDeTrabalhoEspia()

    with pytest.raises(ChaveIdempotenciaInvalida):
        asyncio.run(RegistrarLancamento(unidade).executar(_cmd(), chave, cliente))

    assert unidade.chamadas == []
