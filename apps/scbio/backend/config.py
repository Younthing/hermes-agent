"""scbio backend config."""

from __future__ import annotations

import os
from pathlib import Path


def get_data_root() -> Path:
    env = os.environ.get("SCBIO_DATA_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    # default under HERMES_HOME or ~/.hermes
    hermes = os.environ.get("HERMES_HOME")
    if hermes:
        return Path(hermes).expanduser().resolve() / "scbio"
    return Path.home() / ".hermes" / "scbio"


def project_dir(project_id: str) -> Path:
    return get_data_root() / "projects" / project_id
