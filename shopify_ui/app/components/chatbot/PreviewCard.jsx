import PropTypes from "prop-types";
import DynamicPricingCard from "./DynamicPricingCard";

export default function PreviewCard({ preview, onApply, onCancel, busy }) {
  if (preview.kind === "dynamic_pricing_toggle") {
    return (
      <DynamicPricingCard
        preview={preview}
        onApply={onApply}
        onCancel={onCancel}
        busy={busy}
      />
    );
  }
  // Dynamic-pricing toggles are handled by DynamicPricingCard above; this path
  // renders price-change previews only.
  const s = preview.summary || {};
  return (
    <s-section>
      <s-stack direction="block" gap="base">
        <s-text emphasis="bold">Price change preview</s-text>
        <s-paragraph>
          {s.count} item(s)
          {s.minNew != null
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

PreviewCard.propTypes = {
  preview: PropTypes.object.isRequired,
  onApply: PropTypes.func.isRequired,
  onCancel: PropTypes.func.isRequired,
  busy: PropTypes.bool,
};
