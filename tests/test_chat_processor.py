import json
from types import SimpleNamespace

import httpx
import pytest
from flask import Flask

from signaldeck_ai.chat_processor import AGUIChatProcessor
from signaldeck_ai.chat_runtime import ChatActionError, ChatRuntime


class Provider:
    def registerProcessor(self, processor, export):
        pass


def make_processor():
    return AGUIChatProcessor(
        "chat", {"endpoint": "http://127.0.0.1:8765/ag-ui"},
        SimpleNamespace(), Provider(), False,
    )


def test_chat_is_offline_at_startup():
    processor = make_processor()
    assert processor.endpoint == "http://127.0.0.1:8765/ag-ui"
    assert not processor.runtime.contains("missing")
    assert not hasattr(processor, "api_token_env")


@pytest.mark.parametrize("url", [
    "https://evil.example/ag-ui", "http://evil.example:8765/ag-ui",
    "http://127.0.0.1:8765/path", "http://127.0.0.1:8765/ag-ui?token=x",
])
def test_chat_config_rejects_untrusted_endpoints(url):
    with pytest.raises(ValueError):
        AGUIChatProcessor("chat", {"endpoint": url}, SimpleNamespace(), Provider(), False)


def test_native_action_processor_returns_ack_without_rerender(monkeypatch):
    processor = make_processor()
    app = Flask(__name__)
    monkeypatch.setattr(processor.runtime, "verify_and_connect", lambda token, view_key=None: "opaque-connection")
    with app.test_request_context(
        "/run", method="POST",
        headers={"Origin": "http://localhost"},
    ):
        reply = processor.process(["chat"], "action-hash", ai_action="connect", token="secret")
    assert reply == {"ok": True, "connection_id": "opaque-connection"}
    assert "secret" not in json.dumps(reply)
    assert "html" not in reply  # must not replace the live DOM/SSE client


def test_action_processor_forbids_get_and_cross_origin():
    processor = make_processor()
    app = Flask(__name__)
    with app.test_request_context("/run", method="GET"):
        response = processor.process(["chat"], "action-hash", ai_action="connect", token="x")
    assert response["ok"] is False
    with app.test_request_context(
        "/run", method="POST", headers={"Origin": "https://evil.example"},
    ):
        response = processor.process(["chat"], "action-hash", ai_action="connect", token="x")
    assert response["ok"] is False


def make_client(events_for_run):
    requests = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/ag-ui/capabilities":
            if request.headers.get("Authorization") != "Bearer test-secret":
                return httpx.Response(401)
            return httpx.Response(200, json={"identity": {"name": "agent"}})
        if request.url.path == "/ag-ui" and request.method == "POST":
            body = json.loads(request.content)
            events = events_for_run(body)
            data = "".join("data: " + json.dumps(item) + "\n\n" for item in events)
            return httpx.Response(200, text=data, headers={"content-type": "text/event-stream"})
        if request.method == "DELETE" and request.url.path.startswith("/threads/"):
            return httpx.Response(204)
        return httpx.Response(404)

    mock_transport = httpx.MockTransport(transport)
    client_factory = lambda **kwargs: httpx.Client(transport=mock_transport, **kwargs)
    return client_factory, requests


def await_run(run):
    with run.changed:
        assert run.changed.wait_for(lambda: run.done, timeout=5), "run did not finish"


def test_agui_async_run_stream_and_thread_lifecycle():
    def events(body):
        assert body["protocolVersion"] == "1.0"
        assert body["messages"][-1]["content"] == "Hallo"
        return [
            {"type": "RUN_STARTED", "runId": body["runId"], "threadId": body["threadId"]},
            {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "Guten"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": " Tag"},
            {"type": "TEXT_MESSAGE_END", "messageId": "m1"},
            {"type": "RUN_FINISHED", "runId": body["runId"], "threadId": body["threadId"],
             "outcome": {"type": "success"}},
        ]

    factory, requests = make_client(events)
    runtime = ChatRuntime("http://127.0.0.1:8765/ag-ui", http_client_factory=factory)
    identifier = runtime.verify_and_connect("test-secret")
    run_id = runtime.start_message(identifier, "Hallo")
    run = runtime.get_run(identifier, run_id)
    await_run(run)
    assert run.answer == "Guten Tag"
    assert run.events[-1][1]["type"] == "RUN_FINISHED"
    assert any(r.url.path == "/ag-ui" for r in requests)
    assert runtime.contains(identifier)
    runtime.disconnect(identifier)
    assert not runtime.contains(identifier)


def test_agui_interrupt_requires_correct_resume_and_keeps_thread():
    captures = []

    def events(body):
        captures.append(body)
        if "resume" not in body:
            return [
                {"type": "RUN_STARTED"},
                {"type": "RUN_FINISHED", "outcome": {
                    "type": "interrupt",
                    "interrupts": [{"id": "approval-1", "reason": "tool_call"}],
                }},
            ]
        return [
            {"type": "RUN_STARTED"},
            {"type": "RUN_FINISHED", "outcome": {"type": "success"}},
        ]

    factory, _ = make_client(events)
    runtime = ChatRuntime("http://127.0.0.1:8765/ag-ui", http_client_factory=factory)
    identifier = runtime.verify_and_connect("test-secret")
    first = runtime.start_message(identifier, "Steuere das Gerät")
    await_run(runtime.get_run(identifier, first))
    with pytest.raises(ChatActionError):
        runtime.start_message(identifier, "Andere Frage")
    with pytest.raises(ChatActionError):
        runtime.approve(identifier, "wrong-id", "yes")
    second = runtime.approve(identifier, "approval-1", "yes")
    await_run(runtime.get_run(identifier, second))
    assert captures[0]["threadId"] == captures[1]["threadId"]
    assert captures[1]["resume"][0]["interruptId"] == "approval-1"
    assert captures[1]["resume"][0]["payload"]["approved"] is True
    runtime.shutdown()


def test_token_rejected_without_storing_session():
    factory, _ = make_client(lambda _: [])
    runtime = ChatRuntime("http://127.0.0.1:8765/ag-ui", http_client_factory=factory)
    with pytest.raises(ChatActionError):
        runtime.verify_and_connect("wrong-secret")
    assert runtime._connections == {}


def test_reset_history_starts_fresh_thread_and_clears_agent_context():
    captured = []
    factory, requests = make_client(lambda body: (
        captured.append(body) or [
            {"type": "RUN_STARTED", "runId": body["runId"]},
            {"type": "RUN_FINISHED", "outcome": {"type": "success"}},
        ]
    ))
    runtime = ChatRuntime("http://127.0.0.1:8765/ag-ui", http_client_factory=factory)
    identifier = runtime.verify_and_connect("test-secret")
    first_run_id = runtime.start_message(identifier, "Alte Nachricht")
    await_run(runtime.get_run(identifier, first_run_id))
    first_thread = captured[0]["threadId"]
    assert captured[0]["messages"][0]["content"] == "Alte Nachricht"

    runtime.new_chat(identifier)
    with pytest.raises(ChatActionError):
        runtime.get_run(identifier, first_run_id)
    assert runtime.contains(identifier)  # Token and connection survive reset
    assert runtime._connections[identifier].messages == []
    assert runtime._connections[identifier].pending_interrupt is None

    next_run_id = runtime.start_message(identifier, "Neues Thema")
    await_run(runtime.get_run(identifier, next_run_id))
    assert captured[1]["threadId"] != first_thread
    assert [m["content"] for m in captured[1]["messages"]] == ["Neues Thema"]
    runtime.shutdown()


def test_reset_history_allowed_after_interrupt_and_discards_pending_approval():
    factory, _ = make_client(lambda body: [
        {"type": "RUN_STARTED"},
        {"type": "RUN_FINISHED", "outcome": {
            "type": "interrupt", "interrupts": [{"id": "obsolete-approval"}]
        }},
    ])
    runtime = ChatRuntime("http://127.0.0.1:8765/ag-ui", http_client_factory=factory)
    identifier = runtime.verify_and_connect("test-secret")
    run_id = runtime.start_message(identifier, "Trigger approval")
    await_run(runtime.get_run(identifier, run_id))
    assert runtime._connections[identifier].pending_interrupt
    runtime.new_chat(identifier)
    assert runtime._connections[identifier].pending_interrupt is None
    with pytest.raises(ChatActionError, match="Keine passende"):
        runtime.approve(identifier, "obsolete-approval", "yes")
    runtime.shutdown()


def test_native_reset_history_action_returns_ack_without_rerender(monkeypatch):
    processor = make_processor()
    calls = []
    monkeypatch.setattr(processor.runtime, "new_chat", lambda identifier: calls.append(identifier))
    monkeypatch.setattr(processor.runtime, "assert_view", lambda value, identifier: None)
    app = Flask(__name__)
    with app.test_request_context(
        "/run", method="POST", headers={"Origin": "http://localhost"},
    ):
        reply = processor.process(
            ["chat"], "action-hash", ai_action="reset_history", connection_id="opaque"
        )
    assert reply == {"ok": True, "history_reset": True}
    assert calls == ["opaque"]
    assert "html" not in reply


def test_shared_value_reuses_thread_and_restores_history_between_views():
    import time
    requests_payload = []

    def events(body):
        requests_payload.append(body)
        return [
            {"type": "RUN_STARTED"},
            {"type": "TEXT_MESSAGE_START", "messageId": "a", "role": "assistant"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "a", "delta": "Antwort"},
            {"type": "TEXT_MESSAGE_END", "messageId": "a"},
            {"type": "RUN_FINISHED", "outcome": {"type": "success"}},
        ]

    factory, _ = make_client(events)
    runtime = ChatRuntime(
        "http://127.0.0.1:8765/ag-ui", http_client_factory=factory,
    )
    first = runtime.verify_and_connect("test-secret", "energie")
    other = runtime.verify_and_connect("test-secret", "haussteuerung")
    assert first != other
    assert runtime.verify_and_connect("test-secret", "energie") == first

    run_id = runtime.start_message(first, "Stromverbrauch?")
    await_run(runtime.get_run(first, run_id))
    for attempt in range(100):
        snapshot = runtime.view_state("energie")
        if not snapshot["busy"]:
            break
        time.sleep(0.01)
    assert snapshot["connected"]
    assert snapshot["connection_id"] == first
    assert snapshot["messages"] == [
        {"role": "user", "content": "Stromverbrauch?"},
        {"role": "assistant", "content": "Antwort"},
    ]
    assert runtime.view_state("haussteuerung")["messages"] == []
    assert runtime.view_state("unconfigured")["connected"] is False
    assert requests_payload[0]["threadId"] != runtime._connections[other].thread_id

    runtime.new_chat(first)
    assert runtime.view_state("energie")["messages"] == []
    assert runtime.view_state("energie")["connected"]
    assert runtime.view_state("haussteuerung")["connected"]
    runtime.disconnect(first)
    assert runtime.view_state("energie")["connected"] is False
    assert runtime.view_state("haussteuerung")["connected"] is True
    runtime.shutdown()


def test_native_view_state_restoration_and_cross_value_rejection():
    factory, _ = make_client(lambda _: [
        {"type": "RUN_STARTED"},
        {"type": "RUN_FINISHED", "outcome": {"type": "success"}},
    ])
    processor = make_processor()
    processor.runtime = ChatRuntime(
        "http://127.0.0.1:8765/ag-ui", http_client_factory=factory,
    )
    app = Flask(__name__)
    with app.test_request_context(
        "/run", method="POST", headers={"Origin": "http://localhost"},
    ):
        initial = processor.process("energie", "hash", ai_action="state")
        assert initial == {"ok": True, "connected": False, "messages": [], "busy": False}
        connected = processor.process(
            "energie", "hash", ai_action="connect", token="test-secret",
        )
        assert connected["ok"]
        identifier = connected["connection_id"]
        reopened = processor.process("energie", "hash", ai_action="state")
        assert reopened["connection_id"] == identifier
        cross_view = processor.process(
            "haussteuerung", "other-hash", ai_action="send",
            connection_id=identifier, message="Cross-view",
        )
        assert cross_view["ok"] is False
        assert "anderen Chat-View" in cross_view["error"]
        reset = processor.process(
            "energie", "hash", ai_action="reset_history", connection_id=identifier,
        )
        assert reset == {"ok": True, "history_reset": True}
    processor.shutdown()


@pytest.mark.parametrize("bad", ["", [], ["a", "b"], None, 2, " "])
def test_invalid_view_values_are_rejected(bad):
    with pytest.raises(ChatActionError):
        ChatRuntime.normalize_view(bad)


def test_same_value_in_state_list_and_action_scalar():
    assert ChatRuntime.normalize_view(["energie"]) == "energie"
    assert ChatRuntime.normalize_view("energie") == "energie"


def test_autoapproved_tool_calls_are_retained_as_collapsible_ui_history_only():
    """The display can restore auto-approved tool calls without agent context noise."""
    def events(body):
        assert all(msg.get("role") != "tools" for msg in body["messages"])
        return [
            {"type": "RUN_STARTED"},
            {"type": "TOOL_CALL_START", "toolCallId": "auto-1",
             "toolCallName": "signaldeck_climate__get_values"},
            {"type": "TOOL_CALL_END", "toolCallId": "auto-1"},
            {"type": "TOOL_CALL_RESULT", "toolCallId": "auto-1",
             "content": "success"},
            {"type": "TOOL_CALL_START", "toolCallId": "auto-2",
             "toolCallName": "signaldeck_energy__get_history"},
            {"type": "TOOL_CALL_END", "toolCallId": "auto-2"},
            {"type": "TOOL_CALL_RESULT", "toolCallId": "auto-2",
             "content": "error"},
            {"type": "TEXT_MESSAGE_START", "messageId": "answer", "role": "assistant"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": "answer", "delta": "Fertig"},
            {"type": "TEXT_MESSAGE_END", "messageId": "answer"},
            {"type": "RUN_FINISHED", "outcome": {"type": "success"}},
        ]

    factory, requests = make_client(events)
    runtime = ChatRuntime("http://127.0.0.1:8765/ag-ui", http_client_factory=factory)
    identifier = runtime.verify_and_connect("test-secret", "energie")
    run_id = runtime.start_message(identifier, "Wie viel Strom?")
    await_run(runtime.get_run(identifier, run_id))
    import time
    for _ in range(100):
        if not runtime.view_state("energie")["busy"]:
            break
        time.sleep(0.01)
    snapshot = runtime.view_state("energie")
    assert [msg["role"] for msg in snapshot["messages"]] == [
        "user", "tools", "assistant",
    ]
    group = snapshot["messages"][1]
    assert group["id"] == run_id
    assert group["tools"] == [
        {"id": "auto-1", "name": "signaldeck_climate__get_values", "status": "success"},
        {"id": "auto-2", "name": "signaldeck_energy__get_history", "status": "error"},
    ]
    assert snapshot["messages"][2]["content"] == "Fertig"
    assert runtime._connections[identifier].messages == [
        {"id": runtime._connections[identifier].messages[0]["id"],
         "role": "user", "content": "Wie viel Strom?"},
        {"id": runtime._connections[identifier].messages[1]["id"],
         "role": "assistant", "content": "Fertig"},
    ]
    # No display-only tool messages are ever passed back to the LLM.
    sent = [json.loads(req.content) for req in requests if req.url.path == "/ag-ui"]
    assert all(msg["role"] in {"user", "assistant"} for msg in sent[0]["messages"])
    assert runtime.verify_and_connect("test-secret", "energie") == identifier
    assert runtime.view_state("energie")["messages"] == snapshot["messages"]
    runtime.new_chat(identifier)
    assert runtime.view_state("energie")["messages"] == []
    assert runtime._connections[identifier].tool_calls == {}
    runtime.shutdown()


def test_tool_approval_and_resume_update_existing_call_instead_of_duplicate():
    def events(body):
        if "resume" not in body:
            return [
                {"type": "RUN_STARTED"},
                {"type": "TOOL_CALL_START", "toolCallId": "approval-call",
                 "toolCallName": "device__set"},
                {"type": "TOOL_CALL_ARGS", "toolCallId": "approval-call",
                 "delta": '{"secret":"not-for-ui"}'},
                {"type": "TOOL_CALL_END", "toolCallId": "approval-call"},
                {"type": "RUN_FINISHED", "outcome": {"type": "interrupt",
                 "interrupts": [{"id": "int-1", "toolCallId": "approval-call",
                                 "reason": "tool_call"}]}},
            ]
        return [
            {"type": "RUN_STARTED"},
            {"type": "TOOL_CALL_START", "toolCallId": "approval-call",
             "toolCallName": "device__set"},
            {"type": "TOOL_CALL_RESULT", "toolCallId": "approval-call",
             "content": "success"},
            {"type": "RUN_FINISHED", "outcome": {"type": "success"}},
        ]

    factory, _ = make_client(events)
    runtime = ChatRuntime("http://127.0.0.1:8765/ag-ui", http_client_factory=factory)
    identifier = runtime.verify_and_connect("test-secret", "energie")
    first = runtime.start_message(identifier, "Steuere Gerät")
    await_run(runtime.get_run(identifier, first))
    state = runtime.view_state("energie")
    assert state["messages"][1]["tools"][0]["status"] == "approval"
    assert "not-for-ui" not in json.dumps(state)
    second = runtime.approve(identifier, "int-1", "yes")
    await_run(runtime.get_run(identifier, second))
    state = runtime.view_state("energie")
    groups = [m for m in state["messages"] if m["role"] == "tools"]
    assert len(groups) == 1
    assert groups[0]["tools"] == [
        {"id": "approval-call", "name": "device__set", "status": "success"}
    ]
    runtime.shutdown()


def test_tool_status_without_arguments_error_and_truncation_are_safe():
    from signaldeck_ai.chat_runtime import AgentConnection, AgentRun, ChatRuntime
    connection = AgentConnection(token="secret")
    run = AgentRun("r1", "thread", {})
    ChatRuntime._record_tool_activity(connection, run, {
        "type": "TOOL_CALL_START", "toolCallId": "c1",
        "toolCallName": "x" * 500,
    })
    ChatRuntime._record_tool_activity(connection, run, {
        "type": "RUN_ERROR", "message": "hidden server exception",
    })
    assert connection.display_messages[0]["tools"][0]["status"] == "error"
    assert len(connection.display_messages[0]["tools"][0]["name"]) == 256
    assert "hidden server exception" not in json.dumps(connection.display_messages)
    assert connection.messages == []


def test_chat_script_renders_tools_as_native_collapsible_details_without_arguments():
    from pathlib import Path
    script = (
        Path(__file__).resolve().parents[1] /
        "plugins/signaldeck_ai/static/js/chat.js"
    ).read_text()
    assert 'createElement("details")' in script
    assert 'createElement("summary")' in script
    assert 'case "TOOL_CALL_START":' in script
    assert 'case "TOOL_CALL_RESULT":' in script
    assert 'msg.role === "tools"' in script
    assert "toolCallArgs" not in script
    assert 'case "TOOL_CALL_ARGS":' not in script


def test_chat_loads_local_markdown_renderer_before_streaming_client():
    from pathlib import Path

    processor = make_processor()
    assert processor.getAdditionalJsFiles("chat") == [
        ("signaldeck_ai", "js/markdown-it.min.js"),
        ("signaldeck_ai", "js/chat_markdown.js"),
        ("signaldeck_ai", "js/chat.js"),
    ]
    script = (
        Path(__file__).resolve().parents[1] /
        "plugins/signaldeck_ai/static/js/chat.js"
    ).read_text()
    assert 'updateAssistant(node, String(evt.delta || ""), true)' in script
    assert 'role === "assistant"' in script
    assert 'case "TEXT_MESSAGE_END":' in script
    assert "signaldeckAIMarkdown?.render(source)" in script


def test_browser_markdown_supports_tables_and_escapes_untrusted_content():
    import shutil
    import subprocess
    from pathlib import Path

    if not shutil.which("node"):
        pytest.skip("Node.js is needed for the optional browser Markdown test")
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        ["node", "--test", "tests/chat_markdown.test.cjs"],
        cwd=root, check=True, capture_output=True,
    )


def test_cancel_race_does_not_commit_late_success_or_interrupt():
    """An acknowledged Stop must win over buffered terminal SSE events."""
    from signaldeck_ai.chat_runtime import AgentRun

    def transport(request):
        if request.url.path == "/ag-ui/capabilities":
            return httpx.Response(200, json={"identity": {"name": "agent"}})
        if request.url.path == "/ag-ui":
            assert json.loads(request.content)["action"] == "cancel"
            return httpx.Response(202, json={"status": "cancelling"})
        return httpx.Response(404)

    runtime = ChatRuntime(
        "http://127.0.0.1:8765/ag-ui",
        http_client_factory=lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(transport), **kwargs,
        ),
    )
    identifier = runtime.verify_and_connect("test-secret")
    conn = runtime._connections[identifier]
    for outcome in ("success", "interrupt"):
        run = AgentRun(run_id="run-" + outcome, thread_id=conn.thread_id, payload={})
        conn.current_run = run.run_id
        conn.runs = {run.run_id: run}
        conn.pending_interrupt = None
        runtime.cancel(identifier)
        assert run.cancel_requested
        runtime._receive_frame(conn, run, [
            json.dumps({"type": "TEXT_MESSAGE_START", "messageId": "a", "role": "assistant"})
        ])
        runtime._receive_frame(conn, run, [
            json.dumps({"type": "TEXT_MESSAGE_CONTENT", "messageId": "a", "delta": "late"})
        ])
        runtime._receive_frame(conn, run, [
            json.dumps({"type": "TEXT_MESSAGE_END", "messageId": "a"})
        ])
        runtime._receive_frame(conn, run, [
            json.dumps({"type": "RUN_FINISHED", "outcome": {
                "type": outcome, "interrupts": [{"id": "approval-1"}],
            }})
        ])
        assert run.cancelled
        assert not run.interrupted
        assert conn.pending_interrupt is None
        # Finalization must not turn buffered answer fragments into chat history.
        assert not any(x.get("content") == "late" for x in conn.messages)
        assert run.events[-1][1]["outcome"]["type"] == "cancelled"


def test_cancel_rejected_does_not_discard_terminal_result():
    """When upstream has already finished, the real success remains visible."""
    from signaldeck_ai.chat_runtime import AgentRun

    def transport(request):
        if request.url.path == "/ag-ui/capabilities":
            return httpx.Response(200, json={})
        return httpx.Response(409)

    runtime = ChatRuntime(
        "http://127.0.0.1:8765/ag-ui",
        http_client_factory=lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(transport), **kwargs,
        ),
    )
    ident = runtime.verify_and_connect("test-secret")
    conn = runtime._connections[ident]
    run = AgentRun(run_id="run", thread_id=conn.thread_id, payload={})
    conn.current_run = run.run_id
    conn.runs = {run.run_id: run}
    with pytest.raises(ChatActionError, match="409"):
        runtime.cancel(ident)
    assert not run.cancel_requested
    runtime._receive_frame(conn, run, [json.dumps({
        "type": "RUN_FINISHED", "outcome": {"type": "success"},
    })])
    assert run.done and not run.cancelled
