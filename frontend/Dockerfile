FROM node:24-slim AS build
WORKDIR /app
# O destino do proxy /api é fixado no build (rewrites do modo standalone).
ARG API_URL=http://api:8000
ENV NEXT_TELEMETRY_DISABLED=1 API_URL=${API_URL}
COPY package.json package-lock.json ./
COPY scripts ./scripts
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM node:24-slim
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
USER node
EXPOSE 3000
CMD ["node", "server.js"]
