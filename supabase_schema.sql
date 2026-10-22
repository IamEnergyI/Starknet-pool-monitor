-- ============================================================================
-- Crypto Trading Bot - Supabase / PostgreSQL schema
-- Run this file once in the Supabase SQL Editor.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ----------------------------------------------------------------------------
-- users
-- active_filter_id is intentionally created without an inline FK because the
-- filters table does not exist yet. The FK is added below after filters exists.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.users (
    telegram_id      BIGINT PRIMARY KEY,
    active_filter_id UUID
);

-- ----------------------------------------------------------------------------
-- filters
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.filters (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_id    BIGINT NOT NULL
                   REFERENCES public.users (telegram_id) ON DELETE CASCADE,
    name           TEXT NOT NULL DEFAULT 'Default',
    chains         JSONB NOT NULL DEFAULT '[]'::jsonb,
    req_profile    BOOLEAN NOT NULL DEFAULT FALSE,
    req_boosted    BOOLEAN NOT NULL DEFAULT FALSE,
    req_ads        BOOLEAN NOT NULL DEFAULT FALSE,
    req_launchpad  BOOLEAN NOT NULL DEFAULT FALSE,
    min_liquidity  NUMERIC,
    max_mcap       NUMERIC,
    max_age_days   NUMERIC,
    timeframe      TEXT NOT NULL DEFAULT '24H',
    min_buys       NUMERIC,
    max_buys       NUMERIC,
    min_sells      NUMERIC,
    max_sells      NUMERIC,
    min_volume     NUMERIC,
    max_volume     NUMERIC,
    min_change     NUMERIC,
    max_change     NUMERIC,
    labels         JSONB NOT NULL DEFAULT '[]'::jsonb,
    address_suffix TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Circular relationship: users.active_filter_id -> filters.id.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'users_active_filter_id_fkey'
          AND conrelid = 'public.users'::regclass
    ) THEN
        ALTER TABLE public.users
            ADD CONSTRAINT users_active_filter_id_fkey
            FOREIGN KEY (active_filter_id)
            REFERENCES public.filters (id)
            ON DELETE SET NULL;
    END IF;
END;
$$;

-- ----------------------------------------------------------------------------
-- sent_tokens
-- A token is considered a duplicate for 72 hours. The primary key guarantees
-- that an upsert refreshes sent_at instead of creating duplicate rows.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.sent_tokens (
    token_address TEXT PRIMARY KEY,
    chain_id      TEXT NOT NULL,
    sent_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ----------------------------------------------------------------------------
-- Indexes
-- ----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS filters_telegram_id_idx
    ON public.filters (telegram_id);

CREATE INDEX IF NOT EXISTS filters_updated_at_idx
    ON public.filters (updated_at DESC);

CREATE INDEX IF NOT EXISTS sent_tokens_sent_at_idx
    ON public.sent_tokens (sent_at DESC);

-- Supports filtering on JSONB chain arrays if direct SQL queries are added.
CREATE INDEX IF NOT EXISTS filters_chains_gin_idx
    ON public.filters USING GIN (chains);

CREATE INDEX IF NOT EXISTS filters_labels_gin_idx
    ON public.filters USING GIN (labels);
