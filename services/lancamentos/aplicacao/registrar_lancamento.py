"""Caso de uso `RegistrarLancamento` e sua porta de saída (002-T3).

A aplicação valida o comando pelo domínio, calcula o hash que identifica o
conteúdo da requisição e delega à `UnidadeDeTrabalhoLivro` a transação que
grava, ou nada grava, idempotência, posição, lançamento e evento da outbox
(TD-001, TD-002, TD-004). Nada aqui conhece banco, HTTP ou broker (INFRA 2.1).
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from lancamentos.dominio import ErroDominio, Lancamento

# Status da primeira resposta de um registro (design 002 §4). A repetição
# devolve o status e o corpo gravados, não um novo cálculo.
STATUS_CRIADO = 201


class ChaveIdempotenciaInvalida(ErroDominio):
    """Chave de idempotência ou cliente ausente: a requisição é rejeitada."""


class ConflitoIdempotencia(Exception):
    """Chave já usada pelo cliente com conteúdo diferente (FR-004, D-006).

    A mensagem não repete chave nem conteúdo, para que nada da requisição
    chegue a log (INFRA 8.2).
    """

    def __init__(self) -> None:
        super().__init__("chave de idempotência já usada com conteúdo diferente")


@dataclass(frozen=True)
class RegistrarLancamentoCmd:
    """Lançamento já validado pelo domínio, pronto para registro."""

    lancamento: Lancamento

    @classmethod
    def de(
        cls,
        tipo: object,
        valor: object,
        data_lancamento: object,
        descricao: object = None,
    ) -> "RegistrarLancamentoCmd":
        return cls(Lancamento.novo(tipo, valor, data_lancamento, descricao))

    @property
    def hash_requisicao(self) -> str:
        """SHA-256 do conteúdo canônico da requisição (TD-004).

        Usa os valores já normalizados pelo domínio, para que "10.5" e
        "10.50" sejam o mesmo conteúdo. O `id` gerado para o lançamento fica
        fora: muda a cada tentativa e não faz parte do que o cliente enviou.
        """
        lancamento = self.lancamento
        canonico = json.dumps(
            {
                "tipo": lancamento.tipo.value,
                "valor": str(lancamento.valor),
                "data_lancamento": lancamento.data_lancamento.isoformat(),
                "descricao": lancamento.descricao,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(canonico.encode()).hexdigest()


@dataclass(frozen=True)
class RespostaRegistro:
    """Resposta HTTP do registro, guardada para a repetição (INFRA 3.1).

    `repetida` indica que a resposta veio da tabela de idempotência e que
    nenhum lançamento novo foi criado nesta chamada.
    """

    status_http: int
    corpo: dict[str, object]
    repetida: bool = False


class UnidadeDeTrabalhoLivro(Protocol):
    """Transação atômica de registro no livro (design 002 §4)."""

    async def registrar(
        self, cmd: RegistrarLancamentoCmd, chave: str, cliente: str
    ) -> RespostaRegistro: ...


class RegistrarLancamento:
    """Orquestra o registro idempotente de um lançamento (FR-001, FR-004)."""

    def __init__(self, unidade_de_trabalho: UnidadeDeTrabalhoLivro) -> None:
        self._unidade_de_trabalho = unidade_de_trabalho

    async def executar(
        self, cmd: RegistrarLancamentoCmd, chave: str, cliente: str
    ) -> RespostaRegistro:
        # As fontes não definem formato nem tamanho da chave; exige-se só
        # que exista, como a restrição `chave <> ''` do banco.
        if not chave or not chave.strip():
            raise ChaveIdempotenciaInvalida("Idempotency-Key", "deve ser informada")
        if not cliente or not cliente.strip():
            raise ChaveIdempotenciaInvalida("cliente", "deve ser identificado")
        return await self._unidade_de_trabalho.registrar(cmd, chave, cliente)
