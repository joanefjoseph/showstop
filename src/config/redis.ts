import Redis from 'ioredis';
import dotenv from 'dotenv';

dotenv.config();

// Initialize Redis client using environment variables
const redis = new Redis({
  host: process.env.REDIS_HOST || '127.0.0.1',
  port: Number(process.env.REDIS_PORT) || 6379,
  password: process.env.REDIS_PASSWORD || undefined,
  maxRetriesPerRequest: 3
});

redis.on('connect', () => console.log('🚀 Connected to Redis Cache Layer Successfully'));
redis.on('error', (err) => console.error('❌ Redis Connection Error:', err));

export default redis;