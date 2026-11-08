"""One-shot LLM Cmd: authenticate via chat view, use a fresh thread, then delete it."""
import asyncio
import json
from concurrent.futures import Future
from types import SimpleNamespace

import httpx
import pytest

from signaldeck_ai.chat_processor import AGUIChatProcessor
from signaldeck_ai.chat_runtime import ChatActionError, ChatRuntime
from signaldeck_ai.llm_command import LLMCommand


class Provider:
    def registerProcessor(self, processor, export):
        pass


class Cmd:
    def __init__(self):
        self.commands = {"echo": object()}

    def registerCmd(self, command):
        self.commands[command.name] = command


def client_factory(events=None, status=200):
    requests = []

    async def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/ag-ui/capabilities":
            return httpx.Response(
                200 if request.headers.get("Authorization") == "Bearer test-secret" else 401,
                json={"identity": {"name": "cli-agent"}},
            )
        if request.url.path == "/ag-ui" and request.method == "POST":
            body = json.loads(request.content)
            stream = events(body) if events else [
                {"type": "RUN_STARTED"},
                {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "Erste "},
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "Antwort"},
                {"type": "TEXT_MESSAGE_END", "messageId": "m1"},
                {"type": "RUN_FINISHED", "outcome": {"type": "success"}},
            ]
            data = "".join("data: " + json.dumps(e) + "\n\n" for e in stream)
            return httpx.Response(status, text=data)
        if request.method == "DELETE" and request.url.path.startswith("/threads/"):
            return httpx.Response(204)
        return httpx.Response(404)

    mock = httpx.MockTransport(transport)
    return lambda **kwargs: httpx.AsyncClient(transport=mock, **kwargs), requests


def connected_runtime():
    runtime = ChatRuntime("http://127.0.0.1:8765/ag-ui")
    identifier = "opaque"
    from signaldeck_ai.chat_runtime import AgentConnection
    runtime._connections[identifier] = AgentConnection(token="test-secret", view_key="energie")
    runtime._views["energie"] = identifier
    return runtime, identifier


def test_command_without_chat_login_fails_before_http_request():
    runtime = ChatRuntime("http://127.0.0.1:8765/ag-ui")
    factory, requests = client_factory()
    command = LLMCommand("LLM", runtime, async_client_factory=factory)
    with pytest.raises(ChatActionError, match="Zuerst in einer Chat-View"):
        asyncio.run(command.run("Hallo", "Haus"))
    assert requests == []


def test_multiple_processors_register_distinct_commands_without_singleton():
    a = AGUIChatProcessor("a", {
        "endpoint": "http://127.0.0.1:8765/ag-ui", "cmd": {"name": "LLM"},
    }, SimpleNamespace(), Provider(), False)
    b = AGUIChatProcessor("b", {
        "endpoint": "http://127.0.0.1:8767/ag-ui", "cmd": {"name": "LLM_lokal"},
    }, SimpleNamespace(), Provider(), False)
    cmd = Cmd()
    a.registerCommands(cmd)
    b.registerCommands(cmd)
    assert cmd.commands["LLM"].runtime is a.runtime
    assert cmd.commands["LLM"].timeout_seconds == 1200
    assert cmd.commands["LLM_lokal"].runtime is b.runtime
    with pytest.raises(ValueError, match="already registered"):
        a.registerCommands(cmd)
    a.shutdown()
    b.shutdown()


def test_rejects_invalid_command_config_or_timeout():
    for value in [
        {"name": "echo"}, {"name": "a b"}, {"name": "LLM", "timeout_seconds": 0},
        {"name": "LLM", "timeout_seconds": 1201},
        {"name": "LLM", "token_env": "ENV"},
    ]:
        processor = AGUIChatProcessor("a", {
            "endpoint": "http://127.0.0.1:8765/ag-ui", "cmd": value,
        }, SimpleNamespace(), Provider(), False)
        with pytest.raises(ValueError):
            processor.registerCommands(Cmd())
        processor.shutdown()


def test_command_accepts_1200_seconds_and_rejects_higher_values():
    runtime = ChatRuntime("http://127.0.0.1:8765/ag-ui")
    assert LLMCommand("LLM", runtime).timeout_seconds == 1200
    assert LLMCommand("LLM", runtime, timeout_seconds=1200).timeout_seconds == 1200
    with pytest.raises(ValueError, match="between 1 and 1200"):
        LLMCommand("LLM", runtime, timeout_seconds=1201)


def test_command_response_is_logged_and_temp_thread_deleted():
    runtime, identifier = connected_runtime()
    factory, requests = client_factory()
    command = LLMCommand("LLM", runtime, async_client_factory=factory)
    states = []

    class Result:
        def appendState(self, command, **kwargs):
            states.append((command.name, kwargs["msg"]))

    asyncio.run(command.run("Wie", "geht", "es?", cmdRes=Result()))
    assert states == [("LLM", "Erste Antwort")]
    posts = [r for r in requests if r.method == "POST"]
    deletes = [r for r in requests if r.method == "DELETE"]
    assert len(posts) == len(deletes) == 1
    body = json.loads(posts[0].content)
    assert body["messages"][0]["content"] == "Wie geht es?"
    assert body["threadId"] in deletes[0].url.path
    assert all(r.headers["Authorization"] == "Bearer test-secret" for r in requests)
    assert runtime.view_state("energie")["messages"] == []
    runtime.disconnect(identifier)


def test_two_commands_get_different_threads_and_no_history():
    runtime, _ = connected_runtime()
    factory, requests = client_factory()
    command = LLMCommand("LLM", runtime, async_client_factory=factory)
    asyncio.run(command.run("eins"))
    asyncio.run(command.run("zwei"))
    posts = [json.loads(r.content) for r in requests if r.method == "POST"]
    assert posts[0]["threadId"] != posts[1]["threadId"]
    assert len(posts[0]["messages"]) == len(posts[1]["messages"]) == 1


@pytest.mark.parametrize("events,match", [
    ([{"type": "RUN_FINISHED", "outcome": {
        "type": "interrupt", "interrupts": [{"id": "approval"}]}}],
     "interactive approval"),
    ([{"type": "RUN_ERROR", "message": "fail"}], "run error"),
    ([{"type": "RUN_STARTED"}], "before RUN_FINISHED"),
    ([{"type": "RUN_FINISHED", "outcome": {
        "type": "success", "pendingToolCallIds": ["tool-1"]}}],
     "frontend tools"),
])
def test_one_shot_failures_always_delete_temp_thread(events, match):
    runtime, _ = connected_runtime()
    factory, requests = client_factory(events=lambda body: events)
    command = LLMCommand("LLM", runtime, async_client_factory=factory)
    with pytest.raises(RuntimeError, match=match):
        asyncio.run(command.run("test"))
    assert len([r for r in requests if r.method == "DELETE"]) == 1


def test_command_with_other_processor_login_fails():
    runtime, identifier = connected_runtime()
    other = ChatRuntime("http://127.0.0.1:8767/ag-ui")
    factory, requests = client_factory()
    command = LLMCommand("LLM_lokal", other, async_client_factory=factory)
    with pytest.raises(ChatActionError):
        asyncio.run(command.run("test"))
    runtime.disconnect(identifier)


def test_command_stops_after_last_chat_disconnect():
    runtime, identifier = connected_runtime()
    assert runtime.command_token() == "test-secret"
    runtime.disconnect(identifier)
    with pytest.raises(ChatActionError):
        runtime.command_token()


def test_different_tokens_of_one_processor_fail_closed():
    from signaldeck_ai.chat_runtime import AgentConnection
    runtime, identifier = connected_runtime()
    runtime._connections["other"] = AgentConnection(
        token="other-secret", view_key="haussteuerung"
    )
    runtime._views["haussteuerung"] = "other"
    with pytest.raises(ChatActionError, match="Mehrere verschiedene"):
        runtime.command_token()
    runtime.disconnect(identifier)
    assert runtime.command_token() == "other-secret"
    runtime.shutdown()


def test_stopping_before_run_skips_http():
    runtime, _ = connected_runtime()
    factory, requests = client_factory()
    command = LLMCommand("LLM", runtime, async_client_factory=factory)
    stop = asyncio.Event()
    stop.set()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(command.run("Stop", stopEvent=stop))
    assert not requests


def test_one_shot_exposes_run_identity_after_successful_http_start():
    runtime, _ = connected_runtime()
    factory, requests = client_factory()
    command = LLMCommand("LLM", runtime, async_client_factory=factory)
    identities = []
    output = asyncio.run(command._ask_once(
        "Hallo", "test-secret", on_started=lambda thread_id, run_id:
        identities.append((thread_id, run_id)),
    ))
    assert output == "Erste Antwort"
    assert len(identities) == 1
    payload = json.loads(next(r.content for r in requests if r.method == "POST"))
    assert identities[0] == (payload["threadId"], payload["runId"])
