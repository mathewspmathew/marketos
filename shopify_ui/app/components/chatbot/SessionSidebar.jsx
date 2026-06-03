/**
 * SessionSidebar — left-rail list of past chats.
 * Props:
 *   sessions    : [{id, title, updated_at, message_count}]
 *   activeId    : currently open session id (or null)
 *   onNew       : () => void                  start a fresh chat
 *   onSelect    : (id) => void                open a past chat
 *   onDelete    : (id) => void                delete one chat
 *   onClearAll  : () => void                  delete all chats
 *   busy        : bool                        disable controls while loading
 */
export default function SessionSidebar({
  sessions,
  activeId,
  onNew,
  onSelect,
  onDelete,
  onClearAll,
  busy,
}) {
  return (
    <div style={{ width: "220px", flexShrink: 0, borderRight: "1px solid #e1e3e5", paddingRight: "12px" }}>
      <s-stack direction="block" gap="base">
        <s-button variant="primary" onClick={onNew} disabled={busy}>
          + New chat
        </s-button>

        {sessions.length === 0 ? (
          <s-paragraph tone="subdued">No chats yet.</s-paragraph>
        ) : (
          <s-stack direction="block" gap="tight">
            {sessions.map((sess) => (
              <div
                key={sess.id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: "6px",
                  padding: "6px 8px",
                  borderRadius: "8px",
                  cursor: "pointer",
                  background: sess.id === activeId ? "#f1f1f1" : "transparent",
                }}
                onClick={() => onSelect(sess.id)}
              >
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {sess.title || "New chat"}
                </span>
                <s-button
                  variant="tertiary"
                  tone="critical"
                  onClick={(e) => {
                    e.stopPropagation();
                    onDelete(sess.id);
                  }}
                  disabled={busy}
                >
                  ✕
                </s-button>
              </div>
            ))}
          </s-stack>
        )}

        {sessions.length > 0 && (
          <s-button variant="tertiary" tone="critical" onClick={onClearAll} disabled={busy}>
            Clear all
          </s-button>
        )}
      </s-stack>
    </div>
  );
}
