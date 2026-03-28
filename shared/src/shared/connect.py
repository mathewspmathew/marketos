import asyncio
import os
import ssl
import sys

# psycopg for PostgreSQL
try:
    import psycopg
except ImportError:
    print("Error: psycopg not installed. Please add it to your environment via uv.")
    sys.exit(1)

# redis for Redis event bus
try:
    from redis.asyncio import Redis
except ImportError:
    print("Error: redis not installed. Please add it to your environment via uv.")
    sys.exit(1)


async def check_postgres():
    """Attempt to connect to the local Postgres database spun up by docket-compose"""
    try:
        # Defaults match docker-compose configuration
        conn = await psycopg.AsyncConnection.connect(
            dbname=os.getenv("POSTGRES_DB", "marketos"),
            user=os.getenv("POSTGRES_USER", "marketos"),
            password=os.getenv("POSTGRES_PASSWORD", "password123"),
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=os.getenv("POSTGRES_PORT", "5432"),
        )
        print("✅ [PostgreSQL] Connected successfully.")
        await conn.close()
        return True
    except Exception as e:
        print(f"❌ [PostgreSQL] Connection failed: {e}")
        return False


async def check_redis():
    """Attempt to connect to the local Redis instance spun up by docker-compose"""
    try:
        # Defaults match docker-compose configuration
        host = os.getenv("REDIS_HOST", "localhost")
        port = int(os.getenv("REDIS_PORT", "6379"))
        
        client = Redis(host=host, port=port, socket_connect_timeout=3)
        await client.ping()
        print("✅ [Redis] Connected successfully.")
        await client.aclose()
        return True
    except Exception as e:
        print(f"❌ [Redis] Connection failed: {e}")
        return False


async def main():
    print("Testing connection to MarketOS local infrastructure...\n")
    pg_ok = await check_postgres()
    redis_ok = await check_redis()
    
    if pg_ok and redis_ok:
        print("\n🚀 All local datastores are reachable. You are ready for Phase 1!")
    else:
        print("\n⚠️  Some datastores are unreachable. Did you run `docker compose up -d`?")


if __name__ == "__main__":
    asyncio.run(main())
