import { Request, Response, NextFunction } from 'express';
import axios from 'axios';

// Extend Express Request interface to pass verified fan data down the line
export interface AuthenticatedFanRequest extends Request {
  fanMembership?: {
    membershipId: string;
    tier: string;
    userId: string;
  };
}

export async function verifyFanClubMembership(
  req: AuthenticatedFanRequest,
  res: Response,
  next: NextFunction
): Promise<void> {
  const fanToken = req.headers['x-fanclub-token'];
  const labelId = req.body.labelId || req.query.labelId;
  const mockFanVerify = process.env.MOCK_FAN_VERIFY === 'true';

  if (!fanToken) {
    res.status(401).json({ error: 'Access Denied: Missing x-fanclub-token header' });
    return;
  }

  if (mockFanVerify) {
    req.fanMembership = {
      membershipId: 'mock-membership-id',
      tier: 'MOCK_TIER',
      userId: 'mock-user-id'
    };

    return next();
  }

  try {
    // Mocking an external callout to the music label's official API (e.g., Weverse/SM Town Auth Server)
    const labelAuthUrl = `https://api.label-auth.com/v1/memberships/validate`;
    
    const response = await axios.post(labelAuthUrl, {
      token: fanToken,
      labelId: labelId
    }, {
      headers: { 'Authorization': `Bearer ${process.env.LABEL_API_SECRET}` },
      timeout: 1500 // Tight timeout to keep things lightning fast
    });

    if (response.data && response.data.isValid) {
      // Inject the verified membership data into the request object
      req.fanMembership = {
        membershipId: response.data.membershipId,
        tier: response.data.tier,
        userId: response.data.userId
      };
      
      return next(); // Validation successful! Proceed to the controller.
    }

    res.status(403).json({ error: 'Access Denied: Invalid or Expired Fanclub Membership' });
  } catch (error) {
    console.error('Label API Validation Failure:', error);
    // Fail closed for security: If the auth system crashes, do not let potential bots through
    res.status(500).json({ error: 'External identity verification temporarily unavailable' });
  }
}