# Showstop

Showstop is a TypeScript backend prototype for high-demand concert ticketing.
It focuses on reducing bot-driven seat sniping by combining:

1. Fan-club membership validation before seat lock attempts.
2. Redis-based short-lived seat locks for high-concurrency checkout windows.
3. PostgreSQL as the source-of-truth ticket ledger for finalized sales.

## What This Repo Does

The current code models one critical flow: lock a seat for a verified fan.

Request flow:

1. `POST /seats/lock` hits [src/routes/ticketRoutes.ts](src/routes/ticketRoutes.ts).
2. [src/middleware/verifyFan.ts](src/middleware/verifyFan.ts) validates the `x-fanclub-token` through an external label auth API.
3. If valid, fan membership data is attached to the request.
4. [src/controllers/ticketController.ts](src/controllers/ticketController.ts) attempts an atomic Redis lock (`EX` + `NX`) with a 5-minute TTL.
5. Controller checks PostgreSQL for an already-sold ticket.
6. API returns success with a checkout token (or conflict/gone/errors).

For local development, the fan verification middleware can be bypassed with `MOCK_FAN_VERIFY=true` so you can test the lock flow without an external label auth API.

## Project Structure

- [src/config/redis.ts](src/config/redis.ts): Redis client configuration.
- [src/config/database.ts](src/config/database.ts): PostgreSQL pool configuration.
- [src/middleware/verifyFan.ts](src/middleware/verifyFan.ts): fan membership verification middleware.
- [src/controllers/ticketController.ts](src/controllers/ticketController.ts): seat lock orchestration logic.
- [src/routes/ticketRoutes.ts](src/routes/ticketRoutes.ts): ticket-related route definitions.
- [src/schema.sql](src/schema.sql): relational schema for labels, users, memberships, events, seats, and tickets.
- [src/app.ts](src/app.ts): Express app bootstrap, health route, and ticket route mounting.

## API Endpoints

1. GET /health
Returns API health status.
2. GET /api/tickets/seats/available
Lists unsold seat UUIDs from PostgreSQL.
Optional query params: eventId (UUID), limit (1 to 200, default 50).
3. POST /api/tickets/seats/lock
Attempts to lock a seat for checkout.

## Data Model Summary

[src/schema.sql](src/schema.sql) defines a normalized ticketing domain:

1. `labels` and `events` represent organizers and concerts.
2. `users` and `fan_memberships` enforce gated fan-club access.
3. `seats` models inventory per event.
4. `tickets` is the finalized sales ledger with one ticket per seat.

## Environment Variables

The code currently reads the following variables:

1. `DATABASE_URL` used by [src/config/database.ts](src/config/database.ts)
2. `REDIS_HOST` used by [src/config/redis.ts](src/config/redis.ts)
3. `REDIS_PORT` used by [src/config/redis.ts](src/config/redis.ts)
4. `REDIS_PASSWORD` used by [src/config/redis.ts](src/config/redis.ts)
5. `LABEL_API_SECRET` used by [src/middleware/verifyFan.ts](src/middleware/verifyFan.ts)
6. `MOCK_FAN_VERIFY` used by [src/middleware/verifyFan.ts](src/middleware/verifyFan.ts)

Example:

```env
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/showstop
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_PASSWORD=
LABEL_API_SECRET=replace_with_real_secret
MOCK_FAN_VERIFY=true
```

## Local Setup

1. Install dependencies:

```bash
npm install
```

2. Create/update `.env` with your local credentials.

3. Ensure PostgreSQL and Redis are running.

4. Validate TypeScript:

```bash
npx tsc --noEmit
```

## Docker and Database Workflow

Use these commands from the repository root.

### 1. Start all containers

```bash
docker compose up -d
```

### 2. Stop all containers

```bash
docker compose down
```

### 3. Stop and wipe container data (full reset)

```bash
docker compose down -v
```

### 4. Apply database schema

```bash
npm run schema:db
```

### 5. Seed database data

```bash
npm run seed:db
```

### 6. Check available seats

```bash
curl.exe --% -i "http://localhost:3000/api/tickets/seats/available"
```

### 7. Lock a seat

Use one of the seat UUIDs returned by the available seats endpoint.

```bash
curl.exe --% -i -X POST http://localhost:3000/api/tickets/seats/lock -H "Content-Type: application/json" -H "x-fanclub-token: test-token" -d "{\"eventId\":\"30000000-0000-4000-8000-000000000001\",\"seatId\":\"50000000-0000-4000-8000-000000000001\",\"labelId\":\"hybe\"}"
```

## Notes

- This repository currently contains the core lock-flow logic and schema design.
- [src/app.ts](src/app.ts) runs the Express API server and mounts ticket routes at `/api/tickets`.
