# Infrastructure

The root [`docker-compose.yml`](../docker-compose.yml) is the canonical local
infrastructure topology for PostgreSQL, Redis, Qdrant, MySQL, and Kafka.
Duplicate `infra/docker-compose.dev.yml` definitions were removed in Phase 6C
so database and cache settings cannot drift. The PostgreSQL/Redis compose
services keep their historical `creator-*` names for volume compatibility; the
standalone Creator Service itself is retired.
