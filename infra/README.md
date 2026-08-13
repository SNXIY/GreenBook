# Infrastructure

The root [`docker-compose.yml`](../docker-compose.yml) is the canonical local
infrastructure topology for PostgreSQL, Redis, Qdrant, MySQL, and Kafka.
Duplicate `infra/docker-compose.dev.yml` definitions were removed in Phase 6C
so database and cache settings cannot drift. `creator-agent/docker-compose.yml`
remains a separate Creator deployment profile with its own container network.
