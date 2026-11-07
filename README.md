# signaldeck-plugin-ai

SignalDeck integration for **MCP** (data access via ValueProvider) and a
generic **AG-UI 1.0 chat interface**.

The Python distribution is named `signaldeck-plugin-ai` (`pip install .`);
the import package and SignalDeck processor paths remain `signaldeck_ai`.
If an older `signaldeck-ai` distribution was installed, uninstall that
legacy name before reinstalling to avoid two distributions owning the
same import files: `python -m pip uninstall signaldeck-ai`.

## Docker

This uses the same container layout as `signaldeck-plugin-socketio`:
`Dockerfile`, `docker-compose.yml`, `config/`, `plugins/` and logging.
The demo Docker image tag `signaldeck-ai` is independent of the Python
package name.
The image is derived from locally available `signaldeck:dev` (including `signaldeck-plugin-main`).

```sh
docker compose up --build
```

The Compose service uses `network_mode: host` on Linux. This permits
SignalDeck to reach `cli-agent-api` at `127.0.0.1:8765` despite that
API's deliberate loopback-only binding. The SignalDeck website still
runs on its standard port 5000. The interactive demo uses `signaldeck-plugin-main` DummyData to publish three mocked temperatures (Garten, Wohnzimmer, Keller), half-hourly synthetic temperature time series and an opt-in action changing **only** the dummy garden temperature. The MCP example listens at
`127.0.0.1:8766/mcp`. Host networking does not provide network
isolation, and MCP must not be exposed to untrusted networks.

## Local demo / integration setup

The repository root `config/ai.json` defines an operational three-sensor DummyData setup and `config/group_ai.json` provides the DummyData controls plus MCP status and AG-UI chat. `docker-compose.yml` mounts `./config` at `/config`. The Dockerfile installs the AI plugin, the merged Socket.IO plugin from `main`, and their runtime dependencies; the main plugin is already present in the `signaldeck:dev` base image. Neither `config/` nor `tests/` is packaged in `signaldeck-plugin-ai` wheels. The CI builds a wheel and inspects its members to enforce this.

- **Garten:** 18.5 °C, synthetic trend of +0.35 °C per day, with 2.0 °C daily variation.
- **Wohnzimmer:** 21.3 °C, synthetic trend of −0.08 °C per day, with 0.5 °C daily variation.
- **Keller:** 17.8 °C, synthetic trend of +0.03 °C per day, with 0.2 °C daily variation.
- **Test action:** `set_temp_garten` changes only the simulated garden temperature, through the DummyData processor. No real hardware connection is configured.

Use `get_values({"fields": ["temp_garten"]})` or `execute_action({"name": "set_temp_garten", "arguments": {"temp_garten": 23.5}})` from a connected MCP client. Action calls can also exercise cli-agent approvals without affecting real devices.

The external Main plugin DummyData generates synthetic, timezone-naive histories. The new absolute-time MCP API deliberately rejects these ambiguous timestamps; to exercise `get_history` in the demo, use a provider returning timezone-aware measurement indices. With real persisted SignalDeck histories, the full result can be saved to a cli-agent session file with `workspace_path` and `records_path: "/points"` for Data MCP processing.

While `signaldeck-plugin-main` [PR #7](https://github.com/signaldeck/signaldeck-plugin-main/pull/7) is under review, the demo Dockerfile and CI install its `feature/dummy-history-series` branch. **After that PR is merged**, switch the dependency back to the main release branch. These demo dependencies are not bundled in the AI plugin wheel.

The integration tests under `tests/test_demo_config.py` use the **real** DummyData and SDK ValueProvider, rather than a test-only replica. They verify current values, historical reads, the mock write action, and MCP tool discovery. An actual live Docker/cli-agent/AG-UI test still needs to be carried out on the target environment.

## Socket.IO: private appointments (demo)

The demo SignalDeck config enables two rooms:

- `haus`: a **nonpersistent** chat/image room with the existing `@haus`
  AI bridge. No appointment object type is enabled.
- `privat`: a **persistent** chat/image room with the additional
  `appointment` object type. Appointments sync without an age limit
  (`sync_days: null`) and remain encrypted end-to-end when clients
  have an active room key. No AI bridge or AI prefix is configured for
  this room.

The Socket.IO processor uses a dedicated `sqlite_socketio` datastore.
The demo Compose setup mounts `./db:/app/db`, so records survive container
rebuilds and restarts. Create the host `db` directory with permissions
that allow the container user to write to it before starting Compose.

The configuration is in `config/ai.json` (not embedded in the Python
plugin wheel). The demo Dockerfile installs both
`signaldeck-plugin-sqlite` and `signaldeck-plugin-socketio`; using a
previously built image without these packages will not work.

In the Android client, subscribe to the room **privat**. The server's
`join_room` response advertises `appointment`, making the additional
**Termine** tab available. Unknown or unconfigured rooms remain
chat/image-only.

## Startup

1. Start SignalDeck; its MCP servers start immediately, while the Chat
   DisplayProcessor remains unconnected.
2. Configure cli-agent's Streamable HTTP MCP server to
   `http://127.0.0.1:8766/mcp` and start `cli-agent-api`: `cli-agent-api --approve-tool signaldeck_climate__get_values --approve-tool signaldeck_climate__get_history --port 8765 --config config_openrouter.toml`
3. Open SignalDeck's Chat DisplayProcessor, enter the bearer token printed
   by `cli-agent-api` into the password field, then click **Verbinden**.
   SignalDeck requires no restart or token configuration file.
4. Use the chat; **Verlauf zurücksetzen** discards the conversation and\n   outstanding approvals, starts a fresh agent thread and keeps the token\n   connection active. A confirmation is required. The former thread is\n   deleted from cli-agent on a best-effort basis. **Trennen** additionally\n   clears the server-side token and closes the associated agent threads.

Example cli-agent config:

```toml
[[mcp_servers]]
name = "signaldeck-climate"
transport = "streamable_http"
url = "http://127.0.0.1:8766/mcp"
```

## Demo: LLM via Script View

The developer config includes a `ScriptProcessor` (from
`signaldeck-plugin-main`) and an **LLM-Skript testen** View preselecting
`demo_llm`. Its single editable `prompt` string variable is interpolated
into the command:

```text
LLM $prompt$
```

Start `cli-agent-api`, open either **Agenten-Chat Energie** or
**Agenten-Chat Haus** and enter its bearer token once using **Verbinden**.
Then open **LLM-Skript testen**, change the prompt as desired and start
the script. The answer appears in the Script View's command output
(refresh the view if necessary). Before the Chat UI has authenticated
the AI processor, the LLM command is expected to fail. The script is
only a developer test; it does not modify real devices.

## Optional Socket.IO @haus one-shot bridge (v2)

The AI processor can optionally listen for **pre-filtered inbound** Socket.IO
events on the SignalDeck MessageBus. The Socket.IO plugin's
`incoming_message_routes` publishes only configured text commands after
normal client validation; the AI plugin has no direct Socket.IO dependency.

```json
{
  "socketio_bridge": {
    "routes": [
      {"room": "haus", "source": "socketio.ai",
       "response_source": "ai.socketio.haus"}
    ],
    "max_queue": 5,
    "timeout_seconds": 120
  }
}
```

This configuration belongs in the `AGUIChatProcessor.config`. The Socket.IO
plugin must configure its corresponding inbound route:

```json
{"incoming_message_routes": [
  {"rooms": ["haus"], "type": "chat",
   "prefix": "@haus", "source": "socketio.ai"}
 ],
 "message_routes": [
  {"source": "ai.socketio.haus", "rooms": ["haus"]}
 ]}
```

An incoming cleartext message `@haus Wie warm ist es?` is transformed by
the Socket.IO plugin into a MessageBus message containing only
`Wie warm ist es?`, its originating room, sender information and event ID.

The AI bridge queues the request on a dedicated worker (never blocking
Socket.IO), borrows the API token from an authenticated Chat view of
**this** AI processor, and runs a fresh one-shot AG-UI thread via the same
transport as the existing `LLM` Cmd. Each thread is deleted in
`finally` after the response or error; no room conversation history or
additional permanent cli-agent session is created. Only one request
executes at once; subsequent requests are bounded by `max_queue`. Duplicate
room/event IDs are ignored. Replies and safe error messages are published
with the configured `response_source` and routed by Socket.IO into the
original room. The bridge unsubscribes on shutdown.

Socket.IO encryption remains end-to-end and opaque: encrypted payloads
without a matching cleartext `@haus` prefix never reach this bridge.
Do not provide room keys to SignalDeck or cli-agent.

The development Docker image now installs the merged Socket.IO plugin
alongside the AI plugin. `config/ai.json` configures a non-persistent
Socket.IO room `haus` and both directions of message routing; the
`config/group_ai.json` group shows **Socket.IO – Übersicht** and
**Socket.IO – Haus**. The latter is a *server administration view*, not
a client chat interface. Messages sent using its server-side Send button
do not trigger `incoming_message_routes`: those only run for
authenticated Socket.IO **client** `send_event` calls.

To exercise the complete integration:

1. Start `cli-agent-api` (port 8765) with access to the SignalDeck demo
   MCP server at `http://127.0.0.1:8766/mcp`.
2. Run `docker compose up --build` in this repository.
3. Open **Agenten-Chat Energie** or **Agenten-Chat Haus** in SignalDeck,
   enter the cli-agent bearer token and press **Verbinden**.
4. From your host terminal, install the demo test client's dependency
   (`python -m pip install "python-socketio[client]>=5.12,<6"`)
   and run:

   ```sh
   python scripts/test_socketio_ai.py "@haus Wie warm ist es im Garten?"
   ```

The test client connects to `http://127.0.0.1:5000`, joins the
un-encrypted `haus` room, sends a genuine client-originated
`send_event`, and prints the assistant reply. Use `--url`, `--room`
or `--timeout` to adjust target and wait time. Without a prior Chat UI
login, the bridge returns an authentication-related message. Normal
messages without `@haus` are not forwarded to the AI bridge and will
not generate a response.

Interactive tool approvals are not granted automatically. Limit the
agent's configured tools to suitably trusted room participants; the
Socket.IO client's claimed `id` is not strongly authenticated. The
demo's permissive Socket.IO CORS setting and `network_mode: host`
are for trusted development networks, not public deployment.

## Optional one-shot LLM Cmd

Each `AGUIChatProcessor` can register a separate SignalDeck command.
The `Cmd` registry is global, so names **must be unique** and a collision
is rejected instead of silently replacing another command. Example processor
configuration:

```json
{
  "name": "agui",
  "class": "signaldeck_ai.chat_processor.AGUIChatProcessor",
  "config": {
    "endpoint": "http://127.0.0.1:8765/ag-ui",
    "cmd": {"name": "LLM", "timeout_seconds": 120}
  }
}
```

Invoke from a SignalDeck Cmd script:

```text
LLM Wie hoch war gestern die PV-Produktion?
```

All trailing Cmd arguments become one prompt. The command awaits the
response without blocking SignalDeck's asyncio event loop and stores the
final answer in `CmdResult.state` under the command's `msg` field.

**No token environment variable or persisted API key.** The command works
*only after* a user successfully connects at least one of **this same
processor instance's** Chat UI views to cli-agent. It borrows the API token
from the existing server-side `ChatRuntime` at the time of the invocation.
Different processor instances are not interchangeable. If nobody logged in,
the invocation fails before sending any request. Disconnecting the final
Chat UI view revokes availability until the next manual login. If several
views of the same instance are connected with **different API tokens**, the
command fails rather than arbitrarily picking one. A history reset does
not revoke the token.

Each invocation creates a **fresh cli-agent thread**, with one user prompt
and no previous Chat UI history or approvals. The AG-UI SSE response is
consumed to completion; interactive approval interrupts and frontend tool
handoffs fail rather than being auto-approved. An attempted
`DELETE /threads/{threadId}` in a `finally` block closes the temporary
MCP-owning cli-agent session on success, failure or timeout. Deletion
errors are logged. The command times out after 120 seconds by default,
configurable from 1 to 600 seconds. Commands have no shared conversation
history across invocations.

The demo registers `LLM` on its existing `agui` processor. This is a
server-side feature: an open browser is **not** required once a chat view
has connected. The connection is held in memory until explicitly
disconnected or SignalDeck is restarted.

**Current SDK logging caveat:** the central SignalDeck `Cmd._run_single`
logs whole command lines before and after execution. Prompts included in
`LLM` Cmd invocations are therefore visible in SignalDeck command logs.
Do not pass secrets as prompt arguments. The command never logs the
authentication token.

## ValueProvider mappings and MCP publication

The processor deliberately separates SignalDeck data resolution from MCP
publication:

- `config.values` and `config.methods` use **unmodified** SignalDeck SDK
  configuration. Their `name` keys establish local names and their `from`
  keys reference exports from other processors.
- `config.mcp_data.values` chooses permitted local values by `field`.
- `config.mcp_data.data_rows` chooses permitted local methods by `func`.
- `description` is defined **only** in `mcp_data`. A configured value or
  method is **not** automatically exposed through MCP.

Example of an MCP processor's `config`:

```json
{
  "mcp": {
    "name": "house-climate",
    "host": "127.0.0.1",
    "port": 8766,
    "instructions": "Dieser MCP-Server stellt aktuelle und historische Klimadaten bereit."
  },
  "values": [
    {"name": "temp_garten", "type": "field", "from": "temperature_garten"},
    {"name": "temp_intern", "type": "field", "from": "another_export"}
  ],
  "methods": [
    {"name": "hist_temp_garten", "from": "hist_temperature_garten"}
  ],
  "mcp_data": {
    "values": [
      {
        "field": "temp_garten",
        "description": "Aktuelle Temperatur im Garten",
        "unit": "°C"
      }
    ],
    "data_rows": [
      {
        "func": "hist_temp_garten",
        "description": "Temperaturverlauf im Garten",
        "max_days": 30
      }
    ]
  }
}
```

Here `temperature_garten` illustrates a ValueProvider export name; your
producing processor must actually publish that exact alias. The exported
historical method `hist_temperature_garten` occurs in the Ecowitt example.
`temp_intern` deliberately remains unavailable through MCP.

At startup, every `mcp_data.values[].field` must refer to a local
`config.values[].name`, and every `mcp_data.data_rows[].func` must refer
to a local `config.methods[].name`. Invalid entries fail configuration
validation rather than exposing other fields by accident.

Read calls resolve **only** allowlisted ValueProvider references and never
invoke unrelated entries while refreshing a processor.

### MCP instructions and selective current values

Set `config.mcp.instructions` to a string to supply MCP server-level
instructions. These are provided through the standard MCP initialization
metadata (not embedded in individual tool descriptions).

`get_values(fields)` accepts an optional list of **local MCP-published names**.
For example, `{"fields": ["temp_garten"]}` reads only the garden temperature.
Omitting `fields` reads all published values; `[]` reads none. Unknown
or unpublished names are rejected before reading any values. The tool
description enumerates the permitted fields and their descriptions. Tool
names remain stable, which preserves cli-agent approval matching.

### Historical data

The MCP tools require absolute ISO-8601 instants with an explicit UTC offset.
For `mode=per_day`, the adapter queries the existing `PersistData.hist` once
for every intersecting **UTC calendar day**, using aware UTC midnight,
`days=0`, and `fullDay=True`. It deduplicates the returned points, then filters
by the exact half-open interval. This works across midnight and DST changes,
regardless of the datastore's configured timezone. Numeric statistics are
computed over the filtered points; raw records can be returned using the
cli-agent metadata/file handoff. Individual history mappings restrict elapsed
duration via `max_days`.

For `mode=range`, an exported method receives aware UTC midnight as `date`
and the number of intersecting UTC calendar days as `days`. It must return
timestamped records; the adapter filters them into the exact requested window.

History entries always use ISO 8601 date/time strings (for example,
`2026-10-23T09:10:00+00:00`) rather than synthetic `day-0` labels.
Actual timestamps from a Pandas Series or timestamp-indexed mapping retain
their original UTC offsets. Naive, date-only, and inferred source timestamps
are rejected: no server timezone is silently assigned to them. The external
Main plugin's synthetic DummyData histories currently have naive timestamps
and therefore cannot be queried through this strict absolute-time interface
until that mock source supplies aware timestamps.


### Actions

Actions are optional and configured exclusively through
`config.mcp_data.actions`, with `func` pointing to a local
`config.methods[].name`. Each action needs a `description` and a JSON
Schema `parameters` object with `additionalProperties: false`.
Arguments are validated before calling the underlying ValueProvider
method. No method is published as an action implicitly. LLM-side user
approval remains important for real device changes.

### Named Cmd / script runs

`mcp_data.runs` explicitly publishes **named actions**, not arbitrary
commands, exported functions, or editable script content. Each alias maps
either to a registered SignalDeck Cmd command or to an existing script.
The LLM sees a single stable MCP tool `run(name, arguments?)` regardless
of how many actions are configured.

Example (inside the MCP processor's `config.mcp_data`):

```json
{
  "runs": [
    {
      "name": "Abend",
      "description": "Aktiviere den vorbereiteten Abendmodus.",
      "script": "abendmodus"
    },
    {
      "name": "Licht",
      "description": "Schalte eine ausdrücklich freigegebene Lichtzone.",
      "command": "licht",
      "args": [{"param": "zone"}, {"param": "state"}],
      "parameters": {
        "type": "object",
        "properties": {
          "zone": {"type": "string", "enum": ["wohnzimmer", "kueche"]},
          "state": {"type": "string", "enum": ["ein", "aus"]}
        },
        "required": ["zone", "state"],
        "additionalProperties": false
      }
    }
  ]
}
```

A Socket.IO message action can fix its destination room while allowing the
LLM to supply the message text. With the actual
`signaldeck-plugin-socketio` command **`socketio_send`**, configure:

```json
{
  "name": "send_haus",
  "description": "Sende eine Textnachricht an den Raum haus.",
  "command": "socketio_send",
  "args": ["haus", {"param": "msg"}],
  "parameters": {
    "type": "object",
    "properties": {
      "msg": {
        "type": "string",
        "description": "Eine kurze Nachricht an den Haus-Chat",
        "minLength": 1,
        "maxLength": 500
      }
    },
    "required": ["msg"],
    "additionalProperties": false
  }
}
```

Then the public call
`run({"name": "send_haus", "arguments": {"msg": "Hallo aus SignalDeck!"}})`
starts `socketio_send haus Hallo aus SignalDeck!`. The room name cannot be
changed by the LLM, and the Cmd name is never exposed as an argument.
This requires that the Socket.IO plugin is installed and its command
registered before the MCP processor is initialized. The AI demo does not
install Socket.IO, so its `Nachricht` action uses safe `echo` instead.

The example `licht` command and `abendmodus` script must be defined in
SignalDeck separately; neither is installed by this plugin. The actual
repository demo instead uses harmless built-in `echo` commands and scripts.

`run({"name": "Abend"})` starts the fixed script without any client-visible
script name. `run({"name":"Licht", "arguments":{"zone":"kueche","state":"aus"}})`
starts only the fixed command with these validated tokens.

Configuration rules:
- Public aliases are unique and must have a description.
- Exactly one of `command` or `script` is required per alias.
- A command uses a fixed registered Cmd command and positional `args`.
  Each argument is either a fixed literal or `{"param":"..." }`.
- A script uses a fixed existing script and optional `variables` mapping.
  Every passed variable must exist in its ScriptDefinition.
- Every exposed parameter must be required. By default, string
  parameters require explicitly enumerated **single safe tokens**.
  For actual natural-language text, opt in with `minLength` (optional,
  default 1) and `maxLength` (mandatory, 1–500). The text must be
  nonempty, printable, without control/format characters, newlines or
  leading/trailing whitespace. Such free text is accepted only as the
  **last argument of a Cmd command** that supports joining trailing words;
  free text is not allowed in script variables. Numeric arguments need
  finite bounds or an enum. Unexpected properties are forbidden.
  SignalDeck Cmd uses space-separated arguments rather than shell execution,
  so only commands designed for multiword text are appropriate.
- Cmd/script targets and their source strings never appear in the tool's
  public descriptions. The LLM cannot upload script text or submit arbitrary
  command lines.
- `run` returns `{"name":"Abend","status":"scheduled","run_id":"..."}`
  for named scripts (no `run_id` for single Cmd commands) when scheduling
  succeeds. Multiple instances of the same script may run concurrently.
  The ScriptProcessor displays their separate status, variables and output,
  and lets users stop an individual run. Scheduling does **not** imply that
  asynchronous execution succeeded; inspect the run's status for failures.
- The diagnostic UI marks `run` as a state-changing operation and requires
  explicit confirmation. cli-agent permission for the generic `run` tool
  covers **every** action in the server's configured run allowlist, so
  only group appropriately trusted actions on a given MCP server.

`execute_action` continues to provide an optional low-level
ValueProvider-method integration. For house automation, prefer `run`.

### Multiple MCP servers

Add another `MCPServerProcessor` with a distinct port and its own
value/method allowlist. Each instance publishes its own
`/mcp` endpoint and DisplayData status/tool list.

### MCP-Server-Oberfläche und Tool-Testkonsole

Der MCP-DisplayProcessor zeigt die vom **tatsächlichen FastMCP-Server**
registrierten Tools, ihre ausführlichen Beschreibungen und ihre
generierten JSON-Eingabe-/Ausgabeschemas. Die Ansicht ist kein separat
gepflegtes Toolverzeichnis: sie liest die FastMCP-Registry.

Jedes Tool lässt sich aus der SignalDeck-Oberfläche ausprobieren:

1. MCP-Server-Panel öffnen und ein Tool aufklappen.
2. Das JSON-Eingabefeld bearbeiten (für lesende Tools werden Beispiele
   aus der `mcp_data`-Konfiguration vorgeschlagen).
3. `Tool ausführen` anklicken und die serialisierte MCP-Rückgabe ansehen.

Alle Testaufrufe laufen über SignalDeck `POST /run` und den bestehenden
ActionDispatcher/DisplayProcessor, nicht über einen ungeschützten zweiten
HTTP-Endpunkt. Die Ausführung nutzt `FastMCP.call_tool` und damit die
**gleichen registrierten Handler und Eingabeschemas** wie ein externer
MCP-Client. Es wird kein cli-agent für das Testen benötigt.

`execute_action` ist als Schreibaktion markiert und verlangt ein
zusätzliches Bestätigungs-Kontrollkästchen. Die Bestätigung wird auch
serverseitig geprüft. **Wichtig:** Diese Bestätigung ersetzt keine
Authentifizierung oder Autorisierung. In produktiven Installationen
muss der SignalDeck-Webzugriff geschützt werden; die Testkonsole kann
alle konfigurierten MCP-Schreibaktionen ausführen. Der Demo-Prozessor
verändert ausschließlich Dummy-Werte.

## AG-UI

`AGUIChatProcessor` integrates the generic AG-UI 1.0 HTTP/SSE transport with
SignalDeck's existing action system. The browser sends `connect`, `send`,
`approve`, `reset_history` (`new_chat` remains a compatibility alias) and `disconnect` as normal SignalDeck `/run` actions,
dispatched through `AGUIChatProcessor.process()` and `performActions()`.
A background worker sends standard `RunAgentInput` requests to the agent.
**Only** server-to-browser SSE events have a separate, read-only route at
`GET /plugin/ai/api/chat/<processor>/events/<run_id>`; the browser sends its
opaque connection handle in the `X-SignalDeck-AI-Connection` header.
The client connects manually after startup. Because SignalDeck's inherited
`DisplayProcessor.process()` would rerender the chat element (destroying the
live event stream), this processor overrides `process()` solely to return
JSON action acknowledgments while leaving the DOM intact.

The cli-agent-specific `GET /ag-ui/capabilities` discovery URL and
`DELETE /threads/{threadId}` lifecycle URL are presently used for connection
validation and cleanup. Other AG-UI servers may need different discovery and
cleanup mechanisms.

### Gemeinsame Chats pro View-`value`

Der `AGUIChatProcessor` verwaltet **eine Session pro konfiguriertem
`value`**. Die Zuordnung ist ausschließlich serverseitig und gilt für die
jeweilige Processor-Instanz. Verschiedene Werte haben getrennte Agenten-Threads,
Nachrichten und ausstehende Tool-Freigaben. Identische Werte teilen sich
dieselbe Session, **auch in verschiedenen Browsern**. SignalDeck benötigt
hierfür keine Browser-/Benutzersessions.

Beim Öffnen fragt die Oberfläche über die native `POST /run`-Aktion
`ai_action=state` den Chatstatus ab und stellt den Verlauf wieder her.
Während eine andere View desselben Werts antwortet, wird der Verlauf
regelmäßig aktualisiert; parallele neue Anfragen werden gesperrt. Der
API-Token bleibt im SignalDeck-Serverprozess, wird nicht in der Browser-UI
übertragen oder dauerhaft gespeichert. Der zurückgegebene
`connection_id`-Handle ermöglicht allerdings jedem Browser mit Zugriff
auf dieselbe View, die gemeinsame Agentenverbindung zu benutzen: **SignalDeck
muss daher für vertrauenswürdige Nutzer geschützt erreichbar sein.**

Mit `Verlauf zurücksetzen` wird nur der Thread des jeweiligen Werts
zurückgesetzt. `Trennen` beendet die gemeinsame Verbindung für alle
gleichzeitig geöffneten Ansichten dieses Werts. Die gespeicherten Chats
überleben einen Seitenwechsel und das Schließen des Browsers, **nicht** den
Neustart von SignalDeck. Sie bleiben bis `Trennen` oder Server-Neustart
im Speicher; die 30-Minuten-Leerlaufgrenze gilt weiterhin nur für
nicht benannte Legacy-Verbindungen.

Die Demo unter `config/group_ai.json` enthält zwei Chat-Views des selben
Prozessors mit `value=["energie"]` und `value=["haussteuerung"]`.
Sie lassen sich unabhängig voneinander testen.

### Security

The cli-agent bearer token is provided in the SignalDeck Chat UI at connection
time and kept only in server-side process memory until disconnect or idle
expiration (30 minutes). The UI clears the password field after connection
and keeps only an opaque connection handle in JavaScript memory; neither token
nor handle is stored in browser storage or DisplayData. Avoid placing
SignalDeck behind an untrusted HTTP proxy, because the initial token entry
travels through SignalDeck's HTTP endpoint. The SignalDeck proxy permits
configured loopback endpoints only. All browser state-changing requests use SignalDeck's `/run` action dispatcher;
the chat processor requires **POST** with a same-origin `Origin` header.
The existing core `/run` endpoint also accepts GET for other processors,
but AG-UI chat actions explicitly reject GET. This is CSRF mitigation,
**not user authentication**.
You must separately secure the SignalDeck web application: otherwise
any web user could request AI actions under the configured agent's
permissions. Do not expose an unauthenticated MCP endpoint outside
loopback. Docker host mode shares the host network namespace.

The first browser UI supports textual messages, tool-status events,
and a single approval interrupt. Runs execute on worker threads and bounded
SSE replays keep the SignalDeck `/run` handler responsive. The browser
never receives the agent API token, but does receive a temporary opaque
connection handle held only in its in-page JavaScript state. Connections scoped to a named view remain in server memory until disconnect or shutdown. Only standalone legacy connections expire after 30 minutes of inactivity.
Frontend-provided AG-UI tools, multi-interrupt and multimodal handling are
not implemented yet.

## Tests and status

```sh
python -m pytest
```

This is an **initial implementation**, not an end-to-end-validated
release. It still needs integration testing with a real SignalDeck
instance, MCP server and cli-agent-api.

### Absolute history windows

`get_history` requires `start` and `end` as ISO-8601 timestamps with a
`Z` or explicit numeric UTC offset. A date without a time or a naive local
time is rejected. The start is inclusive and the end exclusive:

```json
{"name":"hist_temp_garten","start":"2026-10-20T06:30:00Z","end":"2026-10-22T10:30:00Z","include_points":true}
```

This is one MCP call covering multiple UTC calendar days. It selects the
same **absolute instants** as bounds written with different offsets, for example
`2026-10-20T08:30:00+02:00` instead of `2026-10-20T06:30:00Z`.
`max_days` limits the *elapsed duration*, not the number of underlying
per-day datastore queries. Cross-midnight windows are supported, including
windows spanning DST changes.

The adapter reads each intersecting UTC day using aware UTC timestamps;
it does not assume the datastore's timezone. Statistics and returned points
refer only to `[start, end)`, with boundary duplicates removed and source
offsets preserved. Naive measurement times are rejected rather than assigned
an invented zone. Requests starting in the future are rejected.

The former MCP parameters `date`, `days`, `start_time` and `end_time`
have been removed. These were ambiguous for callers in other timezones.

### Point-in-time historical lookup

Use `get_historical_value` when you need **one** historical value. Its
`date` parameter also requires an explicit offset:

```json
{"name":"pv_power_history","date":"2026-10-20T14:30:00+02:00"}
```

The equivalent UTC timestamp `2026-10-20T12:30:00Z` identifies exactly
the same instant. The adapter passes an aware UTC datetime to SignalDeck's
existing `hist(date=..., days=0, fullDay=False)`. The datastore returns
its **best matching** historical value; this need not be a measurement at
exactly the requested second. The result includes `requested_time`,
`value`, description and optional unit. Only explicitly allowed per-day
history methods can be used for point lookups.

### In-memory measurements on recent windows

For a recent interval, `get_history` may also request the in-memory
`hist(currentValues=True)` buffer. Cache points are merged by absolute
instant (so differing UTC offsets still identify the same measurement)
and take precedence over persisted values at duplicate timestamps. The
complete merged set is filtered to `[start, end)` before statistics and
optional `points` are returned. Older intervals and custom range methods
never query the live buffer. Missing or incompatible caches do not break
persisted-history reads.


## Einklappbare Tool-Aktivitäten im AG-UI-Chat

Die Chatansicht sammelt alle vom cli-agent bereits gesendeten
`TOOL_CALL_START`- und `TOOL_CALL_RESULT`-Ereignisse in einer
**einklappbaren Übersicht pro Agentenlauf**. Das gilt ausdrücklich
auch für automatisch freigegebene MCP-Tools; neue AG-UI-Events oder
Änderungen an cli-agent sind nicht erforderlich.

Die Anzeige verwendet nur **Toolname und Ausführungsstatus**.
Fehlende Argumente werden weder angefordert noch angezeigt.
Die MCP-Rückgabewerte erscheinen ebenfalls nicht in dieser Übersicht.
Eine unterbrochene Freigabe und deren Fortsetzung aktualisieren
denselben Tool-Eintrag statt ihn zu verdoppeln.

Die Übersicht wird nur in der **serverseitigen Darstellungs-Historie**
gespeichert, getrennt von den Nachrichten für cli-agent. Deshalb
bleiben die Aufrufe nach Seitenwechseln und automatischer Synchronisierung
sichtbar, ohne den LLM-Kontext zu vergrößern. Sie werden mit
**Verlauf zurücksetzen** gelöscht. Die Anzahl der im Speicher
verfolgten Tool-Aufrufe pro Chat ist auf 1024 begrenzt; bei einem
SignalDeck-Neustart geht die In-Memory-Darstellungs-Historie verloren.

## Markdown in Chat-Antworten

Assistant-Nachrichten werden beim Streaming und beim Wiederherstellen des
Chatverlaufs als Markdown gerendert. Unterstützt werden insbesondere
Überschriften, Fett-/Kursivschrift, Listen, Links, Tabellen und Codeblöcke.
Nutzernachrichten sowie Toolnamen/-status bleiben reiner Text.

Die Darstellung verwendet eine lokal ausgelieferte, fest eingebundene Kopie
von `markdown-it` 14.1.0 (`static/js/markdown-it.min.js`, Lizenz daneben).
HTML in Modellantworten wird escaped, gefährliche Link-Protokolle werden
abgewiesen und externe Markdown-Bilder nicht automatisch geladen. Bei
fehlender Renderer-Bibliothek erscheint die Antwort weiterhin als Klartext.
