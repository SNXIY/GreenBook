# Local Setup

1. Copy `.env.example` to `.env` and set the required credentials and secrets.
2. Install Python dependencies with `uv sync`.
3. Install frontend dependencies with `cd zhiguang-fe; npm install`.
4. Start infrastructure with `docker compose up -d`.
5. Start the Java Backend, Agent API/Worker, and frontend with `..\scripts\start-greenbook.ps1` from `zhiguang-fe`, or run the individual scripts in `scripts/`.

Canonical local ports are Java `8080`, Agent API `8094`, and frontend `5173`.
PostgreSQL is `25432`, Redis `26379`, and Qdrant HTTP/gRPC are `26333`/`26334`.

The root `docker-compose.yml` is the single shared infrastructure Compose
file and defines the complete local development topology.
