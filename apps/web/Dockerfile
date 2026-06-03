FROM node:20-bookworm-slim

WORKDIR /app

COPY apps/web/package.json /app/package.json
COPY apps/web/package-lock.json /app/package-lock.json
RUN npm ci

COPY apps/web /app

EXPOSE 3000

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "3000"]
