-- VicePulse initial schema.
-- One database holds all pipeline state; n8n never holds state itself, so any step can be re-run.

CREATE TABLE channels (
    id            SERIAL PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,            -- 'en', 'br'
    name          TEXT NOT NULL,                   -- 'VicePulse EN'
    language      TEXT NOT NULL,                   -- 'en', 'pt-BR'
    clips_per_day INTEGER NOT NULL DEFAULT 5 CHECK (clips_per_day BETWEEN 0 AND 20),
    timezone      TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per (channel, platform) account. Tokens live in environment variables, never here.
CREATE TABLE platform_accounts (
    id            SERIAL PRIMARY KEY,
    channel_id    INTEGER NOT NULL REFERENCES channels(id),
    platform      TEXT NOT NULL CHECK (platform IN ('youtube', 'instagram', 'tiktok')),
    handle        TEXT,
    external_id   TEXT,
    enabled       BOOLEAN NOT NULL DEFAULT FALSE,  -- off until the owner connects the account
    audited       BOOLEAN NOT NULL DEFAULT FALSE,  -- platform audit passed (public posting allowed)
    auto_publish  BOOLEAN NOT NULL DEFAULT FALSE,  -- human approval step removed (go/no-go passed)
    UNIQUE (channel_id, platform)
);

-- Rights ledger: one row per grant of rights. A source cannot be used without a valid, verified row.
CREATE TABLE rights_ledger (
    id                    SERIAL PRIMARY KEY,
    owner_name            TEXT NOT NULL,
    owner_contact         TEXT,
    licence_type          TEXT NOT NULL CHECK (licence_type IN ('own', 'written_licence', 'creative_commons')),
    cc_licence            TEXT,                    -- e.g. 'CC-BY-4.0' when licence_type = creative_commons
    platforms             TEXT[] NOT NULL,         -- platforms the grant covers
    languages             TEXT[] NOT NULL DEFAULT ARRAY['*'],  -- channel languages covered ('*' = all)
    starts_on             DATE NOT NULL,
    expires_on            DATE,                    -- NULL = no expiry (own footage)
    monetisation_allowed  BOOLEAN NOT NULL DEFAULT FALSE,
    derivative_edits_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    credit_text           TEXT,
    document_url          TEXT,                    -- link to the signed document
    document_sha256       TEXT,
    verified_by           TEXT,                    -- the person who checked the signed document
    verified_at           TIMESTAMPTZ,             -- NULL = not verified = not usable
    revoked_at            TIMESTAMPTZ,
    revoked_reason        TEXT,
    notes                 TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (licence_type <> 'creative_commons' OR cc_licence IS NOT NULL),
    CHECK (licence_type = 'own' OR document_url IS NOT NULL),
    CHECK (expires_on IS NULL OR expires_on >= starts_on)
);

CREATE TABLE sources (
    id              SERIAL PRIMARY KEY,
    game            TEXT NOT NULL,                  -- 'gta5', 'gta6'
    origin          TEXT NOT NULL CHECK (origin IN ('own_recording', 'creator_upload')),
    rights_id       INTEGER REFERENCES rights_ledger(id),
    file_path       TEXT NOT NULL,
    file_sha256     TEXT NOT NULL UNIQUE,           -- the same file is never ingested twice
    duration_s      DOUBLE PRECISION,
    width           INTEGER,
    height          INTEGER,
    status          TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new', 'quarantined', 'cleared', 'analysed', 'blocked')),
    status_reason   TEXT,
    signals         JSONB,                          -- scene cuts, loudness peaks, transcript
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX sources_status_idx ON sources(status);

-- Candidate windows produced by signal extraction and scored by triage / selection.
CREATE TABLE candidates (
    id              SERIAL PRIMARY KEY,
    source_id       INTEGER NOT NULL REFERENCES sources(id),
    start_s         DOUBLE PRECISION NOT NULL,
    end_s           DOUBLE PRECISION NOT NULL CHECK (end_s > start_s),
    signal_score    DOUBLE PRECISION NOT NULL DEFAULT 0,
    transcript      TEXT,
    triage_score    DOUBLE PRECISION,
    triage          JSONB,                          -- hook, clarity, policy flags from triage model
    status          TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new', 'triaged', 'selected', 'rejected', 'duplicate', 'blocked')),
    status_reason   TEXT,
    keyframe_hashes TEXT[],
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX candidates_source_idx ON candidates(source_id);

-- A clip is one highlight for one channel (language). The same moment can serve EN and BR,
-- each with its own original script.
CREATE TABLE clips (
    id              SERIAL PRIMARY KEY,
    candidate_id    INTEGER NOT NULL REFERENCES candidates(id),
    channel_id      INTEGER NOT NULL REFERENCES channels(id),
    language        TEXT NOT NULL,
    for_date        DATE NOT NULL,
    in_s            DOUBLE PRECISION NOT NULL,
    out_s           DOUBLE PRECISION NOT NULL CHECK (out_s > in_s),
    selection_reason TEXT,
    policy_tags     TEXT[] NOT NULL DEFAULT '{}',
    script          JSONB,                          -- hook / body / closer lines + caption words
    titles          JSONB,                          -- per-platform title, description, hashtags
    script_text     TEXT,                           -- flattened, used for text-similarity dedupe
    voice_path      TEXT,
    status          TEXT NOT NULL DEFAULT 'selected'
                    CHECK (status IN ('selected', 'duplicate', 'scripted', 'rendered', 'checked',
                                      'failed_check', 'pending_approval', 'approved', 'rejected',
                                      'scheduled', 'published', 'taken_down', 'blocked')),
    status_reason   TEXT,
    check_result    JSONB,
    approved_by     TEXT,
    approved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (candidate_id, channel_id)
);
CREATE INDEX clips_channel_date_idx ON clips(channel_id, for_date);

CREATE TABLE renders (
    id              SERIAL PRIMARY KEY,
    clip_id         INTEGER NOT NULL REFERENCES clips(id),
    platform        TEXT NOT NULL CHECK (platform IN ('youtube', 'instagram', 'tiktok')),
    file_path       TEXT NOT NULL,
    file_sha256     TEXT NOT NULL,
    width           INTEGER NOT NULL,
    height          INTEGER NOT NULL,
    duration_s      DOUBLE PRECISION NOT NULL,
    has_voice       BOOLEAN NOT NULL,
    has_captions    BOOLEAN NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (clip_id, platform)
);

-- One publish job per (clip, platform). The unique key stops a retry from posting twice.
CREATE TABLE publish_jobs (
    id              SERIAL PRIMARY KEY,
    clip_id         INTEGER NOT NULL REFERENCES clips(id),
    platform        TEXT NOT NULL CHECK (platform IN ('youtube', 'instagram', 'tiktok')),
    account_id      INTEGER NOT NULL REFERENCES platform_accounts(id),
    render_id       INTEGER NOT NULL REFERENCES renders(id),
    scheduled_for   TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK (status IN ('scheduled', 'publishing', 'published', 'dry_run', 'failed',
                                      'blocked', 'cancelled', 'taken_down')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    -- Per-post choices the platform requires a person to make (TikTok privacy level,
    -- interaction toggles, commercial disclosure, consent). Recorded at approval time.
    post_options    JSONB NOT NULL DEFAULT '{}',
    external_id     TEXT,                           -- platform post / video id
    external_url    TEXT,
    last_error      TEXT,
    published_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (clip_id, platform)
);
CREATE INDEX publish_jobs_due_idx ON publish_jobs(status, scheduled_for);

CREATE TABLE metrics (
    id               BIGSERIAL PRIMARY KEY,
    publish_job_id   INTEGER NOT NULL REFERENCES publish_jobs(id),
    captured_on      DATE NOT NULL,
    views            BIGINT,
    watch_time_s     DOUBLE PRECISION,
    avg_view_pct     DOUBLE PRECISION,
    likes            BIGINT,
    comments         BIGINT,
    shares           BIGINT,
    followers_delta  BIGINT,
    raw              JSONB,
    UNIQUE (publish_job_id, captured_on)
);

-- Daily topics from the trend scan; they steer selection angles.
CREATE TABLE topics (
    id          SERIAL PRIMARY KEY,
    game        TEXT NOT NULL,
    language    TEXT NOT NULL,
    topic       TEXT NOT NULL,
    source      TEXT NOT NULL,
    score       DOUBLE PRECISION NOT NULL DEFAULT 0,
    seen_on     DATE NOT NULL DEFAULT CURRENT_DATE,
    UNIQUE (game, language, topic, seen_on)
);

-- Creators to contact for a licence (prospecting finds people, never footage to download).
CREATE TABLE creator_prospects (
    id              SERIAL PRIMARY KEY,
    platform        TEXT NOT NULL,
    channel_ref     TEXT NOT NULL,
    display_name    TEXT,
    language        TEXT,
    subscribers     BIGINT,
    fit_score       DOUBLE PRECISION,
    status          TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new', 'contacted', 'negotiating', 'licensed', 'declined')),
    rights_id       INTEGER REFERENCES rights_ledger(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (platform, channel_ref)
);

-- Append-only audit trail of every gate decision and outward action.
CREATE TABLE events (
    id          BIGSERIAL PRIMARY KEY,
    at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    stage       TEXT NOT NULL,
    entity      TEXT NOT NULL,
    entity_id   BIGINT,
    outcome     TEXT NOT NULL,
    detail      JSONB
);
CREATE INDEX events_entity_idx ON events(entity, entity_id);
