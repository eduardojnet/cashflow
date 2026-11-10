"""Estados de geração e tabelas de controle no PostgreSQL real (005-T1; INFRA 9.1).

Usa o PostgreSQL descartável do consolidado (consolidado_descartavel.py), com
as migrações 0001 a 0003 aplicadas por consolidado_migracao, e conecta como
consolidado_operacao, o papel do coordenador (INFRA 5.11).

A maior parte dos testes roda numa transação desfeita ao fim, para que cada
um comece do estado da migração (só a geração inicial ativa). Os que precisam
de COMMIT (concorrência e remoção de dados) terminam com as gerações criadas
descartadas, o que preserva esse estado.
"""

import shutil
import threading
import uuid
from collections.abc import Iterator
from datetime import date

import psycopg
import pytest
from consolidado.tests.integracao.consolidado_descartavel import (
    Consolidado,
    consolidado_descartavel,
)
from coordenador.dominio import EstadoGeracao, transicao_valida
from psycopg import sql

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker ausente no PATH"
)

E = EstadoGeracao
EXCLUSAO = "ex_geracao_uma_alem_da_ativa"


@pytest.fixture(scope="module")
def banco() -> Iterator[Consolidado]:
    with consolidado_descartavel("coordenador-geracoes") as consolidado:
        yield consolidado


@pytest.fixture
def operacao(banco: Consolidado) -> Iterator[psycopg.Connection]:
    """Conexão de consolidado_operacao numa transação sempre desfeita."""
    with psycopg.connect(banco.conninfo("consolidado_operacao")) as conexao:
        try:
            yield conexao
        finally:
            conexao.rollback()


def _um(
    conexao: psycopg.Connection, consulta: str | sql.Composed, *params: object
) -> object:
    linha = conexao.execute(consulta, params).fetchone()
    assert linha is not None
    return linha[0]


def _ativa(conexao: psycopg.Connection) -> int:
    ativa = _um(conexao, "SELECT geracao_ativa_id FROM controle_projecao WHERE id = 1")
    assert isinstance(ativa, int)
    return ativa


def _estado(conexao: psycopg.Connection, geracao: int) -> str:
    estado = _um(conexao, "SELECT estado FROM geracao WHERE id = %s", geracao)
    assert isinstance(estado, str)
    return estado


def _criar_candidata(conexao: psycopg.Connection) -> int:
    """Início da reconstrução: geração `construindo` com checkpoint 0."""
    candidata = _um(
        conexao, "INSERT INTO geracao (estado) VALUES ('construindo') RETURNING id"
    )
    assert isinstance(candidata, int)
    conexao.execute("INSERT INTO checkpoint (geracao_id) VALUES (%s)", (candidata,))
    return candidata


def _mudar(conexao: psycopg.Connection, geracao: int, estado: str) -> None:
    conexao.execute("UPDATE geracao SET estado = %s WHERE id = %s", (estado, geracao))


def _trocar(conexao: psycopg.Connection, nova: int) -> int:
    """Troca de design 005 §4 sem barreira; devolve a geração que saiu."""
    antiga = _ativa(conexao)
    conexao.execute(f"SET CONSTRAINTS {EXCLUSAO} DEFERRED")
    _mudar(conexao, antiga, "anterior")
    _mudar(conexao, nova, "ativa")
    conexao.execute(
        "UPDATE controle_projecao SET geracao_ativa_id = %s, versao = versao + 1"
        " WHERE id = 1",
        (nova,),
    )
    return antiga


def _preparar(conexao: psycopg.Connection, estado: EstadoGeracao) -> int:
    """Geração no `estado` pedido, pelo caminho do ciclo de vida."""
    if estado is E.ATIVA:
        return _ativa(conexao)
    candidata = _criar_candidata(conexao)
    if estado is E.ANTERIOR:
        return _trocar(conexao, candidata)
    if estado is E.DESCARTADA:
        _mudar(conexao, candidata, "descartada")
    return candidata


def _tentar_transicao(
    conexao: psycopg.Connection, geracao: int, destino: EstadoGeracao
) -> bool:
    """Aplica a transição num savepoint; False se o gatilho a recusar."""
    try:
        with conexao.transaction():
            conexao.execute(f"SET CONSTRAINTS {EXCLUSAO} DEFERRED")
            if destino is E.ATIVA:
                # Só uma ativa por vez (ux_geracao_uma_ativa): a atual sai antes.
                atual = _ativa(conexao)
                if atual != geracao:
                    _mudar(conexao, atual, "anterior")
            _mudar(conexao, geracao, destino)
    except psycopg.errors.CheckViolation as erro:
        assert erro.diag.constraint_name == "geracao_transicao_valida"
        return False
    return True


@pytest.mark.parametrize(
    ("origem", "destino"),
    [(origem, destino) for origem in E for destino in E if origem is not destino],
)
def test_banco_aplica_as_mesmas_transicoes_do_dominio(
    operacao: psycopg.Connection, origem: EstadoGeracao, destino: EstadoGeracao
) -> None:
    geracao = _preparar(operacao, origem)

    aceita = _tentar_transicao(operacao, geracao, destino)

    assert aceita is transicao_valida(origem, destino)
    if not aceita:
        assert _estado(operacao, geracao) == origem


@pytest.mark.parametrize("estado", ["ativa", "anterior", "descartada"])
def test_geracao_nova_so_nasce_construindo(
    operacao: psycopg.Connection, estado: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation) as erro:
        operacao.execute("INSERT INTO geracao (estado) VALUES (%s)", (estado,))

    assert erro.value.diag.constraint_name == "geracao_transicao_valida"


def test_operacao_inicia_reconstrucao_sem_mudar_a_ativa(
    operacao: psycopg.Connection,
) -> None:
    ativa = _ativa(operacao)

    candidata = _criar_candidata(operacao)
    operacao.execute(
        "INSERT INTO fase_reconstrucao (geracao_id, fase) VALUES (%s, 'replay')",
        (candidata,),
    )

    linha = operacao.execute(
        "SELECT g.estado, g.ativada_em IS NULL, k.posicao_continua, f.fase"
        " FROM geracao g JOIN checkpoint k ON k.geracao_id = g.id"
        " JOIN fase_reconstrucao f ON f.geracao_id = g.id WHERE g.id = %s",
        (candidata,),
    ).fetchone()
    assert linha == ("construindo", True, 0, "replay")
    assert _ativa(operacao) == ativa
    assert _estado(operacao, ativa) == "ativa"


def test_troca_e_rollback_seguem_o_diagrama(operacao: psycopg.Connection) -> None:
    inicial = _ativa(operacao)
    candidata = _criar_candidata(operacao)

    assert _trocar(operacao, candidata) == inicial
    assert (_estado(operacao, candidata), _estado(operacao, inicial)) == (
        "ativa",
        "anterior",
    )
    assert _um(
        operacao, "SELECT ativada_em IS NOT NULL FROM geracao WHERE id = %s", candidata
    )

    # Rollback (FR-006): a anterior volta a ser a ativa.
    assert _trocar(operacao, inicial) == candidata
    assert (_estado(operacao, inicial), _estado(operacao, candidata)) == (
        "ativa",
        "anterior",
    )
    operacao.execute(f"SET CONSTRAINTS {EXCLUSAO} IMMEDIATE")


def test_reconstrucao_recusada_com_construindo_existente(
    operacao: psycopg.Connection,
) -> None:
    _criar_candidata(operacao)

    with pytest.raises(psycopg.errors.ExclusionViolation) as erro:
        _criar_candidata(operacao)

    assert erro.value.diag.constraint_name == EXCLUSAO


def test_reconstrucao_recusada_com_anterior_existente(
    operacao: psycopg.Connection,
) -> None:
    _trocar(operacao, _criar_candidata(operacao))
    operacao.execute(f"SET CONSTRAINTS {EXCLUSAO} IMMEDIATE")

    with pytest.raises(psycopg.errors.ExclusionViolation) as erro:
        _criar_candidata(operacao)

    assert erro.value.diag.constraint_name == EXCLUSAO


def test_reconstrucao_volta_a_ser_aceita_apos_cancelar_e_descartar(
    operacao: psycopg.Connection,
) -> None:
    # Cancelar a candidata (construindo -> descartada).
    _mudar(operacao, _criar_candidata(operacao), "descartada")
    # Nova candidata, troca e descarte da anterior (D-005).
    anterior = _trocar(operacao, _criar_candidata(operacao))
    _mudar(operacao, anterior, "descartada")
    operacao.execute(f"SET CONSTRAINTS {EXCLUSAO} IMMEDIATE")

    _criar_candidata(operacao)

    contagem = _um(
        operacao, "SELECT count(*) FROM geracao WHERE estado <> 'descartada'"
    )
    assert contagem == 2


def test_reconstrucoes_concorrentes_so_uma_e_aceita(banco: Consolidado) -> None:
    resultado: list[BaseException | int] = []

    def segunda_reconstrucao() -> None:
        try:
            with psycopg.connect(banco.conninfo("consolidado_operacao")) as conexao:
                resultado.append(_criar_candidata(conexao))
        except psycopg.Error as erro:
            resultado.append(erro)

    with psycopg.connect(banco.conninfo("consolidado_operacao")) as primeira:
        candidata = _criar_candidata(primeira)
        concorrente = threading.Thread(target=segunda_reconstrucao)
        concorrente.start()
        # A segunda espera a primeira confirmar ou desfazer.
        concorrente.join(timeout=2)
        assert concorrente.is_alive()
        primeira.commit()
        concorrente.join(timeout=30)

    assert not concorrente.is_alive()
    [erro] = resultado
    assert isinstance(erro, psycopg.errors.ExclusionViolation)
    assert erro.diag.constraint_name == EXCLUSAO

    with psycopg.connect(banco.conninfo("consolidado_operacao")) as conexao:
        assert _estado(conexao, candidata) == "construindo"
        _mudar(conexao, candidata, "descartada")


@pytest.mark.parametrize("tabela", ["inbox", "marcador_inclusao", "quarentena"])
def test_operacao_nao_le_dados_internos_da_projecao(
    operacao: psycopg.Connection, tabela: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        operacao.execute(
            sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier(tabela))
        )


@pytest.mark.parametrize(
    "comando",
    [
        "INSERT INTO saldo_diario (geracao_id, data) VALUES (1, '9999-12-31')",
        "UPDATE saldo_diario SET saldo_dia = saldo_dia",
        "UPDATE checkpoint SET posicao_continua = 0",
        "DELETE FROM checkpoint",
        "INSERT INTO lacuna (geracao_id, posicao) VALUES (1, 1)",
        "DELETE FROM lacuna",
        "DELETE FROM contribuicao",
        "DELETE FROM geracao",
        "UPDATE geracao SET ativada_em = now()",
        "UPDATE geracao SET id = id",
        "INSERT INTO geracao (id, estado) VALUES (999, 'construindo')",
        "DELETE FROM controle_projecao",
        "UPDATE estado_consumidor SET pausado = true",
        "DELETE FROM fase_reconstrucao",
        "TRUNCATE saldo_diario",
    ],
)
def test_operacao_nao_escreve_na_projecao(
    operacao: psycopg.Connection, comando: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        operacao.execute(comando)


def test_operacao_ergue_a_barreira_e_registra_a_fase(
    operacao: psycopg.Connection,
) -> None:
    candidata = _criar_candidata(operacao)

    operacao.execute(
        "UPDATE controle_projecao SET barreira = 'solicitada', versao = versao + 1"
        " WHERE id = 1"
    )
    operacao.execute(
        "INSERT INTO fase_reconstrucao (geracao_id, fase) VALUES (%s, 'replay')",
        (candidata,),
    )
    operacao.execute(
        "UPDATE fase_reconstrucao SET fase = 'barreira', atualizado_em = now()"
        " WHERE geracao_id = %s",
        (candidata,),
    )

    assert operacao.execute(
        "SELECT barreira, versao FROM controle_projecao"
    ).fetchone() == ("solicitada", 1)
    assert operacao.execute("SELECT count(*) FROM estado_consumidor").fetchone() == (0,)
    with pytest.raises(psycopg.errors.CheckViolation) as erro:
        operacao.execute("UPDATE controle_projecao SET barreira = 'parcial'")
    assert erro.value.diag.constraint_name == "controle_projecao_barreira_check"


def test_worker_confirma_a_pausa_e_nao_controla_geracoes(banco: Consolidado) -> None:
    with psycopg.connect(banco.conninfo("consolidado_worker")) as worker:
        try:
            worker.execute(
                "INSERT INTO estado_consumidor (consumidor_id) VALUES ('worker-teste')"
                " ON CONFLICT (consumidor_id) DO UPDATE"
                " SET pausado = true, visto_em = now()"
            )
            worker.execute(
                "INSERT INTO estado_consumidor (consumidor_id) VALUES ('worker-teste')"
                " ON CONFLICT (consumidor_id) DO UPDATE"
                " SET pausado = true, visto_em = now()"
            )
            assert worker.execute(
                "SELECT pausado FROM estado_consumidor"
                " WHERE consumidor_id = 'worker-teste'"
            ).fetchone() == (True,)
            for comando in (
                "UPDATE controle_projecao SET barreira = 'liberada'",
                "INSERT INTO geracao (estado) VALUES ('construindo')",
                "UPDATE geracao SET estado = estado",
                "INSERT INTO fase_reconstrucao (geracao_id, fase) VALUES (1, 'x')",
                "DELETE FROM estado_consumidor",
            ):
                with (
                    pytest.raises(psycopg.errors.InsufficientPrivilege),
                    worker.transaction(),
                ):
                    worker.execute(comando)
        finally:
            worker.rollback()


def test_api_nao_le_a_confirmacao_dos_consumidores(banco: Consolidado) -> None:
    with (
        psycopg.connect(banco.conninfo("consolidado_api")) as api,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        api.execute("SELECT 1 FROM estado_consumidor")


@pytest.mark.parametrize(
    "comando",
    [
        "INSERT INTO fase_reconstrucao (geracao_id, fase) VALUES (1, 'x')",
        "UPDATE fase_reconstrucao SET fase = 'x'",
        "DELETE FROM fase_reconstrucao",
    ],
)
def test_api_so_le_a_fase_da_reconstrucao(banco: Consolidado, comando: str) -> None:
    # Leitura para `cashflow_reconstrucao_fase` (migração 0003, 005-T3).
    with psycopg.connect(banco.conninfo("consolidado_api")) as api:
        api.execute("SELECT geracao_id, fase, atualizado_em FROM fase_reconstrucao")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            api.execute(comando)


@pytest.mark.parametrize("estado", [E.ATIVA, E.CONSTRUINDO, E.ANTERIOR])
def test_remocao_de_dados_recusada_fora_de_descartada(
    operacao: psycopg.Connection, estado: EstadoGeracao
) -> None:
    geracao = _preparar(operacao, estado)

    with pytest.raises(psycopg.errors.CheckViolation) as erro:
        operacao.execute("SELECT remover_dados_geracao_descartada(%s)", (geracao,))

    assert erro.value.diag.constraint_name == "remocao_so_de_geracao_descartada"


def _projetar(worker: psycopg.Connection, geracao: int, posicao: int) -> None:
    """Uma linha em cada tabela da projeção, como o worker grava (004-T3)."""
    lancamento = uuid.uuid4()
    worker.execute(
        "INSERT INTO inbox (geracao_id, evento_id, hash) VALUES (%s, %s, 'h')",
        (geracao, uuid.uuid4()),
    )
    worker.execute(
        "INSERT INTO contribuicao (geracao_id, lancamento_id, posicao,"
        " data_lancamento, tipo, valor, hash)"
        " VALUES (%s, %s, %s, %s, 'CREDITO', 1.00, 'h')",
        (geracao, lancamento, posicao, date(2026, 11, 6)),
    )
    worker.execute(
        "INSERT INTO saldo_diario (geracao_id, data, total_creditos, saldo_dia)"
        " VALUES (%s, %s, 1.00, 1.00)",
        (geracao, date(2026, 11, 6)),
    )
    worker.execute(
        "INSERT INTO marcador_inclusao (geracao_id, lancamento_id, posicao)"
        " VALUES (%s, %s, %s)",
        (geracao, lancamento, posicao),
    )
    worker.execute(
        "INSERT INTO lacuna (geracao_id, posicao) VALUES (%s, %s)",
        (geracao, posicao + 1),
    )


def _linhas_por_tabela(banco: Consolidado, geracao: int) -> dict[str, object]:
    tabelas = [
        "inbox",
        "contribuicao",
        "saldo_diario",
        "marcador_inclusao",
        "lacuna",
        "checkpoint",
    ]
    contagem = sql.SQL("SELECT count(*) FROM {} WHERE geracao_id = %s")
    with psycopg.connect(banco.conninfo("consolidado_migracao")) as dono:
        return {
            tabela: _um(dono, contagem.format(sql.Identifier(tabela)), geracao)
            for tabela in tabelas
        }


def test_descartada_tem_os_dados_removidos_sem_tocar_a_ativa(
    banco: Consolidado,
) -> None:
    with psycopg.connect(
        banco.conninfo("consolidado_operacao"), autocommit=True
    ) as operacao:
        ativa = _ativa(operacao)
        candidata = _criar_candidata(operacao)
    with psycopg.connect(banco.conninfo("consolidado_worker")) as worker:
        _projetar(worker, candidata, posicao=1)
        _projetar(worker, ativa, posicao=9_000_000)
    antes_ativa = _linhas_por_tabela(banco, ativa)

    with psycopg.connect(banco.conninfo("consolidado_operacao")) as operacao:
        _mudar(operacao, candidata, "descartada")
        operacao.execute("SELECT remover_dados_geracao_descartada(%s)", (candidata,))

    assert _linhas_por_tabela(banco, candidata) == dict.fromkeys(antes_ativa, 0)
    assert _linhas_por_tabela(banco, ativa) == antes_ativa
    with psycopg.connect(banco.conninfo("consolidado_operacao")) as operacao:
        assert _estado(operacao, candidata) == "descartada"
