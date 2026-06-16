import { Card, Stack, Badge, Box, Text } from "@shopify/polaris";
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
    <Stack gap="200">
      {activities.map((activity, idx) => (
        <Card key={`${activity.matchId}-${idx}`}>
          <Box padding="400">
            <Stack gap="200" wrap={false} align="space-between">
              {/* Icon + Description */}
              <Stack gap="200" wrap={false} align="center">
                <Box minWidth="24px" minHeight="24px">
                  {activity.type === "confirmed" && (
                    <CheckCircleIcon color="success" />
                  )}
                  {activity.type === "rejected" && (
                    <XCircleIcon color="critical" />
                  )}
                  {activity.type === "created" && (
                    <StarIcon color="info" />
                  )}
                </Box>
                <Stack gap="100" grow>
                  <Text as="p" variant="bodySm">
                    {activity.description}
                  </Text>
                  <Text as="p" variant="bodySm" tone="subdued">
                    {new Date(activity.timestamp).toLocaleString()}
                  </Text>
                </Stack>
              </Stack>

              {/* Status Badge */}
              <Badge
                tone={
                  activity.type === "confirmed"
                    ? "success"
                    : activity.type === "rejected"
                    ? "critical"
                    : "info"
                }
              >
                {activity.type === "confirmed"
                  ? "Confirmed"
                  : activity.type === "rejected"
                  ? "Rejected"
                  : "Discovered"}
              </Badge>
            </Stack>
          </Box>
        </Card>
      ))}
    </Stack>
  );
}
