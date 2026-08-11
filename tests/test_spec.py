"""
Tests for the audit-type spec layer.

These are guard rails against the mistakes that are easy to make in YAML and
expensive to discover after volunteers have started: weights that do not sum
to 1, a show_if pointing at a field that does not exist, two audit types
writing to the same sheet.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from src.core.spec import AuditSpec, SpecError, load_spec, load_specs

VALID = textwrap.dedent(
    """
    key: sample_audit
    spec_version: 1
    title_ar: تدقيق تجريبي
    title_en: Sample audit
    unit: legislation
    applies_to: [law]
    sheet_env_key: SHEET_ID_SAMPLE
    sampling:
      sample_size:
        law: 100
    scoring:
      cannot_determine_policy: exclude
      max_cannot_determine_rate: 0.2
      aggregate: mean_per_unit
    fields:
      - key: status
        label_ar: هل الحالة صحيحة؟
        type: verdict
        weight: 0.7
      - key: name
        label_ar: هل الاسم صحيح؟
        type: verdict
        weight: 0.3
      - key: note
        label_ar: ملاحظات
        type: text
        required: false
    """
).strip()


def _write(tmp_path: Path, content: str, name: str = "sample_audit.yaml") -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def _mutate(content: str, mutator) -> str:
    data = yaml.safe_load(content)
    mutator(data)
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


# --- happy path --------------------------------------------------------
def test_valid_spec_loads(tmp_path: Path) -> None:
    spec = load_spec(_write(tmp_path, VALID))
    assert spec.key == "sample_audit"
    assert len(spec.scored_fields()) == 2
    assert spec.total_sample_size() == 100


def test_arabic_survives_roundtrip(tmp_path: Path) -> None:
    spec = load_spec(_write(tmp_path, VALID))
    assert spec.title_ar == "تدقيق تجريبي"
    assert spec.field_by_key("status").label_ar.startswith("هل")


# --- weights -----------------------------------------------------------
def test_weights_must_sum_to_one(tmp_path: Path) -> None:
    bad = _mutate(VALID, lambda d: d["fields"][0].update(weight=0.5))
    with pytest.raises(ValueError, match="weights sum to"):
        load_spec(_write(tmp_path, bad))


def test_non_verdict_field_cannot_carry_weight(tmp_path: Path) -> None:
    bad = _mutate(VALID, lambda d: d["fields"][2].update(weight=0.1))
    with pytest.raises(ValueError, match="only verdict fields carry a weight"):
        load_spec(_write(tmp_path, bad))


def test_spec_with_no_verdict_field_is_rejected(tmp_path: Path) -> None:
    def drop_verdicts(d):
        d["fields"] = [f for f in d["fields"] if f["type"] != "verdict"]

    bad = _mutate(VALID, drop_verdicts)
    with pytest.raises(ValueError, match="no verdict fields"):
        load_spec(_write(tmp_path, bad))


# --- show_if -----------------------------------------------------------
def test_show_if_unknown_field_is_rejected(tmp_path: Path) -> None:
    def add(d):
        d["fields"].append({
            "key": "why", "label_ar": "لماذا؟", "type": "text", "required": False,
            "show_if": {"field": "does_not_exist", "equals": "incorrect"},
        })

    with pytest.raises(ValueError, match="unknown field"):
        load_spec(_write(tmp_path, _mutate(VALID, add)))


def test_show_if_forward_reference_is_rejected(tmp_path: Path) -> None:
    def reorder(d):
        d["fields"].insert(0, {
            "key": "why", "label_ar": "لماذا؟", "type": "text", "required": False,
            "show_if": {"field": "status", "equals": "incorrect"},
        })

    with pytest.raises(ValueError, match="declared later"):
        load_spec(_write(tmp_path, _mutate(VALID, reorder)))


def test_show_if_illegal_value_is_rejected(tmp_path: Path) -> None:
    def add(d):
        d["fields"].append({
            "key": "why", "label_ar": "لماذا؟", "type": "text", "required": False,
            "show_if": {"field": "status", "equals": "maybe"},
        })

    with pytest.raises(ValueError, match="not .*valid for"):
        load_spec(_write(tmp_path, _mutate(VALID, add)))


# --- field shape -------------------------------------------------------
def test_select_needs_at_least_two_options(tmp_path: Path) -> None:
    def add(d):
        d["fields"].append({
            "key": "which", "label_ar": "أي؟", "type": "select", "required": False,
            "options": [{"key": "a", "label_ar": "أ"}],
        })

    with pytest.raises(ValueError, match="needs >= 2 options"):
        load_spec(_write(tmp_path, _mutate(VALID, add)))


def test_duplicate_field_keys_rejected(tmp_path: Path) -> None:
    def dupe(d):
        d["fields"].append(dict(d["fields"][2]))

    with pytest.raises(ValueError, match="duplicate field keys"):
        load_spec(_write(tmp_path, _mutate(VALID, dupe)))


def test_key_must_be_identifier_safe(tmp_path: Path) -> None:
    bad = _mutate(VALID, lambda d: d["fields"][0].update(key="Status Field"))
    with pytest.raises(ValueError):
        load_spec(_write(tmp_path, bad))


# --- file-level --------------------------------------------------------
def test_key_must_match_filename(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="filename says"):
        load_spec(_write(tmp_path, VALID, name="different_name.yaml"))


def test_sample_size_must_cover_applies_to(tmp_path: Path) -> None:
    bad = _mutate(VALID, lambda d: d.update(applies_to=["law", "bylaw"]))
    with pytest.raises(ValueError, match="no sample_size entry"):
        load_spec(_write(tmp_path, bad))


def test_duplicate_sheet_env_key_rejected(tmp_path: Path) -> None:
    _write(tmp_path, VALID)
    second = _mutate(VALID, lambda d: d.update(key="other_audit"))
    _write(tmp_path, second, name="other_audit.yaml")
    with pytest.raises(SpecError, match="would write to one sheet"):
        load_specs(tmp_path)


def test_all_problems_reported_not_just_first(tmp_path: Path) -> None:
    _write(tmp_path, _mutate(VALID, lambda d: d["fields"][0].update(weight=0.9)))
    _write(
        tmp_path,
        _mutate(VALID, lambda d: (d.update(key="second"), d["fields"][0].update(weight=0.1))[0]),
        name="second.yaml",
    )
    with pytest.raises(SpecError) as exc:
        load_specs(tmp_path)
    assert len(exc.value.problems) == 2


def test_empty_configs_dir_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="no spec files"):
        load_specs(tmp_path)


# --- the real project specs -------------------------------------------
def test_project_specs_are_valid() -> None:
    """The four shipped specs must always validate."""
    configs = Path(__file__).resolve().parents[1] / "configs"
    specs = load_specs(configs)
    assert set(specs) == {"metadata", "chain", "reflection", "article_integrity"}
    for spec in specs.values():
        assert isinstance(spec, AuditSpec)
        total = sum(f.weight for f in spec.scored_fields())
        assert abs(total - 1.0) < 1e-9
