import PropTypes from "prop-types";
import { getCurrencySymbol } from "../../lib/currencyFormatter";

// dynamic_pricing_toggle previews can't occur — the only tool that would
// create one, open_dynamic_pricing_panel, is unregistered/dead (see
// services/chatbot_svc/tools/panel.py). The live chatbot applies dynamic-
// pricing changes directly (apply_dynamic_pricing_config et al.), no card.
// This component only ever renders price-change previews.
export default function PreviewCard({ preview, currency, onApply, onCancel, busy }) {
  const s = preview.summary || {};
  const sym = getCurrencySymbol(currency);
  return (
    <s-section>
      <s-stack direction="block" gap="base">
        <s-text emphasis="bold">Price change preview</s-text>
        <s-paragraph>
          {s.count} item(s)
          {s.minNew != null
            ? `, new range ${sym}${s.minNew}–${sym}${s.maxNew} (avg ${sym}${(s.avgNew ?? 0).toFixed(2)})`
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
  currency: PropTypes.string,
  onApply: PropTypes.func.isRequired,
  onCancel: PropTypes.func.isRequired,
  busy: PropTypes.bool,
};
