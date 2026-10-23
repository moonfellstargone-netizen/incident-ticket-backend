CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE CHECK(length(username) BETWEEN 1 AND 64),
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('submitter', 'handler'))
);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 100),
    description TEXT NOT NULL CHECK(length(trim(description)) BETWEEN 1 AND 2000),
    creator_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'in_progress', 'resolved')),
    resolution TEXT,
    resolved_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (
        (status = 'resolved' AND resolution IS NOT NULL
            AND length(trim(resolution)) > 0 AND resolved_by IS NOT NULL)
        OR
        (status != 'resolved' AND resolution IS NULL AND resolved_by IS NULL)
    )
);

-- Record only observed status changes; existing tickets are not backfilled.
CREATE TABLE IF NOT EXISTS ticket_history (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    actor_id INTEGER NOT NULL REFERENCES users(id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    request_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (
        (from_status = 'pending' AND to_status = 'in_progress')
        OR (from_status = 'in_progress' AND to_status = 'resolved')
    )
);

CREATE INDEX IF NOT EXISTS ticket_history_ticket_id_id
    ON ticket_history (ticket_id, id);
