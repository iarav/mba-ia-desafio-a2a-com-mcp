"""Servidor MCP da Hill Valley Tech (desafio "A Ponte").

Expõe as salas de reunião como ferramentas e a política de uso como resource,
em Streamable HTTP. Implementa o ciclo MRTR na `reservar_sala`: quando o
intervalo pedido conflita, responde `input_required` com uma elicitation em form
mode (as salas alternativas) e um `requestState` protegido por integridade.

O `requestState` é selado/verificado pelo `RequestStateBoundary` do SDK (AES-256-GCM
+ expiração + binding do request), então o servidor não guarda estado entre o
`input_required` e o retry: tudo o que precisa está dentro do token assinado.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, create_model

from mcp.server import MCPServer
from mcp.server.mcpserver import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
    Elicit,
    ElicitationResult,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.request_state import RequestStateSecurity

# ---------------------------------------------------------------------------
# Dados do domínio (entregues pelo starter; não alterar).
# ---------------------------------------------------------------------------
RAIZ = Path(__file__).resolve().parent.parent
DADOS = RAIZ / "dados"

with (DADOS / "salas.json").open("r", encoding="utf-8") as f:
    SALAS = json.load(f)

with (DADOS / "reservas.json").open("r", encoding="utf-8") as f:
    RESERVAS = json.load(f)

POLITICA_CONTEUDO = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO = POLITICA_CONTEUDO.splitlines()[0].split(":", 1)[1].strip()

SALAS_POR_ID = {s["id"]: s for s in SALAS}

FUSO = timezone(timedelta(hours=-3))
JANELA_INICIO = time(8, 0)
JANELA_FIM = time(20, 0)

ERRO_SALA = "Sala inexistente: {sala}"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"

MENSAGEM_ELICITACAO = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."


# ---------------------------------------------------------------------------
# Modelos de saída (estruturada) e a regra de negócio.
# ---------------------------------------------------------------------------
class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


class Escolha(BaseModel):
    """Valor devolvido pelo resolver quando não há elicitação (sala mantida)."""

    sala: str


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(FUSO)


def _sobrepoe(reserva: dict, ini: datetime, fim: datetime) -> bool:
    r_ini = _parse(reserva["inicio"])
    r_fim = _parse(reserva["fim"])
    return ini < r_fim and fim > r_ini


def _validar_sala_intervalo(sala: str, inicio: str, fim: str) -> None:
    if sala not in SALAS_POR_ID:
        raise ToolError(ERRO_SALA.format(sala=sala))
    ini = _parse(inicio)
    fim = _parse(fim)
    if fim <= ini:
        raise ToolError(ERRO_INTERVALO)
    ini_t = ini.time().replace(tzinfo=None)
    fim_t = fim.time().replace(tzinfo=None)
    if not (JANELA_INICIO <= ini_t <= JANELA_FIM and JANELA_INICIO <= fim_t <= JANELA_FIM):
        raise ToolError(ERRO_JANELA)
    if fim - ini > timedelta(hours=2):
        raise ToolError(ERRO_DURACAO)


def _alternativas(sala: str, inicio: str, fim: str) -> list[str]:
    """Salas livres no intervalo com capacidade >= a da sala pedida (máx. 3).

    Ordenadas por capacidade crescente e, em empate, por id alfabético.
    """
    capacidade = SALAS_POR_ID[sala]["capacidade"]
    ini = _parse(inicio)
    fim = _parse(fim)
    livres: list[str] = []
    for sid, s in SALAS_POR_ID.items():
        if sid == sala or s["capacidade"] < capacidade:
            continue
        if any(_sobrepoe(r, ini, fim) for r in RESERVAS if r["sala"] == sid):
            continue
        livres.append(sid)
    livres.sort(key=lambda sid: (SALAS_POR_ID[sid]["capacidade"], sid))
    return livres[:3]


def _proximo_id() -> str:
    return f"res-{len(RESERVAS) + 1:04d}"


def escolher_sala(sala: str, inicio: str, fim: str):
    """Resolver do parâmetro `escolha` da tool `reservar_sala`.

    Só pede elicitação quando o pedido é válido, conflita e há alternativas.
    Caso contrário devolve a sala original e deixa o corpo da tool validar/
    concluir (ou emitir o erro de execução correto).
    """
    try:
        _validar_sala_intervalo(sala, inicio, fim)
    except ToolError:
        return Escolha(sala=sala)
    ini = _parse(inicio)
    fim_dt = _parse(fim)
    conflita = any(_sobrepoe(r, ini, fim_dt) for r in RESERVAS if r["sala"] == sala)
    if not conflita:
        return Escolha(sala=sala)
    alts = _alternativas(sala, inicio, fim)
    if not alts:
        return Escolha(sala=sala)
    modelo = create_model("Escolha", sala=(Literal[tuple(alts)], ...))
    return Elicit(MENSAGEM_ELICITACAO, modelo)


# ---------------------------------------------------------------------------
# O servidor.
# ---------------------------------------------------------------------------
def _criar_servidor() -> MCPServer:
    secret = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(secret.encode()) < 32:
        print(
            "ERRO: defina REQUEST_STATE_SECRET com pelo menos 32 bytes.\n"
            "  Gere com: python3 -c \"import secrets; print(secrets.token_hex(32))\"\n"
            "  E exporte: export REQUEST_STATE_SECRET=<valor>",
            file=sys.stderr,
        )
        sys.exit(1)

    server = MCPServer(
        name="central-de-salas",
        version="1.0.0",
        request_state_security=RequestStateSecurity(keys=[secret], ttl=600),
    )

    @server.tool(description="Lista todas as salas com capacidade e recursos.")
    def listar_salas() -> ListaDeSalas:
        return ListaDeSalas(salas=[SalaOut(**s) for s in SALAS])

    @server.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
    def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
        _validar_sala_intervalo(sala, inicio, fim)
        ini = _parse(inicio)
        fim = _parse(fim)
        conflitos = [r for r in RESERVAS if r["sala"] == sala and _sobrepoe(r, ini, fim)]
        return Disponibilidade(
            sala=sala,
            livre=not conflitos,
            conflitos=[
                ConflitoOut(id=r["id"], inicio=r["inicio"], fim=r["fim"], responsavel=r["responsavel"])
                for r in conflitos
            ],
        )

    @server.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
    def reservar_sala(
        sala: str,
        inicio: str,
        fim: str,
        responsavel: str,
        escolha: Annotated[ElicitationResult[Escolha], Resolve(escolher_sala)],
    ) -> ReservaOut:
        _validar_sala_intervalo(sala, inicio, fim)

        if isinstance(escolha, (DeclinedElicitation, CancelledElicitation)):
            return ReservaOut(reservado=False, motivo="recusado")

        sala_final = escolha.data.sala if isinstance(escolha, AcceptedElicitation) else sala
        ini = _parse(inicio)
        fim_dt = _parse(fim)
        if any(_sobrepoe(r, ini, fim_dt) for r in RESERVAS if r["sala"] == sala_final):
            raise ToolError(ERRO_SEM_ALTERNATIVA)

        res_id = _proximo_id()
        RESERVAS.append(
            {"id": res_id, "sala": sala_final, "inicio": inicio, "fim": fim, "responsavel": responsavel}
        )
        return ReservaOut(
            reserva=res_id,
            reservado=True,
            sala=sala_final,
            inicio=inicio,
            fim=fim,
            responsavel=responsavel,
            politica=POLITICA_VERSAO,
            motivo=None,
        )

    @server.resource("politica://uso", name="Politica de uso", mime_type="text/markdown")
    def politica_uso() -> str:
        return POLITICA_CONTEUDO

    async def registrar_requests(ctx, call_next):
        meta = ctx.meta or {}
        traceparent = meta.get("traceparent")
        print(
            f"MCP {ctx.method} id={ctx.request_id} traceparent={traceparent}",
            file=sys.stderr,
            flush=True,
        )
        return await call_next(ctx)

    server.middleware.append(registrar_requests)
    return server


def main() -> None:
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "7301"))
    path = os.environ.get("MCP_PATH", "/mcp")
    server = _criar_servidor()
    server.run(
        transport="streamable-http",
        host=host,
        port=port,
        streamable_http_path=path,
        json_response=True,
    )


if __name__ == "__main__":
    main()
