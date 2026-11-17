-- Direct live feeds: a creator sends a second copy of their live stream (e.g. an extra OBS output) to
-- VicePulse's own receiver (MediaMTX). Additive only.
--
-- A feed needs a rights record, like a Twitch session. Its stream key is stored only as a SHA-256 hash.
-- The receiver keeps a short rolling buffer (minutes) and nothing else, so only live moments can be cut.

CREATE TABLE live_feeds (
    id              SERIAL PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9_-]{3,40}$'),
    display_name    TEXT NOT NULL,
    channel_key     TEXT NOT NULL,                 -- main VicePulse channel (en / br)
    channels        TEXT[] NOT NULL DEFAULT '{}',  -- every channel that gets a version (e.g. {en,br})
    creator_id      INTEGER REFERENCES creators(id),
    rights_id       INTEGER NOT NULL REFERENCES rights_ledger(id),
    key_hash        TEXT NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT true,
    last_auth_at    TIMESTAMPTZ,
    last_auth_ok    BOOLEAN,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE live_sessions DROP CONSTRAINT live_sessions_platform_check;
ALTER TABLE live_sessions ADD CONSTRAINT live_sessions_platform_check CHECK (platform IN ('twitch', 'feed'));
ALTER TABLE live_sessions ADD COLUMN feed_id INTEGER REFERENCES live_feeds(id);
-- Every VicePulse channel that gets a version of each moment; empty = just channel_key.
ALTER TABLE live_sessions ADD COLUMN channels TEXT[] NOT NULL DEFAULT '{}';
