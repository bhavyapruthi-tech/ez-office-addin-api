import secrets


def generate_ref() -> str:
    """`EZ-####` format, matching the existing frontend mock's convention."""
    return f"EZ-{secrets.randbelow(9000) + 1000}"
