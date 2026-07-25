import { Response } from 'express';
import { AuthenticatedFanRequest } from '../middleware/verifyFan';
import redis from '../config/redis';
import pool from '../config/database';

export async function lockSeat(req: AuthenticatedFanRequest, res: Response): Promise<void> {
  const { eventId, seatId } = req.body;
  const fan = req.fanMembership; // Passed down from the verifyFan middleware

  if (!eventId || !seatId || !fan) {
    res.status(400).json({ error: 'Missing required reservation payload data' });
    return;
  }

  // 1. Establish the unique lock key string for Redis
  const lockKey = `event:${eventId}:seat:${seatId}:lock`;
  const holdTimeSeconds = 300; // 5-minute checkout window

  try {
    // 2. ATOMIC OPERATION: Set key only if it DOES NOT exist (NX) with an Expiration timer (EX)
    const acquiredLock = await redis.set(lockKey, fan.userId, 'EX', holdTimeSeconds, 'NX');

    if (acquiredLock !== 'OK') {
      res.status(409).json({ 
        error: 'Seat holds conflict: This seat is currently held by another fan.' 
      });
      return;
    }

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
    console.error('High-concurrency database lock failure:', err);
    res.status(500).json({ error: 'Internal transaction error occurred' });
  }
}