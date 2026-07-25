import { Router } from 'express';
import { lockSeat } from '../controllers/ticketController';
import { verifyFanClubMembership } from '../middleware/verifyFan';

const router = Router();

// To lock a seat, a request MUST pass through membership validation first
router.post('/seats/lock', verifyFanClubMembership, lockSeat);

export default router;