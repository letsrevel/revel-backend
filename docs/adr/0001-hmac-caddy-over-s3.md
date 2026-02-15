# ADR-0001: HMAC-Signed URLs with Caddy Instead of S3/MinIO

## Status

Accepted

## Context

The platform needs protected file access for user uploads and questionnaire files.
Users should only access files they are authorized to view, and URLs should not be
guessable or permanently valid.

We evaluated **MinIO/S3 pre-signed URLs** as the industry-standard approach. However,
several factors made this less attractive:

- **MinIO licensing**: MinIO moved to AGPL v3 and dropped pre-compiled binaries for
  the community edition, creating licensing concerns for our deployment model.
- **File size**: Our files are small (< 100 MB). We do not need streaming, multipart
  uploads, or CDN distribution -- the typical drivers for object storage.
- **Operational complexity**: Running MinIO adds another stateful service to manage,
  monitor, and back up.

## Decision

Use **HMAC-signed URLs** validated by Django, with files served by **Caddy** via its
`forward_auth` directive.

The flow:

1. Django generates a time-limited HMAC-signed URL for the requested file.
2. The client requests the file from Caddy using this signed URL.
3. Caddy forwards the authorization check to a Django endpoint via `forward_auth`.
4. Django validates the HMAC signature and expiry, returning 200 (allow) or 403 (deny).
5. Caddy serves the file directly from disk if authorized.

This is a **pure Django + Caddy** solution with no external object storage dependency.

## Consequences

**Positive:**

- No AGPL license concerns from MinIO
- No additional service to deploy, monitor, or upgrade
- Simpler infrastructure -- Caddy already serves as the reverse proxy
- No vendor lock-in to any cloud storage provider
- Full control over authorization logic in Django

**Negative:**

- Limited to single-server file storage (files live on disk)
- Not suitable for large file streaming or CDN distribution
- Horizontal scaling requires shared storage (NFS or similar)

**Neutral:**

- Caddy configuration must be maintained alongside Django
- HMAC validation endpoint adds a small amount of latency per file request
