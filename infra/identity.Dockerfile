FROM node:24.21.0-bookworm-slim
WORKDIR /app
COPY package.json package-lock.json ./
COPY apps/web/package.json apps/web/package.json
COPY apps/identity/package.json apps/identity/package.json
RUN npm ci --omit=dev --ignore-scripts
COPY apps/identity apps/identity
COPY infra/certs infra/certs
USER node
ENV NODE_ENV=production
CMD ["node", "apps/identity/server.mjs"]
