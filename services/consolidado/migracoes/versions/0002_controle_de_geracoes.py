"""Controle do ciclo de vida das gerações e privilégios da operação (005-T1).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-06

Completa as tabelas de design 005 §4 sobre a migração 0001:
  - `geracao` ganha `ativada_em`; as transições do diagrama de estados de
    design 005 §3 são impostas no banco, para qualquer papel, pelo gatilho
    `geracao_transicao_valida`. Geração nova só nasce `construindo`;
    `descartada` é estado final;
  - no máximo duas gerações coexistem (INFRA 7.5): a ativa e uma só entre
    `construindo` e `anterior`. Reconstrução com `anterior` ou `construindo`
    existente é recusada pela exclusão `ex_geracao_uma_alem_da_ativa`, que
    também vale entre operadores concorrentes (o segundo espera o primeiro e
    é recusado se ele confirmar). A exclusão é verificada a cada comando, mas
    é adiável: a troca e o rollback (005-T4) passam por um estado
    intermediário com duas gerações fora da ativa (a ativa vira `anterior`
    antes de a outra virar `ativa`, por causa de `ux_geracao_uma_ativa`, que
    não é adiável) e por isso rodam com
    `SET CONSTRAINTS ex_geracao_uma_alem_da_ativa DEFERRED`;
  - `controle_projecao` ganha a barreira dos consumidores e a versão
    (design 005 §4, TD-002);
  - `estado_consumidor` guarda a confirmação de pausa de cada worker;
  - `fase_reconstrucao` guarda a fase de cada geração para a métrica de fase
    (INFRA 8.3). Os nomes das fases são do coordenador (005-T3);
  - `remover_dados_geracao_descartada` libera o espaço de uma geração já
    descartada (D-005; INFRA 7.5) sem dar ao papel da operação DELETE nas
    tabelas da projeção.

Privilégio mínimo (INFRA 5.11; design 005 §6):
  - consolidado_operacao cria a candidata e o seu checkpoint, muda o estado
    das gerações, ergue e libera a barreira, troca o ponteiro da ativa e lê
    o que o status e a reconciliação usam (checkpoint, lacuna, contribuição
    e saldo). Não lê inbox, marcador nem quarentena e não escreve na
    projeção;
  - consolidado_worker registra a própria pausa em `estado_consumidor`;
  - consolidado_api não recebe nada novo.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tabelas com dados de uma geração, removidos quando ela é descartada.
DADOS_DA_GERACAO = [
    "inbox",
    "contribuicao",
    "saldo_diario",
    "marcador_inclusao",
    "lacuna",
    "checkpoint",
]


def upgrade() -> None:
    # Momento da última ativação; a inicial foi ativada ao ser criada (0001).
    op.execute("ALTER TABLE geracao ADD COLUMN ativada_em timestamptz")
    op.execute(
        "UPDATE geracao SET ativada_em = criada_em"
        " WHERE estado IN ('ativa', 'anterior')"
    )
    op.execute(
        "ALTER TABLE geracao ADD CONSTRAINT geracao_ativada_em_check"
        " CHECK (estado NOT IN ('ativa', 'anterior') OR ativada_em IS NOT NULL)"
    )

    # Transições de design 005 §3. Estado desconhecido segue para o CHECK de
    # `estado` (0001), que dá o erro certo. Atualização sem mudança de estado
    # não é transição.
    op.execute(
        """
        CREATE FUNCTION geracao_transicao_valida() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.estado NOT IN ('construindo', 'ativa', 'anterior',
                                  'descartada') THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.estado <> 'construindo' THEN
                    RAISE EXCEPTION
                        'geração nova nasce construindo, não %', NEW.estado
                        USING ERRCODE = 'check_violation',
                              CONSTRAINT = 'geracao_transicao_valida';
                END IF;
                NEW.ativada_em := NULL;
                RETURN NEW;
            END IF;
            IF NEW.estado = OLD.estado THEN
                RETURN NEW;
            END IF;
            IF (OLD.estado, NEW.estado) NOT IN (
                ('construindo', 'ativa'),
                ('ativa', 'anterior'),
                ('anterior', 'ativa'),
                ('construindo', 'descartada'),
                ('anterior', 'descartada')
            ) THEN
                RAISE EXCEPTION 'transição de geração inválida: % para %',
                    OLD.estado, NEW.estado
                    USING ERRCODE = 'check_violation',
                          CONSTRAINT = 'geracao_transicao_valida';
            END IF;
            IF NEW.estado = 'ativa' THEN
                NEW.ativada_em := now();
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER geracao_transicao_valida"
        " BEFORE INSERT OR UPDATE ON geracao"
        " FOR EACH ROW EXECUTE FUNCTION geracao_transicao_valida()"
    )

    # Uma só geração entre `construindo` e `anterior` (INFRA 7.5; D-005).
    op.execute(
        """
        ALTER TABLE geracao ADD CONSTRAINT ex_geracao_uma_alem_da_ativa
            EXCLUDE USING btree ((estado IN ('construindo', 'anterior')) WITH =)
            WHERE (estado IN ('construindo', 'anterior'))
            DEFERRABLE INITIALLY IMMEDIATE
        """
    )

    # Barreira dos consumidores (TD-002). A versão muda a cada troca ou
    # pedido de barreira, para o coordenador detectar operação concorrente.
    op.execute(
        """
        ALTER TABLE controle_projecao
            ADD COLUMN barreira text NOT NULL DEFAULT 'liberada'
                CONSTRAINT controle_projecao_barreira_check
                CHECK (barreira IN ('liberada', 'solicitada')),
            ADD COLUMN versao bigint NOT NULL DEFAULT 0
                CONSTRAINT controle_projecao_versao_check
                CHECK (versao >= 0)
        """
    )

    # Confirmação de pausa por worker (design 005 §4, Barreira).
    op.execute(
        """
        CREATE TABLE estado_consumidor (
            consumidor_id text        PRIMARY KEY CHECK (consumidor_id <> ''),
            pausado       boolean     NOT NULL DEFAULT false,
            visto_em      timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # Fase corrente da reconstrução por geração (INFRA 8.3).
    op.execute(
        """
        CREATE TABLE fase_reconstrucao (
            geracao_id    bigint      PRIMARY KEY REFERENCES geracao (id),
            fase          text        NOT NULL CHECK (fase <> ''),
            atualizado_em timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # Libera o espaço da geração descartada (INFRA 7.5). Roda com os
    # privilégios do dono, mas só para geração já `descartada`: a ativa, a
    # candidata e a anterior não podem ser esvaziadas por aqui. O bloqueio da
    # linha da geração serializa com uma transição concorrente.
    deletes = "\n".join(
        f"            DELETE FROM {tabela} WHERE geracao_id = p_geracao_id;"
        for tabela in DADOS_DA_GERACAO
    )
    op.execute(
        f"""
        CREATE FUNCTION remover_dados_geracao_descartada(p_geracao_id bigint)
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_estado text;
        BEGIN
            SELECT estado INTO v_estado FROM geracao
                WHERE id = p_geracao_id FOR UPDATE;
            IF v_estado IS DISTINCT FROM 'descartada' THEN
                RAISE EXCEPTION
                    'geração % não está descartada (estado: %)',
                    p_geracao_id, coalesce(v_estado, 'inexistente')
                    USING ERRCODE = 'check_violation',
                          CONSTRAINT = 'remocao_so_de_geracao_descartada';
            END IF;
{deletes}
        END
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION remover_dados_geracao_descartada(bigint) FROM PUBLIC"
    )

    # Privilégio mínimo (ver docstring). Nada para PUBLIC.
    op.execute("REVOKE ALL ON estado_consumidor, fase_reconstrucao FROM PUBLIC")

    op.execute(
        "GRANT SELECT, INSERT (estado), UPDATE (estado) ON geracao"
        " TO consolidado_operacao"
    )
    op.execute(
        "GRANT SELECT, UPDATE (geracao_ativa_id, barreira, versao)"
        " ON controle_projecao TO consolidado_operacao"
    )
    op.execute(
        "GRANT SELECT, INSERT (geracao_id) ON checkpoint TO consolidado_operacao"
    )
    op.execute(
        "GRANT SELECT ON lacuna, contribuicao, saldo_diario, estado_consumidor"
        " TO consolidado_operacao"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE (fase, atualizado_em)"
        " ON fase_reconstrucao TO consolidado_operacao"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION remover_dados_geracao_descartada(bigint)"
        " TO consolidado_operacao"
    )

    op.execute(
        "GRANT SELECT, INSERT, UPDATE (pausado, visto_em)"
        " ON estado_consumidor TO consolidado_worker"
    )


def downgrade() -> None:
    # Mesmo critério da 0001: o caminho de volta é a troca de geração.
    raise NotImplementedError("a migração de controle de gerações não tem downgrade")
