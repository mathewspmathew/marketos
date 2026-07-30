import React from "react";
import { Body, Container, Head, Heading, Html, Preview, Row, Column, Section, Text } from "@react-email/components";
import { render } from "@react-email/render";

function PriceChangeEmail({ storeName, productTitle, currency, variants }) {
  return (
    <Html>
      <Head />
      <Preview>{`Price updated on ${storeName}: ${productTitle}`}</Preview>
      <Body style={{ fontFamily: "sans-serif", backgroundColor: "#f6f6f6" }}>
        <Container style={{ backgroundColor: "#ffffff", padding: "24px" }}>
          <Heading as="h2">{storeName}</Heading>
          <Text>A price change was automatically applied to <strong>{productTitle}</strong>:</Text>
          <Section>
            <Row style={{ fontWeight: "bold", borderBottom: "1px solid #ddd" }}>
              <Column>Variant</Column>
              <Column>Old price ({currency})</Column>
              <Column>New price ({currency})</Column>
            </Row>
            {variants.map((v) => (
              <Row key={v.variantTitle} style={{ borderBottom: "1px solid #eee" }}>
                <Column>{v.variantTitle}</Column>
                <Column>{v.oldPrice}</Column>
                <Column>{v.newPrice}</Column>
              </Row>
            ))}
          </Section>
          <Text style={{ color: "#888", fontSize: "12px", marginTop: "24px" }}>
            You're receiving this because price-change notifications are enabled
            for {storeName}. Turn them off any time in Settings.
          </Text>
        </Container>
      </Body>
    </Html>
  );
}

export async function renderPriceChangeEmail({ storeName, productTitle, currency, variants }) {
  return {
    subject: `Price updated on ${storeName}: ${productTitle}`,
    html: await render(<PriceChangeEmail storeName={storeName} productTitle={productTitle} currency={currency} variants={variants} />),
  };
}
