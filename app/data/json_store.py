"""Write a complete JSON document before replacing its previous version."""

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def write_json_atomic(path: Path, value: object) -> None:
    # Validate/serialize before touching the destination. A failed serialization
    # or write must not truncate the last usable project/catalogue document.
    serialized = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as file:
            temporary = Path(file.name)
            file.write(serialized)
            file.flush()
            os.fsync(file.fileno())
        # Close the temporary file before replacement (required on Windows).
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
