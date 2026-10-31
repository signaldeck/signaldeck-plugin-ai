/* SignalDeck action integration and a read-only AG-UI SSE listener. */
(function () {
  function initSignalDeckAIChat(element) {
    const root = typeof element === "string" ? document.getElementById(element) : element;
    if (!root || root.dataset.initialized === "true") return;
    root.dataset.initialized = "true";

    const base = root.dataset.endpoint;
    const actionHash = root.dataset.actionhash;
    const status = root.querySelector(".sdai-status");
    const tokenField = root.querySelector(".sdai-token");
    const transcript = root.querySelector(".sdai-transcript");
    const connect = root.querySelector(".sdai-connect");
    const fresh = root.querySelector(".sdai-new");
    const field = root.querySelector(".sdai-input");
    const send = root.querySelector(".sdai-send");
    const stop = root.querySelector(".sdai-stop");
    const approvals = root.querySelector(".sdai-approval");
    const approvalText = root.querySelector(".sdai-approval-text");
    const form = root.querySelector(".sdai-form");
    let connectionId = null, busy = false, remoteBusy = false, interrupt = null;
    let lastMessages = null, refreshing = false;
    const assistantMarkdown = new WeakMap();
    const pendingMarkdown = new Set();
    let markdownFrame = null;
    // Tool entries are display-only. They never contain arguments or results.
    const toolGroups = new Map();
    // Suppress obsolete download IDs even if an earlier state poll finishes late.
    const expiredDownloadIds = new Set();
    const statusText = {
      running: "Läuft", success: "Erfolgreich", error: "Fehler",
      denied: "Abgelehnt", approval: "Freigabe erforderlich",
      finished: "Beendet"
    };
    function clearTranscript() {
      transcript.replaceChildren();
      toolGroups.clear();
      pendingMarkdown.clear();
    }

    function flushMarkdown() {
      markdownFrame = null;
      for (const node of pendingMarkdown) {
        const source = assistantMarkdown.get(node) || "";
        try {
          const html = window.signaldeckAIMarkdown?.render(source);
          if (typeof html === "string") node.innerHTML = html;
          else node.textContent = source;
        } catch (_) {
          node.textContent = source;
        }
      }
      pendingMarkdown.clear();
      transcript.scrollTop = transcript.scrollHeight;
    }

    function updateAssistant(node, value, append = false) {
      const previous = assistantMarkdown.get(node) || "";
      assistantMarkdown.set(node, append ? previous + value : value);
      pendingMarkdown.add(node);
      if (markdownFrame === null) {
        markdownFrame = window.requestAnimationFrame(flushMarkdown);
      }
    }

    function renderToolGroup(group, live = false) {
      if (!group || !Array.isArray(group.tools) || !group.tools.length) return null;
      const details = document.createElement("details");
      details.className = "sdai-tool-group";
      details.dataset.runId = group.id || "";
      details.open = live;
      const heading = document.createElement("summary");
      heading.textContent = "Tool-Aufrufe (" + group.tools.length + ")";
      details.appendChild(heading);
      const list = document.createElement("ul");
      list.className = "sdai-tool-list";
      details.appendChild(list);
      transcript.appendChild(details);
      if (group.id) toolGroups.set(group.id, {details, heading, list});
      for (const tool of group.tools) renderToolItem(list, tool);
      transcript.scrollTop = transcript.scrollHeight;
      return details;
    }

    function renderToolItem(list, tool) {
      const entry = document.createElement("li");
      entry.className = "sdai-tool-entry";
      entry.dataset.callId = tool.id || "";
      const name = document.createElement("span");
      name.className = "sdai-tool-name";
      name.textContent = tool.name || "Unbekanntes Tool";
      const state = document.createElement("span");
      state.className = "sdai-tool-status";
      state.textContent = statusText[tool.status] || "Beendet";
      entry.append(name, state);
      list.appendChild(entry);
    }

    function liveToolStart(runId, callId, name) {
      if (!callId) return;
      // An approval interrupt can resume the SAME tool in a new AG-UI run.
      // Update its existing item rather than briefly rendering it twice.
      for (const previous of toolGroups.values()) {
        const match = Array.from(previous.list.children).find(
          node => node.dataset.callId === callId
        );
        if (match) {
          match.querySelector(".sdai-tool-status").textContent = statusText.running;
          return;
        }
      }
      let group = toolGroups.get(runId);
      if (!group) {
        const details = document.createElement("details");
        details.className = "sdai-tool-group";
        details.open = true;
        details.dataset.runId = runId;
        const heading = document.createElement("summary");
        details.appendChild(heading);
        const list = document.createElement("ul");
        list.className = "sdai-tool-list";
        details.appendChild(list);
        transcript.appendChild(details);
        group = {details, heading, list};
        toolGroups.set(runId, group);
      }
      const existing = Array.from(group.list.children).find(
        node => node.dataset.callId === callId
      );
      if (!existing) {
        renderToolItem(group.list, {id: callId, name, status: "running"});
      }
      group.heading.textContent = "Tool-Aufrufe (" + group.list.children.length + ")";
      transcript.scrollTop = transcript.scrollHeight;
    }

    function liveToolStatus(callId, value) {
      for (const group of toolGroups.values()) {
        const entry = Array.from(group.list.children).find(
          node => node.dataset.callId === callId
        );
        if (entry) {
          entry.querySelector(".sdai-tool-status").textContent =
            statusText[value] || "Beendet";
          break;
        }
      }
    }

    function renderDownload(item) {
      if (!item || !/^[a-f0-9]{32}$/.test(item.id || "") ||
          expiredDownloadIds.has(item.id)) return;
      const row = document.createElement("div");
      row.className = "sdai-message sdai-download";
      row.dataset.artifactId = item.id;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "sdai-download-button";
      button.textContent = "Datei herunterladen: " + (item.filename || "Export");
      button.addEventListener("click", async () => {
        if (!connectionId) return;
        button.disabled = true;
        try {
          const response = await fetch(
            base + "/download/" + encodeURIComponent(item.id),
            {method: "GET", credentials: "same-origin",
             headers: {"X-SignalDeck-AI-Connection": connectionId}}
          );
          if (!response.ok) throw new Error("Download nicht mehr verfügbar");
          const blob = await response.blob();
          const objectUrl = URL.createObjectURL(blob);
          const link = document.createElement("a");
          link.href = objectUrl;
          link.download = item.filename || "export";
          link.click();
          window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
        } catch (error) {
          render("info", String(error.message || error));
        } finally {
          button.disabled = false;
        }
      });
      row.appendChild(button);
      transcript.appendChild(row);
      return row;
    }

    function expirePreviousDownloads() {
      for (const row of transcript.querySelectorAll(".sdai-download")) {
        if (row.dataset.artifactId) expiredDownloadIds.add(row.dataset.artifactId);
        row.remove();
      }
      // The next successful state refresh must rebuild from server state.
      lastMessages = null;
    }

    function render(role, value) {
      const node = document.createElement("div");
      node.className = "sdai-message sdai-" + role;
      if (role === "assistant") updateAssistant(node, String(value || ""));
      else node.textContent = value;
      transcript.appendChild(node);
      transcript.scrollTop = transcript.scrollHeight;
      return node;
    }

    function updateUI() {
      const connected = Boolean(connectionId);
      connect.textContent = connected ? "Trennen" : "Verbinden";
      connect.disabled = busy || remoteBusy;
      tokenField.hidden = connected;
      tokenField.disabled = connected || busy;
      field.disabled = !connected || busy || remoteBusy || Boolean(interrupt);
      send.disabled = !connected || busy || remoteBusy || Boolean(interrupt);
      stop.hidden = !(connected && (busy || remoteBusy));
      stop.disabled = !connected || !(busy || remoteBusy);
      fresh.disabled = !connected || busy || remoteBusy;
      status.textContent = (busy || remoteBusy) ? "Agent arbeitet ..." :
        connected ? (interrupt ? "Freigabe erforderlich" : "Verbunden") : "Nicht verbunden";
    }

    async function action(ai_action, params = {}) {
      const fd = new FormData();
      fd.append("payload", JSON.stringify({
        actionhash: actionHash,
        ai_action,
        ...(connectionId ? {connection_id: connectionId} : {}),
        ...params
      }));
      const response = await fetch("/run", {
        method: "POST", body: fd, credentials: "same-origin",
        headers: {"Accept": "application/json"},
      });
      if (!response.ok) throw new Error("HTTP " + response.status);
      const result = await response.json();
      if (!result.ok) throw new Error(result.error || "Aktion fehlgeschlagen");
      return result;
    }

    async function syncState() {
      // SignalDeck has no browser user sessions. All browsers opening the same
      // configured view value intentionally share a server-side agent thread.
      if (busy || refreshing || !root.isConnected) return;
      refreshing = true;
      try {
        const state = await action("state");
        const previousConnection = connectionId;
        connectionId = state.connected ? state.connection_id : null;
        remoteBusy = Boolean(state.busy);
        const messages = state.messages || [];
        const serialized = JSON.stringify(messages);
        if (serialized !== lastMessages || previousConnection !== connectionId) {
          if (previousConnection !== connectionId) expiredDownloadIds.clear();
          const openGroups = new Set(
            Array.from(toolGroups.entries())
              .filter(([, group]) => group.details.open)
              .map(([id]) => id)
          );
          clearTranscript();
          for (const msg of messages) {
            if (msg.role === "download") {
              renderDownload(msg);
            } else if (msg.role === "tools") {
              const group = renderToolGroup(msg);
              if (group && openGroups.has(msg.id)) group.open = true;
            } else if (msg.role === "user" || msg.role === "assistant") {
              render(msg.role, msg.content);
            }
          }
          lastMessages = serialized;
        }
        interrupt = state.pending_interrupt || null;
        if (interrupt) {
          approvalText.textContent = interrupt.message || ("Freigabe: " + (interrupt.reason || "Tool"));
          approvals.hidden = false;
        } else {
          approvals.hidden = true;
        }
        updateUI();
      } catch (error) {
        // The next poll can recover after a transient /run failure.
      } finally {
        refreshing = false;
      }
    }

    connect.addEventListener("click", async () => {
      if (busy) return;
      busy = true;
      updateUI();
      try {
        if (connectionId) {
          await action("disconnect");
          connectionId = null;
          interrupt = null;
          clearTranscript();
          expiredDownloadIds.clear();
          lastMessages = null;
          approvals.hidden = true;
        } else {
          const token = tokenField.value.trim();
          if (!token) throw new Error("Bitte API-Token eingeben.");
          const result = await action("connect", {token});
          connectionId = result.connection_id;
          if (!connectionId) throw new Error("Verbindungskennung fehlt.");
          tokenField.value = "";
          lastMessages = null;
          await syncState();
        }
      } catch (err) {
        render("info", "Verbindung: " + String(err.message || err));
      } finally {
        busy = false;
        updateUI();
        void syncState();
      }
    });

    fresh.addEventListener("click", async () => {
      if (!connectionId || busy) return;
      if (!window.confirm(
        "Chat-Verlauf zurücksetzen? Die bisherige Konversation und offene " +
        "Freigaben werden verworfen. Die Verbindung zum Agenten bleibt bestehen."
      )) return;
      busy = true;
      updateUI();
      try {
        const result = await action("reset_history");
        if (!result.history_reset) throw new Error("Zurücksetzen nicht bestätigt.");
        interrupt = null;
        approvals.hidden = true;
        clearTranscript();
        expiredDownloadIds.clear();
        lastMessages = null;
      } catch (err) {
        render("info", "Verlauf zurücksetzen: " + String(err.message || err));
      } finally {
        busy = false;
        updateUI();
        void syncState();
      }
    });

    async function listen(runId) {
      const response = await fetch(
        base + "/events/" + encodeURIComponent(runId),
        {method: "GET", credentials: "same-origin",
         headers: {"X-SignalDeck-AI-Connection": connectionId, "Accept": "text/event-stream"}}
      );
      if (!response.ok || !response.body) throw new Error("SSE HTTP " + response.status);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let completed = false;
      const assistantNodes = new Map();

      function dispatch(evt) {
        switch (evt.type) {
          case "TEXT_MESSAGE_START":
            if (evt.role === "assistant") {
              assistantNodes.set(evt.messageId, render("assistant", ""));
            }
            break;
          case "TEXT_MESSAGE_CONTENT": {
            let node = assistantNodes.get(evt.messageId);
            if (!node) {
              node = render("assistant", "");
              assistantNodes.set(evt.messageId, node);
            }
            updateAssistant(node, String(evt.delta || ""), true);
            break;
          }
          case "TEXT_MESSAGE_END":
            flushMarkdown();
            break;

          case "TOOL_CALL_START":
            liveToolStart(runId, evt.toolCallId, evt.toolCallName);
            break;
          case "TOOL_CALL_RESULT":
            if (Array.isArray(evt.content)) {
              for (const part of evt.content) {
                if (part?.type === "document" && part.metadata?.artifactId) {
                  renderDownload({
                    id: part.metadata.artifactId,
                    filename: part.metadata.filename,
                    mimeType: part.source?.mimeType,
                  });
                }
              }
              liveToolStatus(evt.toolCallId, "success");
            } else {
              liveToolStatus(evt.toolCallId, evt.content);
            }
            break;
          case "RUN_ERROR":
            render("info", "Agent-Fehler: " + (evt.message || "unbekannt"));
            completed = true;
            break;
          case "RUN_FINISHED":
            completed = true;
            if (evt.outcome?.type === "interrupt") {
              const item = evt.outcome.interrupts?.[0];
              if (item && item.id) {
                interrupt = item;
                approvalText.textContent = item.message || ("Freigabe: " + item.reason);
                approvals.hidden = false;
              }
            }
            break;
        }
      }

      function parseFrame(frame) {
        const data = frame.split("\n").filter(line => line.startsWith("data:"))
          .map(line => line.slice(5).trimStart()).join("\n");
        if (data) dispatch(JSON.parse(data));
      }

      try {
        while (true) {
          const {value, done} = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, {stream:true}).replace(/\r\n/g, "\n");
          let index;
          while ((index = buffer.indexOf("\n\n")) >= 0) {
            parseFrame(buffer.slice(0, index));
            buffer = buffer.slice(index + 2);
          }
        }
        if (buffer.trim()) parseFrame(buffer);
        if (!completed) render("info", "AG-UI-Stream unerwartet beendet.");
      } finally {
        flushMarkdown();
        reader.releaseLock();
      }
    }

    async function runAction(name, args) {
      busy = true;
      updateUI();
      try {
        const result = await action(name, args);
        // Only a *new, accepted* user question invalidates previous artifacts.
        // Resume/approval calls must retain downloads from the current question.
        if (name === "send") expirePreviousDownloads();
        await listen(result.run_id);
      } catch (err) {
        render("info", "Agentenlauf: " + String(err.message || err));
      } finally {
        busy = false;
        updateUI();
        void syncState();
      }
    }

    stop.addEventListener("click", () => {
      if (!connectionId) return;
      stop.disabled = true;
      void action("cancel").then(() => { status.textContent = "Abbruch angefordert ..."; })
        .catch(error => { status.textContent = String(error.message || error); stop.disabled = false; });
    });

    form.addEventListener("submit", event => {
      event.preventDefault();
      if (!connectionId || busy || interrupt) return;
      const text = field.value.trim();
      if (!text) return;
      field.value = "";
      render("user", text);
      void runAction("send", {message: text});
    });

    approvals.addEventListener("click", event => {
      const decision = event.target.dataset.decision;
      if (!interrupt || busy || !["yes","no","session"].includes(decision)) return;
      const interruptId = interrupt.id;
      // Keep the current approval displayed if the server rejects the action.
      busy = true;
      updateUI();
      (async () => {
        try {
          const result = await action("approve", {interrupt_id: interruptId, decision});
          interrupt = null;
          approvals.hidden = true;
          await listen(result.run_id);
        } catch (err) {
          render("info", "Freigabe: " + String(err.message || err));
        } finally {
          busy = false;
          updateUI();
          void syncState();
        }
      })();
    });

    updateUI();
    void syncState();
    const poll = window.setInterval(() => {
      if (!root.isConnected) {
        window.clearInterval(poll);
        return;
      }
      void syncState();
    }, 1800);
  }

  window.initSignalDeckAIChat = initSignalDeckAIChat;
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".sdai-chat").forEach(initSignalDeckAIChat);
  });
})();
