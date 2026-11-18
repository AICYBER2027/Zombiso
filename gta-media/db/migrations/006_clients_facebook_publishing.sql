-- Creator clients (accounts, branding, preferences, permission records, deliveries, plans), Facebook Reels as a
-- fourth platform, rights-ledger scopes and safer publishing (retry back-off, duplicate guard, error kinds).
-- Additive only: no existing row is changed or removed.

-- ---------------------------------------------------------------- Facebook Reels
ALTER TABLE platform_accounts DROP CONSTRAINT IF EXISTS platform_accounts_platform_check;
ALTER TABLE platform_accounts ADD CONSTRAINT platform_accounts_platform_check
    CHECK (platform IN ('youtube', 'instagram', 'tiktok', 'facebook'));
ALTER TABLE renders DROP CONSTRAINT IF EXISTS renders_platform_check;
ALTER TABLE renders ADD CONSTRAINT renders_platform_check
    CHECK (platform IN ('youtube', 'instagram', 'tiktok', 'facebook'));
ALTER TABLE publish_jobs DROP CONSTRAINT IF EXISTS publish_jobs_platform_check;
ALTER TABLE publish_jobs ADD CONSTRAINT publish_jobs_platform_check
    CHECK (platform IN ('youtube', 'instagram', 'tiktok', 'facebook'));

-- ---------------------------------------------------------------- publishing
ALTER TABLE publish_jobs ADD COLUMN content_sha256 TEXT;           -- file that was (or will be) posted
ALTER TABLE publish_jobs ADD COLUMN error_kind TEXT;               -- auth | permission | quota | rate_limit |
                                                                    -- network | rejected | duplicate | unknown
ALTER TABLE publish_jobs ADD COLUMN attempt_log JSONB NOT NULL DEFAULT '[]';
CREATE INDEX publish_jobs_hash_idx ON publish_jobs(account_id, content_sha256) WHERE content_sha256 IS NOT NULL;

-- ---------------------------------------------------------------- rights scopes
-- NULL = an entry made before scopes existed: it keeps its old meaning (clip creation + VicePulse channels).
-- New entries list exactly what the creator allowed. Checked separately at each step.
ALTER TABLE rights_ledger ADD COLUMN scopes TEXT[];
ALTER TABLE rights_ledger ADD CONSTRAINT rights_ledger_scopes_check CHECK (scopes IS NULL OR scopes <@ ARRAY[
    'clip_creation', 'vicepulse_channel_publishing', 'creator_channel_publishing', 'monetisation',
    'branding', 'live_feed']::TEXT[]);

-- ---------------------------------------------------------------- plans and clients
CREATE TABLE plans (
    key             TEXT PRIMARY KEY CHECK (key ~ '^[a-z0-9_]{2,30}$'),
    name            TEXT NOT NULL,
    monthly_price_cents INTEGER NOT NULL CHECK (monthly_price_cents >= 0),
    currency        TEXT NOT NULL DEFAULT 'USD',
    clips_per_month INTEGER NOT NULL CHECK (clips_per_month >= 0),
    live_feeds      INTEGER NOT NULL DEFAULT 0,
    styles          TEXT[] NOT NULL,
    features        JSONB NOT NULL DEFAULT '{}',
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    sort            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE clients (
    id              SERIAL PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]{3,40}$'),
    display_name    TEXT NOT NULL,
    language        TEXT NOT NULL DEFAULT 'en' CHECK (language IN ('en', 'pt-BR')),
    creator_id      INTEGER REFERENCES creators(id),
    plan_key        TEXT NOT NULL DEFAULT 'free' REFERENCES plans(key),
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'active', 'suspended', 'closed')),
    -- creator_name, handle, monogram, logo (file in the data volume), theme {primary, secondary, accent, dark},
    -- frame {style: gradient|solid|neon|minimal, radius, thickness}
    branding        JSONB NOT NULL DEFAULT '{}',
    -- style (cinematic|viral|clean|pro), layout, captions (on/off, language), platforms, facecam (auto|x,y,w,h)
    preferences     JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE client_users (
    id              SERIAL PRIMARY KEY,
    client_id       INTEGER NOT NULL REFERENCES clients(id),
    email           TEXT NOT NULL UNIQUE CHECK (email = lower(email) AND email LIKE '%_@_%'),
    password_hash   TEXT NOT NULL,                  -- pbkdf2_sha256$iterations$salt$hash, never the password
    role            TEXT NOT NULL DEFAULT 'owner' CHECK (role IN ('owner', 'editor', 'viewer')),
    is_staff        BOOLEAN NOT NULL DEFAULT FALSE,  -- VicePulse staff (sees every client)
    failed_logins   INTEGER NOT NULL DEFAULT 0,
    locked_until    TIMESTAMPTZ,
    last_login_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE web_sessions (
    token_sha256    TEXT PRIMARY KEY,               -- the cookie holds the token; only its hash is stored
    user_id         INTEGER NOT NULL REFERENCES client_users(id),
    csrf            TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX web_sessions_user_idx ON web_sessions(user_id);

-- What the creator says they allow, recorded from the website. A record is a *request*: it never makes footage
-- usable by itself. VicePulse staff check the evidence and write the verified rights_ledger row (rights_id).
CREATE TABLE client_permissions (
    id              SERIAL PRIMARY KEY,
    client_id       INTEGER NOT NULL REFERENCES clients(id),
    scope           TEXT NOT NULL CHECK (scope IN ('clip_creation', 'vicepulse_channel_publishing',
                        'creator_channel_publishing', 'monetisation', 'branding', 'live_feed')),
    platforms       TEXT[] NOT NULL DEFAULT '{}',
    languages       TEXT[] NOT NULL DEFAULT ARRAY['*'],
    evidence_url    TEXT,
    note            TEXT,
    status          TEXT NOT NULL DEFAULT 'requested'
                    CHECK (status IN ('requested', 'verified', 'rejected', 'revoked')),
    rights_id       INTEGER REFERENCES rights_ledger(id),
    granted_by_user INTEGER REFERENCES client_users(id),
    verified_by     TEXT,
    verified_at     TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX client_permissions_client_idx ON client_permissions(client_id, scope);

-- A finished clip delivered to a client (preview + download page).
CREATE TABLE client_deliveries (
    id              SERIAL PRIMARY KEY,
    client_id       INTEGER NOT NULL REFERENCES clients(id),
    clip_id         INTEGER REFERENCES clips(id),
    title           TEXT NOT NULL,
    style           TEXT,
    file_path       TEXT NOT NULL,
    file_sha256     TEXT,
    duration_s      DOUBLE PRECISION,
    status          TEXT NOT NULL DEFAULT 'ready' CHECK (status IN ('processing', 'ready', 'approved',
                        'changes_requested', 'withdrawn')),
    client_note     TEXT,
    views           INTEGER,                         -- latest known views if the client published it
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX client_deliveries_client_idx ON client_deliveries(client_id, created_at DESC);

-- Billing architecture only: no payment provider is connected and nothing here charges anyone.
CREATE TABLE subscriptions (
    id              SERIAL PRIMARY KEY,
    client_id       INTEGER NOT NULL REFERENCES clients(id),
    plan_key        TEXT NOT NULL REFERENCES plans(key),
    status          TEXT NOT NULL DEFAULT 'none' CHECK (status IN ('none', 'trialing', 'active', 'past_due',
                        'cancelled')),
    provider        TEXT NOT NULL DEFAULT 'none' CHECK (provider IN ('none', 'manual', 'stripe', 'mercadopago')),
    provider_ref    TEXT,
    current_period_start DATE,
    current_period_end   DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX subscriptions_client_idx ON subscriptions(client_id, created_at DESC);

INSERT INTO plans (key, name, monthly_price_cents, clips_per_month, live_feeds, styles, features, sort) VALUES
 ('free',    'Trial',   0,     5,   0, ARRAY['clean'],
    '{"watermark": true, "resolution": "720p", "support": "email"}', 0),
 ('starter', 'Starter', 4900,  30,  0, ARRAY['clean', 'cinematic', 'viral'],
    '{"watermark": false, "resolution": "1080p", "captions": true, "support": "email"}', 1),
 ('pro',     'Pro',     14900, 120, 1, ARRAY['clean', 'cinematic', 'viral', 'pro'],
    '{"watermark": false, "resolution": "1080p", "captions": true, "branding": true, "live_clipping": true,
      "support": "priority"}', 2),
 ('studio',  'Studio',  39900, 400, 3, ARRAY['clean', 'cinematic', 'viral', 'pro'],
    '{"watermark": false, "resolution": "1080p", "captions": true, "branding": true, "live_clipping": true,
      "publishing": true, "analytics": true, "support": "dedicated"}', 3);
