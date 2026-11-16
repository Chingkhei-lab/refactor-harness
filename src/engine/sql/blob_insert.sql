-- Store one content-addressed blob. The hash is the key, so a duplicate
-- upload (same bytes, same hash) is a no-op: ON CONFLICT DO NOTHING.
-- Callers verify data integrity on read (patchlog.load_blob re-hashes).
INSERT INTO blobs (hash, data, size)
VALUES (%(hash)s, %(data)s, %(size)s)
ON CONFLICT (hash) DO NOTHING;
