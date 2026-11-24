# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Share SPDX declarations, licence classes and upstream file names."""

import re
from pathlib import Path

PERMISSIVE_LICENCES = frozenset({
    "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD",
    "Zlib", "Unlicense", "CC0-1.0", "MIT-0",
})
# IDs and GNU suffixes verified against SPDX license-list-data v3.29.0 (json/licenses.json).
GNU_WEAK_LICENCES = frozenset({"LGPL-2.0", "LGPL-2.1", "LGPL-3.0"})
WEAK_LICENCES = GNU_WEAK_LICENCES | {"MPL-2.0", "EUPL-1.2"}
STRONG_LICENCES = frozenset({"AGPL-1.0", "AGPL-3.0", "GPL-1.0", "GPL-2.0", "GPL-3.0"})
SPDX_TABLE = tuple(sorted(PERMISSIVE_LICENCES | WEAK_LICENCES | STRONG_LICENCES | {"CC-BY-4.0"}))
SPDX_LOOKUP = {spdx.upper(): spdx for spdx in SPDX_TABLE}
_GNU_SPDX_VARIANTS = {
    f"{base}-{suffix}".upper(): (f"{base}-{suffix}", base)
    for base in GNU_WEAK_LICENCES | STRONG_LICENCES for suffix in ("only", "or-later")
}

LICENSE_FILENAMES = (
    "LICENSE", "LICENCE", "COPYING",
    "LICENSE.md", "LICENSE.txt", "LICENCE.md", "LICENCE.txt", "COPYING.txt",
)
_LICENCE_BASENAME = r"(LICEN[SC]E|COPYING)(?:\.[A-Za-z0-9_-]+)?"
LICENSE_FILE_PATTERN = re.compile(rf"\b{_LICENCE_BASENAME}\b", re.IGNORECASE)
LICENSE_BASENAME_PATTERN = re.compile(rf"^{_LICENCE_BASENAME}$", re.IGNORECASE)

CANDIDATE_LICENCE_CLASSES = ("permissive", "weak")
# Candidate discovery also searches free-form prose for filenames. Validation
# permits only complete pointers, using the same filename grammar as discovery.
LICENCE_POINTERS = re.compile(
    rf"(?:see license in |see |complete terms in )?(?P<name>{_LICENCE_BASENAME})", re.IGNORECASE,
)


def licence_pointer_name(raw: str) -> str | None:
    """Return the filename in a complete front matter licence pointer."""
    match = LICENCE_POINTERS.fullmatch(" ".join(raw.split()))
    return match["name"] if match else None


def normalise_licence(raw: str) -> tuple[str, str, bool]:
    """Normalise a raw licence declaration.

    Returns (output_id, base_id, is_known).
    Keep unrecognised text for diagnostics and use "unknown" for empty declarations.
    """
    if not raw:
        return ("unknown", "unknown", False)
    val = raw.strip().strip("'\"").strip()
    val = re.sub(r"\s+licen[sc]e$", "", val, flags=re.IGNORECASE).strip()
    val = val.strip("'\"").strip()
    if not val or val.lower() == "unknown":
        return ("unknown", "unknown", False)

    if variant := _GNU_SPDX_VARIANTS.get(val.upper()):
        return (*variant, True)
    if base := SPDX_LOOKUP.get(val.upper()):
        return base, base, True
    return val, val, False


def licence_class(spdx: str) -> str:
    """Classify an SPDX identifier using the candidate policy."""
    identifier = spdx.strip()
    variant = _GNU_SPDX_VARIANTS.get(identifier.upper())
    base = variant[1] if variant else SPDX_LOOKUP.get(identifier.upper(), identifier)
    if base in PERMISSIVE_LICENCES:
        return "permissive"
    if base in WEAK_LICENCES:
        return "weak"
    if base in STRONG_LICENCES:
        return "strong"
    return "other"


def candidate_licence_id(raw: str) -> str | None:
    """Return a candidate SPDX ID safe to use as a canonical template filename."""
    spdx, _, known = normalise_licence(raw)
    if known and licence_class(spdx) in CANDIDATE_LICENCE_CLASSES:
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
