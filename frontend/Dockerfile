# syntax=docker/dockerfile:1.7
# The web app as a standalone Next.js server. In compose, nginx routes /api to the API and everything else here.
# Build from frontend/:  docker build -t rag-web --build-arg NEXT_PUBLIC_APP_NAME=ALAIN .

ARG NODE_IMAGE=node:22-alpine

FROM ${NODE_IMAGE} AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY . .
# NEXT_PUBLIC_* values are compiled into the browser bundle. API_URL is where the server forwards /api when no edge
# proxy sits in front (the edge proxy normally answers /api itself).
ARG NEXT_PUBLIC_APP_NAME=ALAIN
ARG API_URL=http://backend:8000
ENV NEXT_PUBLIC_APP_NAME=${NEXT_PUBLIC_APP_NAME} API_URL=${API_URL}
# prebuild draws the service and provider marks from the icon packages (scripts/brand-icons.mjs).
RUN npm run build

FROM ${NODE_IMAGE} AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
COPY --from=build --chown=node:node /app/public ./public
USER node
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:3000/login').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"]
CMD ["node", "server.js"]
