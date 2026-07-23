/**
 * ChatPanel — the Assistant tab's main chat UI: session list, message
 * thread, sending messages to /api/chat, and applying the previews
 * (price-change / ask_user turns) the chatbot returns. All chatbot state
 * lives in Python; this component only renders it and forwards actions.
 */
import { useState, useRef, useEffect, useCallback } from "react";
import PropTypes from "prop-types";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import PreviewCard from "./PreviewCard";
import AskCard from "./AskCard";
import SessionSidebar from "./SessionSidebar";

export default function ChatPanel({ currency }) {
  const [turns, setTurns] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [sessions, setSessions] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const sessionId = useRef(null);
  const delayedRefreshRef = useRef(null);

  const refreshSessions = useCallback(async () => {
    const r = await fetch("/api/sessions");
    const data = await r.json().catch(() => null);
    if (data?.ok) setSessions(data.sessions);
  }, []);

  useEffect(() => {
    refreshSessions();
    // Cancel any pending delayed refresh on unmount to avoid setState after unmount.
    return () => {
      if (delayedRefreshRef.current != null) clearTimeout(delayedRefreshRef.current);
    };
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
    const r = await fetch(`/api/sessions/${id}`, { method: "DELETE" }).catch(() => null);
    if (!r?.ok) {
      setTurns((t) => [...t, { role: "assistant", text: "Couldn't delete chat." }]);
      return;
    }
    if (id === sessionId.current) newChat();
    refreshSessions();
  }

  async function clearAll() {
    const r = await fetch("/api/sessions", { method: "DELETE" }).catch(() => null);
    if (!r?.ok) {
      setTurns((t) => [...t, { role: "assistant", text: "Couldn't clear chats." }]);
      return;
    }
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
    if (isNewSession) delayedRefreshRef.current = setTimeout(refreshSessions, 2500);
  }

  async function applyPreview(preview, extras = {}) {
    setBusy(true);
    const r = await fetch("/app/apply-preview", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ preview_id: preview.preview_id, kind: preview.kind, ...extras }),
    }).catch(() => null);
    let data; try { data = await r?.json(); } catch { data = null; }
    if (!r?.ok || !data?.ok) {
      const REASON_TEXT = {
        state_changed: "This product's dynamic-pricing status changed since the card was created — ask me again to get a fresh card.",
        already_applied: "This card was already applied.",
        expired: "This card expired — ask me again to get a fresh one.",
      };
      const failMsg = REASON_TEXT[data?.reason] ?? `Apply failed: ${data?.reason ?? "network error"}`;
      setTurns((t) => [...t, { role: "assistant", text: failMsg }]);
      setBusy(false);
      return;
    }
    const ACTION_DONE = {
      enable: "Dynamic pricing set up — first competitor fetch is on the way.",
      resume: "Dynamic pricing resumed.",
      pause: "Dynamic pricing paused — competitor data kept.",
      delete: "Dynamic pricing turned off and competitor data deleted.",
    };
    const doneMsg = data.action
      ? (ACTION_DONE[data.action] ?? "Done.")
      : `Applied to ${data.succeeded?.length ?? 0} item(s).`;
    setTurns((t) => [...t, { role: "assistant", text: doneMsg }]);
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
            {turns.length > 0 && (
              <div style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
                {turns.map((t, i) => (
                  <div key={i}>
                    {t.text && (
                      t.role === "user" ? (
                        <div style={{ display: "flex", justifyContent: "flex-end" }}>
                          <div
                            style={{
                              background: "var(--s-color-bg-subdued, #f1f1f1)",
                              borderRadius: "12px",
                              padding: "8px 12px",
                              maxWidth: "75%",
                              overflowWrap: "anywhere",
                            }}
                          >
                            {t.text}
                          </div>
                        </div>
                      ) : (
                        <div className="chat-md">
                          <ReactMarkdown remarkPlugins={[remarkGfm]}>{t.text}</ReactMarkdown>
                        </div>
                      )
                    )}
                    {t.preview && (
                      <PreviewCard preview={t.preview} currency={currency} onApply={applyPreview} onCancel={() => {}} busy={busy} /> // TODO: wire cancel
                    )}
                    {t.ask && <AskCard ask={t.ask} onAnswer={send} />}
                  </div>
                ))}
              </div>
            )}
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

ChatPanel.propTypes = {
  currency: PropTypes.string,
};
