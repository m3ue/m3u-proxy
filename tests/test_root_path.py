"""
Tests for ROOT_PATH handling behind reverse proxies
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import api
from api import RootPathMiddleware


def build_client(root_path: str) -> TestClient:
    app = FastAPI()
    app.add_middleware(RootPathMiddleware, root_path=root_path)

    @app.get("/ping")
    async def ping(request: Request):
        return {"root_path": request.scope["root_path"]}

    return TestClient(app)


def test_routes_match_with_prefix_stripped_or_forwarded():
    client = build_client("/m3u-proxy")

    for path in ("/ping", "/m3u-proxy/ping"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.json() == {"root_path": "/m3u-proxy"}


def test_openapi_advertises_root_path_as_server():
    client = build_client("/m3u-proxy")

    assert client.get("/openapi.json").json()["servers"] == [{"url": "/m3u-proxy"}]


def test_empty_root_path_does_not_serve_prefixed_routes():
    client = build_client("")

    assert client.get("/ping").json() == {"root_path": ""}
    assert client.get("/m3u-proxy/ping").status_code == 404


def test_app_uses_middleware_instead_of_deprecated_parameter():
    assert api.app.root_path == ""

    middleware = next(m for m in api.app.user_middleware if m.cls is RootPathMiddleware)
    assert middleware.kwargs == {"root_path": api.settings.ROOT_PATH}
