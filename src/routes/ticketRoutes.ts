import { Router } from 'express';
import {
  getEventAvailability,
  createCartAndLock,
  addBilling,
  commitCart,
  getSafeTix
} from '../controllers/ticketController';
import { verifyFanClubMembership } from '../middleware/verifyFan';

const router = Router();

// STEP 1: Get Availability (Discovery & Inventory)
router.get('/events/:tmEventId/availability', getEventAvailability);

// STEP 2: Create Cart & Lock Seat (Requires Fan Membership Validation)
router.post('/cart/create', verifyFanClubMembership, createCartAndLock);

// STEP 3: Add Billing / Payment Info
router.put('/cart/:cartId/billing', verifyFanClubMembership, addBilling);

// STEP 4: Commit Cart & Finalize Purchase
router.put('/cart/:cartId/commit', verifyFanClubMembership, commitCart);

// STEP 5: SafeTix Secure Entry Token Delivery
router.get('/:ticketId/safetix-token', verifyFanClubMembership, getSafeTix);

export default router;