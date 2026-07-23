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
import PropTypes from "prop-types";

export default function SessionSidebar({
  sessions,
  activeId = null,
  onNew,
  onSelect,
  onDelete,
  onClearAll,
  busy = false,
}) {
  return (
    <div style={{ width: "220px", flexShrink: 0, borderRight: "1px solid var(--s-color-border, #e1e3e5)", paddingRight: "12px" }}>
      <s-stack direction="block" gap="base">
        <s-button variant="primary" onClick={onNew} disabled={busy}>
          + New chat
        </s-button>

        {sessions.length === 0 ? (
          <s-paragraph tone="subdued">No chats yet.</s-paragraph>
        ) : (
          <s-stack direction="block" gap="small">
            {sessions.map((sess) => (
              <div
                key={sess.id}
                role="button"
                tabIndex={0}
                aria-disabled={busy}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: "6px",
                  padding: "6px 8px",
                  borderRadius: "8px",
                  cursor: "pointer",
                  background: sess.id === activeId ? "var(--s-color-bg-subdued, #f1f1f1)" : "transparent",
                  opacity: busy ? 0.6 : 1,
                  pointerEvents: busy ? "none" : "auto",
                }}
                onClick={() => !busy && onSelect(sess.id)}
                onKeyDown={(e) => { if (!busy && (e.key === "Enter" || e.key === " ")) onSelect(sess.id); }}
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

SessionSidebar.propTypes = {
  sessions: PropTypes.arrayOf(
    PropTypes.shape({
      id: PropTypes.string.isRequired,
      title: PropTypes.string,
      updated_at: PropTypes.string,
      message_count: PropTypes.number,
    })
  ).isRequired,
  activeId: PropTypes.string,
  onNew: PropTypes.func.isRequired,
  onSelect: PropTypes.func.isRequired,
  onDelete: PropTypes.func.isRequired,
  onClearAll: PropTypes.func.isRequired,
  busy: PropTypes.bool,
};

