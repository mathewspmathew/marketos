// Set required environment variables for tests
process.env.SHOPIFY_API_KEY = 'test-key';
process.env.SHOPIFY_API_SECRET = 'test-secret';
process.env.SHOPIFY_APP_URL = 'http://localhost:3000';
process.env.DATABASE_URL = 'postgresql://test:test@localhost/test';
process.env.REDIS_URL = 'redis://localhost:6379/0';
