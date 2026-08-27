import axios from 'axios';
import dotenv from 'dotenv';

dotenv.config();

const TM_BASE = process.env.TM_API_BASE_URL || 'https://app.ticketmaster.com';
const TM_PARTNER_BASE = process.env.TM_PARTNER_API_BASE_URL || 'https://partner-api.ticketmaster.com';
const DEV_KEY = process.env.TM_DEVELOPER_API_KEY || '';
const PARTNER_KEY = process.env.TM_PARTNER_API_KEY || 'YOUR_TM_PARTNER_COMMERCE_API_KEY_PLACEHOLDER';

export class TicketmasterService {
  /**
   * [STEP 1] Fetch Event Details, Inventory & Pricing Tiers
   */
  static async getEventAvailability(tmEventId: string) {
    try {
      // 1. Fetch basic event info via Discovery API
      const discoveryRes = await axios.get(`${TM_BASE}/discovery/v2/events/${tmEventId}.json?apikey=${DEV_KEY}`);
      
      // 2. Fetch live seat tiers (Requires Partner API / Inventory endpoints)
      let liveSeats = [];
      try {
        const inventoryRes = await axios.get(`${TM_PARTNER_BASE}/partners/v1/events/${tmEventId}/inventory`, {
          headers: { 'Authorization': `Bearer ${PARTNER_KEY}` }
        });
        liveSeats = inventoryRes.data;
      } catch (e) {
        // Fallback or mock data while waiting for Partner API access
        liveSeats = discoveryRes.data?.priceRanges || [];
      }

      return {
        event: discoveryRes.data,
        inventory: liveSeats
      };
    } catch (error: any) {
      throw new Error(`Ticketmaster Step 1 (Availability) Failed: ${error.response?.data?.message || error.message}`);
    }
  }

  /**
   * [STEP 2] Create Cart & Lock Seats in Ticketmaster Backend
   */
  static async createCart(tmEventId: string, seatIds: string[], fanUserId: string) {
    try {
      // POST to TM Partner Cart Endpoint
      const response = await axios.post(
        `${TM_PARTNER_BASE}/partners/v1/events/${tmEventId}/cart`,
        {
          seats: seatIds,
          thirdPartyUserId: fanUserId,
          reservationTtlSeconds: 300 // 5 minute lock
        },
        {
          headers: {
            'Authorization': `Bearer ${PARTNER_KEY}`,
            'Content-Type': 'application/json'
          }
        }
      );
      return response.data; // Expected { cartId: "...", expiresAt: "...", totals: {...} }
    } catch (error: any) {
      // Temporary fallback payload for testing without active Partner API access
      if (PARTNER_KEY.includes('PLACEHOLDER')) {
        return {
          cartId: `mock_tm_cart_${Date.now()}`,
          expiresInSeconds: 300,
          status: 'RESERVED_MOCK'
        };
      }
      throw new Error(`Ticketmaster Step 2 (Create Cart) Failed: ${error.response?.data?.message || error.message}`);
    }
  }

  /**
   * [STEP 3] Add Billing Information & Payment Method
   */
  static async addBilling(cartId: string, billingPayload: {
    paymentToken: string;
    billingAddress: any;
    fanEmail: string;
  }) {
    try {
      const response = await axios.put(
        `${TM_PARTNER_BASE}/partners/v1/carts/${cartId}/billing`,
        billingPayload,
        {
          headers: {
            'Authorization': `Bearer ${PARTNER_KEY}`,
            'Content-Type': 'application/json'
          }
        }
      );
      return response.data;
    } catch (error: any) {
      if (PARTNER_KEY.includes('PLACEHOLDER')) {
        return { cartId, billingStatus: 'ATTACHED_MOCK' };
      }
      throw new Error(`Ticketmaster Step 3 (Add Billing) Failed: ${error.response?.data?.message || error.message}`);
    }
  }

  /**
   * [STEP 4] Commit Cart & Finalize Order
   */
  static async commitCart(cartId: string) {
    try {
      const response = await axios.put(
        `${TM_PARTNER_BASE}/partners/v1/carts/${cartId}/commit`,
        {},
        {
          headers: {
            'Authorization': `Bearer ${PARTNER_KEY}`,
            'Content-Type': 'application/json'
          }
        }
      );
      return response.data; // Expected { orderId: "...", status: "CONFIRMED", tickets: [...] }
    } catch (error: any) {
      if (PARTNER_KEY.includes('PLACEHOLDER')) {
        return {
          orderId: `TM-ORD-${Math.floor(Math.random() * 1000000)}`,
          status: 'CONFIRMED_MOCK',
          tickets: [{ tmTicketId: `tkt_${Date.now()}` }]
        };
      }
      throw new Error(`Ticketmaster Step 4 (Commit Cart) Failed: ${error.response?.data?.message || error.message}`);
    }
  }

  /**
   * [STEP 5] Retrieve SafeTix Token for Ticketmaster Secure Entry SDK
   */
  static async getSafeTixToken(tmOrderId: string, secureDeviceId: string) {
    try {
      const response = await axios.get(
        `${TM_PARTNER_BASE}/partners/v1/orders/${tmOrderId}/safetix-token?deviceId=${secureDeviceId}`,
        {
          headers: { 'Authorization': `Bearer ${PARTNER_KEY}` }
        }
      );
      return response.data; // Expected { renderToken: "eyJhbGci...", rotationIntervalMs: 15000 }
    } catch (error: any) {
      if (PARTNER_KEY.includes('PLACEHOLDER')) {
        return {
          renderToken: `mock_safetix_jwt_${Date.now()}`,
          rotationIntervalMs: 15000,
          sdkKey: process.env.TM_SAFETIX_SDK_KEY || 'MOCK_SDK_KEY'
        };
      }
      throw new Error(`Ticketmaster Step 5 (SafeTix) Failed: ${error.response?.data?.message || error.message}`);
    }
  }
}