"""python -m scripts.check_case_runtime : read-only DB integration preflight."""
import json
from app.core.config import get_settings
from app.features.system.application.extraction_diagnostics import check_extraction_schema


def main():
    result = check_extraction_schema(get_settings())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'schema_ready' else 1


if __name__ == '__main__':
    raise SystemExit(main())
