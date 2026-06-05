-- PiCommand Database Schema
-- PostgreSQL 14+

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================
-- USERS & ROLES
-- ============================================================

CREATE TYPE user_role AS ENUM ('admin', 'operator', 'viewer');

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    username VARCHAR(64) UNIQUE NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role user_role NOT NULL DEFAULT 'viewer',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login TIMESTAMPTZ
);

CREATE TABLE api_tokens (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    name VARCHAR(128) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    last_used TIMESTAMPTZ
);

-- ============================================================
-- NODES
-- ============================================================

CREATE TYPE node_status AS ENUM ('online', 'offline', 'pending', 'disabled');

CREATE TABLE nodes (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    node_id VARCHAR(64) UNIQUE NOT NULL,         -- human-readable slug e.g. "garage-pi"
    display_name VARCHAR(128) NOT NULL,
    description TEXT,
    public_key TEXT NOT NULL,                    -- RSA/ED25519 public key for auth
    status node_status NOT NULL DEFAULT 'pending',
    tags TEXT[] DEFAULT '{}',
    location VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_at TIMESTAMPTZ,
    approved_by UUID REFERENCES users(id),
    last_seen TIMESTAMPTZ,
    -- SSH tunnel config
    ssh_tunnel_port INTEGER UNIQUE,              -- assigned reverse tunnel port
    -- Metadata from last checkin
    ip_address INET,
    hostname VARCHAR(255),
    os_version TEXT,
    arch VARCHAR(32),
    pi_model TEXT
);

CREATE TABLE node_metrics (
    id BIGSERIAL PRIMARY KEY,
    node_id UUID NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cpu_percent REAL,
    ram_percent REAL,
    ram_used_mb INTEGER,
    ram_total_mb INTEGER,
    disk_percent REAL,
    disk_used_gb REAL,
    disk_total_gb REAL,
    cpu_temp_c REAL,
    load_avg_1 REAL,
    load_avg_5 REAL,
    load_avg_15 REAL,
    uptime_seconds BIGINT,
    net_bytes_sent BIGINT,
    net_bytes_recv BIGINT
);

-- Keep 7 days of per-minute metrics, 90 days of hourly aggregates
CREATE INDEX idx_metrics_node_time ON node_metrics(node_id, recorded_at DESC);

CREATE TABLE node_services (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    node_id UUID NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    service_name VARCHAR(128) NOT NULL,
    is_active BOOLEAN,
    is_enabled BOOLEAN,
    last_checked TIMESTAMPTZ,
    UNIQUE(node_id, service_name)
);

-- ============================================================
-- COMMANDS & AUDIT LOG
-- ============================================================

CREATE TYPE command_status AS ENUM ('pending', 'running', 'completed', 'failed', 'timeout');

CREATE TABLE commands (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    node_id UUID NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    issued_by UUID REFERENCES users(id),
    command TEXT NOT NULL,
    status command_status NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    exit_code INTEGER,
    stdout TEXT,
    stderr TEXT,
    timeout_seconds INTEGER NOT NULL DEFAULT 30
);

CREATE INDEX idx_commands_node ON commands(node_id, created_at DESC);

CREATE TABLE audit_log (
    id BIGSERIAL PRIMARY KEY,
    user_id UUID REFERENCES users(id),
    node_id UUID REFERENCES nodes(id),
    action VARCHAR(128) NOT NULL,
    details JSONB,
    ip_address INET,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_created ON audit_log(created_at DESC);
CREATE INDEX idx_audit_node ON audit_log(node_id, created_at DESC);

-- ============================================================
-- FILE TRANSFERS
-- ============================================================

CREATE TYPE transfer_direction AS ENUM ('upload', 'download');
CREATE TYPE transfer_status AS ENUM ('pending', 'in_progress', 'completed', 'failed');

CREATE TABLE file_transfers (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    node_id UUID NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    initiated_by UUID REFERENCES users(id),
    direction transfer_direction NOT NULL,
    remote_path TEXT NOT NULL,
    file_name VARCHAR(255) NOT NULL,
    file_size_bytes BIGINT,
    status transfer_status NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    error_message TEXT
);

-- ============================================================
-- ALERTS
-- ============================================================

CREATE TYPE alert_severity AS ENUM ('info', 'warning', 'critical');

CREATE TABLE alert_rules (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    node_id UUID REFERENCES nodes(id) ON DELETE CASCADE,  -- NULL = all nodes
    metric VARCHAR(64) NOT NULL,   -- 'cpu_percent', 'ram_percent', 'disk_percent', etc
    threshold REAL NOT NULL,
    severity alert_severity NOT NULL DEFAULT 'warning',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE alerts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    rule_id UUID REFERENCES alert_rules(id),
    node_id UUID NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    severity alert_severity NOT NULL,
    message TEXT NOT NULL,
    metric VARCHAR(64),
    metric_value REAL,
    fired_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    acknowledged_by UUID REFERENCES users(id),
    acknowledged_at TIMESTAMPTZ
);

-- ============================================================
-- DEPLOYMENT PACKAGES
-- ============================================================

CREATE TYPE deploy_status AS ENUM ('pending', 'in_progress', 'success', 'failed');

CREATE TABLE deployments (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    node_id UUID NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    initiated_by UUID REFERENCES users(id),
    package_name VARCHAR(255) NOT NULL,
    script TEXT NOT NULL,
    status deploy_status NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    output TEXT
);

-- ============================================================
-- DEFAULT ADMIN USER (change password immediately)
-- ============================================================

INSERT INTO users (username, email, password_hash, role)
VALUES (
    'admin',
    'admin@localhost',
    crypt('changeme', gen_salt('bf', 12)),
    'admin'
);
