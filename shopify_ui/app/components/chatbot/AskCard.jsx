/**
 * AskCard — renders a single ask_user turn from the chatbot: the question
 * text, optional preset-answer buttons, and a free-text fallback field.
 * Forwards whichever answer the merchant picks via onAnswer.
 */
import { useState } from "react";

export default function AskCard({ ask, onAnswer }) {
  const [custom, setCustom] = useState("");
  return (
    <s-section>
      <s-stack direction="block" gap="base">
        <s-paragraph>{ask.question}</s-paragraph>
        {ask.options?.length ? (
          <s-stack direction="inline" gap="tight">
            {ask.options.map((o) => (
              <s-button key={o} onClick={() => onAnswer(o)}>{o}</s-button>
            ))}
          </s-stack>
        ) : null}
        <s-text-field
          label="Or type your answer"
          value={custom}
          onInput={(e) => setCustom(e.currentTarget.value)}
          autoComplete="off"
        />
        <s-button onClick={() => custom && onAnswer(custom)}>Send</s-button>
      </s-stack>
    </s-section>
  );
}
