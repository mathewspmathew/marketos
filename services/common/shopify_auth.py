"""
services/common/shopify_auth.py

Direct Token Exchange with Shopify Admin API.
Workers call this module instead of proxying through React Router.

Handles:
1. Reading refresh tokens from Session table
2. Exchanging expired tokens via Shopify Token Exchange
3. Calling Shopify Admin API directly
4. Updating Session table with new tokens if needed
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import certifi
import requests
from dotenv import load_dotenv
from sqlalchemy import text

from services.common.db import get_db

# Load .env from project root
project_root = Path(__file__).parent.parent.parent
load_dotenv(project_root / ".env")

logger = logging.getLogger(__name__)


class ShopifyAuthError(Exception):
    """Token exchange or auth-related errors."""
    pass


class ShopifyAPIError(Exception):
    """Shopify API call errors."""
    pass


class ShopifyTokenExpiredError(ShopifyAuthError):
    """Refresh token has expired (> 6 months old)."""
    pass


def get_access_token(shop_domain: str, session) -> tuple[str, datetime]:
    """
    Get a fresh access token by exchanging the refresh token with Shopify.

    Args:
        shop_domain: e.g., "fabric-dressing.myshopify.com"
        session: SQLAlchemy session

    Returns:
        (access_token, expires_at) tuple

    Raises:
        ShopifyTokenExpiredError: If refresh token > 6 months old
        ShopifyAuthError: If token exchange fails
    """
    api_key = os.environ.get("SHOPIFY_API_KEY")
    api_secret = os.environ.get("SHOPIFY_API_SECRET")

    if not api_key or not api_secret:
        raise ShopifyAuthError("SHOPIFY_API_KEY or SHOPIFY_API_SECRET not configured")

    # Read refresh token from Session table
    result = session.execute(
        text("""
            SELECT "refreshToken", "refreshTokenExpires"
            FROM "Session"
            WHERE shop = :shop
            ORDER BY "refreshTokenExpires" DESC NULLS LAST
            LIMIT 1
        """),
        {"shop": shop_domain},
    ).first()

    if not result or not result[0]:
        raise ShopifyAuthError(f"No refresh token found for shop {shop_domain}")

    refresh_token, token_expires = result

    # Check if refresh token itself is expired (Shopify refresh tokens last 6 months)
    if token_expires:
        if token_expires.tzinfo is None:
            token_expires = token_expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > token_expires:
            raise ShopifyTokenExpiredError(
                f"Refresh token expired at {token_expires}. Merchant must re-authorize app."
            )

    # Call Shopify's Token Exchange endpoint
    # Use certifi for proper SSL verification (industry standard)
    try:
        response = requests.post(
            f"https://{shop_domain}/admin/oauth/access_token",
            json={
                "client_id": api_key,
                "client_secret": api_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=10,
            verify=certifi.where(),
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise ShopifyAuthError(f"Token exchange failed: {str(e)}")

    try:
        data = response.json()
    except ValueError as e:
        raise ShopifyAuthError(f"Invalid JSON from token exchange: {str(e)}")

    if "error" in data:
        raise ShopifyAuthError(f"Shopify token exchange error: {data.get('error_description', data['error'])}")

    access_token = data.get("access_token")
    new_refresh_token = data.get("refresh_token")
    expires_in = data.get("expires_in", 3600)  # Default to 1 hour

    if not access_token:
        raise ShopifyAuthError("No access token in Shopify response")

    # Calculate when this access token expires
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    # If Shopify returned a new refresh token, update Session table
    if new_refresh_token and new_refresh_token != refresh_token:
        try:
            new_refresh_expires = datetime.now(timezone.utc) + timedelta(days=180)
            session.execute(
                text("""
                    UPDATE "Session"
                    SET "refreshToken" = :rt, "refreshTokenExpires" = :rte
                    WHERE shop = :shop
                """),
                {
                    "rt": new_refresh_token,
                    "rte": new_refresh_expires,
                    "shop": shop_domain,
                },
            )
            session.commit()
            logger.info(f"Updated refresh token for {shop_domain}")
        except Exception as e:
            logger.error(f"Failed to update refresh token: {e}")
            # Don't fail - token exchange succeeded, just couldn't persist new token

    return access_token, expires_at


def call_shopify_admin(
    shop_domain: str,
    query_or_mutation: str,
    variables: dict,
    session,
) -> dict:
    """
    Call Shopify Admin API directly.

    Args:
        shop_domain: e.g., "fabric-dressing.myshopify.com"
        query_or_mutation: GraphQL query or mutation string
        variables: GraphQL variables dict
        session: SQLAlchemy session

    Returns:
        Parsed response data (data + errors if any)

    Raises:
        ShopifyAuthError: Token exchange failed
        ShopifyAPIError: API call failed
    """
    # Get access token (handles token exchange transparently)
    try:
        access_token, expires_at = get_access_token(shop_domain, session)
    except ShopifyAuthError as e:
        raise ShopifyAuthError(f"Failed to get access token: {str(e)}")

    # Call Shopify GraphQL endpoint
    api_version = "2025-01"
    url = f"https://{shop_domain}/admin/api/{api_version}/graphql.json"

    # Note: In dev environments with SSL cert issues, set SHOPIFY_SKIP_SSL_VERIFY=true
    verify_ssl = os.environ.get("SHOPIFY_SKIP_SSL_VERIFY", "").lower() != "true"

    try:
        response = requests.post(
            url,
            json={"query": query_or_mutation, "variables": variables},
            headers={
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            },
            timeout=30,
            verify=verify_ssl,
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise ShopifyAPIError(f"API call to {url} failed: {str(e)}")

    try:
        result = response.json()
    except ValueError as e:
        raise ShopifyAPIError(f"Invalid JSON response from Shopify: {str(e)}")

    # Check for GraphQL errors (Shopify returns 200 even with GraphQL errors)
    if result.get("errors"):
        error_msg = str(result["errors"])
        raise ShopifyAPIError(f"GraphQL errors: {error_msg}")

    return result
