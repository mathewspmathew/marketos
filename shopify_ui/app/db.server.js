// Singleton Prisma client, HMR-safe in dev (a fresh `npm run dev` reload
// would otherwise open a new DB connection pool on every file save).
import { PrismaClient } from "@prisma/client";

if (process.env.NODE_ENV !== "production") {
  if (!global.prismaGlobal) {
    global.prismaGlobal = new PrismaClient();
  }
}

const prisma = global.prismaGlobal ?? new PrismaClient();

export default prisma;
