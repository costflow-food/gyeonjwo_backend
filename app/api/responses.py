import json
from fastapi.responses import JSONResponse
from app.shared.infrastructure.llm import json_default

def api_json(data, status=200, model=None):
    data = json.loads(json.dumps(data, default=json_default))
    if model is not None:
        data = model.model_validate(data).model_dump(mode='json')
    return JSONResponse(data, status_code=status)
