# Infrastructure Services

This directory contains Docker Compose configuration for shared infrastructure services (MongoDB, Redis) used by all trading projects.

## Services

- **MongoDB**: `infra_mongodb` (port 27017, localhost only)
- **Redis**: `infra_redis` (port 6379, localhost only)
- **Network**: `infra_network` (shared network for all services)

## Usage

### Start services

```bash
cd infra
chmod +x start.sh
./start.sh
```

Or manually:

```bash
cd infra
docker compose up -d
```

### Stop services

```bash
cd infra
docker compose down
```

### View logs

```bash
cd infra
docker compose logs -f
```

### Stop and remove volumes (⚠️ This will delete all data)

```bash
cd infra
docker compose down -v
```

## For Project Developers

To use these services in your project's `docker-compose.yml`:

```yaml
services:
  your_service:
    networks:
      - infra_network
    environment:
      - MONGO_HOST=infra_mongodb
      - REDIS_HOST=infra_redis

networks:
  infra_network:
    external: true
```

## Architecture

```
infra/docker-compose.yml
  ├── mongodb (infra_mongodb)
  └── redis (infra_redis)

Business Project docker-compose.yml
  └── your_service (uses external infra_network)
```

## Notes

- MongoDB and Redis are bound to `127.0.0.1` only for security
- Data is persisted in Docker volumes (`mongodb_data`, `redis_data`)
- Projects install `pytradekit` via `requirements.txt` from GitHub (git+https)
