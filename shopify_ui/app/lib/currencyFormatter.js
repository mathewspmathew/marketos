// Static ISO-currency-code → display-symbol lookup, for formatting prices
// in the shop's configured currency. i18n data, not a business rule — no
// Python equivalent needed. Chatbot preview cards should use this rather
// than hardcoding a symbol (they show whatever currency the shop uses).
const CURRENCY_SYMBOLS = {
  USD: "$",
  EUR: "€",
  GBP: "£",
  INR: "₹",
  JPY: "¥",
  AUD: "A$",
  CAD: "C$",
  CHF: "CHF",
  CNY: "¥",
  SEK: "kr",
  NZD: "NZ$",
};

export function getCurrencySymbol(currencyCode) {
  return CURRENCY_SYMBOLS[currencyCode?.toUpperCase()] || currencyCode || "$";
}

export function formatPrice(price, currencyCode) {
  const symbol = getCurrencySymbol(currencyCode);
  const numPrice = Number(price);
  return `${symbol}${numPrice.toFixed(2)}`;
}
