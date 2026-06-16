import { useState } from "react";
import { Card, Text, Button, Stack, Box } from "@shopify/polaris";
import { ChevronDownIcon, ChevronUpIcon } from "@shopify/polaris-icons";
import { MatchItemList } from "./MatchItemList.jsx";

export function ProductMatchCard({
  product,
  topMatch,
  onConfirm,
  onReject,
  onLoadMore,
  expandedMatches = [],
  allMatchesCount = 0,
  isLoading = false,
  showAllMatches = false,
}) {
  const [isExpanded, setIsExpanded] = useState(false);

  const handleExpand = () => {
    setIsExpanded(true);
    if (!expandedMatches.length && allMatchesCount > 0) {
      onLoadMore?.(product.id, 3);
    }
  };

  const handleCollapse = () => {
    setIsExpanded(false);
  };

  return (
    <Card>
      <Box padding="500">
        <Stack gap="400">
          {/* Product Header (Always Visible) */}
          <Box borderBottomWidth="1" borderColor="border" paddingBlockEnd="300">
            <Stack gap="300" wrap={false} align="space-between">
              <Stack gap="300" wrap={false} align="center" grow>
                {product.imageUrl && (
                  <Box minWidth="80px" minHeight="80px">
                    <img
                      src={product.imageUrl}
                      alt={product.title}
                      width={80}
                      height={80}
                      style={{ objectFit: "cover", borderRadius: 4 }}
                    />
                  </Box>
                )}
                <Stack gap="100" grow>
                  <Text as="p" variant="headingMd">{product.title}</Text>
                  {product.merchantPrice && (
                    <Text as="p" tone="subdued">
                      Your price: ₹{product.merchantPrice}
                    </Text>
                  )}
                  {allMatchesCount > 0 && (
                    <Text as="p" tone="subdued" variant="bodySm">
                      {allMatchesCount} competitor{allMatchesCount !== 1 ? "s" : ""} found
                    </Text>
                  )}
                </Stack>
              </Stack>

              {/* Expand/Collapse Button */}
              {allMatchesCount > 0 && (
                <Button
                  icon={isExpanded ? ChevronUpIcon : ChevronDownIcon}
                  onClick={isExpanded ? handleCollapse : handleExpand}
                  variant="plain"
                  accessibilityLabel={isExpanded ? "Collapse matches" : "Expand matches"}
                />
              )}
            </Stack>
          </Box>

          {/* Top Match (Always shown when available) */}
          {topMatch && (
            <Box>
              <Text as="p" variant="bodySm" tone="subdued">Top match</Text>
              <Box marginBlockStart="200">
                <MatchItemList
                  matches={[topMatch]}
                  onConfirm={onConfirm}
                  onReject={onReject}
                  loading={isLoading}
                />
              </Box>
            </Box>
          )}

          {/* Expanded Content */}
          {isExpanded && expandedMatches.length > 0 && (
            <Box>
              <Text as="p" variant="bodySm" tone="subdued">
                Top 3 competitors
              </Text>
              <Box marginBlockStart="200">
                <MatchItemList
                  matches={expandedMatches}
                  onConfirm={onConfirm}
                  onReject={onReject}
                  loading={isLoading}
                />
              </Box>
            </Box>
          )}

          {/* Expanded: View All Button */}
          {isExpanded && allMatchesCount > 3 && (
            <Button
              variant="plain"
              onClick={() => onLoadMore?.(product.id, null)}
              loading={isLoading}
            >
              View all {allMatchesCount} competitors
            </Button>
          )}

          {/* Expanded: Show All Matches */}
          {showAllMatches && isExpanded && (
            <Box>
              <Text as="p" variant="bodySm" tone="subdued">
                All competitors
              </Text>
              <Box marginBlockStart="200">
                <MatchItemList
                  matches={expandedMatches}
                  onConfirm={onConfirm}
                  onReject={onReject}
                  loading={isLoading}
                />
              </Box>
            </Box>
          )}

          {/* Activity Link */}
          {isExpanded && (
            <Box borderTopWidth="1" borderColor="border" paddingBlockStart="300">
              <Button
                variant="plain"
                url={`/app/product/${encodeURIComponent(product.id)}/activity`}
              >
                View product activity →
              </Button>
            </Box>
          )}
        </Stack>
      </Box>
    </Card>
  );
}
