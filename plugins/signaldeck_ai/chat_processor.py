"""DisplayData-based chat processor using the native SignalDeck action contract."""
from __future__ import annotations

from urllib.parse import urlsplit
from flask import request
from signaldeck_sdk import DisplayProcessor, DisplayData

from .chat_runtime import ChatActionError, ChatRuntime
from .llm_command import LLMCommand
from .socketio_bridge import SocketIOChatBridge


class ChatDisplayData(DisplayData):
    def __init__(self, ctx, action_hash, processor, value):
        super().__init__(ctx, action_hash)
        self.name = processor.name
        self.api_base = f"/plugin/ai/api/chat/{processor.name}"
        self.view_key = processor.runtime.normalize_view(value)


class AGUIChatProcessor(DisplayProcessor):
    """All user actions use /run; only streamed responses use plugin HTTP.

    The inherited DisplayProcessor.process would rerender the chat panel
    after every action and destroy its active SSE stream. Keep the action
    dispatch and validation, return a small JSON acknowledgment instead.
    """

    def __init__(self, name, config, ctx, valueProvider, collect_data):
        super().__init__(name, config, ctx, valueProvider, collect_data)
        endpoint = str(config.get("endpoint", "")).rstrip("/")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/ag-ui"}
        ):
            raise ValueError("AG-UI endpoint must be http://localhost:<port>/ag-ui")
        if parsed.port is None or parsed.port < 1 or parsed.port > 65535:
            raise ValueError("AG-UI endpoint requires a TCP port")
        self.endpoint = endpoint if parsed.path == "/ag-ui" else endpoint + "/ag-ui"
        self.runtime = ChatRuntime(self.endpoint)
        bridge = config.get("socketio_bridge")
        self.socketio_bridge = (
            SocketIOChatBridge(bridge, ctx, self.runtime)
            if bridge is not None else None
        )

    def registerCommands(self, cmd):
        config = self.config.get("cmd")
        if config is None:
            return
        if not isinstance(config, dict) or set(config) - {"name", "timeout_seconds"}:
            raise ValueError("AGUI cmd configuration supports name and timeout_seconds")
        name = config.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("AGUI cmd.name is required")
        # Cmd.registerCmd normally silently replaces an existing name.
        # For per-instance LLM commands this is an invalid configuration.
        if name in cmd.commands:
            raise ValueError(f"Cmd '{name}' already registered by another processor")
        cmd.registerCmd(LLMCommand(
            name, self.runtime,
            timeout_seconds=config.get("timeout_seconds", 120),
        ))

    def getTemplate(self, value):
        return "ai/chat.html"

    def getDisplayData(self, value, actionHash, **kwargs):
        return ChatDisplayData(self.ctx, actionHash, self, value)

    def getAdditionalJsFiles(self, value):
        return [
            ("signaldeck_ai", "js/markdown-it.min.js"),
            ("signaldeck_ai", "js/chat_markdown.js"),
            ("signaldeck_ai", "js/chat.js"),
        ]

    def getAdditionalCssFiles(self, value):
        return [("signaldeck_ai", "css/chat.css")]

    def getJS_functions_to_call_on_client(self, data):
        return {f"signaldeck-ai-chat-{data.hash}": "initSignalDeckAIChat"}

    def process(self, value, actionHash, file=None, **params):
        # The core /run endpoint also supports GET; disallow that path for
        # credentials and state-changing chat commands.
        if request.method != "POST":
            return {"ok": False, "error": "POST erforderlich."}
        if request.headers.get("Origin") != f"{request.scheme}://{request.host}":
            return {"ok": False, "error": "Same-Origin-Anfrage erforderlich."}
        if file is not None:
            return {"ok": False, "error": "Dateiupload nicht unterstützt."}
        try:
            return {"ok": True, **self.performActions(value, actionHash, **params)}
        except ChatActionError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            self.logger.exception("AG-UI chat action failed")
            return {"ok": False, "error": "Chat-Aktion fehlgeschlagen."}

    def performActions(self, value, actionHash, **kwargs):
        action = kwargs.get("ai_action")
        view_key = self.runtime.normalize_view(value)
        identifier = kwargs.get("connection_id", "")
        if action == "state":
            return self.runtime.view_state(view_key)
        if action != "connect":
            self.runtime.assert_view(view_key, identifier)
        if action == "connect":
            # The token is supplied only at runtime, never in config or
            # DisplayData. It is not returned or logged.
            return {"connection_id": self.runtime.verify_and_connect(kwargs.get("token"), view_key)}
        if action == "cancel":
            return {"run_id": self.runtime.cancel(identifier)}
        if action == "send":
            return {"run_id": self.runtime.start_message(identifier, kwargs.get("message"))}
        if action == "approve":
            return {"run_id": self.runtime.approve(
                identifier, kwargs.get("interrupt_id"), kwargs.get("decision"),
            )}
        if action in {"reset_history", "new_chat"}:
            # Keep the authenticated connection, but start a fresh thread
            # with no previous messages, tool approvals or SSE run events.
            # "new_chat" remains an alias for already loaded browser clients.
            self.runtime.new_chat(identifier)
            return {"history_reset": True}
        if action == "disconnect":
            self.runtime.disconnect(identifier)
            return {}
        raise ChatActionError("Unbekannte Chat-Aktion.")

    def shutdown(self):
        if self.socketio_bridge is not None:
            self.socketio_bridge.shutdown()
        self.runtime.shutdown()
