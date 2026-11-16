-- Fetch a blob by content hash; the loader re-verifies sha256(data) == hash
-- so a corrupted row (bit rot, a bad migration, tampering) is detected.
SELECT hash, data, size FROM blobs WHERE hash = %(hash)s;
