"""Declarative, fixture-bound behavior recipes for catalog plugin probes."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RECIPE_ROOT = ROOT / "recipes"
RECIPE_SCHEMA = ROOT / "contracts/lab/v1/recipe.schema.json"
RECIPE_CATEGORIES = (
    "config",
    "preload",
    "services",
    "loader",
    "command",
    "message",
    "storage",
    "scheduler",
    "adapter",
    "console",
)
ECOSYSTEMS = ("nonebot", "koishi")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SECRET_KEY = re.compile(r"token|secret|password|credential|api[_-]?key", re.IGNORECASE)


class RecipeError(ValueError):
    """A recipe catalog or binding is malformed."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _object_hash(value: Any) -> str:
    return _sha256(_canonical(value).encode("utf-8"))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RecipeError(f"cannot read recipe JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise RecipeError(f"recipe JSON must be an object: {path}")
    return value


def validate_recipe_catalog(root: Path = RECIPE_ROOT) -> dict[str, Any]:
    """Validate both ecosystem catalogs and require one recipe per probe category."""
    from jsonschema import Draft202012Validator

    schema = _read_json(RECIPE_SCHEMA)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    seen: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    for ecosystem in ECOSYSTEMS:
        path = root / ecosystem / "recipes.json"
        if not path.is_file():
            errors.append(f"missing recipe catalog: {path}")
            continue
        catalog = _read_json(path)
        errors.extend(
            f"{path}: {error.message}"
            for error in sorted(validator.iter_errors(catalog), key=lambda item: tuple(str(part) for part in item.path))
        )
        if catalog.get("ecosystem") != ecosystem:
            errors.append(f"{path}: ecosystem does not match directory")
        by_category: dict[str, Any] = {}
        for recipe in catalog.get("recipes", []):
            if not isinstance(recipe, dict):
                continue
            category = recipe.get("category")
            if recipe.get("ecosystem") != ecosystem:
                errors.append(f"{ecosystem}: recipe {recipe.get('recipe_id')} has a mismatched ecosystem")
            if category in by_category:
                errors.append(f"{ecosystem}: duplicate recipe category {category}")
            by_category[category] = recipe
        missing = sorted(set(RECIPE_CATEGORIES) - set(by_category))
        unexpected = sorted(set(by_category) - set(RECIPE_CATEGORIES))
        if missing:
            errors.append(f"{ecosystem}: missing categories {', '.join(missing)}")
        if unexpected:
            errors.append(f"{ecosystem}: unexpected categories {', '.join(unexpected)}")
        seen[ecosystem] = by_category

    if errors:
        raise RecipeError("recipe catalog validation failed: " + "; ".join(errors))
    return {
        "status": "passed",
        "schema_version": 1,
        "ecosystems": {ecosystem: len(seen[ecosystem]) for ecosystem in ECOSYSTEMS},
        "categories": list(RECIPE_CATEGORIES),
    }


def load_recipe(ecosystem: str, category: str, root: Path = RECIPE_ROOT) -> dict[str, Any]:
    if ecosystem not in ECOSYSTEMS or category not in RECIPE_CATEGORIES:
        raise RecipeError(f"unknown recipe selector: {ecosystem}/{category}")
    catalog = _read_json(root / ecosystem / "recipes.json")
    recipe = next((item for item in catalog.get("recipes", []) if item.get("category") == category), None)
    if recipe is None:
        raise RecipeError(f"missing recipe: {ecosystem}/{category}")
    return recipe


def load_host_profile(ecosystem: str, root: Path = ROOT / "samples/profiles") -> dict[str, Any]:
    if ecosystem not in ECOSYSTEMS:
        raise RecipeError(f"unknown ecosystem: {ecosystem}")
    profile = _read_json(root / f"{ecosystem}-lab-v1.json")
    base_name = profile.get("base_profile")
    expected_hash = profile.get("base_profile_sha256")
    if not isinstance(base_name, str) or not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
        raise RecipeError(f"{ecosystem} recipe profile has no valid base profile pin")
    base_path = root / f"{base_name}.json"
    try:
        actual_hash = _sha256(base_path.read_bytes())
    except OSError as error:
        raise RecipeError(f"cannot read base profile {base_path}: {error}") from error
    if actual_hash != expected_hash:
        raise RecipeError(f"base profile hash mismatch for {ecosystem}: {base_name}")
    return profile


def _skip(reason_code: str, reason: str, recipe: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "skipped",
        "reason_code": reason_code,
        "reason": reason,
        "recipe_id": recipe.get("recipe_id"),
        "binding": None,
    }


def _fail(reason_code: str, reason: str, recipe: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "failed",
        "reason_code": reason_code,
        "reason": reason,
        "recipe_id": recipe.get("recipe_id"),
        "binding": None,
    }


def _has_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            (str(key).casefold() != "required_credentials" and _SECRET_KEY.search(str(key)))
            or _has_secret_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_has_secret_key(item) for item in value)
    return False


def _valid_fixture_object(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("fixture_id"), str)
        and bool(value["fixture_id"])
        and isinstance(value.get("values"), dict)
        and not _has_secret_key(value)
    )


def validate_behavior_fixture(recipe: dict[str, Any], fixture: Any) -> list[str]:
    """Return fixture contract errors; an explicit empty expected value is valid."""
    errors: list[str] = []
    if not isinstance(fixture, dict):
        return ["fixture must be an object"]
    if fixture.get("schema_version") != 1:
        errors.append("fixture schema_version must be 1")
    if fixture.get("category") != recipe.get("category"):
        errors.append("fixture category must match recipe")
    if not isinstance(fixture.get("fixture_id"), str) or not fixture.get("fixture_id"):
        errors.append("fixture_id is required")
    if not isinstance(fixture.get("values"), dict):
        errors.append("values must be an object")
    if _has_secret_key(fixture):
        errors.append("fixture must not contain credential or secret fields")
    required_inputs = fixture.get("required_inputs", [])
    if not isinstance(required_inputs, list) or any(value not in {"random_seed", "fixed_time_utc"} for value in required_inputs):
        errors.append("required_inputs may contain only random_seed and fixed_time_utc")
    credentials = fixture.get("required_credentials", [])
    if not isinstance(credentials, list) or any(not isinstance(value, str) or not value for value in credentials):
        errors.append("required_credentials must be an array of non-empty credential names")
    expected = fixture.get("expected")
    if not isinstance(expected, dict):
        errors.append("expected oracle must be an object")
    else:
        missing = sorted(set(recipe.get("observables", [])) - set(expected))
        if missing:
            errors.append("expected oracle is missing: " + ", ".join(missing))
    return errors


def missing_fixture_prerequisite(fixture: dict[str, Any]) -> tuple[str, str] | None:
    """Return a skipped reason for nondeterministic or credential-gated inputs."""
    if fixture.get("required_credentials"):
        return "missing_credentials", "Fixture requires external credentials unavailable to the virtual profile"
    values = fixture.get("values", {})
    for name, code in (("random_seed", "missing_random_fixture"), ("fixed_time_utc", "missing_time_fixture")):
        if name in fixture.get("required_inputs", []) and name not in values:
            return code, f"Fixture declares required input {name} but does not provide it"
    return None


def prepare_recipe(
    recipe: dict[str, Any],
    entry: dict[str, Any],
    host_profile: dict[str, Any],
    *,
    config: dict[str, Any] | None,
    services: dict[str, Any] | None,
    fixture: dict[str, Any] | None,
) -> dict[str, Any]:
    """Bind a recipe to immutable entry/profile/config/service/fixture identities.

    This prepares execution inputs; it does not execute a plugin. Runtime values are
    represented only by fixture IDs and hashes in the returned binding.
    """
    if not isinstance(entry, dict) or entry.get("ecosystem") != recipe.get("ecosystem"):
        return _fail("invalid_entry", "entry ecosystem must match recipe", recipe)
    if not all(isinstance(entry.get(key), str) and entry.get(key) for key in ("registry_entry_id", "package")):
        return _fail("invalid_entry", "registry_entry_id and package are required", recipe)
    artifact_hash = entry.get("artifact_sha256")
    if not isinstance(artifact_hash, str) or not _SHA256.fullmatch(artifact_hash):
        return _skip("artifact_hash_missing", "entry has no verified SHA-256 artifact identity", recipe)
    if not isinstance(host_profile, dict) or host_profile.get("ecosystem") != recipe.get("ecosystem"):
        return _fail("profile_mismatch", "Host profile ecosystem must match recipe", recipe)
    contract = host_profile.get("recipe_contract", {})
    if contract.get("schema_version") != 1 or recipe.get("category") not in contract.get("categories", []):
        return _skip("profile_capability_missing", "Host profile has no declared probe contract for this category", recipe)

    if fixture is None:
        return _skip("missing_fixture", "No frozen behavior fixture is bound to this recipe", recipe)
    fixture_errors = validate_behavior_fixture(recipe, fixture)
    if fixture_errors:
        return _fail("invalid_fixture", "; ".join(fixture_errors), recipe)
    missing_prerequisite = missing_fixture_prerequisite(fixture)
    if missing_prerequisite:
        reason_code, reason = missing_prerequisite
        return _skip(reason_code, reason, recipe)

    if recipe.get("requires_config_fixture"):
        if config is None:
            return _skip("missing_config_fixture", "Recipe requires an explicit configuration fixture", recipe)
        if not _valid_fixture_object(config):
            return _fail("invalid_config_fixture", "Configuration fixture must contain a fixture_id and non-secret values", recipe)
    elif config is not None and not _valid_fixture_object(config):
        return _fail("invalid_config_fixture", "Configuration fixture must contain a fixture_id and non-secret values", recipe)

    if services is None:
        service_inputs: dict[str, Any] = {}
    elif isinstance(services, dict):
        service_inputs = services
    else:
        return _fail("invalid_service_fixtures", "services must be a mapping of service fixture IDs to values", recipe)
    required_services = recipe.get("required_services", [])
    declared_services = contract.get("service_fixtures", [])
    unsupported = sorted(set(required_services) - set(declared_services))
    if unsupported:
        return _skip("profile_service_missing", "Host profile lacks service fixtures: " + ", ".join(unsupported), recipe)
    missing_services = sorted(set(required_services) - set(service_inputs))
    if missing_services:
        return _skip("missing_service_fixture", "Required service fixtures are absent: " + ", ".join(missing_services), recipe)
    invalid_services = sorted(
        name for name in required_services if not _valid_fixture_object(service_inputs.get(name))
    )
    if invalid_services:
        return _fail("invalid_service_fixture", "Invalid service fixtures: " + ", ".join(invalid_services), recipe)
    for name, value in service_inputs.items():
        if not _valid_fixture_object(value):
            return _fail("invalid_service_fixture", f"Invalid service fixture: {name}", recipe)

    profile_bytes = _canonical(host_profile).encode("utf-8")
    binding = {
        "schema_version": 1,
        "recipe_id": recipe["recipe_id"],
        "registry_entry_id": entry["registry_entry_id"],
        "ecosystem": entry["ecosystem"],
        "package": entry["package"],
        "version": entry.get("version"),
        "artifact_sha256": artifact_hash,
        "profile_id": host_profile.get("profile_id"),
        "profile_sha256": _sha256(profile_bytes),
        "config_fixture_id": config.get("fixture_id") if config else None,
        "config_sha256": _object_hash(config.get("values", {})) if config else _object_hash({}),
        "service_fixtures": {
            name: {"fixture_id": value["fixture_id"], "sha256": _object_hash(value)}
            for name, value in sorted(service_inputs.items())
        },
        "behavior_fixture_id": fixture["fixture_id"],
        "behavior_fixture_sha256": _object_hash(fixture),
    }
    binding["binding_sha256"] = _object_hash(binding)
    return {"status": "ready", "recipe_id": recipe["recipe_id"], "reason_code": None, "binding": binding}


_CATEGORY_ALIASES = {
    "command": "command",
    "message": "message",
    "storage": "storage",
    "database": "storage",
    "task": "scheduler",
    "scheduler": "scheduler",
    "cron": "scheduler",
    "service": "services",
    "services": "services",
    "dependency": "services",
    "external-service": "services",
    "adapter": "adapter",
    "console": "console",
    "web-console": "console",
    "config": "config",
    "configuration": "config",
    "preload": "preload",
    "loader": "loader",
}


def suggest_recipe_categories(entry: dict[str, Any]) -> dict[str, Any]:
    """Conservatively map explicit metadata; retain unmapped labels for accounting."""
    raw: list[str] = []
    explicit = entry.get("recipe_categories")
    if isinstance(explicit, list):
        raw.extend(str(value) for value in explicit)
    else:
        categories = entry.get("categories")
        if isinstance(categories, list):
            raw.extend(str(value) for value in categories)
        record = entry.get("source_record", {})
        if isinstance(record, dict):
            tags = record.get("tags", [])
            if isinstance(tags, list):
                raw.extend(
                    str(tag.get("label"))
                    for tag in tags
                    if isinstance(tag, dict) and tag.get("label") is not None
                )
            manifest = entry.get("manifest") or record.get("manifest") or {}
            if isinstance(manifest, dict):
                service = manifest.get("service", {})
                implements = service.get("implements", []) if isinstance(service, dict) else []
                if isinstance(implements, list):
                    raw.extend(str(value) for value in implements)

    categories: list[str] = []
    unmapped: list[str] = []
    for label in raw:
        normalized = label.strip().casefold().replace("_", "-")
        mapped = _CATEGORY_ALIASES.get(normalized)
        if mapped is None:
            unmapped.append(label)
        elif mapped not in categories:
            categories.append(mapped)
    return {
        "status": "mapped" if categories else "skipped",
        "reason_code": None if categories else "unmapped_category",
        "categories": categories,
        "unmapped_labels": sorted(set(unmapped), key=str.casefold),
    }
