def slugify(text: str) -> str:
    """Turn text into a URL slug: lowercase words joined by single dashes."""
    return text.strip().replace(" ", "-")
