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

## Project Structure

- [src/config/redis.ts](src/config/redis.ts): Redis client configuration.
- [src/config/database.ts](src/config/database.ts): PostgreSQL pool configuration.
- [src/middleware/verifyFan.ts](src/middleware/verifyFan.ts): fan membership verification middleware.
- [src/controllers/ticketController.ts](src/controllers/ticketController.ts): seat lock orchestration logic.
- [src/routes/ticketRoutes.ts](src/routes/ticketRoutes.ts): ticket-related route definitions.
- [src/schema.sql](src/schema.sql): relational schema for labels, users, memberships, events, seats, and tickets.
- [src/app.ts](src/app.ts): currently empty; app bootstrap/server wiring is not yet implemented.

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

Example:

```env
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/showstop
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_PASSWORD=
LABEL_API_SECRET=replace_with_real_secret
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

## Notes

- This repository currently contains the core lock-flow logic and schema design.
- HTTP server bootstrap and runtime entrypoint are pending implementation in [src/app.ts](src/app.ts).
