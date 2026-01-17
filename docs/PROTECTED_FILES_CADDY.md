# Caddy Configuration for Protected Files

This document describes the Caddy configuration required to enable HMAC-signed URL validation for protected file access.

## Overview

All files stored under the `protected/` directory require signature validation before serving. This is a single, simple rule in Caddy - any path starting with `/media/protected/` goes through `forward_auth`.

## Configuration

Add the following to your Caddyfile:

```caddyfile
# Protected media files - require signed URL validation
# Single rule covers ALL protected files (protected/file/*, protected/questionnaire_files/*, etc.)
handle_path /media/protected/* {
    # Validate signature with Django
    forward_auth revel_web:8000 {
        uri /api/media/validate{uri}
    }

    # Serve from media directory
    root * /srv/revel_media
    file_server

    # Private caching (browser only, not CDN)
    header Cache-Control "private, max-age=3600"
}

# Public media files - served directly without validation
# (logos, cover-art, and other non-protected content)
handle_path /media/* {
    root * /srv/revel_media
    file_server
    header Cache-Control "public, max-age=86400"
}
```

## How It Works

1. **Client requests** `/media/protected/file/abc.pdf?exp=1704067200&sig=a1b2c3d4e5f6`
2. **Caddy's forward_auth** calls `http://revel_web:8000/api/media/validate/protected/file/abc.pdf?exp=...&sig=...`
3. **Django validates**:
   - Signature matches the path and expiry
   - URL hasn't expired
4. **Django returns**:
   - `200 OK` → Caddy serves the file
   - `401 Unauthorized` → Caddy returns 401 to client
5. **Caddy serves** the file from `/srv/revel_media/protected/file/abc.pdf`

## Protected Path Convention

Any file stored with a path starting with `protected/` requires signed URL access:

| Django Field | `upload_to` | Stored Path | Caddy Route |
|-------------|-------------|-------------|-------------|
| `ProtectedFileField(upload_to="file")` | `protected/file` | `protected/file/doc.pdf` | `/media/protected/file/*` |
| `ProtectedImageField(upload_to="profile-pics")` | `protected/profile-pics` | `protected/profile-pics/avatar.jpg` | `/media/protected/profile-pics/*` |
| `ImageField(upload_to="logos")` | `logos` | `logos/org.png` | `/media/logos/*` (public) |

Use `ProtectedFileField` or `ProtectedImageField` from `common.fields` to ensure files are stored in the protected directory.

## Important Notes

### Path Order Matters

Caddy processes `handle_path` blocks in order. The `/media/protected/*` rule **must come before** the general `/media/*` catch-all.

### Cache Headers

- **Protected files**: Use `private, max-age=3600` (browser-only caching)
- **Public files**: Use `public, max-age=86400` (CDN cacheable)

### Docker Compose

Ensure your Django container is accessible to Caddy:

```yaml
services:
  caddy:
    # ...
    depends_on:
      - web

  web:
    # Django/Gunicorn
    # Must be accessible as revel_web:8000 from Caddy
```

## Debugging

### Test Signature Validation

```bash
# Generate a signed URL (from Django shell)
from common.signing import generate_signed_url
url = generate_signed_url("protected/file/test.pdf")
print(url)
# Output: /media/protected/file/test.pdf?exp=1704067200&sig=a1b2c3d4e5f6

# Test with curl
curl -I "http://localhost$url"
```

### Check forward_auth

If files aren't being served:

1. Check Caddy logs for forward_auth failures
2. Verify Django is reachable from Caddy container
3. Test validation endpoint directly:
   ```bash
   curl -I "http://localhost:8000/api/media/validate/protected/file/test.pdf?exp=...&sig=..."
   ```

### Common Issues

- **401 on all requests**: Check `SECRET_KEY` is consistent across Django instances
- **404 on forward_auth**: Verify Django URL routing includes `/api/media/validate/`
- **Files not found after validation**: Check `root` path in Caddyfile matches actual media location
