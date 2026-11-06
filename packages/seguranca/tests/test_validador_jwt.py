"""Validação JWT e cache de JWKS sem Keycloak (002-T4).

Chaves RSA geradas a cada execução assinam tokens com as claims do Keycloak
(`iss`, `aud`, `azp`, `scope`, `exp`, `iat`). O JWKS vem de uma fonte em
memória e o relógio do cache é controlado pelo teste, para cobrir o TTL sem
esperar. A integração com o Keycloak real fica em
`services/lancamentos/tests/integracao/test_validacao_jwt_keycloak.py`.
"""

import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from observabilidade import Metricas
from seguranca import (
    CacheJwks,
    ConfiguracaoJwt,
    ErroObtencaoJwks,
    EscopoInsuficiente,
    JwksIndisponivel,
    MotivoFalhaJwt,
    Principal,
    TokenInvalido,
    ValidadorJwt,
    fonte_https,
)

EMISSOR = "https://localhost:8443/realms/cashflow"
AUDIENCIA = "lancamentos-api"
ESCOPO = "lancamentos:registrar"
TTL = 300.0


def _nova_chave() -> RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk_publica(chave: RSAPrivateKey, kid: str) -> dict[str, Any]:
    jwk: dict[str, Any] = jwt.algorithms.RSAAlgorithm.to_jwk(
        chave.public_key(), as_dict=True
    )
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}


@dataclass
class Relogio:
    agora: float = 1000.0

    def __call__(self) -> float:
        return self.agora


@dataclass
class FonteMemoria:
    """JWKS em memória; `fora` simula o Keycloak parado."""

    chaves: list[dict[str, Any]] = field(default_factory=list)
    fora: bool = False
    leituras: int = 0

    def __call__(self) -> Mapping[str, Any]:
        self.leituras += 1
        if self.fora:
            raise ErroObtencaoJwks("ConnectionRefusedError")
        return {"keys": list(self.chaves)}


@dataclass
class Ambiente:
    chave: RSAPrivateKey
    fonte: FonteMemoria
    relogio: Relogio
    metricas: Metricas
    validador: ValidadorJwt

    def token(
        self,
        chave: RSAPrivateKey | None = None,
        kid: str = "k1",
        algoritmo: str = "RS256",
        **claims: Any,
    ) -> str:
        agora = int(time.time())
        carga: dict[str, Any] = {
            "iss": EMISSOR,
            "aud": [AUDIENCIA],
            "azp": "cliente-lancamentos",
            "scope": ESCOPO,
            "iat": agora,
            "exp": agora + 300,
            **claims,
        }
        carga = {k: v for k, v in carga.items() if v is not None}
        return jwt.encode(
            carga,
            chave or self.chave,
            algorithm=algoritmo,
            headers={"kid": kid},
        )

    def falhas_jwt(self, motivo: MotivoFalhaJwt) -> float:
        valor = self.metricas.registro.get_sample_value(
            "cashflow_jwt_falhas_total", {"motivo": motivo.value}
        )
        return valor or 0.0

    def falhas_jwks(self) -> float:
        valor = self.metricas.registro.get_sample_value("cashflow_jwks_falhas_total")
        return valor or 0.0


@pytest.fixture(scope="module")
def chave() -> RSAPrivateKey:
    return _nova_chave()


@pytest.fixture
def amb(chave: RSAPrivateKey) -> Ambiente:
    fonte = FonteMemoria([_jwk_publica(chave, "k1")])
    relogio = Relogio()
    metricas = Metricas()
    configuracao = ConfiguracaoJwt(
        emissor=EMISSOR,
        audiencia=AUDIENCIA,
        url_jwks="https://keycloak:8443/realms/cashflow/protocol/openid-connect/certs",
        arquivo_ac="/inexistente",
        ttl_jwks_segundos=TTL,
    )
    cache = CacheJwks(fonte, TTL, metricas, relogio=relogio)
    return Ambiente(
        chave, fonte, relogio, metricas, ValidadorJwt(configuracao, cache, metricas)
    )


def test_token_valido_com_o_escopo_devolve_o_cliente(amb: Ambiente) -> None:
    principal = amb.validador.validar(amb.token(), ESCOPO)

    assert principal == Principal("cliente-lancamentos", frozenset({ESCOPO}))


def test_token_sem_o_escopo_exigido_e_recusado_com_403(amb: Ambiente) -> None:
    token = amb.token(scope="lancamentos:consultar")

    with pytest.raises(EscopoInsuficiente):
        amb.validador.validar(token, ESCOPO)
    assert amb.falhas_jwt(MotivoFalhaJwt.ESCOPO) == 1


def test_token_sem_claim_de_escopo_e_recusado(amb: Ambiente) -> None:
    with pytest.raises(EscopoInsuficiente):
        amb.validador.validar(amb.token(scope=None), ESCOPO)


def test_token_expirado_e_recusado(amb: Ambiente) -> None:
    passado = int(time.time()) - 600
    token = amb.token(iat=passado, exp=passado + 300)

    with pytest.raises(TokenInvalido) as erro:
        amb.validador.validar(token, ESCOPO)
    assert erro.value.motivo is MotivoFalhaJwt.EXPIRADO
    assert amb.falhas_jwt(MotivoFalhaJwt.EXPIRADO) == 1


def test_token_assinado_por_outra_chave_com_o_mesmo_kid_e_recusado(
    amb: Ambiente,
) -> None:
    token = amb.token(chave=_nova_chave())

    with pytest.raises(TokenInvalido) as erro:
        amb.validador.validar(token, ESCOPO)
    assert erro.value.motivo is MotivoFalhaJwt.ASSINATURA
    assert amb.falhas_jwt(MotivoFalhaJwt.ASSINATURA) == 1


def test_token_com_carga_adulterada_e_recusado(amb: Ambiente) -> None:
    cabecalho, _, assinatura = amb.token(scope="nenhum").split(".")
    _, carga, _ = amb.token().split(".")

    with pytest.raises(TokenInvalido) as erro:
        amb.validador.validar(f"{cabecalho}.{carga}.{assinatura}", ESCOPO)
    assert erro.value.motivo is MotivoFalhaJwt.ASSINATURA


@pytest.mark.parametrize(
    ("claims", "motivo"),
    [
        ({"iss": "https://outro/realms/cashflow"}, MotivoFalhaJwt.EMISSOR),
        ({"aud": ["consolidado-api"]}, MotivoFalhaJwt.AUDIENCIA),
        ({"aud": None}, MotivoFalhaJwt.MALFORMADO),
        ({"exp": None}, MotivoFalhaJwt.MALFORMADO),
        ({"azp": None}, MotivoFalhaJwt.MALFORMADO),
        ({"nbf": int(time.time()) + 3600}, MotivoFalhaJwt.NAO_VIGENTE),
    ],
)
def test_emissor_audiencia_e_claims_obrigatorias_sao_conferidos(
    amb: Ambiente, claims: dict[str, Any], motivo: MotivoFalhaJwt
) -> None:
    with pytest.raises(TokenInvalido) as erro:
        amb.validador.validar(amb.token(**claims), ESCOPO)
    assert erro.value.motivo is motivo
    assert amb.falhas_jwt(motivo) == 1


def test_algoritmo_simetrico_e_recusado_antes_de_verificar(amb: Ambiente) -> None:
    token = jwt.encode(
        {"iss": EMISSOR, "aud": AUDIENCIA, "scope": ESCOPO},
        "segredo-qualquer-com-tamanho-suficiente-para-hs256",
        algorithm="HS256",
        headers={"kid": "k1"},
    )

    with pytest.raises(TokenInvalido) as erro:
        amb.validador.validar(token, ESCOPO)
    assert erro.value.motivo is MotivoFalhaJwt.ALGORITMO
    assert amb.fonte.leituras == 0


@pytest.mark.parametrize("token", ["", "nao-e-jwt", "a.b.c"])
def test_token_ausente_ou_malformado_e_recusado(amb: Ambiente, token: str) -> None:
    with pytest.raises(TokenInvalido) as erro:
        amb.validador.validar(token, ESCOPO)
    esperado = MotivoFalhaJwt.AUSENTE if not token else MotivoFalhaJwt.MALFORMADO
    assert erro.value.motivo is esperado


def test_mensagem_do_erro_nao_contem_o_token(amb: Ambiente) -> None:
    token = amb.token(chave=_nova_chave())

    with pytest.raises(TokenInvalido) as erro:
        amb.validador.validar(token, ESCOPO)
    assert token not in str(erro.value)
    assert token.split(".")[1] not in repr(erro.value)


def test_jwks_e_lido_uma_vez_dentro_do_ttl(amb: Ambiente) -> None:
    for _ in range(5):
        amb.validador.validar(amb.token(), ESCOPO)

    assert amb.fonte.leituras == 1


def test_keycloak_parado_token_valido_e_aceito_dentro_do_ttl(amb: Ambiente) -> None:
    amb.validador.validar(amb.token(), ESCOPO)
    amb.fonte.fora = True
    amb.relogio.agora += TTL - 1

    principal = amb.validador.validar(amb.token(), ESCOPO)

    assert principal.cliente_id == "cliente-lancamentos"
    assert amb.falhas_jwks() == 0


def test_keycloak_parado_apos_o_ttl_sem_chave_valida_da_503(amb: Ambiente) -> None:
    amb.validador.validar(amb.token(), ESCOPO)
    amb.fonte.fora = True
    amb.relogio.agora += TTL

    with pytest.raises(JwksIndisponivel):
        amb.validador.validar(amb.token(), ESCOPO)
    assert amb.falhas_jwks() == 1
    assert amb.falhas_jwt(MotivoFalhaJwt.JWKS_INDISPONIVEL) == 1
    assert not amb.validador.cache.pronto()


def test_keycloak_de_volta_apos_o_ttl_renova_e_aceita(amb: Ambiente) -> None:
    amb.validador.validar(amb.token(), ESCOPO)
    amb.fonte.fora = True
    amb.relogio.agora += TTL
    with pytest.raises(JwksIndisponivel):
        amb.validador.validar(amb.token(), ESCOPO)

    amb.fonte.fora = False
    amb.relogio.agora += 10

    assert amb.validador.validar(amb.token(), ESCOPO).cliente_id
    assert amb.validador.cache.pronto()


def test_sem_jwks_na_partida_o_validador_da_503(amb: Ambiente) -> None:
    amb.fonte.fora = True

    with pytest.raises(JwksIndisponivel):
        amb.validador.validar(amb.token(), ESCOPO)
    assert amb.falhas_jwks() == 1


def test_falhas_seguidas_nao_consultam_o_keycloak_a_cada_requisicao(
    amb: Ambiente,
) -> None:
    amb.fonte.fora = True
    for _ in range(5):
        with pytest.raises(JwksIndisponivel):
            amb.validador.validar(amb.token(), ESCOPO)

    assert amb.fonte.leituras == 1
    assert amb.falhas_jwks() == 1


def test_kid_novo_dentro_do_ttl_renova_o_jwks_rotacao(amb: Ambiente) -> None:
    amb.validador.validar(amb.token(), ESCOPO)
    nova = _nova_chave()
    amb.fonte.chaves.append(_jwk_publica(nova, "k2"))
    amb.relogio.agora += 60

    principal = amb.validador.validar(amb.token(chave=nova, kid="k2"), ESCOPO)

    assert principal.cliente_id == "cliente-lancamentos"
    assert amb.fonte.leituras == 2


def test_kid_inexistente_e_recusado_sem_renovar_a_cada_requisicao(
    amb: Ambiente,
) -> None:
    amb.validador.validar(amb.token(), ESCOPO)
    amb.relogio.agora += 60
    for _ in range(3):
        with pytest.raises(TokenInvalido) as erro:
            amb.validador.validar(amb.token(kid="inventado"), ESCOPO)
        assert erro.value.motivo is MotivoFalhaJwt.KID_DESCONHECIDO

    assert amb.fonte.leituras == 2
    assert amb.falhas_jwt(MotivoFalhaJwt.KID_DESCONHECIDO) == 3


def test_kid_desconhecido_com_keycloak_parado_da_503(amb: Ambiente) -> None:
    amb.validador.validar(amb.token(), ESCOPO)
    amb.fonte.fora = True
    amb.relogio.agora += 60

    with pytest.raises(JwksIndisponivel):
        amb.validador.validar(amb.token(kid="k2"), ESCOPO)
    # A chave conhecida continua aceita dentro do TTL.
    assert amb.validador.validar(amb.token(), ESCOPO).cliente_id


def test_jwks_so_com_chave_de_cifragem_conta_como_falha(amb: Ambiente) -> None:
    amb.fonte.chaves = [{**_jwk_publica(amb.chave, "k1"), "use": "enc"}]

    with pytest.raises(JwksIndisponivel):
        amb.validador.validar(amb.token(), ESCOPO)
    assert amb.falhas_jwks() == 1


def test_series_de_falha_jwt_existem_em_zero_desde_a_partida(amb: Ambiente) -> None:
    for motivo in MotivoFalhaJwt:
        assert (
            amb.metricas.registro.get_sample_value(
                "cashflow_jwt_falhas_total", {"motivo": motivo.value}
            )
            == 0
        )


def test_configuracao_exige_variaveis_e_aplica_o_ttl() -> None:
    ambiente = {
        "JWT_EMISSOR": EMISSOR,
        "JWT_JWKS_URL": "https://keycloak:8443/certs",
        "JWT_AC_ARQUIVO": "/ac.crt",
        "JWT_JWKS_TTL_SEGUNDOS": "60",
    }

    configuracao = ConfiguracaoJwt.de_ambiente(ambiente, audiencia_padrao=AUDIENCIA)

    assert configuracao.audiencia == AUDIENCIA
    assert configuracao.ttl_jwks_segundos == 60
    with pytest.raises(ValueError, match="JWT_AUDIENCIA"):
        ConfiguracaoJwt.de_ambiente(ambiente)
    with pytest.raises(ValueError, match="JWT_EMISSOR"):
        ConfiguracaoJwt.de_ambiente({}, audiencia_padrao=AUDIENCIA)


def test_jwks_so_e_obtido_por_tls() -> None:
    with pytest.raises(ValueError, match="TLS"):
        fonte_https("http://keycloak:8080/certs", ssl.create_default_context(), 1)
