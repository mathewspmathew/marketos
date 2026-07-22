// Singleton Prisma client, HMR-safe in dev (a fresh `npm run dev` reload
// would otherwise open a new DB connection pool on every file save).
import { PrismaClient } from "@prisma/client";

// DATABASE_URL is shared with the Python services (psycopg), so the pool cap
// is applied here via datasourceUrl instead of a query param on the shared
// env var — Prisma's default pool (num_cpus*2+1) is otherwise uncapped.
function cappedDatabaseUrl() {
  const url = new URL(process.env.DATABASE_URL);
  url.searchParams.set("connection_limit", "5");
  url.searchParams.set("pool_timeout", "10");
  return url.toString();
}

function createPrismaClient() {
  return new PrismaClient({ datasourceUrl: cappedDatabaseUrl() });
}

if (process.env.NODE_ENV !== "production") {
  if (!global.prismaGlobal) {
    global.prismaGlobal = createPrismaClient();
  }
}

const prisma = global.prismaGlobal ?? createPrismaClient();

export default prisma;
