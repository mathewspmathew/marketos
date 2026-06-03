import { useState, useRef, useEffect, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import PreviewCard from "./PreviewCard";
import AskCard from "./AskCard";
import SessionSidebar from "./SessionSidebar";

export default function ChatPanel() {
  const [turns, setTurns] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [sessions, setSessions] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const sessionId = useRef(null);

  const refreshSessions = useCallback(async () => {
    const r = await fetch("/api/sessions");
    const data = await r.json().catch(() => null);
    if (data?.ok) setSessions(data.sessions);
  }, []);

  useEffect(() => {
    refreshSessions();
  }, [refreshSessions]);

  function newChat() {
    sessionId.current = null;
    setActiveId(null);
    setTurns([]);
    setInput("");
  }

  async function openChat(id) {
    setBusy(true);
    const r = await fetch(`/api/sessions/${id}`);
    const data = await r.json().catch(() => null);
    if (data?.ok) {
      sessionId.current = id;
      setActiveId(id);
      setTurns(data.turns);
    }
    setBusy(false);
  }

  async function deleteChat(id) {
    await fetch(`/api/sessions/${id}`, { method: "DELETE" });
    if (id === sessionId.current) newChat();
    refreshSessions();
  }

  async function clearAll() {
    await fetch("/api/sessions", { method: "DELETE" });
    newChat();
    refreshSessions();
  }

  async function send(text) {
    if (!text.trim()) return;
    setTurns((t) => [...t, { role: "user", text }]);
    setInput("");
    setBusy(true);

    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ session_id: sessionId.current, message: text }),
    });

    let payload;
    try { payload = await resp.json(); } catch { payload = null; }
    if (!resp.ok || !payload?.ok) {
      setTurns((t) => [...t, { role: "assistant", text: `Failed to reach assistant${payload?.reason ? ` (${payload.reason})` : ""}.` }]);
      setBusy(false);
      return;
    }

    const isNewSession = !sessionId.current;
    if (payload.session_id) {
      sessionId.current = payload.session_id;
      setActiveId(payload.session_id);
    }
    for (const { type, data } of payload.events ?? []) {
      if (type === "text") setTurns((t) => [...t, { role: "assistant", text: data.text }]);
      else if (type === "ask") setTurns((t) => [...t, { role: "assistant", ask: data }]);
      else if (type === "preview") setTurns((t) => [...t, { role: "assistant", preview: data }]);
      else if (type === "error") setTurns((t) => [...t, { role: "assistant", text: `Error: ${data.message ?? "unknown"}` }]);
    }
    setBusy(false);
    // New chats appear in the list; the title fills in shortly after (async),
    // so refresh now and once more after a short delay.
    refreshSessions();
    if (isNewSession) setTimeout(refreshSessions, 2500);
  }

  async function applyPreview(preview) {
    setBusy(true);
    const r = await fetch("/app/apply-preview", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ preview_id: preview.preview_id, kind: preview.kind }),
    });
    const data = await r.json();
    if (data.ok) {
      setTurns((t) => [...t, { role: "assistant", text: `Applied to ${data.succeeded?.length ?? 0} item(s).` }]);
    } else {
      setTurns((t) => [...t, { role: "assistant", text: `Apply failed: ${data.reason}` }]);
    }
    setBusy(false);
  }

  return (
    <s-section>
      <div style={{ display: "flex", gap: "16px", alignItems: "flex-start" }}>
        <SessionSidebar
          sessions={sessions}
          activeId={activeId}
          onNew={newChat}
          onSelect={openChat}
          onDelete={deleteChat}
          onClearAll={clearAll}
          busy={busy}
        />

        <div style={{ flexGrow: 1, minWidth: 0 }}>
          <s-stack direction="block" gap="base">
            {turns.map((t, i) => (
              <div key={i}>
                {t.text && (
                  t.role === "user" ? (
                    <s-paragraph tone="subdued">You: {t.text}</s-paragraph>
                  ) : (
                    <div className="chat-md">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{t.text}</ReactMarkdown>
                    </div>
                  )
                )}
                {t.preview && (
                  <PreviewCard preview={t.preview} onApply={applyPreview} onCancel={() => {}} busy={busy} />
                )}
                {t.ask && <AskCard ask={t.ask} onAnswer={send} />}
              </div>
            ))}
            <s-stack direction="inline" gap="base">
              <s-text-field
                label=""
                value={input}
                onInput={(e) => setInput(e.currentTarget.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey && !busy) {
                    e.preventDefault();
                    send(input);
                  }
                }}
                autoComplete="off"
                placeholder="Ask anything…"
              />
              <s-button variant="primary" loading={busy} onClick={() => send(input)}>
                Send
              </s-button>
            </s-stack>
          </s-stack>
        </div>
      </div>
    </s-section>
  );
}
