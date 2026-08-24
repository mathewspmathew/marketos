// eslint-disable-next-line no-unused-vars -- required in scope: this file's JSX compiles under the classic runtime
import React from "react";
import { Body, Container, Head, Heading, Html, Preview, Text } from "@react-email/components";
import { render } from "@react-email/render";

function ConfirmationEmail({ storeName, email }) {
  return (
    <Html>
      <Head />
      <Preview>{`Price-change notifications enabled for ${storeName}`}</Preview>
      <Body style={{ fontFamily: "sans-serif", backgroundColor: "#f6f6f6" }}>
        <Container style={{ backgroundColor: "#ffffff", padding: "24px" }}>
          <Heading as="h2">{storeName}</Heading>
          <Text>
            Price-change notifications are now enabled, sent to <strong>{email}</strong>.
          </Text>
          <Text>
            From now on, whenever this app automatically updates a product&apos;s
            price in your store, you&apos;ll get an email here with the old price,
            new price, and product details.
          </Text>
        </Container>
      </Body>
    </Html>
  );
}

export async function renderConfirmationEmail({ storeName, email }) {
  return {
    subject: `Price-change notifications enabled for ${storeName}`,
    html: await render(<ConfirmationEmail storeName={storeName} email={email} />),
  };
}
