import { Card, Grid, Text, Box } from "@shopify/polaris";

function MetricCard({ label, value, tone = "default" }) {
  const textTone = tone === "success" ? "success"
                 : tone === "critical" ? "critical"
                 : undefined;

  return (
    <Card>
      <Box padding="400">
        <Text as="p" variant="bodySm" tone="subdued">{label}</Text>
        <Text as="p" variant="headingMd" tone={textTone}>
          {value}
        </Text>
      </Box>
    </Card>
  );
}

export function MetricsSection({
  totalProducts,
  pendingReviews,
  reviewPercentage,
  avgConfidence
}) {
  return (
    <Grid columns={4} gap="400">
      <Grid.Cell>
        <MetricCard label="Products with matches" value={totalProducts} />
      </Grid.Cell>
      <Grid.Cell>
        <MetricCard label="Pending review" value={pendingReviews} tone={pendingReviews > 0 ? "critical" : "default"} />
      </Grid.Cell>
      <Grid.Cell>
        <MetricCard label="Review completion" value={`${reviewPercentage}%`} tone={reviewPercentage >= 80 ? "success" : "default"} />
      </Grid.Cell>
      <Grid.Cell>
        <MetricCard label="Avg confidence" value={avgConfidence} />
      </Grid.Cell>
    </Grid>
  );
}
