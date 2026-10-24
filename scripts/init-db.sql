\connect p2p_office_suite;

-- Enable UUID generator functions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Enable pgcrypto for password hashing & column encryption
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Enable ltree for high-speed materialized path traversal
CREATE EXTENSION IF NOT EXISTS "ltree";

-- Optimize PostgreSQL configuration for transaction concurrency
ALTER SYSTEM SET max_connections = '200';
ALTER SYSTEM SET shared_buffers = '512MB';
ALTER SYSTEM SET work_mem = '16MB';
ALTER SYSTEM SET checkpoint_completion_target = '0.9';