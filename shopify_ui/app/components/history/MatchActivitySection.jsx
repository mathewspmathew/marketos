import { Card, BlockStack, InlineStack, Badge, Box, Text } from "@shopify/polaris";
import { CheckCircleIcon, XCircleIcon, StarIcon } from "@shopify/polaris-icons";

export function MatchActivitySection({ activities = [] }) {
  if (!activities || activities.length === 0) {
    return (
      <Card>
        <Box padding="400">
          <Text as="p" tone="subdued">No match activity yet.</Text>
        </Box>
      </Card>
    );
  }

  return (
    <BlockStack gap="200">
      {activities.map((activity, idx) => (
        <Card key={`${activity.matchId}-${idx}`}>
          <Box padding="400">
            <InlineStack gap="200" align="space-between" wrap={false}>
              <InlineStack gap="200" align="start" wrap={false}>
                {activity.type === "confirmed" && <CheckCircleIcon />}
                {activity.type === "rejected" && <XCircleIcon />}
                {activity.type === "created" && <StarIcon />}
                <BlockStack gap="100">
                  <Text as="p" variant="bodySm">{activity.description}</Text>
                  <Text as="p" variant="bodySm" tone="subdued">
                    {new Date(activity.timestamp).toLocaleString()}
                  </Text>
                </BlockStack>
              </InlineStack>
              <Badge
                tone={
                  activity.type === "confirmed" ? "success"
                  : activity.type === "rejected" ? "critical"
                  : "info"
                }
              >
                {activity.type === "confirmed" ? "Confirmed"
                  : activity.type === "rejected" ? "Rejected"
                  : "Discovered"}
              </Badge>
            </InlineStack>
          </Box>
        </Card>
      ))}
    </BlockStack>
  );
}
