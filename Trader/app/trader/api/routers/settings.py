"""GET /api/settings, PUT /api/settings/{key} (P4-T8; BR-30, BR-53, SPEC §6.2, §11, §13).

Every runtime setting, keyed by its DB key, with its current value, default, group and form descriptor.
A change goes through `SettingsStore.set` (validated, audited with actor `web:<username>`); changing
`approval_mode` is just this route, the store audits it (SPEC §6.2) and pending proposals stay pending.
The router is registered under `/api` by `trader.api.routers.ROUTERS`.
"""

from functools import cache
from typing import Any

from fastapi import APIRouter
from pydantic import ValidationError
from sqlalchemy import select

from trader.api.deps import CsrfUser, CurrentUser, Services, actor
from trader.api.errors import ApiError
from trader.api.forms import GROUP_ORDER, group_of, model_fields_out
from trader.api.schemas import FieldOut, SettingIn, SettingOut, SettingsOut
from trader.db import models as m
from trader.settings_store import RuntimeSettings

router = APIRouter(tags=["settings"])


@cache
def _fields() -> dict[str, FieldOut]:
    """DB key -> form descriptor, in declaration order (static: built once)."""
    return {f.name: f for f in model_fields_out(RuntimeSettings, by_alias=True)}


@cache
def _defaults() -> dict[str, Any]:
    return RuntimeSettings().model_dump(mode="json", by_alias=True)


def _current(key: str, row: m.Setting | None) -> Any:
    """The value in force for `key`: its stored row validated (JSON mode), else the default. A stored row
    that no longer validates is shown as it is stored, so the Settings page can show and repair it."""
    if row is None:
        return _defaults()[key]
    try:
        return RuntimeSettings.model_validate({key: row.value}).model_dump(mode="json", by_alias=True)[key]
    except ValidationError:
        return row.value


def _setting_out(key: str, row: m.Setting | None) -> SettingOut:
    value = _current(key, row)
    default = _defaults()[key]
    return SettingOut(
        key=key,
        value=value,
        default=default,
        is_default=value == default and type(value) is type(default),
        group=group_of(key),
        field=_fields()[key],
        updated_at=row.updated_at if row is not None else None,
        updated_by=row.updated_by if row is not None else None,
    )


def _sort_key(item: SettingOut) -> tuple[int, str]:
    return GROUP_ORDER.index(item.group), item.key


@router.get("/settings")
def get_settings(services: Services, user: CurrentUser) -> SettingsOut:
    with services.core.factory() as s:
        rows = {row.key: row for row in s.execute(select(m.Setting)).scalars()}
    items = [_setting_out(key, rows.get(key)) for key in _fields()]
    return SettingsOut(items=sorted(items, key=_sort_key))


@router.put("/settings/{key}")
def put_setting(key: str, body: SettingIn, services: Services, user: CsrfUser) -> SettingOut:
    if key not in _fields():
        raise ApiError(404, "not_found", "Unknown setting")
    try:
        services.core.settings.set(key, body.value, actor(user))
    except ValidationError as exc:
        fields = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors(include_input=False)]
        raise ApiError(422, "validation", "Invalid value", fields=fields) from None
    with services.core.factory() as s:
        return _setting_out(key, s.get(m.Setting, key))
