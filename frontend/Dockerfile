# Node 24 LTS: Vite 8 requires ^20.19 || >=22.12 and Node 20 reached end of life on 2026-04-30.
FROM node:24-alpine

WORKDIR /app

COPY package*.json ./
RUN npm ci

COPY . .

EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
