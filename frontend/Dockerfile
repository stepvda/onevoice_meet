FROM node:20-alpine AS build
WORKDIR /app
COPY package.json ./
# Use npm install (no lockfile yet); regenerate a lockfile after the first
# successful build and switch to `npm ci` for reproducibility.
RUN npm install
COPY . .
RUN npm run build

# The image is one-shot: docker-compose's frontend-build service copies
# /app/dist into the shared volume mounted at /output, then exits.
CMD ["sh", "-c", "cp -r /app/dist/* /output/ 2>/dev/null || true"]
