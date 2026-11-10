"""Build a source offer from an explicit list, excluding all runtime data."""
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def source_archive(root):
    root = Path(root)
    names = ["app.py", "usage_stats.py", "manual_quota.py", "codex_stats.py", "codex_accounts.py", "codex-current", "account_attribution.py", "source_archive.py",
             "install_terminal.py", "claude-current", "claude-account-web.service",
             "README.md", "NOTICE.md", "LICENSE", "LICENSE-ADDITIONAL-TERMS.md",
             ".impeccable.md"]
    paths = [root / name for name in names]
    paths.extend(root / "static" / name for name in ("index.html", "app.js", "style.css", "favicon.svg", "login.html", "login.js"))
    paths.extend(p for p in (root / "tests").glob("*.py") if p.is_file())
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in paths:
            if path.is_file() and not path.is_symlink():
                archive.write(path, "claude-accounts-web/" + str(path.relative_to(root)))
    return output.getvalue()
