-- CreateTable
CREATE TABLE "ScrapingConfig" (
    "id" TEXT NOT NULL,
    "userId" TEXT NOT NULL,
    "shopId" TEXT NOT NULL,
    "competitorUrl" TEXT NOT NULL,
    "llmInstructions" TEXT,
    "includeImages" BOOLEAN NOT NULL DEFAULT true,
    "productLimit" INTEGER,
    "frequencyInterval" INTEGER NOT NULL,
    "nextRunAt" TIMESTAMP(3),
    "isActive" BOOLEAN NOT NULL DEFAULT true,
    "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" TIMESTAMP(3) NOT NULL,

    CONSTRAINT "ScrapingConfig_pkey" PRIMARY KEY ("id")
);

-- AddForeignKey
ALTER TABLE "ScrapingConfig" ADD CONSTRAINT "ScrapingConfig_userId_fkey" FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
