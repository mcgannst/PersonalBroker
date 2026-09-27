"""P4-REVIEW: the web client (`web/src/api/http.ts`) calls only real routes, with real query names and bodies.

`test_ts_contract.py` checks the response types; this checks the other half of the seam. For every method of
the HTTP client it finds the HTTP method and path it requests (`get`/`post`/`put`/`del`, `request({...})` or a
same-origin `url(...)` link), and checks against the real route table of `create_app`:

- the route exists with that method and path (path parameters matched by position);
- every query field the client can send (the `...Query` interface of the method in `client.ts`, or an inline
  `queryString({ date })`) is a query parameter of the route, sub-dependencies (`run`) included;
- a passed-through body has the TypeScript type named like the route's pydantic body model, and an inline
  body (`{ value }`) has exactly the model's field names;
- every `/api` route is used by some client method (no dead route, no missing method).

Phase 5 builders: a new client method whose route is not registered yet goes in `PENDING_ROUTES`.
"""

import inspect
import re
import types
import typing
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel

from trader.api.main import create_app

WEB_API = Path(__file__).resolve().parents[3] / "web" / "src" / "api"
HTTP_TS = WEB_API / "http.ts"
CLIENT_TS = WEB_API / "client.ts"
NOT_ROUTES = {"csrfToken", "setCsrfToken"}  # accessors of the client itself
# Client methods declared by the P5-T1 contracts whose routes a later Phase 5 task registers: skipped until
# the route exists, checked like every other method from then on. Remove a name once its route is wired.
PENDING_ROUTES = frozenset(
    {"replayOptions", "replays", "replay", "startReplay", "cancelReplay", "weeklyReport"}
)

_CALL = re.compile(r"\b(get|post|put|del)<[^(]*?>\(\s*[`\"]([^`\"]+)[`\"]")
_REQUEST = re.compile(r'request<[^(]*?>\(\{\s*method:\s*"(\w+)",\s*path:\s*[`"]([^`"]+)[`"]')
_URL = re.compile(r"\burl\(\s*[`\"]([^`\"]+)[`\"]")
_QUERY_ARG = re.compile(r"\$\{queryString\((\w+|\{[^}]*\})\)\}")
_INLINE_BODY = re.compile(r",\s*\{\s*([^}]*)\}\s*\)")
_VERB = {"get": "GET", "post": "POST", "put": "PUT", "del": "DELETE"}


class ClientCall(typing.NamedTuple):
    method: str
    path: str  # `/api/...` with every path parameter as `{}` and no query string
    query_fields: frozenset[str]
    inline_body: frozenset[str] | None  # the keys of an inline object body, else None


def _client_methods_block() -> dict[str, str]:
    """`name -> source` for every property of the object `createHttpClient` returns."""
    text = HTTP_TS.read_text(encoding="utf-8")
    start = text.index("  return {\n", text.index("export function createHttpClient"))
    body = text[start:]
    chunks = re.split(r"^ {4}(\w+):", body, flags=re.MULTILINE)
    return {chunks[i]: chunks[i + 1] for i in range(1, len(chunks) - 1, 2)}


def _interfaces() -> dict[str, set[str]]:
    """Field names of every `export interface X [extends Y] {...}` and `export type X = Y;` in client.ts."""
    text = CLIENT_TS.read_text(encoding="utf-8")
    out: dict[str, set[str]] = {}
    for name, parent, body in re.findall(
        r"^export interface (\w+)(?: extends (\w+))? \{\n(.*?)^\}", text, re.MULTILINE | re.DOTALL
    ):
        fields = set(re.findall(r"^\s+(\w+)\??:", body, re.MULTILINE))
        out[name] = fields | out.get(parent, set())
    for name, alias in re.findall(r"^export type (\w+) = (\w+);", text, re.MULTILINE):
        out[name] = set(out[alias])
    return out


def _query_types() -> dict[str, str]:
    """`method -> query interface` from the `ApiClient` signatures (`proposals(q: ProposalsQuery)`)."""
    text = CLIENT_TS.read_text(encoding="utf-8")
    iface = text[text.index("export interface ApiClient") :]
    return dict(re.findall(r"^\s+(\w+)\(q: (\w+)\)", iface, re.MULTILINE))


def _body_types() -> dict[str, str]:
    """`method -> TS body type` for methods taking a `body: X` parameter."""
    text = CLIENT_TS.read_text(encoding="utf-8")
    iface = text[text.index("export interface ApiClient") :]
    return {name: ty for name, ty in re.findall(r"^\s+(\w+)\([^)]*\bbody: (\w+)\)", iface, re.MULTILINE)}


def _normalise(path: str) -> str:
    path = _QUERY_ARG.sub("", path)
    return "/api" + re.sub(r"\$\{seg\([^)]*\)\}", "{}", path)


def client_calls() -> dict[str, ClientCall]:
    interfaces = _interfaces()
    query_types = _query_types()
    out: dict[str, ClientCall] = {}
    for name, src in _client_methods_block().items():
        if name in NOT_ROUTES:
            continue
        match = _REQUEST.search(src)
        if match:
            method, raw = match.group(1), match.group(2)
        elif match := _CALL.search(src):
            method, raw = _VERB[match.group(1)], match.group(2)
        elif match := _URL.search(src):
            method, raw = "GET", match.group(1)
        else:
            raise AssertionError(f"http.ts {name}: no request found")
        fields: set[str] = set()
        q = _QUERY_ARG.search(raw)
        if q:
            arg = q.group(1)
            if arg.startswith("{"):
                fields = {f.strip() for f in arg.strip("{} ").split(",") if f.strip()}
            else:
                fields = interfaces[query_types[name]]
        inline = None
        after = src[src.index(raw) + len(raw) :]
        body = _INLINE_BODY.match(after.lstrip('`"'))
        if body:
            inline = frozenset(k.split(":")[0].strip() for k in body.group(1).split(",") if k.strip())
        out[name] = ClientCall(method, _normalise(raw), frozenset(fields), inline)
    return out


def _query_params(dependant: Any) -> set[str]:
    names = {p.alias for p in dependant.query_params}
    for sub in dependant.dependencies:
        names |= _query_params(sub)
    return names


def _body_model(route: APIRoute) -> type[BaseModel] | None:
    param = inspect.signature(route.endpoint).parameters.get("body")
    if param is None:
        return None
    annotation = param.annotation
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        annotation = next(a for a in typing.get_args(annotation) if a is not type(None))
    assert isinstance(annotation, type) and issubclass(annotation, BaseModel), route.path
    return annotation


def api_routes() -> dict[tuple[str, str], APIRoute]:
    async def never(stack: Any) -> Any:
        raise AssertionError("the app is not started")

    app = create_app(services_factory=never, web_dist=Path("/nonexistent"))
    out: dict[tuple[str, str], APIRoute] = {}

    def walk(routes: Any, prefix: str) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                path = prefix + route.path
                if path.startswith("/api"):
                    for method in route.methods or ():
                        out[(method, re.sub(r"\{[^}]+\}", "{}", path))] = route
            elif hasattr(route, "original_router"):
                walk(route.original_router.routes, prefix + route.include_context.prefix)

    walk(app.routes, "")
    return out


def test_the_client_parser_sees_every_api_method() -> None:
    methods = set(re.findall(r'^\s+"(\w+)",$', CLIENT_TS.read_text(encoding="utf-8"), re.MULTILINE))
    assert "approve" in methods and "streamUrl" in methods
    assert set(client_calls()) == methods


def _route_of(name: str) -> tuple[ClientCall, APIRoute]:
    call = client_calls()[name]
    route = api_routes().get((call.method, call.path))
    if route is None and name in PENDING_ROUTES:
        pytest.skip(f"{name}: its route is registered by a later Phase 5 task")
    assert route is not None, f"{name}: no route {call.method} {call.path}"
    return call, route


def test_the_pending_methods_are_client_methods() -> None:
    assert PENDING_ROUTES <= set(client_calls())


@pytest.mark.parametrize("name", sorted(client_calls()))
def test_each_client_method_requests_a_real_route_with_real_query_names(name: str) -> None:
    call, route = _route_of(name)
    unknown = call.query_fields - _query_params(route.dependant)
    assert not unknown, f"{name}: the route has no query parameter {sorted(unknown)}"


@pytest.mark.parametrize("name", sorted(client_calls()))
def test_each_client_body_matches_the_route_body_model(name: str) -> None:
    call, route = _route_of(name)
    model = _body_model(route)
    ts_type = _body_types().get(name)
    if call.inline_body is not None:
        assert model is not None, f"{name}: sends a body the route does not read"
        assert set(call.inline_body) == set(model.model_fields), name
    elif ts_type is not None:
        assert model is not None and model.__name__ == ts_type, f"{name}: body {ts_type} vs {model}"
    elif model is not None:
        assert not any(f.is_required() for f in model.model_fields.values()), f"{name}: sends no body"


def test_every_api_route_is_used_by_the_client() -> None:
    used = {(c.method, c.path) for c in client_calls().values()}
    unused = set(api_routes()) - used - {("HEAD", p) for _, p in used}
    assert not unused, sorted(unused)


def test_the_watchlist_upload_form_fields_are_the_ones_the_route_reads() -> None:
    src = _client_methods_block()["uploadWatchlist"]
    sent = set(re.findall(r'form\.append\("(\w+)"', src))
    router = Path(__file__).resolve().parents[2] / "trader" / "api" / "routers" / "watchlist.py"
    read = set(re.findall(r'form\.get\("(\w+)"\)', router.read_text(encoding="utf-8")))
    assert sent == read == {"file", "date", "run_nightly"}
