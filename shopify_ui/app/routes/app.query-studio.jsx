import { useState } from "react";
import QueryStudioPanel from "../components/chatbot/QueryStudioPanel";

export default function QueryStudioTestPage() {
  const [pid, setPid] = useState("");
  const [active, setActive] = useState("");
  const [selected, setSelected] = useState("");

  return (
    <s-page heading="Query Studio (test)">
      <s-section>
        <s-stack direction="inline" gap="base" align="end">
          <s-text-field
            label="Product ID"
            value={pid}
            placeholder="gid://shopify/Product/..."
            onInput={(e) => setPid(e.currentTarget.value)}
          />
          <s-button variant="primary" onClick={() => setActive(pid)}>
            Open
          </s-button>
        </s-stack>
        {selected && <s-text emphasis="bold">Selected query: {selected}</s-text>}
      </s-section>

      {active && <QueryStudioPanel productId={active} onUse={(q) => setSelected(q)} />}
    </s-page>
  );
}
