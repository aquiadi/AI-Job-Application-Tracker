# The web image. Built from the repository root.
#
# Next's `standalone` output emits a server with only the files the app actually
# imports, which is what keeps this small enough to cold-start acceptably at
# min-instances 0. Copying node_modules instead would be an order of magnitude larger.

FROM node:22-slim AS build

WORKDIR /app

COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci

COPY apps/web/ ./

# NEXT_PUBLIC_* values are inlined at build time, not read at runtime, so they have to
# be present here. None of them is a credential: they identify the project, and what
# protects data is the ID token plus the policies behind the api.
ARG NEXT_PUBLIC_API_URL
ARG NEXT_PUBLIC_FIREBASE_PROJECT_ID
ARG NEXT_PUBLIC_FIREBASE_API_KEY
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL} \
    NEXT_PUBLIC_FIREBASE_PROJECT_ID=${NEXT_PUBLIC_FIREBASE_PROJECT_ID} \
    NEXT_PUBLIC_FIREBASE_API_KEY=${NEXT_PUBLIC_FIREBASE_API_KEY} \
    NEXT_TELEMETRY_DISABLED=1

RUN npm run build

FROM node:22-slim AS runtime

RUN useradd --create-home --uid 10001 app
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

# The three pieces a standalone build needs: the server, the static assets it serves,
# and anything in public/. They are copied separately because the standalone trace
# deliberately does not include the last two.
COPY --from=build --chown=app:app /app/.next/standalone ./
COPY --from=build --chown=app:app /app/.next/static ./.next/static
COPY --from=build --chown=app:app /app/public ./public

USER app
EXPOSE 8080

CMD ["sh", "-c", "HOSTNAME=0.0.0.0 PORT=${PORT:-8080} exec node server.js"]
