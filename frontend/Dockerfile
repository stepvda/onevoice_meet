FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
# Installs exactly what package-lock.json pins (and fails if it is out of
# date with package.json).
RUN npm ci
COPY . .
RUN npm run build

# The image is one-shot: docker-compose's frontend-build service copies
# /app/dist into the shared volume mounted at /output, then exits.
CMD ["sh", "-c", "cp -r /app/dist/* /output/ 2>/dev/null || true"]
