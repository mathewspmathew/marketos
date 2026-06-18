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
