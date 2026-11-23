# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Share SPDX declarations, licence classes and upstream file names."""

import re
from pathlib import Path

SPDX_TABLE = (
    "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD",
    "Zlib", "Unlicense", "CC0-1.0", "CC-BY-4.0", "MPL-2.0", "EUPL-1.2",
    "LGPL-2.1", "LGPL-3.0", "GPL-2.0", "GPL-3.0", "AGPL-3.0", "MIT-0",
)
SPDX_LOOKUP = {s.upper(): s for s in SPDX_TABLE}

PERMISSIVE_LICENCES = frozenset({
    "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD",
    "Zlib", "Unlicense", "CC0-1.0", "MIT-0",
})
WEAK_LICENCES = frozenset({"MPL-2.0", "EUPL-1.2"})

LICENSE_FILENAMES = (
    "LICENSE", "LICENCE", "COPYING",
    "LICENSE.md", "LICENSE.txt", "LICENCE.md", "LICENCE.txt", "COPYING.txt",
)
_LICENCE_BASENAME = r"(LICEN[SC]E|COPYING)(?:\.[A-Za-z0-9_-]+)?"
LICENSE_FILE_PATTERN = re.compile(rf"\b{_LICENCE_BASENAME}\b", re.I)
LICENSE_BASENAME_PATTERN = re.compile(rf"^{_LICENCE_BASENAME}$", re.I)

CANDIDATE_LICENCE_CLASSES = ("permissive", "weak")
# Candidate discovery also searches free-form prose for filenames. Validation
# permits only complete pointers, using the same fixed filename spellings.
LICENCE_POINTERS = frozenset(
    form.format(name=name.casefold())
    for name in LICENSE_FILENAMES
    for form in ("{name}", "see {name}", "see license in {name}", "complete terms in {name}")
)
SAFE_SPDX_ID = re.compile(r"[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*")


def normalise_licence(raw: str) -> tuple[str, str, bool]:
    """Normalise a raw licence declaration.

    Returns (output_id, base_id, is_known).
    If unknown, returns ("unknown", "unknown", False).
    """
    if not raw:
        return ("unknown", "unknown", False)
    val = raw.strip().strip("'\"").strip()
    val = re.sub(r"\s+licen[sc]e$", "", val, flags=re.I).strip()
    val = val.strip("'\"").strip()
    if not val or val.lower() == "unknown":
        return ("unknown", "unknown", False)

    m = re.search(r"^(.*?)-(only|or-later)$", val, flags=re.I)
    if m:
        base_cand = m.group(1).strip()
        suffix = m.group(2).lower()
        if base_cand.upper() in SPDX_LOOKUP:
            canonical_base = SPDX_LOOKUP[base_cand.upper()]
            full_id = f"{canonical_base}-{suffix}"
            return (full_id, canonical_base, True)

    if val.upper() in SPDX_LOOKUP:
        canonical = SPDX_LOOKUP[val.upper()]
        return (canonical, canonical, True)

    return (val, val, False)


def licence_class(spdx: str) -> str:
    """Classify an SPDX identifier using the candidate policy."""
    m = re.search(r"^(.*?)-(?:only|or-later)$", spdx, flags=re.I)
    base = m.group(1).strip() if m else spdx.strip()
    if base in PERMISSIVE_LICENCES:
        return "permissive"
    if base in WEAK_LICENCES or base.startswith("LGPL-"):
        return "weak"
    if base.startswith("GPL-") or base.startswith("AGPL-"):
        return "strong"
    return "other"


def candidate_licence_id(raw: str) -> str | None:
    """Return a candidate SPDX ID safe to use as a canonical template filename."""
    spdx, _, _ = normalise_licence(raw)
    if SAFE_SPDX_ID.fullmatch(spdx) and licence_class(spdx) in CANDIDATE_LICENCE_CLASSES:
        return spdx
    return None


def find_folder_licence_file(folder: Path) -> Path | None:
    """Find the first recognised upstream licence filename in priority order."""
    for name in LICENSE_FILENAMES:
        cand = folder / name
        if cand.is_file():
            return cand
    if folder.is_dir():
        for child in sorted(folder.iterdir()):
            if child.is_file() and LICENSE_BASENAME_PATTERN.match(child.name):
                return child
    return None
