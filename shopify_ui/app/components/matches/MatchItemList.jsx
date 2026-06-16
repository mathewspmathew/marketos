import { Card, Text, Button, Stack, Badge, Box } from "@shopify/polaris";
import { ExternalIcon } from "@shopify/polaris-icons";

export function MatchItemList({ matches, onConfirm, onReject, loading = false }) {
  if (!matches || matches.length === 0) {
    return (
      <Box paddingBlockStart="400">
        <Text as="p" tone="subdued">No matches found.</Text>
      </Box>
    );
  }

  return (
    <Stack gap="300" wrap={false}>
      {matches.map((m) => (
        <Card key={m.id}>
          <Box padding="400">
            <Stack gap="300">
              {/* Header: Title, Domain, Confidence */}
              <Stack gap="200" wrap={false}>
                {m.scrapedImageUrl && (
                  <Box minWidth="60px" minHeight="60px">
                    <img
                      src={m.scrapedImageUrl}
                      alt={m.scrapedTitle}
                      width={60}
                      height={60}
                      style={{ objectFit: "cover", borderRadius: 4 }}
                    />
                  </Box>
                )}
                <Stack gap="100" grow>
                  <Text as="p" variant="headingSm">{m.scrapedTitle}</Text>
                  <Stack gap="200" wrap={false} align="center">
                    <Badge tone={m.confidenceTier === "CONFIRMED" ? "success" : "info"}>
                      {m.confidenceTier} ({(m.confidence * 100).toFixed(0)}%)
                    </Badge>
                    <Badge>{m.scrapedDomain}</Badge>
                    {m.confirmedByMerchant && (
                      <Badge tone="success">✓ Confirmed</Badge>
                    )}
                  </Stack>
                </Stack>
              </Stack>

              {/* Price & Link */}
              <Stack gap="200" wrap={false} align="center">
                {m.competitorPrice && (
                  <Text as="span">Their price: ₹{m.competitorPrice}</Text>
                )}
                {m.competitorUrl && (
                  <Button
                    variant="plain"
                    url={m.competitorUrl}
                    target="_blank"
                    icon={ExternalIcon}
                  >
                    Open
                  </Button>
                )}
              </Stack>

              {/* Actions */}
              {m.confidenceTier === "LIKELY" && !m.confirmedByMerchant && (
                <Stack gap="200" wrap={false}>
                  <Button
                    primary
                    size="slim"
                    onClick={() => onConfirm(m.id)}
                    loading={loading}
                  >
                    Confirm match
                  </Button>
                  <Button
                    size="slim"
                    onClick={() => onReject(m.id)}
                    loading={loading}
                  >
                    Reject
                  </Button>
                </Stack>
              )}
            </Stack>
          </Box>
        </Card>
      ))}
    </Stack>
  );
}
