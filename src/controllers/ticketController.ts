import { Request, Response } from 'express';
import { AuthenticatedFanRequest } from '../middleware/verifyFan';
import redis from '../config/redis';
import pool from '../config/database';

const UUID_V4_OR_GENERIC_REGEX =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export async function listAvailableSeats(req: Request, res: Response): Promise<void> {
  const eventId = typeof req.query.eventId === 'string' ? req.query.eventId : undefined;
  const parsedLimit = Number(req.query.limit);
  const limit = Number.isInteger(parsedLimit) && parsedLimit > 0 && parsedLimit <= 200
    ? parsedLimit
    : 50;

  if (eventId && !UUID_V4_OR_GENERIC_REGEX.test(eventId)) {
    res.status(400).json({
      error: 'Invalid eventId format. Expected a UUID matching events.id from PostgreSQL.'
    });
    return;
  }

  try {
    const params: Array<string | number> = [];
    const filters: string[] = ['t.id IS NULL'];

    if (eventId) {
      params.push(eventId);
      filters.push(`s.event_id = $${params.length}`);
    }

    params.push(limit);

    const query = `
      SELECT s.id
      FROM seats s
      LEFT JOIN tickets t ON t.seat_id = s.id
      WHERE ${filters.join(' AND ')}
      ORDER BY s.id
      LIMIT $${params.length}
    `;

    const result = await pool.query<{ id: string }>(query, params);

    res.status(200).json({
      count: result.rows.length,
      seatIds: result.rows.map((row) => row.id)
    });
  } catch (err) {
    console.error('Failed to list available seats:', err);
    res.status(500).json({ error: 'Failed to list available seats' });
  }
}

export async function lockSeat(req: AuthenticatedFanRequest, res: Response): Promise<void> {
  const { eventId, seatId } = req.body;
  const fan = req.fanMembership; // Passed down from the verifyFan middleware

  if (!eventId || !seatId || !fan) {
    res.status(400).json({ error: 'Missing required reservation payload data' });
    return;
  }

  if (!UUID_V4_OR_GENERIC_REGEX.test(seatId)) {
    res.status(400).json({
      error: 'Invalid seatId format. Expected a UUID matching seats.id from PostgreSQL.'
    });
    return;
  }

  // 1. Establish the unique lock key string for Redis
  const lockKey = `event:${eventId}:seat:${seatId}:lock`;
  const holdTimeSeconds = 300; // 5-minute checkout window
  let lockAcquired = false;

  try {
    // 2. ATOMIC OPERATION: Set key only if it DOES NOT exist (NX) with an Expiration timer (EX)
    const acquiredLock = await redis.set(lockKey, fan.userId, 'EX', holdTimeSeconds, 'NX');

    if (acquiredLock !== 'OK') {
      res.status(409).json({ 
        error: 'Seat holds conflict: This seat is currently held by another fan.' 
      });
      return;
    }

    lockAcquired = true;

    // 3. Optional verification: Double-check PostgreSQL ledger to ensure it hasn't already been sold permanently
    const dbCheck = await pool.query('SELECT id FROM tickets WHERE seat_id = $1', [seatId]);
    if (dbCheck.rows.length > 0) {
      // If already sold, clear the faulty redis lock immediately
      await redis.del(lockKey);
      res.status(410).json({ error: 'Seat Gone: This ticket has already been purchased.' });
      return;
    }

    // Success! Seat held safely for 5 minutes in cache memory
    res.status(200).json({
      message: 'Seat successfully reserved!',
      lockExpiresInSeconds: holdTimeSeconds,
      checkoutToken: lockKey
    });

  } catch (err) {
    if (lockAcquired) {
      await redis.del(lockKey);
    }

    console.error('High-concurrency database lock failure:', err);
    res.status(500).json({ error: 'Internal transaction error occurred' });
  }
}