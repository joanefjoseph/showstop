# Showstop

Showstop is a TypeScript backend prototype for high-demand concert ticketing.
It focuses on reducing bot-driven seat sniping by combining:

1. Fan-club membership validation before seat lock attempts.
2. Redis-based short-lived seat locks for high-concurrency checkout windows.
3. PostgreSQL as the source-of-truth ticket ledger for finalized sales.

## What This Repo Does

The current code models a five-step Ticketmaster checkout flow for a verified fan:

1. Fetch event availability and pricing from Ticketmaster.
2. Acquire a five-minute Redis seat lock and create a Ticketmaster cart.
3. Add billing information to the cart.
4. Commit the cart and write the completed ticket to PostgreSQL.
5. Retrieve a SafeTix token for the purchaser's device.

The fan verification middleware can be bypassed locally with `MOCK_FAN_VERIFY=true`.
Protected requests still need a non-empty `x-fanclub-token` header when mock verification is enabled.

## Project Structure

- [src/config/redis.ts](src/config/redis.ts): Redis client configuration.
- [src/config/database.ts](src/config/database.ts): PostgreSQL pool configuration.
- [src/middleware/verifyFan.ts](src/middleware/verifyFan.ts): fan membership verification middleware.
- [src/controllers/ticketController.ts](src/controllers/ticketController.ts): seat lock orchestration logic.
- [src/routes/ticketRoutes.ts](src/routes/ticketRoutes.ts): ticket-related route definitions.
- [src/schema.sql](src/schema.sql): relational schema for labels, users, memberships, events, seats, and tickets.
- [src/app.ts](src/app.ts): Express app bootstrap, health route, and ticket route mounting.

## API Endpoints

The server listens on `http://localhost:3000` by default. The ticket routes are mounted
under `/api/tickets` in [src/app.ts](src/app.ts).

### Health check

```text
GET /health
```

Returns `200` when the Express server is running:

```json
{"status":"ok"}
```

PowerShell:

```powershell
curl.exe -i "http://localhost:3000/health"
```

### 1. Get event availability

```text
GET /api/tickets/events/:tmEventId/availability
```

Looks up the event through the Ticketmaster Discovery API and attempts to retrieve
live inventory through the Ticketmaster Partner API. If the Partner API request fails,
the controller falls back to the Discovery API `priceRanges` data.

Example using the `eventID_911NYconcert` value from `.env`:

```powershell
$tmEventId = "G5diZ_G4W9wCD"
curl.exe -i "http://localhost:3000/api/tickets/events/$tmEventId/availability"
```

Successful response (`200`) shape:

```json
{
	"event": {},
	"inventory": []
}
```

The `event` and `inventory` contents come from Ticketmaster. A missing event ID returns
`400`; a failed Discovery API request returns `500` with an `error` property.

### 2. Create a cart and lock a seat

```text
POST /api/tickets/cart/create
```

Requires the `x-fanclub-token` header. The middleware verifies the fan, then the
controller acquires an atomic Redis lock for five minutes and creates a Ticketmaster
cart. `eventId` and `seatId` are local PostgreSQL identifiers; `tmSeatId` is optional
and is used as the Ticketmaster seat identifier when supplied.

Request body:

```json
{
	"eventId": "30000000-0000-4000-8000-000000000001",
	"tmEventId": "G5diZ_G4W9wCD",
	"seatId": "50000000-0000-4000-8000-000000000001",
	"tmSeatId": "ticketmaster-seat-id"
}
```

PowerShell with the seeded local IDs:

```powershell
curl.exe -i -X POST "http://localhost:3000/api/tickets/cart/create" `
	-H "Content-Type: application/json" `
	-H "x-fanclub-token: test-token" `
	-d '{"eventId":"30000000-0000-4000-8000-000000000001","tmEventId":"G5diZ_G4W9wCD","seatId":"50000000-0000-4000-8000-000000000001","tmSeatId":"ticketmaster-seat-id"}'
```

Successful response (`200`):

```json
{
	"message": "Cart created and seats reserved",
	"cartId": "...",
	"expiresInSeconds": 300
}
```

The same seat returns `409` while its Redis lock is active. Missing data returns `400`,
and Redis or Ticketmaster failures return `500`.

### 3. Add billing information

```text
PUT /api/tickets/cart/:cartId/billing
```

Requires the `x-fanclub-token` header and the `cartId` returned by the previous step.
The payment token is passed to the Ticketmaster Partner API; this prototype does not
process or store raw card details.

Request body:

```json
{
	"paymentToken": "test-payment-token",
	"billingAddress": {
		"line1": "123 Main Street",
		"city": "New York",
		"state": "NY",
		"postalCode": "10001",
		"country": "US"
	},
	"fanEmail": "testfan@example.com"
}
```

PowerShell:

```powershell
$cartId = "YOUR_CART_ID"
curl.exe -i -X PUT "http://localhost:3000/api/tickets/cart/$cartId/billing" `
	-H "Content-Type: application/json" `
	-H "x-fanclub-token: test-token" `
	-d '{"paymentToken":"test-payment-token","billingAddress":{"line1":"123 Main Street","city":"New York","state":"NY","postalCode":"10001","country":"US"},"fanEmail":"testfan@example.com"}'
```

Successful response (`200`):

```json
{
	"message": "Billing info added successfully",
	"details": {}
}
```

Missing `cartId` or `paymentToken` returns `400`; Ticketmaster failures return `500`.

### 4. Commit a cart

```text
PUT /api/tickets/cart/:cartId/commit
```

Requires the `x-fanclub-token` header and an unexpired cart. The controller retrieves
the cart from Redis, commits it through Ticketmaster, inserts the order into the local
`tickets` table, then releases the seat and cart Redis keys.

Request body:

```json
{
	"secureDeviceId": "test-device-001",
	"purchasePriceCents": 15000
}
```

PowerShell:

```powershell
curl.exe -i -X PUT "http://localhost:3000/api/tickets/cart/$cartId/commit" `
	-H "Content-Type: application/json" `
	-H "x-fanclub-token: test-token" `
	-d '{"secureDeviceId":"test-device-001","purchasePriceCents":15000}'
```

Successful response (`200`):

```json
{
	"message": "Order successfully confirmed!",
	"ticketId": "...",
	"tmOrderId": "...",
	"issuedAt": "..."
}
```

An expired or unknown cart returns `410`; missing commit data returns `400`; database
or Ticketmaster failures return `500`.

### 5. Get a SafeTix token

```text
GET /api/tickets/:ticketId/safetix-token?deviceId=:deviceId
```

Requires the `x-fanclub-token` header. The route looks up the Ticketmaster order ID
for the local database `ticketId`, then requests a SafeTix token for the specified
device.

PowerShell:

```powershell
$ticketId = "YOUR_TICKET_ID"
curl.exe -i "http://localhost:3000/api/tickets/$ticketId/safetix-token?deviceId=test-device-001" `
	-H "x-fanclub-token: test-token"
```

Successful response (`200`) is returned by the Ticketmaster SafeTix service:

```json
{
	"renderToken": "...",
	"rotationIntervalMs": 15000,
	"sdkKey": "..."
}
```

Missing parameters return `400`; an unknown ticket returns `404`; database or
Ticketmaster failures return `500`.

All protected routes return `401` when `x-fanclub-token` is missing. With mock
verification disabled, invalid or expired memberships return `403`.

## Data Model Summary

[src/schema.sql](src/schema.sql) defines a normalized ticketing domain:

1. `labels` and `events` represent organizers and concerts.
2. `users` and `fan_memberships` enforce gated fan-club access.
3. `seats` models inventory per event.
4. `tickets` is the finalized sales ledger with one ticket per seat.

## Environment Variables

The application loads `.env` through `dotenv`:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | PostgreSQL connection string used by [src/config/database.ts](src/config/database.ts). |
| `REDIS_HOST` | Redis hostname. Use `127.0.0.1` when the API runs on the host, or `redis` inside Docker Compose. |
| `REDIS_PORT` | Redis port, normally `6379`. |
| `REDIS_PASSWORD` | Redis password, blank for the included local Redis container. |
| `LABEL_API_SECRET` | Credential used by the real fan membership validation service. |
| `MOCK_FAN_VERIFY` | Set to `true` to bypass the external membership API during local development. |
| `PORT` | HTTP port, default `3000`. |
| `TM_DEVELOPER_API_KEY` | Ticketmaster Discovery API key. |
| `TM_API_BASE_URL` | Ticketmaster Discovery API base URL. |
| `TM_PARTNER_API_BASE_URL` | Ticketmaster Partner Commerce API base URL. |
| `TM_PARTNER_API_KEY` | Partner Commerce credential placeholder currently defined for configuration. |
| `TM_PARTNER_API_SECRET` | Partner Commerce credential placeholder currently defined for configuration. |
| `TM_PARTNER_CLIENT_ID` | Partner Commerce client ID placeholder currently defined for configuration. |
| `TM_SAFETIX_SDK_KEY` | SafeTix SDK key, used as a mock fallback value when configured. |
| `eventID_911NYconcert` | Local convenience variable containing the Ticketmaster event ID `G5diZ_G4W9wCD`. |
| `eventID_910DCconcert` | Local convenience variable containing the Ticketmaster event ID `17A8v0G6Gxtf_68`. |
| `eventID_920GAconcert` | Local convenience variable containing the Ticketmaster event ID `17k8v0G6G9qMnGs`. |
| `jaypark_attractionID` | Local convenience variable containing the Ticketmaster attraction ID `K8vZ9172mb7`. |

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

2. Create or update `.env` with local credentials. Do not commit `.env` or expose API keys.

3. Start PostgreSQL and Redis. The included Docker Compose configuration starts both services:

```powershell
docker compose up -d postgres redis
```

4. Apply the schema and optional seed data:

```powershell
npm run schema:db
npm run seed:db
```

The seed creates predictable local event, seat, user, membership, and ticket IDs. One
of the three seats is intentionally marked as sold.

5. Start the API directly from the host:

```powershell
npm run dev
```

When using this command, `.env` must point to `127.0.0.1:6379` for Redis and
`localhost:5432` for PostgreSQL.

Alternatively, start the API and both databases in Docker:

```powershell
docker compose up -d
```

In that mode, Docker supplies `REDIS_HOST=redis` and a PostgreSQL hostname of `postgres`
to the API container.

6. Validate TypeScript:

```bash
npx tsc --noEmit
```

## Docker and Database Workflow

Use these commands from the repository root.

### Stop all containers

```bash
docker compose down
```

### Stop and wipe container data (full reset)

```bash
docker compose down -v
```

### Apply database schema

```bash
npm run schema:db
```

### Seed database data

```bash
npm run seed:db
```

## Notes

- The current route implementation is the five endpoints documented above; older
	`/api/tickets/seats/available` and `/api/tickets/seats/lock` examples are no longer valid.
- `TM_PARTNER_API_KEY` is currently a placeholder. Cart, billing, commit, and SafeTix
	service methods have mock fallbacks, but availability still requires a valid Discovery API key.
- `MOCK_FAN_VERIFY=true` supplies the strings `mock-user-id` and `mock-membership-id`.
	Because the database schema expects UUID foreign keys, the commit route can fail when
	inserting a ticket while this mock mode is enabled. Use UUID-shaped mock membership data
	or a real membership integration before testing a complete commit flow.
- The SafeTix route reads an existing local ticket record; it does not itself create a ticket.
- [src/app.ts](src/app.ts) runs the Express API server and mounts ticket routes at `/api/tickets`.
