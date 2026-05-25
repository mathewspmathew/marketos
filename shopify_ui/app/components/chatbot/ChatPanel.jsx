import { useState, useRef } from "react";
import PreviewCard from "./PreviewCard";
import AskCard from "./AskCard";

export default function ChatPanel() {
  const [turns, setTurns] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const sessionId = useRef(null);

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

    if (payload.session_id) sessionId.current = payload.session_id;
    for (const { type, data } of payload.events ?? []) {
      if (type === "text") setTurns((t) => [...t, { role: "assistant", text: data.text }]);
      else if (type === "ask") setTurns((t) => [...t, { role: "assistant", ask: data }]);
      else if (type === "preview") setTurns((t) => [...t, { role: "assistant", preview: data }]);
      else if (type === "error") setTurns((t) => [...t, { role: "assistant", text: `Error: ${data.message ?? "unknown"}` }]);
    }
    setBusy(false);
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
      <s-stack direction="block" gap="base">
        {turns.map((t, i) => (
          <div key={i}>
            {t.text && (
              <s-paragraph tone={t.role === "user" ? "subdued" : undefined}>
                {t.role === "user" ? "You: " : ""}{t.text}
              </s-paragraph>
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
            autoComplete="off"
            placeholder="Ask anything…"
          />
          <s-button variant="primary" loading={busy} onClick={() => send(input)}>
            Send
          </s-button>
        </s-stack>
      </s-stack>
    </s-section>
  );
}
