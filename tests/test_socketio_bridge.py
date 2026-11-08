"""Socket.IO bridge: only routed prompts, no persistent cli-agent sessions."""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from signaldeck_ai.chat_runtime import AgentConnection, ChatRuntime
from signaldeck_ai.socketio_bridge import SocketIOChatBridge


class Bus:
    def __init__(self):
        self.listeners = []

    def subscribe(self, callback):
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback)

    def publish(self, msg):
        for cb in tuple(self.listeners):
            cb(msg)


class Context:
    def __init__(self):
        self.message_bus = Bus()
        self.sent = []

    def message(self, source, content, *, metadata=None):
        self.sent.append((source, content, metadata))
        self.message_bus.publish(SimpleNamespace(
            source=source, content=content, metadata=metadata or {},
        ))


def runtime(with_login=True):
    result = ChatRuntime("http://127.0.0.1:8765/ag-ui")
    if with_login:
        result._connections["connection"] = AgentConnection(
            token="secret", view_key="energie",
        )
        result._views["energie"] = "connection"
    return result


def config():
    return {
        "routes": [{
            "room": "haus", "source": "socketio.ai",
            "response_source": "ai.socketio.haus",
        }],
        "max_queue": 5,
        "timeout_seconds": 30,
    }


def send(ctx, room="haus", event_id="e1", prompt="Wie warm?", source="socketio.ai"):
    ctx.message_bus.publish(SimpleNamespace(
        source=source, content=prompt,
        metadata={"room": room, "event_id": event_id,
                  "transmitter": "family-user"},
    ))


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for bridge response")


def test_bridge_processes_routed_message_once_and_ignores_other_sources():
    ctx = Context()
    rt = runtime()
    captured = []

    async def ask(prompt, token):
        captured.append((prompt, token))
        return "21 °C"

    bridge = SocketIOChatBridge(config(), ctx, rt, ask=ask)
    try:
        send(ctx)
        send(ctx)  # event replay must not trigger a second run
        send(ctx, source="ai.socketio.haus", event_id="e2")
        send(ctx, room="privat", event_id="e3")
        wait_for(lambda: len(ctx.sent) >= 1)
        assert captured == [("Wie warm?", "secret")]
        assert ctx.sent == [(
            "ai.socketio.haus", "21 °C",
            {"type": "chat", "content_type": "text", "system": False}
        )]
        assert rt._connections["connection"].messages == []
    finally:
        bridge.shutdown()
        assert not ctx.message_bus.listeners


def test_no_login_generates_explanatory_response_and_no_agent_call():
    ctx = Context()
    rt = runtime(False)
    calls = []

    async def ask(prompt, token):
        calls.append((prompt, token))
        return "never"

    bridge = SocketIOChatBridge(config(), ctx, rt, ask=ask)
    try:
        send(ctx)
        wait_for(lambda: len(ctx.sent) == 1)
        assert "nicht verbunden" in ctx.sent[0][1]
        assert calls == []
    finally:
        bridge.shutdown()


def test_queued_requests_are_serialized():
    ctx, rt = Context(), runtime()
    active = 0
    peak = 0
    completed = []

    async def ask(prompt, token):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.025)
        completed.append(prompt)
        active -= 1
        return "Antwort: " + prompt

    bridge = SocketIOChatBridge(config(), ctx, rt, ask=ask)
    try:
        for index in range(3):
            send(ctx, event_id=f"id-{index}", prompt=f"Frage {index}")
        wait_for(lambda: len(ctx.sent) == 3)
        assert peak == 1
        assert completed == ["Frage 0", "Frage 1", "Frage 2"]
    finally:
        bridge.shutdown()


def test_agent_error_is_answered_and_worker_continues():
    ctx, rt = Context(), runtime()
    calls = []

    async def ask(prompt, token):
        calls.append(prompt)
        if prompt == "fail":
            raise RuntimeError("private agent error")
        return "OK"

    bridge = SocketIOChatBridge(config(), ctx, rt, ask=ask)
    try:
        send(ctx, prompt="fail", event_id="1")
        send(ctx, prompt="success", event_id="2")
        wait_for(lambda: len(ctx.sent) == 2)
        assert "nicht bearbeiten" in ctx.sent[0][1]
        assert ctx.sent[1][1] == "OK"
        assert calls == ["fail", "success"]
    finally:
        bridge.shutdown()


def test_bad_config_fails_closed_before_subscribing():
    ctx, rt = Context(), runtime()
    for bad in [
        {}, {"routes": []}, {"routes": [{"room": "haus"}]},
        {"routes": config()["routes"], "max_queue": -1},
        {"routes": config()["routes"], "timeout_seconds": 0},
        {"routes": config()["routes"], "timeout_seconds": 1201},
    ]:
        with pytest.raises(ValueError):
            SocketIOChatBridge(bad, ctx, rt)
    assert ctx.message_bus.listeners == []


def test_bridge_default_and_explicit_timeouts_are_1200_seconds():
    ctx, rt = Context(), runtime()
    for timeout in (None, 1200):
        cfg = config()
        if timeout is None:
            cfg.pop("timeout_seconds")
        else:
            cfg["timeout_seconds"] = timeout
        bridge = SocketIOChatBridge(cfg, ctx, rt)
        try:
            assert bridge._command.timeout_seconds == 1200
        finally:
            bridge.shutdown()


def test_zero_queue_rejects_parallel_requests_without_second_session():
    ctx, rt = Context(), runtime()
    cfg = config()
    cfg["max_queue"] = 0
    active = threading.Event()
    release = threading.Event()
    calls = []

    async def ask(prompt, token):
        calls.append(prompt)
        active.set()
        await asyncio.to_thread(release.wait, 2)
        return "Fertig"

    bridge = SocketIOChatBridge(cfg, ctx, rt, ask=ask)
    try:
        send(ctx, prompt="erste Frage", event_id="1")
        assert active.wait(2)
        send(ctx, prompt="zweite Frage", event_id="2")
        wait_for(lambda: len(ctx.sent) >= 1)
        assert "ausgelastet" in ctx.sent[0][1]
        assert calls == ["erste Frage"]
        release.set()
        wait_for(lambda: len(ctx.sent) == 2)
        assert ctx.sent[1][1] == "Fertig"
    finally:
        release.set()
        bridge.shutdown()


def test_stop_and_status_bypass_queue_and_suppress_late_answer():
    ctx, rt = Context(), runtime()
    started = threading.Event()
    release = threading.Event()
    calls = []

    async def ask(prompt, token):
        calls.append(prompt)
        if prompt == "first":
            started.set()
            await asyncio.to_thread(release.wait, 2)
            return "Stale response"
        return "Second complete"

    bridge = SocketIOChatBridge(config(), ctx, rt, ask=ask)
    try:
        send(ctx, prompt="first", event_id="first-id")
        assert started.wait(2)
        send(ctx, prompt="second", event_id="second-id")
        snapshot = bridge.status("haus")
        assert snapshot["active"]["id"] == "first-id"
        assert snapshot["waiting_count"] == 1
        send(ctx, prompt="status", event_id="status-id")
        assert "1 weitere Anfragen warten" in ctx.sent[-1][1]
        send(ctx, prompt="stop", event_id="stop-id")
        assert "Abbruch angefordert" in ctx.sent[-1][1]
        assert bridge.status("haus")["active"]["cancelling"] is True
        send(ctx, prompt="stop", event_id="stop-id")  # replay cannot cancel another run
        release.set()
        wait_for(lambda: any(m[1] == "Second complete" for m in ctx.sent))
        assert any(m[1] == "Anfrage abgebrochen." for m in ctx.sent)
        assert not any("Stale response" in m[1] for m in ctx.sent)
        assert calls == ["first", "second"]
    finally:
        release.set()
        bridge.shutdown()


def test_waiting_request_can_be_cancelled_by_id_without_execution():
    ctx, rt = Context(), runtime()
    started, release = threading.Event(), threading.Event()
    calls = []

    async def ask(prompt, token):
        calls.append(prompt)
        started.set()
        await asyncio.to_thread(release.wait, 2)
        return "done"

    bridge = SocketIOChatBridge(config(), ctx, rt, ask=ask)
    try:
        send(ctx, prompt="first", event_id="first-id")
        assert started.wait(2)
        send(ctx, prompt="cancel this", event_id="queued-id")
        assert bridge.status()["waiting_count"] == 1
        assert bridge.cancel(request_id="queued-id") == "Wartende Anfrage abgebrochen."
        assert bridge.status()["waiting_count"] == 0
        release.set()
        wait_for(lambda: len(ctx.sent) == 1)
        assert calls == ["first"]
    finally:
        release.set()
        bridge.shutdown()


def test_real_backend_cancel_targets_only_current_run(monkeypatch):
    import json
    import httpx

    ctx, rt = Context(), runtime()
    started = threading.Event()
    requests = []

    async def fake_ask(prompt, token, *, stop_event=None, on_started=None):
        assert token == "secret"
        on_started("thread-123", "run-456")
        started.set()
        assert await asyncio.to_thread(stop_event.wait, 2)
        raise asyncio.CancelledError()

    class FakeClient:
        def __init__(self, timeout):
            assert timeout == 8.0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, endpoint, *, headers, json):
            requests.append((endpoint, headers, json))
            return httpx.Response(202)

    monkeypatch.setattr("signaldeck_ai.socketio_bridge.httpx.Client", FakeClient)
    bridge = SocketIOChatBridge(config(), ctx, rt)
    bridge._command._ask_once = fake_ask
    try:
        send(ctx, prompt="diagnose", event_id="abc")
        assert started.wait(2)
        send(ctx, prompt="stop", event_id="control")
        wait_for(lambda: any(m[1] == "Anfrage abgebrochen." for m in ctx.sent))
        assert len(requests) == 1
        endpoint, headers, payload = requests[0]
        assert endpoint == "http://127.0.0.1:8765/ag-ui"
        assert headers["Authorization"] == "Bearer secret"
        assert payload == {
            "action": "cancel", "threadId": "thread-123", "runId": "run-456",
        }
        wait_for(lambda: bridge.status()["active"] is None)
    finally:
        bridge.shutdown()


def test_stop_before_upstream_run_ids_still_cancels_once_available(monkeypatch):
    import httpx

    ctx, rt = Context(), runtime()
    before_registration = threading.Event()
    allow_registration = threading.Event()
    requests = []

    async def fake_ask(prompt, token, *, stop_event=None, on_started=None):
        before_registration.set()
        await asyncio.to_thread(allow_registration.wait, 2)
        on_started("late-thread", "late-run")
        assert await asyncio.to_thread(stop_event.wait, 2)
        raise asyncio.CancelledError()

    class FakeClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, endpoint, *, headers, json):
            requests.append(json)
            return httpx.Response(202)

    monkeypatch.setattr("signaldeck_ai.socketio_bridge.httpx.Client", FakeClient)
    bridge = SocketIOChatBridge(config(), ctx, rt)
    bridge._command._ask_once = fake_ask
    try:
        send(ctx, prompt="diagnose", event_id="initial")
        assert before_registration.wait(2)
        send(ctx, prompt="stop", event_id="stop-control")
        assert bridge.status()["active"]["cancelling"]
        allow_registration.set()
        wait_for(lambda: any(m[1] == "Anfrage abgebrochen." for m in ctx.sent))
        assert requests == [{
            "action": "cancel", "threadId": "late-thread", "runId": "late-run",
        }]
    finally:
        allow_registration.set()
        bridge.shutdown()



def test_cancelling_waiting_job_immediately_releases_full_queue_slot():
    ctx, rt = Context(), runtime()
    cfg = config()
    cfg["max_queue"] = 1
    started = threading.Event()
    release = threading.Event()
    calls = []

    async def ask(prompt, token):
        calls.append(prompt)
        if prompt == "active":
            started.set()
            await asyncio.to_thread(release.wait, 2)
        return "Antwort: " + prompt

    bridge = SocketIOChatBridge(cfg, ctx, rt, ask=ask)
    try:
        send(ctx, event_id="run", prompt="active")
        assert started.wait(2)
        send(ctx, event_id="queued", prompt="old queued")
        assert bridge.status()["waiting_count"] == 1
        send(ctx, event_id="overflow", prompt="should be rejected")
        assert any("ausgelastet" in m[1] for m in ctx.sent)

        assert bridge.cancel(request_id="queued") == "Wartende Anfrage abgebrochen."
        assert bridge.status()["waiting_count"] == 0
        send(ctx, event_id="replacement", prompt="new queued")
        assert [w["id"] for w in bridge.status()["waiting"]] == ["replacement"]
        release.set()
        wait_for(lambda: any(m[1] == "Antwort: new queued" for m in ctx.sent))
        assert calls == ["active", "new queued"]
    finally:
        release.set()
        bridge.shutdown()


def test_failed_upstream_stop_can_retry_and_keep_original_answer(monkeypatch):
    import httpx

    ctx, rt = Context(), runtime()
    started, release = threading.Event(), threading.Event()
    called = threading.Event()
    attempts = []

    async def fake_ask(prompt, token, *, stop_event=None, on_started=None):
        on_started("thread-1", "run-1")
        started.set()
        await asyncio.to_thread(release.wait, 2)
        return "Original answer"

    class FakeClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, endpoint, *, headers, json):
            attempts.append(json)
            called.set()
            return httpx.Response(503 if len(attempts) == 1 else 202)

    monkeypatch.setattr("signaldeck_ai.socketio_bridge.httpx.Client", FakeClient)
    bridge = SocketIOChatBridge(config(), ctx, rt)
    bridge._command._ask_once = fake_ask
    try:
        send(ctx, event_id="original", prompt="work")
        assert started.wait(2)
        send(ctx, event_id="stop1", prompt="stop")
        assert called.wait(2)
        wait_for(lambda: bridge.status()["active"]["cancel_error"] is not None)
        failed = bridge.status()["active"]
        assert failed["cancelling"] is False
        assert "nicht bestätigt" in failed["cancel_error"]
        send(ctx, event_id="stop2", prompt="stop")
        wait_for(lambda: len(attempts) == 2)
        wait_for(lambda: bridge.status()["active"]["cancelling"])
        release.set()
        wait_for(lambda: any(m[1] == "Anfrage abgebrochen." for m in ctx.sent))
        assert not any(m[1] == "Original answer" for m in ctx.sent)
    finally:
        release.set()
        bridge.shutdown()


def test_failed_cancel_does_not_discard_normal_success_when_not_retried(monkeypatch):
    import httpx

    ctx, rt = Context(), runtime()
    started, release = threading.Event(), threading.Event()

    async def fake_ask(prompt, token, *, stop_event=None, on_started=None):
        on_started("thread-2", "run-2")
        started.set()
        await asyncio.to_thread(release.wait, 2)
        return "Actual answer"

    class FakeClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, endpoint, *, headers, json):
            return httpx.Response(503)

    monkeypatch.setattr("signaldeck_ai.socketio_bridge.httpx.Client", FakeClient)
    bridge = SocketIOChatBridge(config(), ctx, rt)
    bridge._command._ask_once = fake_ask
    try:
        send(ctx, event_id="request", prompt="test")
        assert started.wait(2)
        send(ctx, event_id="stop-control", prompt="stop")
        wait_for(lambda: bridge.status()["active"]["cancel_error"] is not None)
        assert bridge.status()["active"]["cancelling"] is False
        release.set()
        wait_for(lambda: any(m[1] == "Actual answer" for m in ctx.sent))
    finally:
        release.set()
        bridge.shutdown()



def test_successful_run_waits_for_rejected_cancel_before_publishing(monkeypatch):
    """A late HTTP 503 must not replace an already successful agent answer."""
    import httpx

    ctx, rt = Context(), runtime()
    run_started = threading.Event()
    run_release = threading.Event()
    cancel_started = threading.Event()
    cancel_release = threading.Event()
    calls = []

    async def fake_ask(prompt, token, *, stop_event=None, on_started=None):
        on_started("thread-race", "run-race")
        run_started.set()
        await asyncio.to_thread(run_release.wait, 2)
        return "Real completed answer"

    class FakeClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, endpoint, *, headers, json):
            calls.append(json)
            cancel_started.set()
            assert cancel_release.wait(2)
            return httpx.Response(503)

    monkeypatch.setattr("signaldeck_ai.socketio_bridge.httpx.Client", FakeClient)
    bridge = SocketIOChatBridge(config(), ctx, rt)
    bridge._command._ask_once = fake_ask
    try:
        send(ctx, event_id="run-race-id", prompt="analyze")
        assert run_started.wait(2)
        send(ctx, event_id="cancel-race-id", prompt="stop")
        assert cancel_started.wait(2)
        run_release.set()
        # The backend has completed, but the cancellation result is pending.
        # The worker must not emit the misleading "not confirmed" fallback.
        wait_for(lambda: bridge.status()["active"] is not None)
        assert not any(m[1] == "Real completed answer" for m in ctx.sent)
        assert not any("Anfrage beendet" in m[1] for m in ctx.sent)
        cancel_release.set()
        wait_for(lambda: any(m[1] == "Real completed answer" for m in ctx.sent))
        assert not any("Anfrage beendet" in m[1] for m in ctx.sent)
        assert calls == [{
            "action": "cancel", "threadId": "thread-race", "runId": "run-race",
        }]
    finally:
        cancel_release.set()
        run_release.set()
        bridge.shutdown()


def test_successful_run_waits_for_accepted_cancel_before_publishing(monkeypatch):
    import httpx

    ctx, rt = Context(), runtime()
    run_started, run_release = threading.Event(), threading.Event()
    cancel_started, cancel_release = threading.Event(), threading.Event()

    async def fake_ask(prompt, token, *, stop_event=None, on_started=None):
        on_started("thread-success", "run-success")
        run_started.set()
        await asyncio.to_thread(run_release.wait, 2)
        return "Result must be suppressed"

    class FakeClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, endpoint, *, headers, json):
            cancel_started.set()
            assert cancel_release.wait(2)
            return httpx.Response(202)

    monkeypatch.setattr("signaldeck_ai.socketio_bridge.httpx.Client", FakeClient)
    bridge = SocketIOChatBridge(config(), ctx, rt)
    bridge._command._ask_once = fake_ask
    try:
        send(ctx, event_id="request-success", prompt="analyze")
        assert run_started.wait(2)
        send(ctx, event_id="stop-success", prompt="stop")
        assert cancel_started.wait(2)
        run_release.set()
        assert not any("Result must be suppressed" in m[1] for m in ctx.sent)
        cancel_release.set()
        wait_for(lambda: any(m[1] == "Anfrage abgebrochen." for m in ctx.sent))
        assert not any("Result must be suppressed" in m[1] for m in ctx.sent)
    finally:
        cancel_release.set()
        run_release.set()
        bridge.shutdown()
