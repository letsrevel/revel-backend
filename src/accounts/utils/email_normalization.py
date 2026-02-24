"""Email and username normalization utilities for global ban matching."""

# Providers that ignore dots in the local part of email addresses
DOT_INSENSITIVE_PROVIDERS: frozenset[str] = frozenset({"gmail.com", "googlemail.com"})


def normalize_email_for_matching(email: str) -> str:
    """Normalize an email for ban matching.

    - Lowercases the entire email
    - Strips everything after '+' in the local part (tagged emails)
    - Removes dots from the local part for dot-insensitive providers (Gmail)

    Args:
        email: Raw email address.

    Returns:
        Normalized email string.
    """
    email = email.strip().lower()
    local, _, domain = email.rpartition("@")
    if not local or not domain:
        return email

    # Strip +tag suffix
    local = local.split("+")[0]

    # Remove dots for dot-insensitive providers
    if domain in DOT_INSENSITIVE_PROVIDERS:
        local = local.replace(".", "")

    return f"{local}@{domain}"


def normalize_telegram_for_matching(username: str) -> str:
    """Normalize a Telegram username for ban matching.

    - Strips leading '@'
    - Lowercases
    - Strips whitespace

    Args:
        username: Raw Telegram username.

    Returns:
        Normalized username string.
    """
    return username.strip().lstrip("@").lower()


def normalize_domain_for_matching(domain: str) -> str:
    """Normalize a domain string for ban matching.

    Args:
        domain: Raw domain string.

    Returns:
        Lowercased, stripped domain string.
    """
    return domain.strip().lower()


def extract_domain(email: str) -> str:
    """Extract and lowercase the domain part of an email.

    Args:
        email: Email address.

    Returns:
        Lowercased domain string.
    """
    _, sep, domain = email.strip().lower().rpartition("@")
    if not sep:
        return ""
    return domain
