FROM node:24.21.0-bookworm-slim
WORKDIR /app
COPY package.json package-lock.json ./
COPY apps/web/package.json apps/web/package.json
COPY apps/identity/package.json apps/identity/package.json
RUN npm ci --ignore-scripts
COPY apps/web apps/web
COPY apps/identity apps/identity
COPY packages packages
COPY scripts/identity-keys.mjs scripts/identity-keys.mjs
ARG API_INTERNAL_ORIGIN=http://api:8017
ENV API_INTERNAL_ORIGIN=$API_INTERNAL_ORIGIN
RUN npm run build
RUN mkdir /identity && chown 10001:10001 /identity
USER node
CMD ["npx", "next", "start", "apps/web", "--hostname", "0.0.0.0", "--port", "3017"]
