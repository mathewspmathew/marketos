/*
  Warnings:

  - Added the required column `userId` to the `ScrapedProduct` table without a default value. This is not possible if the table is not empty.
  - Added the required column `userId` to the `ScrapedVariant` table without a default value. This is not possible if the table is not empty.

*/
-- AlterTable
ALTER TABLE "ScrapedProduct" ADD COLUMN     "userId" TEXT NOT NULL;

-- AlterTable
ALTER TABLE "ScrapedVariant" ADD COLUMN     "userId" TEXT NOT NULL;

-- AddForeignKey
ALTER TABLE "ScrapedProduct" ADD CONSTRAINT "ScrapedProduct_userId_fkey" FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "ScrapedVariant" ADD CONSTRAINT "ScrapedVariant_userId_fkey" FOREIGN KEY ("userId") REFERENCES "User"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
