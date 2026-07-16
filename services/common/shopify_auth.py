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

import structlog
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

logger = structlog.get_logger(__name__)


class ShopifyAuthError(Exception):
    """Token exchange or auth-related errors."""
    pass


class ShopifyAPIError(Exception):
    """Shopify API call errors."""
    pass


class ShopifyTokenExpiredError(ShopifyAuthError):
    """Refresh token has expired (> 6 months old)."""
    pass


# How close to expiry a stored access token is still considered usable.
# Matches the JS library's WITHIN_MILLISECONDS_OF_EXPIRY (5 min) so both
# sides of the shared Session row refresh in step.
TOKEN_EXPIRY_MARGIN = timedelta(seconds=300)


def get_access_token(shop_domain: str, session) -> tuple[str, datetime]:
    """
    Get a valid access token for the shop's offline session.

    Reuses the access token stored on the Session row while it is still valid
    (refresh tokens are single-use, so needless exchanges rotate them and can
    race with other workers or the React Router side). When expired, exchanges
    the refresh token with Shopify and persists the new accessToken/expires
    (and rotated refresh token) back to the row so all consumers see fresh state.

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

    # The offline row only — the UPDATE below must never stomp online sessions.
    result = session.execute(
        text("""
            SELECT "accessToken", expires, "refreshToken", "refreshTokenExpires"
            FROM "Session"
            WHERE shop = :shop AND "isOnline" = false
            LIMIT 1
        """),
        {"shop": shop_domain},
    ).first()

    if not result:
        raise ShopifyAuthError(f"No offline session found for shop {shop_domain}")

    stored_token, stored_expires, refresh_token, token_expires = result

    # Reuse the stored token while it is comfortably within its lifetime.
    if stored_token and stored_expires:
        if stored_expires.tzinfo is None:
            stored_expires = stored_expires.replace(tzinfo=timezone.utc)
        if stored_expires - TOKEN_EXPIRY_MARGIN > datetime.now(timezone.utc):
            return stored_token, stored_expires

    if not refresh_token:
        raise ShopifyAuthError(f"No refresh token found for shop {shop_domain}")

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
    refresh_expires_in = data.get("refresh_token_expires_in")

    if not access_token:
        raise ShopifyAuthError("No access token in Shopify response")

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=expires_in)

    # Persist the new access token (and rotated refresh token) on the offline
    # row so subsequent calls reuse it and the React Router side sees fresh
    # state. Columns are timestamp-without-tz, hence the naive UTC values.
    try:
        set_clauses = ['"accessToken" = :t', "expires = :e"]
        params = {
            "t": access_token,
            "e": expires_at.replace(tzinfo=None),
            "shop": shop_domain,
        }
        if new_refresh_token and new_refresh_token != refresh_token:
            new_refresh_expires = (
                now + timedelta(seconds=refresh_expires_in)
                if refresh_expires_in
                else now + timedelta(days=180)
            )
            set_clauses += ['"refreshToken" = :rt', '"refreshTokenExpires" = :rte']
            params["rt"] = new_refresh_token
            params["rte"] = new_refresh_expires.replace(tzinfo=None)
        # Persisted in its own transaction, not the caller's `session` — the
        # caller may be holding a transaction-scoped resource (e.g.
        # pg_try_advisory_xact_lock in pricing_svc/apply.py) that must stay
        # held until the caller's own work finishes. Committing the caller's
        # session here would end that transaction early and release it.
        with get_db() as token_session:
            token_session.execute(
                text(
                    f'UPDATE "Session" SET {", ".join(set_clauses)} '
                    'WHERE shop = :shop AND "isOnline" = false'
                ),
                params,
            )
        logger.info("token_persisted", shop_domain=shop_domain)
    except Exception:
        logger.exception("token_persist_failed", shop_domain=shop_domain)
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
