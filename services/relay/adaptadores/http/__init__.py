"""Adaptador HTTP do relay: live e ready em porta interna (003-T5)."""

from relay.adaptadores.http.saude import Prontidao, ServidorSaude

__all__ = ["Prontidao", "ServidorSaude"]
