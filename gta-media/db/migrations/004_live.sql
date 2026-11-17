-- Live-only clipping: one row per watched broadcast, one row per detected moment.
-- Additive only. Moments are created only while their session is live (enforced in vicepulse.live and
-- by the trigger below), so archived streams and VODs can never start a capture.

CREATE TABLE live_sessions (
    id              SERIAL PRIMARY KEY,
    platform        TEXT NOT NULL CHECK (platform IN ('twitch')),
    broadcaster_id  TEXT NOT NULL,
    login           TEXT NOT NULL,
    display_name    TEXT,
    channel_key     TEXT NOT NULL,                 -- VicePulse channel the clips go to (en / br)
    creator_id      INTEGER REFERENCES creators(id),
    rights_id       INTEGER NOT NULL REFERENCES rights_ledger(id),
    status          TEXT NOT NULL DEFAULT 'waiting'
                    CHECK (status IN ('waiting', 'live', 'ended', 'stopped')),
    stream_id       TEXT,                          -- Twitch stream id while live
    stream_title    TEXT,
    started_at      TIMESTAMPTZ,                   -- when Twitch says the broadcast started
    ended_at        TIMESTAMPTZ,
    last_seen_at    TIMESTAMPTZ,
    chat_rate       DOUBLE PRECISION,              -- messages per second, last 5 s
    chat_baseline   DOUBLE PRECISION,
    last_score      DOUBLE PRECISION,
    detail          JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE live_moments (
    id              SERIAL PRIMARY KEY,
    session_id      INTEGER NOT NULL REFERENCES live_sessions(id),
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT now(),   -- wall clock of the chat spike
    score           DOUBLE PRECISION NOT NULL,
    signals         JSONB NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'spike'
                    CHECK (status IN ('spike', 'capturing', 'downloading', 'editing', 'ready', 'failed',
                                      'merged', 'skipped')),
    status_reason   TEXT,
    twitch_clip_id  TEXT,
    captured_at     TIMESTAMPTZ,                   -- when Twitch made the clip (stream was live)
    file_path       TEXT,
    source_id       INTEGER REFERENCES sources(id),
    clip_ids        INTEGER[],
    ready_at        TIMESTAMPTZ,
    latency_s       DOUBLE PRECISION,              -- spike -> finished clip in the review queue
    timings         JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX live_moments_session_idx ON live_moments(session_id, detected_at);

-- Backend guard: a new moment can only be recorded for a session that is live right now.
CREATE FUNCTION live_moment_requires_live() RETURNS trigger AS $$
BEGIN
    IF (SELECT status FROM live_sessions WHERE id = NEW.session_id) IS DISTINCT FROM 'live' THEN
        RAISE EXCEPTION 'live-only: session % is not live, no new capture allowed', NEW.session_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER live_moment_requires_live BEFORE INSERT ON live_moments
    FOR EACH ROW EXECUTE FUNCTION live_moment_requires_live();
