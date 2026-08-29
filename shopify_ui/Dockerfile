FROM node:20-alpine
RUN apk add --no-cache openssl

EXPOSE 3000

WORKDIR /app

ENV NODE_ENV=production

COPY package.json package-lock.json* ./

RUN npm ci --omit=dev && npm cache clean --force

COPY . .

# Baked in at build time so containers don't pay this ~45s cost on every
# start — only `prisma migrate deploy` (needs a live DB) runs at runtime.
RUN npx prisma generate

RUN npm run build

CMD ["npm", "run", "docker-start"]
