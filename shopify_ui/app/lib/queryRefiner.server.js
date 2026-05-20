const MODEL = process.env.GROQ_REFINER_MODEL || "llama-3.3-70b-versatile";
const GROQ_URL = "https://api.groq.com/openai/v1/chat/completions";

const SYSTEM = `You turn merchant product data into a single Google search query that returns direct product pages from sellers (not blog posts or category listings).

Rules:
- Output ONLY the query string. No quotes, no commentary, no labels.
- Lead with the most distinctive descriptor (style, color, material) before the generic noun.
- Include the gender/target if obvious from the title.
- Don't include the merchant's own brand unless it's the literal product type.
- Avoid words like "buy", "shop", "online", "best" — they tank specificity.
- Keep it 6-12 words.`;

export async function refineQuery({ title, vendor, category, description, hint }) {
  const apiKey = process.env.GROQ_API_KEY;
  if (!apiKey) throw new Error("GROQ_API_KEY not set");

  const userMsg = [
    `Title: ${title || "(none)"}`,
    vendor ? `Vendor: ${vendor}` : null,
    category ? `Category: ${category}` : null,
    description ? `Description: ${description.slice(0, 400)}` : null,
    hint ? `Merchant hint: ${hint}` : null,
  ].filter(Boolean).join("\n");

  const res = await fetch(GROQ_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({
      model: MODEL,
      temperature: 0.2,
      max_tokens: 60,
      messages: [
        { role: "system", content: SYSTEM },
        { role: "user", content: userMsg },
      ],
    }),
  });

  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`Groq ${res.status}: ${body.slice(0, 200)}`);
  }
  const data = await res.json();
  return (data.choices?.[0]?.message?.content || "").trim().replace(/^["']|["']$/g, "");
}
