"""
Audit-type specifications.

An audit type (metadata, chain, reflection, ...) is declared in a YAML file
under configs/. This module defines what a valid spec looks like, loads them,
and validates them hard.

The whole point: adding an audit type a legal expert asks for in October is
writing one YAML file. No Python changes, no schema migration, no UI work.
The UI renders from these specs, and the scorer reads its weights from them.

    from src.core.spec import load_specs
    specs = load_specs()
    specs["metadata"].scored_fields()

Validation deliberately collects *every* problem before raising, rather than
failing on the first. Fixing a spec file one error per run is miserable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from src.core.config import get_settings
from src.core.logging_setup import get_logger

log = get_logger(__name__)

# The three answers every scored question offers. "cannot_determine" is not
# optional: without it auditors guess, and guesses become data.
VERDICT_VALUES: tuple[str, ...] = ("correct", "incorrect", "cannot_determine")

# Keys become database columns, JSON keys and Google Sheet headers.
KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

FieldType = Literal["verdict", "select", "multi_select", "text", "boolean"]
AuditUnit = Literal["legislation", "chain", "article"]
LegislationType = Literal["law", "bylaw"]

WEIGHT_TOLERANCE = 1e-6


class SpecError(Exception):
    """Raised when one or more spec files are invalid. Carries every problem."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        detail = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"{len(problems)} spec problem(s):\n{detail}")


class Option(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label_ar: str
    label_en: str | None = None

    @field_validator("key")
    @classmethod
    def _valid_key(cls, v: str) -> str:
        if not KEY_PATTERN.match(v):
            raise ValueError(f"option key {v!r} must match {KEY_PATTERN.pattern}")
        return v


class ShowIf(BaseModel):
    """Render this field only when another field holds one of these values."""

    model_config = ConfigDict(extra="forbid")

    field: str
    equals: list[str]

    @field_validator("equals", mode="before")
    @classmethod
    def _listify(cls, v: Any) -> Any:
        return [v] if isinstance(v, str) else v


class AuditField(BaseModel):
    """One question put to the auditor."""

    model_config = ConfigDict(extra="forbid")

    key: str
    label_ar: str
    label_en: str | None = None
    type: FieldType
    help_ar: str | None = None
    required: bool = True
    weight: float | None = None          # scored fields only; must sum to 1.0
    options: list[Option] | None = None  # select / multi_select only
    show_if: ShowIf | None = None
    max_length: int | None = None        # text only

    @field_validator("key")
    @classmethod
    def _valid_key(cls, v: str) -> str:
        if not KEY_PATTERN.match(v):
            raise ValueError(f"field key {v!r} must match {KEY_PATTERN.pattern}")
        return v

    @model_validator(mode="after")
    def _check_shape(self) -> "AuditField":
        if self.type in ("select", "multi_select"):
            if not self.options or len(self.options) < 2:
                raise ValueError(f"field {self.key!r}: {self.type} needs >= 2 options")
            keys = [o.key for o in self.options]
            if len(keys) != len(set(keys)):
                raise ValueError(f"field {self.key!r}: duplicate option keys")
        elif self.options:
            raise ValueError(f"field {self.key!r}: type {self.type} cannot have options")

        if self.type == "verdict":
            if self.weight is None:
                raise ValueError(f"field {self.key!r}: verdict fields need a weight")
            if not 0 < self.weight <= 1:
                raise ValueError(f"field {self.key!r}: weight must be in (0, 1]")
        elif self.weight is not None:
            raise ValueError(
                f"field {self.key!r}: only verdict fields carry a weight "
                "(a diagnostic field must not affect the score)"
            )

        if self.max_length is not None and self.type != "text":
            raise ValueError(f"field {self.key!r}: max_length applies to text only")
        return self

    def legal_values(self) -> list[str]:
        """Values this field can hold — used to validate show_if conditions."""
        if self.type == "verdict":
            return list(VERDICT_VALUES)
        if self.type == "boolean":
            return ["true", "false"]
        if self.options:
            return [o.key for o in self.options]
        return []


class Sampling(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_size: dict[LegislationType, int]
    # Share of items assigned to two auditors, so agreement can be reported.
    overlap_fraction: float = 0.12
    # Share of items with known answers, to detect an auditor clicking through.
    golden_fraction: float = 0.05

    @model_validator(mode="after")
    def _check_fractions(self) -> "Sampling":
        for name in ("overlap_fraction", "golden_fraction"):
            value = getattr(self, name)
            if not 0 <= value < 1:
                raise ValueError(f"{name} must be in [0, 1), got {value}")
        if self.overlap_fraction + self.golden_fraction >= 1:
            raise ValueError("overlap_fraction + golden_fraction must stay below 1")
        for lt, n in self.sample_size.items():
            if n <= 0:
                raise ValueError(f"sample_size[{lt}] must be positive, got {n}")
        return self


class Scoring(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # How "cannot_determine" is treated. 'exclude' drops it from the
    # denominator — the honest default, since it is missing data rather than a
    # defect. That is only safe while the rate stays low, hence the ceiling
    # below: past it, the audit is reporting on too little evidence to trust.
    cannot_determine_policy: Literal["exclude", "count_as_incorrect"] = "exclude"
    max_cannot_determine_rate: float = 0.20

    # 'mean_per_unit' scores each legislation first, then averages across
    # legislations — so a heavily-amended item cannot dominate the result.
    aggregate: Literal["mean_per_unit", "mean_per_decision"] = "mean_per_unit"

    @field_validator("max_cannot_determine_rate")
    @classmethod
    def _check_rate(cls, v: float) -> float:
        if not 0 < v <= 1:
            raise ValueError(f"max_cannot_determine_rate must be in (0, 1], got {v}")
        return v


class AuditSpec(BaseModel):
    """A complete audit type."""

    model_config = ConfigDict(extra="forbid")

    key: str
    spec_version: int
    title_ar: str
    title_en: str
    description_ar: str | None = None
    unit: AuditUnit
    applies_to: list[LegislationType]
    sheet_env_key: str
    sampling: Sampling
    scoring: Scoring
    fields: list[AuditField]

    @field_validator("key")
    @classmethod
    def _valid_key(cls, v: str) -> str:
        if not KEY_PATTERN.match(v):
            raise ValueError(f"spec key {v!r} must match {KEY_PATTERN.pattern}")
        return v

    @field_validator("spec_version")
    @classmethod
    def _positive_version(cls, v: int) -> int:
        if v < 1:
            raise ValueError("spec_version starts at 1")
        return v

    @model_validator(mode="after")
    def _check_spec(self) -> "AuditSpec":
        problems: list[str] = []

        keys = [f.key for f in self.fields]
        duplicates = {k for k in keys if keys.count(k) > 1}
        if duplicates:
            problems.append(f"duplicate field keys: {sorted(duplicates)}")

        scored = [f for f in self.fields if f.type == "verdict"]
        if not scored:
            problems.append("no verdict fields — nothing would be scored")
        else:
            total = sum(f.weight or 0 for f in scored)
            if abs(total - 1.0) > WEIGHT_TOLERANCE:
                problems.append(
                    f"verdict weights sum to {total:.4f}, must be exactly 1.0 "
                    f"({', '.join(f'{f.key}={f.weight}' for f in scored)})"
                )

        # show_if must point backwards at a real field and a legal value,
        # otherwise the UI cannot render the form in a single pass.
        seen: dict[str, AuditField] = {}
        for field in self.fields:
            if field.show_if:
                target = field.show_if.field
                if target == field.key:
                    problems.append(f"field {field.key!r}: show_if references itself")
                elif target not in seen:
                    if target in keys:
                        problems.append(
                            f"field {field.key!r}: show_if references {target!r}, "
                            "which is declared later — move it earlier"
                        )
                    else:
                        problems.append(
                            f"field {field.key!r}: show_if references unknown field {target!r}"
                        )
                else:
                    legal = seen[target].legal_values()
                    unknown = [v for v in field.show_if.equals if v not in legal]
                    if unknown:
                        problems.append(
                            f"field {field.key!r}: show_if values {unknown} are not "
                            f"valid for {target!r} (allowed: {legal})"
                        )
            seen[field.key] = field

        declared = set(self.applies_to)
        sampled = set(self.sampling.sample_size)
        if not sampled:
            problems.append("sampling.sample_size is empty")
        if sampled - declared:
            problems.append(
                f"sample_size covers {sorted(sampled - declared)} "
                f"but applies_to is {sorted(declared)}"
            )
        if declared - sampled:
            problems.append(
                f"applies_to includes {sorted(declared - sampled)} "
                "with no sample_size entry"
            )

        if problems:
            raise ValueError("; ".join(problems))
        return self

    # ------------------------------------------------------------------
    def scored_fields(self) -> list[AuditField]:
        return [f for f in self.fields if f.type == "verdict"]

    def field_by_key(self, key: str) -> AuditField | None:
        return next((f for f in self.fields if f.key == key), None)

    def total_sample_size(self) -> int:
        return sum(self.sampling.sample_size.values())

    def summary(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "spec_version": self.spec_version,
            "unit": self.unit,
            "applies_to": self.applies_to,
            "fields": len(self.fields),
            "scored_fields": len(self.scored_fields()),
            "sample_size": self.sampling.sample_size,
            "total_items": self.total_sample_size(),
        }


# ----------------------------------------------------------------------
def load_spec(path: Path) -> AuditSpec:
    """Load and validate a single spec file."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name}: expected a YAML mapping at the top level")

    spec = AuditSpec.model_validate(raw)
    if spec.key != path.stem:
        raise ValueError(
            f"{path.name}: key is {spec.key!r} but the filename says {path.stem!r} — "
            "they must match so specs can be found by key"
        )
    return spec


def load_specs(configs_dir: Path | None = None) -> dict[str, AuditSpec]:
    """
    Load every configs/*.yaml. Collects all errors before raising.

    Returns a dict keyed by audit type.
    """
    configs_dir = configs_dir or get_settings().configs_dir
    paths = sorted(configs_dir.glob("*.yaml")) + sorted(configs_dir.glob("*.yml"))

    specs: dict[str, AuditSpec] = {}
    problems: list[str] = []

    for path in paths:
        try:
            spec = load_spec(path)
        except Exception as exc:                       # noqa: BLE001
            problems.append(f"{path.name}: {exc}")
            continue
        if spec.key in specs:
            problems.append(f"{path.name}: duplicate audit key {spec.key!r}")
            continue
        specs[spec.key] = spec

    env_keys: dict[str, str] = {}
    for spec in specs.values():
        if spec.sheet_env_key in env_keys:
            problems.append(
                f"{spec.key!r} and {env_keys[spec.sheet_env_key]!r} both use "
                f"{spec.sheet_env_key} — two audit types would write to one sheet"
            )
        env_keys[spec.sheet_env_key] = spec.key

    if problems:
        raise SpecError(problems)

    if not specs:
        raise SpecError([f"no spec files found in {configs_dir}"])

    log.info(
        "specs.loaded",
        extra={"count": len(specs), "keys": sorted(specs), "dir": str(configs_dir)},
    )
    return specs


def iter_audit_items(specs: Iterable[AuditSpec]) -> int:
    """Total audit items across specs — for planning volunteer effort."""
    return sum(s.total_sample_size() for s in specs)
