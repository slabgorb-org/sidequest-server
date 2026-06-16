"""Semantic validation of projection.yaml rules.

Run at pack load and in CI. Pack fails to load on any error (no silent
fallbacks). Every error names the kind + rule index for debuggability.
"""

from __future__ import annotations

from typing import Any, get_args, get_origin

from pydantic import BaseModel, RootModel

from sidequest.game.projection.predicates import PREDICATES
from sidequest.game.projection.rules import (
    IncludeIfRule,
    PredicateCall,
    ProjectionRules,
    RedactFieldsRule,
    TargetOnlyRule,
    VisibilityTagRule,
)
from sidequest.protocol.enums import MessageType


class ValidationError(Exception):
    """Raised when a projection.yaml rule set is semantically invalid."""


def _filter_reachable_kinds() -> frozenset[str]:
    """Kinds that flow through ``_emit_event`` today.

    Derived from ``session_handler._KIND_TO_MESSAGE_CLS`` at call time so the
    two definitions cannot drift. Deferred import mirrors
    ``_schema_fields_for_kind`` — the server module imports game modules, so
    validator must not import the server module at its own load time.
    """
    from sidequest.server.session_handler import _KIND_TO_MESSAGE_CLS  # noqa: PLC0415

    return frozenset(_KIND_TO_MESSAGE_CLS.keys())


def _unwrap_rootmodel(ann: Any) -> Any:
    """If ann is a RootModel subclass, return the type of its root field.

    RootModel[str] subclasses (e.g. NonBlankString, Stat) represent a single
    wrapped scalar.  They carry model_fields = {'root': FieldInfo(annotation=X)}.
    Unwrap them so that mask-compatibility checks see the underlying primitive.
    """
    if isinstance(ann, type) and issubclass(ann, RootModel) and "root" in ann.model_fields:
        return ann.model_fields["root"].annotation
    return ann


def _unwrap_optional(ann: Any) -> Any:
    """Strip Optional[X] (Union[X, None]) down to X."""
    args = getattr(ann, "__args__", None)
    if args is not None and type(None) in args:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return ann


def _unwrap_type(ann: Any) -> Any:
    """Strip Optional / Union-with-None and RootModel wrappers."""
    ann = _unwrap_optional(ann)
    ann = _unwrap_rootmodel(ann)
    return ann


def _flatten_schema(model: type[BaseModel], *, prefix: str) -> dict[str, Any]:
    """Return a flat field-name → python-type map for a pydantic model.

    Recurses into nested BaseModels and list[BaseModel] to surface dotted
    paths ("state_delta.hp") and wildcarded list paths ("footnotes[*].text").
    RootModel subclasses are treated as their underlying scalar type.
    """
    out: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        key = f"{prefix}{name}"
        ann = _unwrap_type(info.annotation)

        origin = get_origin(ann)
        if origin is list:
            args = get_args(ann)
            item_ann = _unwrap_type(args[0]) if args else Any
            if isinstance(item_ann, type) and issubclass(item_ann, BaseModel):
                out.update(_flatten_schema(item_ann, prefix=f"{key}[*]."))
            else:
                out[f"{key}[*]"] = item_ann
            out[key] = list
        elif isinstance(ann, type) and issubclass(ann, BaseModel):
            # Non-root BaseModel: recurse, also register the key itself.
            out.update(_flatten_schema(ann, prefix=f"{key}."))
            out[key] = ann
        else:
            out[key] = ann
    return out


def _schema_fields_for_kind(kind: str) -> dict[str, Any]:
    """Return the flat field-name → python-type map for a kind's payload.

    The import of session_handler is deferred to function scope to avoid a
    circular import (session_handler imports game modules; game/projection/
    validator must not be in that chain at module load time).
    """
    from sidequest.server.session_handler import _KIND_TO_MESSAGE_CLS  # noqa: PLC0415

    message_cls = _KIND_TO_MESSAGE_CLS.get(kind)
    if message_cls is None:
        return {}

    payload_field = message_cls.model_fields.get("payload")
    if payload_field is None:
        raise ValidationError(f"kind {kind!r} has no payload field on its message class")

    payload_cls = payload_field.annotation
    if not (isinstance(payload_cls, type) and issubclass(payload_cls, BaseModel)):
        raise ValidationError(
            f"kind {kind!r} payload type {payload_cls!r} is not a pydantic BaseModel"
        )

    return _flatten_schema(payload_cls, prefix="")


def _mask_is_compatible(mask: Any, field_type: Any) -> bool:
    """Return True if mask is type-compatible with field_type.

    null (None) mask is always compatible — it means "omit entirely".
    For everything else, the mask value must be the same broad Python type
    as the field (str for str-like, int/float for numeric, list for list, etc.).
    """
    if mask is None:
        return True

    # Unwrap Optional / RootModel wrappers on the declared field type.
    field_type = _unwrap_type(field_type)

    # str-like fields (including str subclasses after RootModel unwrapping)
    if isinstance(field_type, type) and issubclass(field_type, str):
        return isinstance(mask, str)
    if isinstance(field_type, type) and issubclass(field_type, bool):
        return isinstance(mask, bool)
    if isinstance(field_type, type) and issubclass(field_type, (int, float)):
        return isinstance(mask, (int, float))
    if field_type is list or get_origin(field_type) is list:
        return isinstance(mask, list)
    if isinstance(field_type, type) and issubclass(field_type, BaseModel):
        return isinstance(mask, dict) or mask is None
    # Unknown / Any → be permissive
    return True


def _check_predicate(
    call: PredicateCall,
    *,
    kind: str,
    rule_idx: int,
    schema: dict[str, Any],
) -> None:
    """Validate a predicate call: name must exist, arg (if given) must be a field."""
    if call.predicate not in PREDICATES:
        raise ValidationError(
            f"rule[{rule_idx}] kind={kind!r}: unknown predicate {call.predicate!r}"
        )
    if call.arg is not None and call.arg not in schema:
        raise ValidationError(
            f"rule[{rule_idx}] kind={kind!r}: predicate arg {call.arg!r} "
            f"is not a field of {kind!r}'s payload"
        )


def validate_projection_rules(rules: ProjectionRules) -> None:
    """Run all 7 semantic checks against a ProjectionRules set.

    Raises ValidationError on the first violation found. Error messages
    include kind and rule index for debuggability.

    Checks:
      1. Kind exists (member of MessageType).
      2. Kind is filter-reachable (in _filter_reachable_kinds()).
      3. Fields exist on the payload's pydantic schema.
      4. Predicates exist in PREDICATES.
      5. Masks are type-compatible with the field type.
      6. No conflicting redactions (same kind+field, different unless).
      7. Predicate args reference canonical payload fields.
    """
    # Track (kind, field) → PredicateCall for conflict detection (check 6).
    seen_redactions: dict[tuple[str, str], PredicateCall] = {}

    for idx, rule in enumerate(rules.rules):
        # Check 1: Kind must be a valid MessageType value.
        try:
            MessageType(rule.kind)
        except ValueError as exc:
            raise ValidationError(
                f"rule[{idx}]: unknown kind {rule.kind!r} (not in MessageType)"
            ) from exc

        # Check 2: Kind must be filter-reachable.
        if rule.kind not in _filter_reachable_kinds():
            raise ValidationError(
                f"rule[{idx}] kind={rule.kind!r}: not filter-reachable "
                f"(kind does not flow through _emit_event yet)"
            )

        schema = _schema_fields_for_kind(rule.kind)

        if isinstance(rule, TargetOnlyRule):
            # Check 3: field must exist in the payload schema.
            if rule.target_only.field not in schema:
                raise ValidationError(
                    f"rule[{idx}] kind={rule.kind!r}: unknown field "
                    f"{rule.target_only.field!r} in target_only"
                )

        elif isinstance(rule, IncludeIfRule):
            # Checks 4 + 7: predicate name exists; arg is a real field.
            _check_predicate(rule.include_if, kind=rule.kind, rule_idx=idx, schema=schema)

        elif isinstance(rule, VisibilityTagRule):
            # visibility_tag has no payload-schema dependency: it reads the
            # runtime _visibility sidecar attached by the narration decomposer.
            # No further validation needed.
            pass

        elif isinstance(rule, RedactFieldsRule):
            for spec in rule.redact_fields:
                # Check 3: field must exist.
                if spec.field not in schema:
                    raise ValidationError(
                        f"rule[{idx}] kind={rule.kind!r}: unknown field "
                        f"{spec.field!r} in redact_fields"
                    )
                # Checks 4 + 7: predicate name exists; arg is a real field.
                _check_predicate(spec.unless, kind=rule.kind, rule_idx=idx, schema=schema)
                # Check 5: mask type must be compatible with the field type.
                field_type = schema[spec.field]
                if not _mask_is_compatible(spec.mask, field_type):
                    raise ValidationError(
                        f"rule[{idx}] kind={rule.kind!r}: type-incompatible mask "
                        f"{spec.mask!r} for field {spec.field!r} "
                        f"(type {field_type!r})"
                    )
                # Check 6: same (kind, field) must not have a different unless predicate.
                key = (rule.kind, spec.field)
                existing = seen_redactions.get(key)
                if existing is not None and existing != spec.unless:
                    raise ValidationError(
                        f"rule[{idx}] kind={rule.kind!r}: conflicting redactions on "
                        f"field {spec.field!r} — "
                        f"existing unless={existing.model_dump()!r}, "
                        f"new unless={spec.unless.model_dump()!r}"
                    )
                seen_redactions[key] = spec.unless

        else:
            raise ValidationError(f"rule[{idx}]: unrecognized rule type {type(rule).__name__}")
