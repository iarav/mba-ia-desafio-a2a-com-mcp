"""Agente A2A da Hill Valley Tech (desafio "A Ponte").

Por dentro, este processo é um host MCP: descobre as ferramentas via
`tools/list`, lê o resource `politica://uso` e chama `reservar_sala` — sempre por
HTTP, como um cliente MCP de verdade. Ele NÃO implementa regra de negócio: a
decisão de conflito/política/alternativas é do servidor MCP.

Por fora, é um servidor A2A v1.0 (JSON-RPC 2.0 sobre HTTP): publica o Agent Card
em `/.well-known/agent-card.json` e atende `SendMessage`/`GetTask` em `/a2a`.

A ponte está aqui: quando o servidor MCP responde `input_required`, o agente
pausa a Task em `TASK_STATE_INPUT_REQUIRED`, guarda o `requestState` (opaco)
associado àquela Task e devolve a lista de alternativas. Quando chega o
`escolha=...`, ele repete o `tools/call` com um id JSON-RPC novo, `inputResponses`
e o `requestState` ecoado — sem jamais abrir nem interpretar o estado.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import secrets
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Configuração (portas e endpoints; valores padrão do enunciado).
# ---------------------------------------------------------------------------
AGENTE_HOST = os.environ.get("AGENTE_HOST", "127.0.0.1")
AGENTE_PORT = int(os.environ.get("AGENTE_PORT", "7300"))
MCP_BASE = os.environ.get("MCP_BASE", "http://127.0.0.1:7301")
MCP_PATH = os.environ.get("MCP_PATH", "/mcp")

PROTOCOLO_MCP = "2026-07-28"
CAP_ELICITATION = {"elicitation": {"form": {}}}


def _trace_parent(trace_id: str, span_id: str) -> str:
    return f"00-{trace_id}-{span_id}-01"


def _novo_trace_id() -> str:
    return secrets.token_hex(16)


def _novo_span_id() -> str:
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------
# Contadores para ids determinísticos e únicos por processo (sem LLM, sem
# aleatoriedade no resultado).
# ---------------------------------------------------------------------------
_seq = itertools.count(1)


def _proximo(prefixo: str) -> str:
    return f"{prefixo}-{next(_seq):012x}"


# ---------------------------------------------------------------------------
# Cliente MCP (HTTP cru).
# ---------------------------------------------------------------------------
def _mcp_request(method: str, params: dict, trace_id: str, name: str | None = None) -> dict:
    """Envia um request MCP moderno e devolve o corpo JSON-RPC já parseado.

    Sempre declara a capability de elicitation em form mode no `_meta` e propaga
    o `traceparent` com o trace-id informado (span-id novo por request).
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOLO_MCP,
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    meta = {
        "io.modelcontextprotocol/protocolVersion": PROTOCOLO_MCP,
        "io.modelcontextprotocol/clientCapabilities": CAP_ELICITATION,
        "traceparent": _trace_parent(trace_id, _novo_span_id()),
    }
    body = {
        "jsonrpc": "2.0",
        "id": _proximo("id"),
        "method": method,
        "params": {**params, "_meta": meta},
    }
    req = urllib.request.Request(
        MCP_BASE + MCP_PATH,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8"))


# Cache da descoberta (tools/list + resources/read), feita uma única vez.
_descobriu = threading.Event()
_tools: list[str] = []
_politica_versao: str = ""


def _descobrir(trace_id: str) -> str:
    """Garante tools/list e resources/read antes da primeira chamada.

    Devolve a versão da política lida do resource.
    """
    global _tools, _politica_versao
    if not _descobriu.is_set():
        resp = _mcp_request("tools/list", {}, trace_id)
        tools = (resp.get("result") or {}).get("tools", [])
        _tools = [t.get("name") for t in tools if t.get("name")]
        resp = _mcp_request("resources/read", {"uri": "politica://uso"}, trace_id, name="politica://uso")
        contents = (resp.get("result") or {}).get("contents", [{}])
        texto = contents[0].get("text", "") if contents else ""
        primeira = texto.splitlines()[0] if texto else ""
        _politica_versao = primeira.split(":", 1)[1].strip() if ":" in primeira else ""
        _descobriu.set()
    return _politica_versao


# ---------------------------------------------------------------------------
# Armazenamento das Tasks (em memória, por processo).
# ---------------------------------------------------------------------------
_TASKS: dict[str, dict] = {}
_LOCK = threading.RLock()

_TERMINAIS = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED"}


def _msg(mensagem_id: str, role: str, texto: str, task_id: str | None = None, context_id: str | None = None) -> dict:
    m: dict = {"messageId": mensagem_id, "role": role, "parts": [{"text": texto}]}
    if task_id:
        m["taskId"] = task_id
    if context_id:
        m["contextId"] = context_id
    return m


def _task_para_resposta(task: dict) -> dict:
    return {
        "id": task["id"],
        "contextId": task["contextId"],
        "status": {
            "state": task["state"],
            "message": task["status_message"],
        },
        "history": list(task["history"]),
        "artifacts": list(task["artifacts"]),
    }


# ---------------------------------------------------------------------------
# O núcleo: interpreta o pedido fixo e costura a ponte.
# ---------------------------------------------------------------------------
def _processar_reserva(texto: str, trace_id: str) -> dict:
    """Abre uma Task nova, chama o servidor MCP e costura o input_required."""
    m = re.fullmatch(r"reservar\s+sala=(\S+)\s+inicio=(\S+)\s+fim=(\S+)\s+responsavel=(\S+)", texto.strip())
    if not m:
        raise _ProtocoloErro("Formato de pedido invalido")

    sala, inicio, fim, responsavel = m.groups()

    task_id = _proximo("task")
    context_id = _proximo("ctx")
    politica_versao = _descobrir(trace_id)

    task: dict = {
        "id": task_id,
        "contextId": context_id,
        "state": "TASK_STATE_WORKING",
        "status_message": _msg(_proximo("msg"), "ROLE_AGENT", "", task_id, context_id),
        "history": [_msg(_proximo("msg"), "ROLE_USER", texto.strip())],
        "artifacts": [],
        "trace_id": trace_id,
        # Estado da ponte (preenchido quando há pausa):
        "request_state": None,
        "input_key": None,
        "alternativas": [],
        "argumentos": {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel},
    }

    resposta = _mcp_request(
        "tools/call",
        {"name": "reservar_sala", "arguments": task["argumentos"]},
        trace_id,
        name="reservar_sala",
    )
    _concluir_ou_pausar(task, resposta, politica_versao)
    return task


def _concluir_ou_pausar(task: dict, resposta: dict, politica_versao: str) -> None:
    """Aplica o resultado de um tools/call `reservar_sala` à Task.

    - erro de execução (isError)          -> TASK_STATE_FAILED
    - input_required                       -> pausa a Task (guarda requestState)
    - complete (reservado ou recusado)     -> terminal
    """
    if resposta.get("error"):
        raise _ProtocoloErro(resposta["error"].get("message", "erro do servidor MCP"))

    result = resposta.get("result") or {}

    if result.get("isError"):
        texto = " ".join(p.get("text", "") for p in result.get("content", []))
        _terminar(task, "TASK_STATE_FAILED", texto)
        return

    if result.get("resultType") == "input_required":
        input_requests = result.get("inputRequests") or {}
        chave = next(iter(input_requests), None)
        params_pedido = (input_requests.get(chave) or {}).get("params", {})
        schema = params_pedido.get("requestedSchema") or {}
        campo = (schema.get("properties") or {}).get("sala", {})
        alts = campo.get("enum") or ([campo["const"]] if "const" in campo else [])

        task["state"] = "TASK_STATE_INPUT_REQUIRED"
        task["request_state"] = result.get("requestState")
        task["input_key"] = chave
        task["alternativas"] = alts
        linha = "alternativas: " + ", ".join(alts)
        task["status_message"] = _msg(
            _proximo("msg"), "ROLE_AGENT", linha, task["id"], task["contextId"]
        )
        task["history"].append(task["status_message"])
        return

    # complete
    dados = result.get("structuredContent") or {}
    if dados.get("reservado"):
        _concluir_reserva(task, dados, politica_versao)
    else:
        _terminar(task, "TASK_STATE_CANCELED", "Reserva recusada.")


def _concluir_reserva(task: dict, dados: dict, politica_versao: str) -> None:
    artefato = {
        "reserva": dados.get("reserva"),
        "sala": dados.get("sala"),
        "inicio": dados.get("inicio"),
        "fim": dados.get("fim"),
        "responsavel": dados.get("responsavel"),
        "politica": politica_versao,
    }
    task["artifacts"].append(
        {
            "artifactId": _proximo("art"),
            "name": "reserva",
            "parts": [{"text": json.dumps(artefato)}],
        }
    )
    task["state"] = "TASK_STATE_COMPLETED"
    texto = f"Reserva {dados.get('reserva')} confirmada na {dados.get('sala')}."
    task["status_message"] = _msg(_proximo("msg"), "ROLE_AGENT", texto, task["id"], task["contextId"])
    task["history"].append(task["status_message"])


def _terminar(task: dict, estado: str, texto: str) -> None:
    task["state"] = estado
    task["status_message"] = _msg(_proximo("msg"), "ROLE_AGENT", texto, task["id"], task["contextId"])
    task["history"].append(task["status_message"])


def _processar_escolha(texto: str, task_id: str) -> dict:
    m = re.fullmatch(r"escolha=(\S+)", texto.strip())
    if not m:
        raise _ProtocoloErro("Formato de escolha invalido")
    escolha = m.group(1)

    with _LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            raise _ProtocoloErro("Task nao encontrada")
        if task["state"] in _TERMINAIS:
            raise _ProtocoloErro("Task em estado terminal")

        if task["state"] == "TASK_STATE_INPUT_REQUIRED":
            trace_id = task["trace_id"]
            if escolha == "recusar":
                resposta = _mcp_request(
                    "tools/call",
                    {
                        "name": "reservar_sala",
                        "arguments": task["argumentos"],
                        "inputResponses": {task["input_key"]: {"action": "decline"}},
                        "requestState": task["request_state"],
                    },
                    trace_id,
                    name="reservar_sala",
                )
                result = resposta.get("result") or {}
                if result.get("resultType") == "complete" and not result.get("isError"):
                    _terminar(task, "TASK_STATE_CANCELED", "Reserva recusada.")
                else:
                    _terminar(task, "TASK_STATE_FAILED", "Falha ao processar a recusa.")
                return task

            if escolha in task["alternativas"]:
                resposta = _mcp_request(
                    "tools/call",
                    {
                        "name": "reservar_sala",
                        "arguments": task["argumentos"],
                        "inputResponses": {
                            task["input_key"]: {"action": "accept", "content": {"sala": escolha}}
                        },
                        "requestState": task["request_state"],
                    },
                    trace_id,
                    name="reservar_sala",
                )
                _concluir_ou_pausar(task, resposta, _descobrir(trace_id))
                return task

            # Escolha fora do enum: mantém pausada e repete as alternativas.
            linha = "alternativas: " + ", ".join(task["alternativas"])
            task["status_message"] = _msg(
                _proximo("msg"), "ROLE_AGENT", linha, task["id"], task["contextId"]
            )
            task["history"].append(task["status_message"])
            return task

        raise _ProtocoloErro("Task nao esta aguardando escolha")


class _ProtocoloErro(Exception):
    pass


# ---------------------------------------------------------------------------
# Servidor A2A (HTTP + JSON-RPC).
# ---------------------------------------------------------------------------
_CARD = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [
        {
            "url": f"http://{AGENTE_HOST}:{AGENTE_PORT}/a2a",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }
    ],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Reservar sala",
            "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            "tags": ["salas", "agenda"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
            "examples": [
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        }
    ],
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silencia o log padrão de acesso
        return

    def _json(self, payload: dict, status: int = 200) -> None:
        corpo = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def _rpc_error(self, request_id, code: int, message: str) -> None:
        self._json({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def do_GET(self) -> None:
        if self.path == "/.well-known/agent-card.json":
            self._json(_CARD)
        else:
            self._json({"erro": "nao encontrado"}, 404)

    def do_POST(self) -> None:
        if self.path != "/a2a":
            self._json({"erro": "nao encontrado"}, 404)
            return

        tamanho = int(self.headers.get("Content-Length", "0"))
        corpo = self.rfile.read(tamanho).decode("utf-8")
        try:
            payload = json.loads(corpo)
        except json.JSONDecodeError:
            self._rpc_error(None, -32700, "Parse error")
            return

        request_id = payload.get("id")
        metodo = payload.get("method")
        params = payload.get("params") or {}

        traceparent = self.headers.get("traceparent")
        trace_id = traceparent.split("-")[1] if traceparent and len(traceparent.split("-")) >= 2 else _novo_trace_id()

        try:
            if metodo == "SendMessage":
                mensagem = params.get("message") or {}
                texto = " ".join(p.get("text", "") for p in mensagem.get("parts", []))
                task_id = mensagem.get("taskId")

                with _LOCK:
                    if task_id:
                        task = _processar_escolha(texto, task_id)
                    else:
                        task = _processar_reserva(texto, trace_id)
                        _TASKS[task["id"]] = task
                    self._json({"jsonrpc": "2.0", "id": request_id, "result": {"task": _task_para_resposta(task)}})

            elif metodo == "GetTask":
                task_id = params.get("id")
                with _LOCK:
                    task = _TASKS.get(task_id)
                if task is None:
                    self._rpc_error(request_id, -32602, "Task nao encontrada")
                else:
                    self._json({"jsonrpc": "2.0", "id": request_id, "result": {"task": _task_para_resposta(task)}})

            else:
                self._rpc_error(request_id, -32601, "Method not found")

        except _ProtocoloErro as exc:
            self._rpc_error(request_id, -32000, str(exc))


def main() -> None:
    server = ThreadingHTTPServer((AGENTE_HOST, AGENTE_PORT), _Handler)
    print(f"Agente A2A em http://{AGENTE_HOST}:{AGENTE_PORT}/a2a", file=sys.stderr, flush=True)
    print(
        f"Agent Card em http://{AGENTE_HOST}:{AGENTE_PORT}/.well-known/agent-card.json",
        file=sys.stderr,
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
