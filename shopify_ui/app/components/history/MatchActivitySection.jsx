import { Card, Badge, Box, Text } from "@shopify/polaris";
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
    <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
      {activities.map((activity, idx) => (
        <Card key={`${activity.matchId}-${idx}`}>
          <Box padding="400">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: "12px" }}>
              {/* Icon + Description */}
              <div style={{ display: "flex", gap: "12px", alignItems: "flex-start", flex: 1 }}>
                <div style={{ minWidth: "24px", minHeight: "24px", marginTop: "2px" }}>
                  {activity.type === "confirmed" && (
                    <CheckCircleIcon color="success" />
                  )}
                  {activity.type === "rejected" && (
                    <XCircleIcon color="critical" />
                  )}
                  {activity.type === "created" && (
                    <StarIcon color="info" />
                  )}
                </div>
                <div style={{ flex: 1 }}>
                  <Text as="p" variant="bodySm">
                    {activity.description}
                  </Text>
                  <Text as="p" variant="bodySm" tone="subdued">
                    {new Date(activity.timestamp).toLocaleString()}
                  </Text>
                </div>
              </div>

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
            </div>
          </Box>
        </Card>
      ))}
    </div>
  );
}
