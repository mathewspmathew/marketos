export default function PreviewCard({ preview, onApply, onCancel, busy }) {
  const s = preview.summary || {};
  const isPrice = preview.kind === "price_change";
  return (
    <s-section>
      <s-stack direction="block" gap="base">
        <s-text emphasis="bold">
          {isPrice ? "Price change preview" : "Dynamic pricing toggle preview"}
        </s-text>
        <s-paragraph>
          {s.count} item(s)
          {s.minNew != null && isPrice
            ? `, new range $${s.minNew}–$${s.maxNew} (avg $${(s.avgNew ?? 0).toFixed(2)})`
            : ""}
        </s-paragraph>
        <s-stack direction="inline" gap="base">
          <s-button variant="primary" loading={busy} onClick={() => onApply(preview)}>
            Apply
          </s-button>
          <s-button onClick={onCancel}>Cancel</s-button>
        </s-stack>
      </s-stack>
    </s-section>
  );
}
