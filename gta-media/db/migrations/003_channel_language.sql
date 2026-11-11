-- Strict EN / PT-BR targeting: detected language per channel, and a reason when a channel stops being
-- monitored. Additive only. Excluded channels keep all their records; they are just no longer tracked.
ALTER TABLE creator_channels
    ADD COLUMN detected_language   TEXT,
    ADD COLUMN language_confidence DOUBLE PRECISION,
    ADD COLUMN language_evidence   JSONB,
    ADD COLUMN language_checked_at TIMESTAMPTZ,
    ADD COLUMN excluded_reason     TEXT,
    ADD COLUMN excluded_at         TIMESTAMPTZ;
