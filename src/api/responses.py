"""统一 HTTP 响应与 SSE 编码，避免路由模块互相依赖。"""
import json
from uuid import uuid4

from fastapi.encoders import jsonable_encoder


def response(request, data):
    return {"request_id": request.state.request_id, "data": data, "error_code": None, "retryable": False}


def sse_event(name, data, trace_id):
    payload = {"event_id": uuid4().hex, "trace_id": trace_id, **data}
    return f"event: {name}\ndata: {json.dumps(jsonable_encoder(payload), ensure_ascii=False)}\n\n"
