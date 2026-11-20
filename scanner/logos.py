"""Pick a logo for a package: its own logo when we have one, else its ecosystem's."""
from django.templatetags.static import static

LOGOS = {
    # PyPI
    "django": "django", "flask": "flask", "numpy": "numpy", "pandas": "pandas",
    "fastapi": "fastapi", "sqlalchemy": "sqlalchemy", "pytest": "pytest",
    "redis": "redis", "pyyaml": "yaml",
    # npm
    "react": "react", "react-dom": "react", "express": "express", "lodash": "lodash",
    "lodash-es": "lodash", "jquery": "jquery", "vue": "vuejs", "axios": "axios",
    "webpack": "webpack", "next": "nextjs", "typescript": "typescript",
    "@angular/core": "angular",
    # Other software
    "postgresql": "postgresql", "postgres": "postgresql", "redis-server": "redis",
    "nodejs": "nodejs", "node": "nodejs", "python": "python",
}
ECOSYSTEM_LOGO = {"PyPI": "python", "npm": "nodejs"}


def logo_for(ecosystem, name):
    """Return (url or None, is_own_logo)."""
    key = LOGOS.get(name.lower())
    if not key and ecosystem == "npm" and name.startswith("@angular/"):
        key = "angular"
    if key:
        return static(f"scanner/logos/{key}.png"), True
    fallback = ECOSYSTEM_LOGO.get(ecosystem)
    return (static(f"scanner/logos/{fallback}.png"), False) if fallback else (None, False)
