# A Ponte: um agente A2A com MCP por dentro

*Onde termina a profundidade e começa o alcance*

**Projeto:** MBA Engenharia de Software com IA, curso de MCP e A2A

Dois processos separados, na fronteira que o curso desenha:

- **`servidor-mcp/`** — um servidor MCP em Streamable HTTP que expõe as salas de reunião da Hill Valley Tech como ferramentas (`listar_salas`, `consultar_disponibilidade`, `reservar_sala`) e a política de uso como resource (`politica://uso`).
- **`agente/`** — um agente que é **host MCP por dentro** (descobre as ferramentas via `tools/list`, lê o resource e chama `reservar_sala` por HTTP) e **servidor A2A por fora** (publica o Agent Card e atende `SendMessage`/`GetTask`).

O desafio é a **ponte**: quando o servidor MCP responde `input_required` pedindo a escolha de uma sala alternativa, o agente pausa a Task em `TASK_STATE_INPUT_REQUIRED`, guarda o `requestState` (opaco) ligado àquela Task e devolve a pergunta ao cliente A2A. Quando a resposta chega, ele repete o `tools/call` com um id JSON-RPC novo, `inputResponses` e o `requestState` ecoado.

## Como rodar

Requisitos: Python 3.10+ e `uv` (ou `pip`). A partir de um clone limpo:

```bash
# 1. Instalar as dependências (Python 3.10+, travadas em requirements.txt/pyproject.toml)
uv venv --python 3.10 .venv
uv pip install -r requirements.txt
# alternativa sem uv:
#   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. Gerar e exportar a chave de integridade do requestState (nunca hardcode; mínimo 32 bytes)
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

# 3. Subir o servidor MCP (porta 7301)
.venv/bin/python servidor-mcp/servidor.py

# 4. Em outro terminal, subir o agente A2A (porta 7300)
export REQUEST_STATE_SECRET=<o mesmo valor usado no passo 2>
.venv/bin/python agente/agente.py

# 5. Em um terceiro terminal, rodar o validador
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Portas e endpoints podem ser sobrescritos por variável de ambiente (padrões entre parênteses): `MCP_HOST` (`127.0.0.1`), `MCP_PORT` (`7301`), `MCP_PATH` (`/mcp`), `AGENTE_HOST` (`127.0.0.1`), `AGENTE_PORT` (`7300`), `MCP_BASE` (`http://127.0.0.1:7301`).

Rode o validador **sempre com os dois processos recém-iniciados**: as reservas criadas por uma execução mudam o resultado da seguinte.

## Onde a ponte acontece

Tudo acontece no agente (`agente/agente.py`), que traduz protocolo — nunca domínio.

- **A pausa** está em `_concluir_ou_pausar`: quando o `tools/call` volta com `resultType == "input_required"`, o agente lê o `enum` do `requestedSchema` da elicitation, guarda o `requestState` (e a chave do `inputRequests`) no dict da Task, põe a Task em `TASK_STATE_INPUT_REQUIRED` e devolve a mensagem `alternativas: <ids>` — sem abrir nem interpretar o estado.
- **A retomada** está em `_processar_escolha`: um `SendMessage` de continuação com `escolha=<valor>` faz um **novo** `tools/call` (id JSON-RPC novo), levando `inputResponses` com a mesma chave e o `requestState` ecoado byte a byte. `escolha=recusar` vira `action: "decline"` e a Task termina em `TASK_STATE_CANCELED`.

No lado do servidor (`servidor-mcp/servidor.py`), a elicitation nasce no resolver `escolher_sala`: quando o intervalo conflita e há alternativas, ele devolve `Elicit(mensagem, modelo)`; o SDK do MCP transforma isso no `input_required` com a elicitation em form mode e o `requestState` selado.

## Decisões técnicas

- **Proteção do `requestState`**: o SDK do MCP (`mcp==2.3.0`) instala o middleware `RequestStateBoundary`, que sela e verifica cada token com AES-256-GCM (HKDF-SHA256) sob a chave de `REQUEST_STATE_SECRET`. O envelope assina `iat`, `exp` e o binding do request (método + argumentos + audience), então uma adulteração ou expiração é rejeitada com `-32602`. A chave vem só do ambiente, nunca do código.
- **Validade do `requestState`**: 600 segundos (10 minutos), dentro da faixa de 5–30 minutos exigida. O servidor **não** guarda nada em memória entre o `input_required` e o retry — todo o pedido original é reconstruído a partir do próprio token, por isso o retry funciona mesmo depois de reiniciar o processo.
- **Estado das Tasks**: em memória, num `dict` no processo do agente, indexado por `taskId` (protegido por `RLock`). Cada Task guarda seu próprio `requestState`, `input_key` e lista de alternativas — duas Tasks pausadas ao mesmo tempo não trocam estado.
- **Sem LLM**: o agente interpreta o pedido em formato fixo (`reservar sala=... inicio=... fim=... responsavel=...`) por regex e decide por regra. Mesmo pedido, mesmo resultado — determinístico.
- **Capability de elicitação**: todo request MCP emitido pelo agente declara `io.modelcontextprotocol/clientCapabilities.elicitation.form = {}` no `_meta`. O validador confirma que um pedido de conflito **sem** essa capability recebe `-32021` com HTTP 400.
- **`traceparent`**: o agente lê o header `traceparent` da chamada A2A, preserva o trace-id e gera um span-id novo em cada request MCP daquela Task. O servidor MCP registra método, id e `traceparent` de cada request no stderr.

## Saída do validador

```text
trace-id desta execucao: bb9126cda0836b1c15f7e8485e3a2a0c
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```

## Estrutura

```
.
├── README.md              (este arquivo)
├── pyproject.toml         (dependências travadas)
├── requirements.txt       (mcp==2.3.0)
├── dados/                 (não alterar — salas, reservas, política)
├── validador/             (não alterar — validar.py)
├── exemplos/              (não alterar — wire contracts)
├── servidor-mcp/
│   └── servidor.py        (servidor MCP em Streamable HTTP)
└── agente/
    └── agente.py          (agente A2A + host MCP)
```
