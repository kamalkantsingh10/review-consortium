"""Export the config models as versioned JSON Schemas.

Run ``python -m consortium.config.schema docs/schema`` to regenerate the
committed schemas.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import BaseModel

from consortium.config.models import (
    SCHEMA_VERSION,
    InstrumentDef,
    PricesConfig,
    StudyConfig,
    TestConfig,
)

SCHEMAS: dict[str, type[BaseModel]] = {
    "study": StudyConfig,
    "test": TestConfig,
    "instrument": InstrumentDef,
    "prices": PricesConfig,
}


def render_schema(model: type[BaseModel]) -> str:
    """The JSON Schema text for ``model``: sorted keys, 2-space indent, trailing newline."""
    schema = model.model_json_schema()
    schema["x-schema-version"] = SCHEMA_VERSION
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def export_schemas(out_dir: Path | str) -> list[Path]:
    """Write ``{study,test,instrument,prices}.schema.json`` into ``out_dir``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, model in SCHEMAS.items():
        path = out / f"{name}.schema.json"
        path.write_text(render_schema(model), encoding="utf-8")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m consortium.config.schema OUT_DIR", file=sys.stderr)
        return 2
    for path in export_schemas(args[0]):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
