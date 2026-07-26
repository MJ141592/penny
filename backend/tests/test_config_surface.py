"""The mechanism that keeps configuration honest after tonight.

The failure this exists to prevent is not dramatic: someone adds a setting, sets it by hand in
the Railway dashboard "for visibility", and from that moment the running configuration and the
repository disagree. Nothing breaks, no test fails, and a teammate who clones the repo builds a
deployment that behaves differently for reasons no diff can explain. A doc saying "classify your
settings" drifts the first busy week; this does not.

So every field in `Settings` must be declared through one of the four helpers in `config.py`, and
`.env.example` must only mention the ones that are legitimately environment-supplied.
"""

from __future__ import annotations

from pathlib import Path

from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from app.config import (
    ENV_SPECIFIC_FIELDS,
    REPO_ROOT,
    SETTING_CLASSES,
    SettingClass,
    Settings,
    settings_in_class,
)

ENV_EXAMPLE = REPO_ROOT / ".env.example"

_HELPERS = "env_specific() / secret() / infra() / code_default()"


def _env_var_names(field_name: str, field: FieldInfo) -> set[str]:
    """Every environment variable that can populate this field. No env_prefix is configured, so
    it is the field name plus any explicit validation alias.
    """
    names = {field_name.upper()}
    alias = field.validation_alias
    if isinstance(alias, str):
        names.add(alias.upper())
    elif alias is not None:
        for choice in getattr(alias, "choices", ()):
            if isinstance(choice, str):
                names.add(choice.upper())
    return names


def _uncommented_keys(path: Path) -> list[str]:
    keys = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.append(stripped.partition("=")[0].strip().upper())
    return keys


def test_every_setting_declares_what_class_of_thing_it_is() -> None:
    """Adding a field without choosing fails here, which is the whole point.

    There is no plain-default form in `config.py`: a bare `x: int = 5` lands in this list, and the
    fix is to decide whether the value is env-specific, a secret, infra, or a code default.
    """
    unclassified = sorted(name for name, found in SETTING_CLASSES.items() if found is None)
    assert not unclassified, (
        f"These Settings fields skipped the classification: {unclassified}. "
        f"Declare each one with {_HELPERS} from app.config."
    )
    assert set(SETTING_CLASSES) == set(Settings.model_fields), (
        "SETTING_CLASSES must be derived from Settings.model_fields, never hand-maintained."
    )


def test_only_the_openai_key_and_the_public_url_are_environment_specific() -> None:
    """The user's contract: a teammate rebuilding this deployment supplies exactly two values.

    A third entry here means replicating the deploy now requires knowing something that is not in
    the repository, which is the condition this whole exercise removed.
    """
    assert settings_in_class(SettingClass.ENV_SPECIFIC) == ENV_SPECIFIC_FIELDS
    assert set(ENV_SPECIFIC_FIELDS) == {"openai_api_key", "app_public_url"}


def test_code_defaults_actually_have_a_usable_default() -> None:
    """`code_default()` promises the app runs correctly with the variable unset. A required field
    claiming that class would turn "do not set this in Railway" into a boot failure.
    """
    missing = sorted(
        name
        for name in settings_in_class(SettingClass.CODE_DEFAULT)
        if Settings.model_fields[name].default is PydanticUndefined
    )
    assert not missing, f"code_default() fields with no default: {missing}"


def test_env_example_never_lists_a_setting_that_has_a_code_default() -> None:
    """`.env.example` is the instruction sheet, so a code default appearing in it is how the
    dashboard fills up again: someone copies the file, sets all of it, and production is once more
    running on values that are not the ones in `config.py`.
    """
    by_env_var = {
        env_var: name
        for name, field in Settings.model_fields.items()
        for env_var in _env_var_names(name, field)
    }
    code_defaults = settings_in_class(SettingClass.CODE_DEFAULT)

    offenders = []
    unknown = []
    for key in _uncommented_keys(ENV_EXAMPLE):
        field_name = by_env_var.get(key)
        if field_name is None:
            unknown.append(key)
        elif field_name in code_defaults:
            offenders.append(key)

    assert not offenders, (
        f"{offenders} have working defaults in config.py and must not be set. Delete them from "
        ".env.example (and from the Railway service), or reclassify the field if it really is "
        "environment-supplied."
    )
    assert not unknown, (
        f"{unknown} in .env.example match no field in Settings, so nothing reads them."
    )


def test_env_example_still_documents_every_value_a_deployment_must_supply() -> None:
    """The other direction: a secret or an env-specific value that is NOT in the file is one a
    teammate finds out about when production refuses to boot, or worse, when it boots insecure.
    """
    keys = set(_uncommented_keys(ENV_EXAMPLE))
    must_document = settings_in_class(SettingClass.ENV_SPECIFIC) | settings_in_class(
        SettingClass.SECRET
    )

    undocumented = sorted(
        name
        for name in must_document
        if not (_env_var_names(name, Settings.model_fields[name]) & keys)
    )
    assert not undocumented, (
        f"{undocumented} must be supplied per deployment but are not in .env.example. "
        "Add them, with a placeholder that app/startup_checks.py rejects in production."
    )
