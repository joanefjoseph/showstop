import { Request, Response } from 'express';
import { AuthenticatedFanRequest } from '../middleware/verifyFan';
import redis from '../config/redis';
import pool from '../config/database';
import { TicketmasterService } from '../services/ticketmasterService';

/**
 * STEP 1: GET Event Availability & Pricing
 */
export async function getEventAvailability(req: Request, res: Response): Promise<void> {
  const { tmEventId } = req.params;
  if (typeof tmEventId !== 'string') {
    res.status(400).json({ error: 'Missing tmEventId' });
    return;
  }

  try {
    const data = await TicketmasterService.getEventAvailability(tmEventId);
    res.status(200).json(data);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
}

/**
 * STEP 2: Lock Seat (Redis + Ticketmaster Cart Creation)
 */
export async function createCartAndLock(req: AuthenticatedFanRequest, res: Response): Promise<void> {
  const { eventId, tmEventId, seatId, tmSeatId } = req.body;
  const fan = req.fanMembership; // Passed down from the verifyFan middleware

  if (!eventId || !seatId || !fan || !tmEventId) {
    res.status(400).json({ error: 'Missing required reservation payload data' });
    return;
  }

  const lockKey = `event:${eventId}:seat:${seatId}:lock`;
  const holdTimeSeconds = 300;

  try {
    // 1. Local Atomic Redis Lock
    const acquired = await redis.set(lockKey, fan.userId, 'EX', holdTimeSeconds, 'NX');
    if (acquired !== 'OK') {
      res.status(409).json({ error: 'Seat is currently held by another fan.' });
      return;
    }

    // 2. Call TM Partner Commerce API to reserve on TM host backend
    const tmCart = await TicketmasterService.createCart(tmEventId, [tmSeatId || seatId], fan.userId);

    // 3. Cache cart details in Redis
    await redis.set(`cart:${tmCart.cartId}`, JSON.stringify({
      userId: fan.userId,
      membershipId: fan.membershipId,
      seatId,
      eventId
    }), 'EX', holdTimeSeconds);

    res.status(200).json({
      message: 'Cart created and seats reserved',
      cartId: tmCart.cartId,
      expiresInSeconds: holdTimeSeconds
    });
  } catch (err: any) {
    await redis.del(lockKey);
    res.status(500).json({ error: err.message || 'Failed to create cart' });
  }
}

/**
 * STEP 3: Add Billing / Payment Information
 */
export async function addBilling(req: AuthenticatedFanRequest, res: Response): Promise<void> {
  const { cartId } = req.params;
  const { paymentToken, billingAddress, fanEmail } = req.body;

  if (typeof cartId !== 'string' || !paymentToken) {
    res.status(400).json({ error: 'Missing cartId or paymentToken' });
    return;
  }

  try {
    const result = await TicketmasterService.addBilling(cartId, {
      paymentToken,
      billingAddress,
      fanEmail
    });
    res.status(200).json({ message: 'Billing info added successfully', details: result });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
}

/**
 * STEP 4: Commit Cart & Finalize Order (DB Ledger insertion)
 */
export async function commitCart(req: AuthenticatedFanRequest, res: Response): Promise<void> {
  const { cartId } = req.params;
  const { secureDeviceId, purchasePriceCents } = req.body;
  const fan = req.fanMembership;

  if (typeof cartId !== 'string' || !secureDeviceId || !fan) {
    res.status(400).json({ error: 'Missing required commit parameters' });
    return;
  }

  try {
    // 1. Get cached cart information from Redis
    const cartDataRaw = await redis.get(`cart:${cartId}`);
    if (!cartDataRaw) {
      res.status(410).json({ error: 'Cart reservation has expired. Please re-select seats.' });
      return;
    }
    const cartData = JSON.parse(cartDataRaw);

    // 2. Commit Cart on Ticketmaster
    const tmOrder = await TicketmasterService.commitCart(cartId);

    // 3. Write confirmed purchase to local PostgreSQL ledger
    const query = `
      INSERT INTO tickets (tm_order_id, seat_id, user_id, fan_membership_id, purchase_price_cents, secure_device_id)
      VALUES ($1, $2, $3, $4, $5, $6)
      RETURNING id, issued_at;
    `;
    const dbResult = await pool.query(query, [
      tmOrder.orderId,
      cartData.seatId,
      fan.userId,
      fan.membershipId,
      purchasePriceCents || 15000,
      secureDeviceId
    ]);

    // 4. Release Redis lock key
    await redis.del(`event:${cartData.eventId}:seat:${cartData.seatId}:lock`);
    await redis.del(`cart:${cartId}`);

    res.status(200).json({
      message: 'Order successfully confirmed!',
      ticketId: dbResult.rows[0].id,
      tmOrderId: tmOrder.orderId,
      issuedAt: dbResult.rows[0].issued_at
    });
  } catch (err: any) {
    res.status(500).json({ error: err.message || 'Failed to finalize order' });
  }
}

/**
 * STEP 5: SafeTix Token Delivery for Web/Mobile SDK
 */
export async function getSafeTix(req: AuthenticatedFanRequest, res: Response): Promise<void> {
  const { ticketId } = req.params;
  const { deviceId } = req.query;

  if (!ticketId || typeof deviceId !== 'string') {
    res.status(400).json({ error: 'Missing ticketId or deviceId' });
    return;
  }

  try {
    const ticketRes = await pool.query('SELECT tm_order_id FROM tickets WHERE id = $1', [ticketId]);
    if (ticketRes.rows.length === 0) {
      res.status(404).json({ error: 'Ticket not found' });
      return;
    }

    const tmOrderId = ticketRes.rows[0].tm_order_id;
    const safetixData = await TicketmasterService.getSafeTixToken(tmOrderId, deviceId);

    res.status(200).json(safetixData);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
}