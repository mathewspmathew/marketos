import { useState } from "react";
import PropTypes from "prop-types";

export default function QueryStudioPanel({ productId, productTitle = "", onUse }) {
  const [focus, setFocus] = useState("");
  const [instruction, setInstruction] = useState("");
  const [candidates, setCandidates] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function call(payload) {
    setBusy(true);
    setError("");
    try {
      const r = await fetch("/api/query-studio", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ product_id: productId, ...payload }),
      });
      const data = await r.json().catch(() => null);
      if (!r.ok || !data?.ok) {
        setError("Couldn't reach Query Studio.");
        return;
      }
      setCandidates(data.candidates ?? []);
    } finally {
      setBusy(false);
    }
  }

  const propose = () => call({ mode: "propose", focus });
  const refine = () => call({ mode: "refine", focus, prior: candidates, instruction });
  const editQuery = (i, val) =>
    setCandidates((cs) => cs.map((c, j) => (j === i ? { ...c, query: val } : c)));

  return (
    <s-section>
      <s-stack direction="block" gap="base">
        <s-text emphasis="bold">
          {productTitle ? `Query Studio · ${productTitle}` : "Query Studio"}
        </s-text>

        <s-stack direction="inline" gap="base" align="end">
          <s-text-field
            label="Focus"
            value={focus}
            placeholder="e.g. premium chino brands"
            onInput={(e) => setFocus(e.currentTarget.value)}
          />
          <s-button variant="primary" loading={busy || undefined} onClick={propose}>
            Find
          </s-button>
        </s-stack>

        {error && <s-text>{error}</s-text>}

        {candidates.map((c, i) => (
          <s-stack key={i} direction="block" gap="tight">
            <s-stack direction="inline" gap="base" align="end">
              <s-text-field
                label={`Candidate ${i + 1} · ${c.confidence}/10`}
                value={c.query}
                onInput={(e) => editQuery(i, e.currentTarget.value)}
              />
              <s-button onClick={() => onUse && onUse(c.query)}>Use</s-button>
            </s-stack>
            {c.reason && <s-text>{c.reason}</s-text>}
          </s-stack>
        ))}

        {candidates.length > 0 && (
          <s-stack direction="inline" gap="base" align="end">
            <s-text-field
              label="Refine"
              value={instruction}
              placeholder="e.g. cheaper, under $40"
              onInput={(e) => setInstruction(e.currentTarget.value)}
            />
            <s-button loading={busy || undefined} onClick={refine}>
              Refine
            </s-button>
          </s-stack>
        )}
      </s-stack>
    </s-section>
  );
}

QueryStudioPanel.propTypes = {
  productId: PropTypes.string.isRequired,
  productTitle: PropTypes.string,
  onUse: PropTypes.func,
};
