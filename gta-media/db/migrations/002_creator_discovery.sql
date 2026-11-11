-- Priority 1: automatic creator discovery, monitoring, ranking and permission tracking.
-- Additive only: new tables, plus one widened CHECK on rights_ledger. Nothing is dropped or rewritten.

-- A creator is a person or brand; they can have channels on several platforms.
CREATE TABLE creators (
    id                  SERIAL PRIMARY KEY,
    display_name        TEXT NOT NULL,
    primary_language    TEXT,
    seed                BOOLEAN NOT NULL DEFAULT FALSE,     -- came from config/discovery.json
    -- Permission status. Discovery can only ever set unknown / needs_review / stated_allow / stated_deny /
    -- program_available. Only a person (creator-review) can set authorized, which also writes a verified
    -- rights_ledger row with the evidence. Nothing is clipped or published without that row.
    permission_status   TEXT NOT NULL DEFAULT 'unknown'
                        CHECK (permission_status IN ('unknown', 'needs_review', 'stated_allow', 'stated_deny',
                                                     'program_available', 'authorized', 'denied')),
    permission_summary  TEXT,
    permission_checked_at TIMESTAMPTZ,
    reviewed_by         TEXT,
    reviewed_at         TIMESTAMPTZ,
    review_notes        TEXT,
    rights_id           INTEGER REFERENCES rights_ledger(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE creator_channels (
    id              SERIAL PRIMARY KEY,
    creator_id      INTEGER NOT NULL REFERENCES creators(id),
    platform        TEXT NOT NULL CHECK (platform IN ('youtube', 'twitch')),
    channel_ref     TEXT NOT NULL,          -- YouTube channel id (UC...) / Twitch user id
    handle          TEXT,                   -- @handle / twitch login
    title           TEXT,
    url             TEXT,
    country         TEXT,
    language        TEXT,
    description     TEXT,
    uploads_playlist TEXT,                  -- YouTube uploads playlist (cheap monitoring)
    followers       BIGINT,                 -- subscribers (YouTube) / followers (Twitch)
    total_views     BIGINT,
    video_count     BIGINT,
    discovered_via  TEXT,                   -- seed / youtube_search / youtube_live / twitch_category / ...
    tracked         BOOLEAN NOT NULL DEFAULT TRUE,
    last_polled_at  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (platform, channel_ref)
);

-- Uploads, scheduled livestreams, live streams and past broadcasts seen on a tracked channel.
CREATE TABLE creator_content (
    id              SERIAL PRIMARY KEY,
    channel_id      INTEGER NOT NULL REFERENCES creator_channels(id),
    platform        TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('upload', 'scheduled_live', 'live', 'past_live')),
    title           TEXT,
    description     TEXT,
    url             TEXT,
    language        TEXT,
    game            TEXT,                   -- gta6 / gta5 / other
    published_at    TIMESTAMPTZ,
    scheduled_start TIMESTAMPTZ,
    actual_start    TIMESTAMPTZ,
    actual_end      TIMESTAMPTZ,
    duration_s      DOUBLE PRECISION,
    views           BIGINT,
    likes           BIGINT,
    comments        BIGINT,
    live_viewers    BIGINT,
    peak_viewers    BIGINT,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (platform, external_id)
);
CREATE INDEX creator_content_live_idx ON creator_content(kind, last_seen_at);

-- Point-in-time numbers, so view velocity and viewer peaks can be measured.
CREATE TABLE creator_content_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    content_id      INTEGER NOT NULL REFERENCES creator_content(id),
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    views           BIGINT,
    likes           BIGINT,
    comments        BIGINT,
    live_viewers    BIGINT
);
CREATE INDEX creator_content_snapshots_idx ON creator_content_snapshots(content_id, captured_at);

CREATE TABLE creator_scores (
    id              BIGSERIAL PRIMARY KEY,
    creator_id      INTEGER NOT NULL REFERENCES creators(id),
    scored_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    audience        DOUBLE PRECISION NOT NULL,
    engagement      DOUBLE PRECISION NOT NULL,
    viral           DOUBLE PRECISION NOT NULL,
    gta_focus       DOUBLE PRECISION NOT NULL,
    live_now        DOUBLE PRECISION NOT NULL,
    total           DOUBLE PRECISION NOT NULL,
    detail          JSONB
);
CREATE INDEX creator_scores_latest_idx ON creator_scores(creator_id, scored_at DESC);

-- Every permission check, with the text it was based on, so a reviewer can see the evidence.
CREATE TABLE creator_permission_checks (
    id              BIGSERIAL PRIMARY KEY,
    creator_id      INTEGER NOT NULL REFERENCES creators(id),
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    verdict         TEXT NOT NULL CHECK (verdict IN ('unknown', 'stated_allow', 'stated_deny', 'program_available',
                                                     'conflicting')),
    conditions      TEXT[] NOT NULL DEFAULT '{}',   -- e.g. credit_required, no_monetisation
    evidence        JSONB NOT NULL                  -- [{source, url, phrase, snippet}]
);

-- Daily API quota use (YouTube Data API units, Twitch requests).
CREATE TABLE api_usage (
    day     DATE NOT NULL DEFAULT CURRENT_DATE,
    api     TEXT NOT NULL,
    units   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, api)
);

-- A creator's documented public clipping permission (or a clipping programme) becomes a rights entry of
-- type 'creator_permission' once a person has checked and saved the evidence.
ALTER TABLE rights_ledger DROP CONSTRAINT IF EXISTS rights_ledger_licence_type_check;
ALTER TABLE rights_ledger ADD CONSTRAINT rights_ledger_licence_type_check
    CHECK (licence_type IN ('own', 'written_licence', 'creative_commons', 'creator_permission'));
