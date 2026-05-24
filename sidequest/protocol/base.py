"""Base class for sidequest.protocol pydantic models.

Rust uses #[serde(skip_serializing_if = "...")] on many payload fields to omit
empty/None values from the wire. pydantic v2 doesn't do this by default.
This base class flips the default so serialization matches Rust:
  - None fields are omitted (Rust Option::is_none)
  - Empty lists, dicts, and strings are omitted when the field's declared
    default is also empty (Rust Vec::is_empty / String::is_empty)

Implemented via @model_serializer(mode='wrap') so the behavior applies
in both direct serialization AND when models are nested inside a RootModel
(e.g., GameMessage wrapping a payload).

Numeric fields (int, float, bool) with zero/false defaults are NOT dropped —
Rust doesn't use skip_serializing_if on those types.

Alias handling: fields with alias/serialization_alias are always emitted under
their wire name regardless of the call-site by_alias context. This is required
because pydantic's C-level serializer (used for nested RootModel paths) does not
call Python-level model_dump overrides — so we enforce alias mapping inside the
@model_serializer itself.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, model_serializer
from pydantic_core.core_schema import SerializerFunctionWrapHandler


class ProtocolBase(BaseModel):
    """Base class for all sidequest protocol models.

    @model_serializer applies Rust-equivalent skip_serializing_if semantics:
      - None → omitted (Rust Option::is_none), UNLESS the field has None as
        its declared default (e.g., rig_composure_current = None, which means
        "no rig" — a meaningful protocol state that must be included)
      - empty list/dict/str matching its declared default → omitted (Rust is_empty)
      - numeric/bool fields → always present regardless of value

    Works in nested contexts (e.g., inside GameMessage RootModel).

    Fields with alias/serialization_alias are always emitted under their wire
    name — no per-class model_dump overrides needed.

    Fields can opt out of empty-omission by setting a non-empty default value
    (they'll always be included then), or by passing explicit override args to
    model_dump().
    """

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_serializer(mode="wrap")
    def _protocol_serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Serialize omitting None and empty containers that match their defaults.

        Also remaps Python field names to their wire aliases unconditionally.
        The handler may return either Python names or alias names depending on
        the call-site by_alias context; the remap handles both cases correctly
        since alias keys pass through unchanged (they're not in the remap dict
        as python-name keys).
        """
        d: dict[str, Any] = handler(self)
        cls = type(self)

        # Build remap: python_field_name -> wire_alias_name for aliased fields only.
        # When handler returns alias names (by_alias=True context), the python name
        # is absent from d so remap.get(k, k) returns k unchanged — safe either way.
        remap: dict[str, str] = {}
        for fname, finfo in cls.model_fields.items():
            _ser: Any = getattr(finfo, "serialization_alias", None)
            _ali: Any = getattr(finfo, "alias", None)
            _raw: Any = _ser or _ali
            if isinstance(_raw, str) and _raw != fname:
                remap[fname] = _raw

        result: dict[str, Any] = {}
        for k, v in d.items():
            wire_key = remap.get(k, k)
            # Handle None values: skip them UNLESS the field has None as its
            # declared default. Fields like rig_composure_current (nullable rig pool)
            # have None as their default, meaning "no rig", which is a meaningful
            # protocol state that must be included in serialization.
            if v is None:
                field_info = _find_field(cls, k)
                if field_info is not None:
                    default_val = _field_default(field_info)
                    # Only skip None if the field's declared default is NOT None
                    if default_val is not None:
                        continue
                else:
                    # Field not found in model — skip it (shouldn't happen)
                    continue
            # Drop empty list/dict/str ONLY when the field default is also empty.
            # This mirrors Rust's skip_serializing_if = "Vec::is_empty" and
            # skip_serializing_if = "String::is_empty" — numeric/bool fields
            # are never dropped here.
            if isinstance(v, (list, dict, str)) and not v:
                field_info = _find_field(cls, k)
                if field_info is not None:
                    default_val = _field_default(field_info)
                    if default_val == v:
                        continue
            result[wire_key] = v
        return result

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        """model_dump respects the same exclude logic as serialization.

        Note: exclude_none is NOT set to True by default here anymore.
        The @model_serializer handles None-exclusion logic based on field defaults.
        Callers can still pass exclude_none explicitly if needed.
        """
        return super().model_dump(**kwargs)

    def model_dump_json(self, **kwargs: Any) -> str:
        """model_dump_json is handled by @model_serializer — this override
        exists only for call sites that pass explicit kwargs like by_alias.
        The serializer already excludes None/empty; no additional work needed.
        """
        return super().model_dump_json(**kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_field(cls: type, serialized_key: str) -> Any | None:
    """Return the FieldInfo for the field that serializes to `serialized_key`."""
    for fname, finfo in cls.model_fields.items():
        if fname == serialized_key:
            return finfo
        if getattr(finfo, "alias", None) == serialized_key:
            return finfo
        if getattr(finfo, "serialization_alias", None) == serialized_key:
            return finfo
    return None


def _field_default(field_info: Any) -> Any:
    """Return the declared default value for a FieldInfo."""
    default_factory = getattr(field_info, "default_factory", None)
    if default_factory is not None:
        return default_factory()
    default = getattr(field_info, "default", ...)
    if default is ... or default is None:
        return None
    return default
