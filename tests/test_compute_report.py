from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from src.core.spec import load_specs
from compute_report import defect_label_map, translate_defects


def _specs():
    configs = Path(__file__).resolve().parents[1] / "configs"
    return load_specs(configs)


def test_defect_label_map_covers_all_options() -> None:
    spec = _specs()["chain"]
    labels = defect_label_map(spec)
    assert labels["taadil_mafqud"] == "تعديل مفقود"
    assert labels["ukhra"] == "أخرى"


def test_translate_defects_uses_arabic_labels_not_keys() -> None:
    spec = _specs()["chain"]
    counts = Counter({"taadil_mafqud": 3, "ukhra": 1})
    translated = translate_defects(counts, spec)
    labels = [label for label, _ in translated]
    assert "تعديل مفقود" in labels
    assert "taadil_mafqud" not in labels


def test_translate_defects_preserves_most_common_order() -> None:
    spec = _specs()["chain"]
    counts = Counter({"ukhra": 1, "taadil_mafqud": 5, "tartib_khati": 3})
    translated = translate_defects(counts, spec)
    assert translated[0][1] == 5
    assert translated[-1][1] == 1


def test_translate_defects_falls_back_to_raw_key_for_unknown_code() -> None:
    spec = _specs()["chain"]
    counts = Counter({"some_future_defect_code": 2})
    translated = translate_defects(counts, spec)
    assert translated == [("some_future_defect_code", 2)]


def test_defect_label_map_empty_for_spec_with_no_multi_select() -> None:
    from src.core.spec import AuditField, AuditSpec, Sampling, Scoring
    spec = AuditSpec(
        key="t", spec_version=1, title_ar="ت", title_en="t", unit="legislation",
        applies_to=["law"], sheet_env_key="SHEET_T3",
        sampling=Sampling(sample_size={"law": 10}), scoring=Scoring(),
        fields=[AuditField(key="x", label_ar="x", type="verdict", weight=1.0)],
    )
    assert defect_label_map(spec) == {}
