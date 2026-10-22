-- ============================================================================
-- Crypto Scanner Bot - Supabase / PostgreSQL schema
--
-- Safe to run on a fresh database or on an existing database created with the
-- original schema. The sent_tokens block below migrates the old single-user
-- table to per-user deduplication without losing existing history.
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
-- Alerts are deduplicated per user. The composite primary key guarantees that
-- an upsert refreshes sent_at instead of creating duplicate rows.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.sent_tokens (
    telegram_id   BIGINT NOT NULL
                  REFERENCES public.users (telegram_id) ON DELETE CASCADE,
    token_address TEXT NOT NULL,
    chain_id      TEXT NOT NULL,
    sent_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (telegram_id, token_address)
);

-- Upgrade an existing single-user sent_tokens table in place. Existing token
-- history is copied to each current user before the old primary key is removed.
DO $$
DECLARE
    v_has_telegram_id BOOLEAN;
BEGIN
    SELECT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'sent_tokens'
          AND column_name = 'telegram_id'
    ) INTO v_has_telegram_id;

    IF NOT v_has_telegram_id THEN
        ALTER TABLE public.sent_tokens ADD COLUMN telegram_id BIGINT;
        ALTER TABLE public.sent_tokens DROP CONSTRAINT IF EXISTS sent_tokens_pkey;

        INSERT INTO public.sent_tokens (
            telegram_id, token_address, chain_id, sent_at
        )
        SELECT
            u.telegram_id, s.token_address, s.chain_id, s.sent_at
        FROM public.sent_tokens AS s
        CROSS JOIN public.users AS u
        WHERE s.telegram_id IS NULL;

        DELETE FROM public.sent_tokens WHERE telegram_id IS NULL;

        ALTER TABLE public.sent_tokens
            ALTER COLUMN telegram_id SET NOT NULL;
        ALTER TABLE public.sent_tokens
            ADD CONSTRAINT sent_tokens_pkey
            PRIMARY KEY (telegram_id, token_address);
        ALTER TABLE public.sent_tokens
            ADD CONSTRAINT sent_tokens_telegram_id_fkey
            FOREIGN KEY (telegram_id)
            REFERENCES public.users (telegram_id)
            ON DELETE CASCADE;
    END IF;
END;
$$;

-- ----------------------------------------------------------------------------
-- paper_accounts
-- One fake-money account per Telegram user.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.paper_accounts (
    telegram_id       BIGINT PRIMARY KEY
                      REFERENCES public.users (telegram_id) ON DELETE CASCADE,
    starting_balance  NUMERIC(18, 2) NOT NULL DEFAULT 1000
                      CHECK (starting_balance >= 0),
    cash_balance      NUMERIC(18, 2) NOT NULL DEFAULT 1000
                      CHECK (cash_balance >= 0),
    total_deposited   NUMERIC(18, 2) NOT NULL DEFAULT 1000
                      CHECK (total_deposited >= 0),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ----------------------------------------------------------------------------
-- paper_alerts
-- Persisted alerts give inline buy buttons a short, stable callback id.
-- Buttons expire after one hour.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.paper_alerts (
    id             BIGSERIAL PRIMARY KEY,
    telegram_id    BIGINT NOT NULL
                   REFERENCES public.users (telegram_id) ON DELETE CASCADE,
    filter_id      UUID REFERENCES public.filters (id) ON DELETE SET NULL,
    token_address  TEXT NOT NULL,
    chain_id       TEXT NOT NULL,
    pair_address   TEXT NOT NULL,
    symbol         TEXT,
    name           TEXT,
    price_usd      NUMERIC(38, 18),
    warning        TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at     TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '1 hour')
);

-- ----------------------------------------------------------------------------
-- paper_positions
-- Aggregated holding per user, chain, and token. Individual executions are
-- recorded in paper_fills so multiple buys and partial sells are preserved.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.paper_positions (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_id       BIGINT NOT NULL
                      REFERENCES public.users (telegram_id) ON DELETE CASCADE,
    chain_id          TEXT NOT NULL,
    token_address     TEXT NOT NULL,
    pair_address      TEXT NOT NULL,
    symbol            TEXT,
    name              TEXT,
    quantity          NUMERIC(38, 18) NOT NULL DEFAULT 0
                      CHECK (quantity >= 0),
    avg_entry_price   NUMERIC(38, 18) NOT NULL DEFAULT 0
                      CHECK (avg_entry_price >= 0),
    cost_basis_usd    NUMERIC(18, 2) NOT NULL DEFAULT 0
                      CHECK (cost_basis_usd >= 0),
    realized_pnl_usd  NUMERIC(18, 2) NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open', 'closed')),
    last_price_usd    NUMERIC(38, 18),
    last_priced_at    TIMESTAMPTZ,
    opened_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at         TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (telegram_id, chain_id, token_address)
);

-- ----------------------------------------------------------------------------
-- paper_fills
-- Every paper buy and sell execution.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.paper_fills (
    id                 BIGSERIAL PRIMARY KEY,
    telegram_id        BIGINT NOT NULL
                       REFERENCES public.users (telegram_id) ON DELETE CASCADE,
    position_id        UUID NOT NULL
                       REFERENCES public.paper_positions (id) ON DELETE CASCADE,
    alert_id           BIGINT
                       REFERENCES public.paper_alerts (id) ON DELETE SET NULL,
    side               TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    symbol             TEXT,
    chain_id           TEXT NOT NULL,
    token_address      TEXT NOT NULL,
    quantity           NUMERIC(38, 18) NOT NULL CHECK (quantity > 0),
    price_usd          NUMERIC(38, 18) NOT NULL CHECK (price_usd > 0),
    market_price_usd   NUMERIC(38, 18) NOT NULL CHECK (market_price_usd > 0),
    cash_amount_usd    NUMERIC(18, 2) NOT NULL CHECK (cash_amount_usd >= 0),
    fee_usd            NUMERIC(18, 2) NOT NULL DEFAULT 0 CHECK (fee_usd >= 0),
    slippage_bps       INTEGER NOT NULL DEFAULT 0 CHECK (slippage_bps >= 0),
    realized_pnl_usd   NUMERIC(18, 2) NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ----------------------------------------------------------------------------
-- paper_deposits
-- Initial balances and later top-ups. These are kept separate from PnL.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.paper_deposits (
    id          BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT NOT NULL
                REFERENCES public.users (telegram_id) ON DELETE CASCADE,
    amount      NUMERIC(18, 2) NOT NULL CHECK (amount > 0),
    kind        TEXT NOT NULL DEFAULT 'topup'
                CHECK (kind IN ('initial', 'topup')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ----------------------------------------------------------------------------
-- Indexes
-- ----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS filters_telegram_id_idx
    ON public.filters (telegram_id);

CREATE INDEX IF NOT EXISTS filters_updated_at_idx
    ON public.filters (updated_at DESC);

CREATE INDEX IF NOT EXISTS sent_tokens_telegram_sent_idx
    ON public.sent_tokens (telegram_id, sent_at DESC);

CREATE INDEX IF NOT EXISTS sent_tokens_sent_at_idx
    ON public.sent_tokens (sent_at DESC);

CREATE INDEX IF NOT EXISTS filters_chains_gin_idx
    ON public.filters USING GIN (chains);

CREATE INDEX IF NOT EXISTS filters_labels_gin_idx
    ON public.filters USING GIN (labels);

CREATE INDEX IF NOT EXISTS paper_alerts_telegram_created_idx
    ON public.paper_alerts (telegram_id, created_at DESC);

CREATE INDEX IF NOT EXISTS paper_positions_telegram_status_idx
    ON public.paper_positions (telegram_id, status);

CREATE INDEX IF NOT EXISTS paper_fills_telegram_created_idx
    ON public.paper_fills (telegram_id, created_at DESC);

CREATE INDEX IF NOT EXISTS paper_deposits_telegram_created_idx
    ON public.paper_deposits (telegram_id, created_at DESC);

-- ============================================================================
-- Paper-trading functions
-- These run as one PostgreSQL transaction so double-taps cannot overspend or
-- sell more than the account owns.
-- ============================================================================

CREATE OR REPLACE FUNCTION public.paper_get_or_create_account(
    p_telegram_id BIGINT,
    p_starting_balance NUMERIC DEFAULT 1000
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    v_account public.paper_accounts%ROWTYPE;
    v_inserted INTEGER := 0;
    v_balance NUMERIC(18, 2);
BEGIN
    IF p_starting_balance IS NULL
        OR p_starting_balance < 1
        OR p_starting_balance > 1000000 THEN
        RAISE EXCEPTION 'invalid_amount';
    END IF;

    v_balance := ROUND(p_starting_balance, 2);

    INSERT INTO public.paper_accounts (
        telegram_id, starting_balance, cash_balance, total_deposited
    )
    VALUES (p_telegram_id, v_balance, v_balance, v_balance)
    ON CONFLICT (telegram_id) DO NOTHING;

    GET DIAGNOSTICS v_inserted = ROW_COUNT;

    IF v_inserted > 0 THEN
        INSERT INTO public.paper_deposits (telegram_id, amount, kind)
        VALUES (p_telegram_id, v_balance, 'initial');
    END IF;

    SELECT * INTO v_account
    FROM public.paper_accounts
    WHERE telegram_id = p_telegram_id;

    RETURN jsonb_build_object(
        'telegram_id', v_account.telegram_id,
        'starting_balance', v_account.starting_balance,
        'cash_balance', v_account.cash_balance,
        'total_deposited', v_account.total_deposited
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.paper_add_funds(
    p_telegram_id BIGINT,
    p_amount NUMERIC
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    v_account public.paper_accounts%ROWTYPE;
    v_amount NUMERIC(18, 2);
BEGIN
    v_amount := ROUND(p_amount, 2);
    IF v_amount IS NULL OR v_amount <= 0 OR v_amount > 1000000 THEN
        RAISE EXCEPTION 'invalid_amount';
    END IF;

    UPDATE public.paper_accounts
    SET cash_balance = cash_balance + v_amount,
        total_deposited = total_deposited + v_amount,
        updated_at = NOW()
    WHERE telegram_id = p_telegram_id
    RETURNING * INTO v_account;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'account_not_found';
    END IF;

    INSERT INTO public.paper_deposits (telegram_id, amount, kind)
    VALUES (p_telegram_id, v_amount, 'topup');

    RETURN jsonb_build_object(
        'telegram_id', v_account.telegram_id,
        'starting_balance', v_account.starting_balance,
        'cash_balance', v_account.cash_balance,
        'total_deposited', v_account.total_deposited,
        'added', v_amount
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.paper_set_balance(
    p_telegram_id BIGINT,
    p_balance NUMERIC
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    v_account public.paper_accounts%ROWTYPE;
    v_balance NUMERIC(18, 2);
BEGIN
    v_balance := ROUND(p_balance, 2);
    IF v_balance IS NULL OR v_balance < 1 OR v_balance > 1000000 THEN
        RAISE EXCEPTION 'invalid_amount';
    END IF;

    IF EXISTS (
        SELECT 1 FROM public.paper_fills WHERE telegram_id = p_telegram_id
    ) THEN
        RAISE EXCEPTION 'trading_started';
    END IF;

    INSERT INTO public.paper_accounts (
        telegram_id, starting_balance, cash_balance, total_deposited
    )
    VALUES (p_telegram_id, v_balance, v_balance, v_balance)
    ON CONFLICT (telegram_id) DO UPDATE
    SET starting_balance = EXCLUDED.starting_balance,
        cash_balance = EXCLUDED.cash_balance,
        total_deposited = EXCLUDED.total_deposited,
        updated_at = NOW()
    RETURNING * INTO v_account;

    DELETE FROM public.paper_deposits WHERE telegram_id = p_telegram_id;
    INSERT INTO public.paper_deposits (telegram_id, amount, kind)
    VALUES (p_telegram_id, v_balance, 'initial');

    RETURN jsonb_build_object(
        'telegram_id', v_account.telegram_id,
        'starting_balance', v_account.starting_balance,
        'cash_balance', v_account.cash_balance,
        'total_deposited', v_account.total_deposited
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.paper_reset(
    p_telegram_id BIGINT,
    p_balance NUMERIC DEFAULT 1000
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    v_account public.paper_accounts%ROWTYPE;
    v_balance NUMERIC(18, 2);
BEGIN
    v_balance := ROUND(p_balance, 2);
    IF v_balance IS NULL OR v_balance < 1 OR v_balance > 1000000 THEN
        RAISE EXCEPTION 'invalid_amount';
    END IF;

    DELETE FROM public.paper_fills WHERE telegram_id = p_telegram_id;
    DELETE FROM public.paper_positions WHERE telegram_id = p_telegram_id;
    DELETE FROM public.paper_alerts WHERE telegram_id = p_telegram_id;
    DELETE FROM public.paper_deposits WHERE telegram_id = p_telegram_id;

    INSERT INTO public.paper_accounts (
        telegram_id, starting_balance, cash_balance, total_deposited,
        created_at, updated_at
    )
    VALUES (p_telegram_id, v_balance, v_balance, v_balance, NOW(), NOW())
    ON CONFLICT (telegram_id) DO UPDATE
    SET starting_balance = EXCLUDED.starting_balance,
        cash_balance = EXCLUDED.cash_balance,
        total_deposited = EXCLUDED.total_deposited,
        updated_at = NOW()
    RETURNING * INTO v_account;

    INSERT INTO public.paper_deposits (telegram_id, amount, kind)
    VALUES (p_telegram_id, v_balance, 'initial');

    RETURN jsonb_build_object(
        'telegram_id', v_account.telegram_id,
        'starting_balance', v_account.starting_balance,
        'cash_balance', v_account.cash_balance,
        'total_deposited', v_account.total_deposited
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.paper_buy(
    p_telegram_id BIGINT,
    p_alert_id BIGINT,
    p_amount_usd NUMERIC,
    p_market_price NUMERIC,
    p_fee_rate NUMERIC DEFAULT 0.005,
    p_slippage_bps INTEGER DEFAULT 200
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    v_alert public.paper_alerts%ROWTYPE;
    v_account public.paper_accounts%ROWTYPE;
    v_position public.paper_positions%ROWTYPE;
    v_amount NUMERIC(18, 2);
    v_fee NUMERIC(18, 2);
    v_net NUMERIC(18, 2);
    v_effective_price NUMERIC(38, 18);
    v_quantity NUMERIC(38, 18);
    v_fill_id BIGINT;
BEGIN
    IF p_amount_usd IS NULL OR p_amount_usd <= 0 THEN
        RAISE EXCEPTION 'invalid_amount';
    END IF;
    IF p_market_price IS NULL OR p_market_price <= 0 THEN
        RAISE EXCEPTION 'invalid_price';
    END IF;
    IF p_fee_rate IS NULL OR p_fee_rate < 0 OR p_fee_rate >= 1 THEN
        RAISE EXCEPTION 'invalid_fee_rate';
    END IF;
    IF p_slippage_bps IS NULL OR p_slippage_bps < 0 OR p_slippage_bps > 5000 THEN
        RAISE EXCEPTION 'invalid_slippage';
    END IF;

    v_amount := ROUND(p_amount_usd, 2);
    IF v_amount < 1 OR v_amount > 1000000 THEN
        RAISE EXCEPTION 'invalid_amount';
    END IF;

    SELECT * INTO v_alert
    FROM public.paper_alerts
    WHERE id = p_alert_id AND telegram_id = p_telegram_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'alert_not_found';
    END IF;
    IF NOW() > v_alert.expires_at THEN
        RAISE EXCEPTION 'alert_expired';
    END IF;

    SELECT * INTO v_account
    FROM public.paper_accounts
    WHERE telegram_id = p_telegram_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'account_not_found';
    END IF;
    IF v_account.cash_balance < v_amount THEN
        RAISE EXCEPTION 'insufficient_funds';
    END IF;

    v_fee := ROUND(v_amount * p_fee_rate, 2);
    v_net := v_amount - v_fee;
    IF v_net <= 0 THEN
        RAISE EXCEPTION 'invalid_amount';
    END IF;

    v_effective_price := p_market_price
        * (1 + (p_slippage_bps::NUMERIC / 10000));
    v_quantity := v_net / v_effective_price;

    UPDATE public.paper_accounts
    SET cash_balance = cash_balance - v_amount,
        updated_at = NOW()
    WHERE telegram_id = p_telegram_id;

    INSERT INTO public.paper_positions AS p (
        telegram_id, chain_id, token_address, pair_address, symbol, name,
        quantity, avg_entry_price, cost_basis_usd, status,
        last_price_usd, last_priced_at, opened_at, updated_at
    )
    VALUES (
        p_telegram_id, v_alert.chain_id, v_alert.token_address,
        v_alert.pair_address, v_alert.symbol, v_alert.name,
        v_quantity, v_amount / v_quantity, v_amount, 'open',
        p_market_price, NOW(), NOW(), NOW()
    )
    ON CONFLICT (telegram_id, chain_id, token_address) DO UPDATE
    SET pair_address = EXCLUDED.pair_address,
        symbol = COALESCE(EXCLUDED.symbol, p.symbol),
        name = COALESCE(EXCLUDED.name, p.name),
        quantity = p.quantity + EXCLUDED.quantity,
        cost_basis_usd = p.cost_basis_usd + EXCLUDED.cost_basis_usd,
        avg_entry_price = (
            p.cost_basis_usd + EXCLUDED.cost_basis_usd
        ) / (
            p.quantity + EXCLUDED.quantity
        ),
        status = 'open',
        last_price_usd = EXCLUDED.last_price_usd,
        last_priced_at = NOW(),
        closed_at = NULL,
        opened_at = CASE
            WHEN p.quantity <= 0 THEN NOW()
            ELSE p.opened_at
        END,
        updated_at = NOW()
    RETURNING * INTO v_position;

    INSERT INTO public.paper_fills (
        telegram_id, position_id, alert_id, side, symbol, chain_id,
        token_address, quantity, price_usd, market_price_usd,
        cash_amount_usd, fee_usd, slippage_bps, realized_pnl_usd
    )
    VALUES (
        p_telegram_id, v_position.id, p_alert_id, 'buy', v_position.symbol,
        v_alert.chain_id, v_alert.token_address, v_quantity,
        v_effective_price, p_market_price, v_amount, v_fee,
        p_slippage_bps, 0
    )
    RETURNING id INTO v_fill_id;

    RETURN jsonb_build_object(
        'fill_id', v_fill_id,
        'position_id', v_position.id,
        'token_address', v_alert.token_address,
        'chain_id', v_alert.chain_id,
        'symbol', v_position.symbol,
        'quantity', v_quantity,
        'price_usd', v_effective_price,
        'market_price_usd', p_market_price,
        'cash_amount_usd', v_amount,
        'fee_usd', v_fee,
        'cash_balance', v_account.cash_balance - v_amount,
        'position_quantity', v_position.quantity,
        'avg_entry_price', v_position.avg_entry_price
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.paper_sell(
    p_telegram_id BIGINT,
    p_position_id UUID,
    p_fraction NUMERIC,
    p_market_price NUMERIC,
    p_fee_rate NUMERIC DEFAULT 0.005,
    p_slippage_bps INTEGER DEFAULT 200
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    v_account public.paper_accounts%ROWTYPE;
    v_position public.paper_positions%ROWTYPE;
    v_fraction NUMERIC;
    v_sell_quantity NUMERIC(38, 18);
    v_effective_price NUMERIC(38, 18);
    v_gross NUMERIC(18, 2);
    v_fee NUMERIC(18, 2);
    v_proceeds NUMERIC(18, 2);
    v_cost_removed NUMERIC(18, 2);
    v_realized NUMERIC(18, 2);
    v_new_quantity NUMERIC(38, 18);
    v_new_cost_basis NUMERIC(18, 2);
    v_new_status TEXT;
    v_fill_id BIGINT;
BEGIN
    IF p_fraction IS NULL OR p_fraction <= 0 OR p_fraction > 1 THEN
        RAISE EXCEPTION 'invalid_fraction';
    END IF;
    IF p_market_price IS NULL OR p_market_price <= 0 THEN
        RAISE EXCEPTION 'invalid_price';
    END IF;
    IF p_fee_rate IS NULL OR p_fee_rate < 0 OR p_fee_rate >= 1 THEN
        RAISE EXCEPTION 'invalid_fee_rate';
    END IF;
    IF p_slippage_bps IS NULL OR p_slippage_bps < 0 OR p_slippage_bps > 5000 THEN
        RAISE EXCEPTION 'invalid_slippage';
    END IF;

    v_fraction := p_fraction;

    SELECT * INTO v_position
    FROM public.paper_positions
    WHERE id = p_position_id AND telegram_id = p_telegram_id
    FOR UPDATE;

    IF NOT FOUND OR v_position.status <> 'open' OR v_position.quantity <= 0 THEN
        RAISE EXCEPTION 'position_not_found';
    END IF;

    SELECT * INTO v_account
    FROM public.paper_accounts
    WHERE telegram_id = p_telegram_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'account_not_found';
    END IF;

    IF v_fraction >= 0.999999 THEN
        v_fraction := 1;
        v_sell_quantity := v_position.quantity;
    ELSE
        v_sell_quantity := v_position.quantity * v_fraction;
    END IF;

    IF v_sell_quantity <= 0 THEN
        RAISE EXCEPTION 'invalid_fraction';
    END IF;

    v_effective_price := p_market_price
        * (1 - (p_slippage_bps::NUMERIC / 10000));
    v_gross := ROUND(v_sell_quantity * v_effective_price, 2);
    v_fee := ROUND(v_gross * p_fee_rate, 2);
    v_proceeds := v_gross - v_fee;

    IF v_proceeds < 0 THEN
        RAISE EXCEPTION 'invalid_price';
    END IF;

    IF v_fraction = 1 THEN
        v_cost_removed := v_position.cost_basis_usd;
        v_new_quantity := 0;
        v_new_cost_basis := 0;
        v_new_status := 'closed';
    ELSE
        v_cost_removed := ROUND(
            v_position.cost_basis_usd * v_fraction, 2
        );
        v_new_quantity := v_position.quantity - v_sell_quantity;
        v_new_cost_basis := v_position.cost_basis_usd - v_cost_removed;
        v_new_status := 'open';
    END IF;

    v_realized := v_proceeds - v_cost_removed;

    UPDATE public.paper_accounts
    SET cash_balance = cash_balance + v_proceeds,
        updated_at = NOW()
    WHERE telegram_id = p_telegram_id;

    UPDATE public.paper_positions
    SET quantity = v_new_quantity,
        cost_basis_usd = v_new_cost_basis,
        realized_pnl_usd = realized_pnl_usd + v_realized,
        status = v_new_status,
        last_price_usd = p_market_price,
        last_priced_at = NOW(),
        closed_at = CASE
            WHEN v_new_status = 'closed' THEN NOW()
            ELSE NULL
        END,
        updated_at = NOW()
    WHERE id = v_position.id
    RETURNING * INTO v_position;

    INSERT INTO public.paper_fills (
        telegram_id, position_id, alert_id, side, symbol, chain_id,
        token_address, quantity, price_usd, market_price_usd,
        cash_amount_usd, fee_usd, slippage_bps, realized_pnl_usd
    )
    VALUES (
        p_telegram_id, v_position.id, NULL, 'sell', v_position.symbol,
        v_position.chain_id, v_position.token_address, v_sell_quantity,
        v_effective_price, p_market_price, v_proceeds, v_fee,
        p_slippage_bps, v_realized
    )
    RETURNING id INTO v_fill_id;

    RETURN jsonb_build_object(
        'fill_id', v_fill_id,
        'position_id', v_position.id,
        'token_address', v_position.token_address,
        'chain_id', v_position.chain_id,
        'symbol', v_position.symbol,
        'quantity', v_sell_quantity,
        'price_usd', v_effective_price,
        'market_price_usd', p_market_price,
        'cash_amount_usd', v_proceeds,
        'fee_usd', v_fee,
        'realized_pnl_usd', v_realized,
        'cash_balance', v_account.cash_balance + v_proceeds,
        'position_quantity', v_position.quantity,
        'position_status', v_position.status,
        'fraction', v_fraction
    );
END;
$$;

-- Refresh PostgREST's schema cache so new tables/functions are exposed immediately.
NOTIFY pgrst, 'reload schema';
