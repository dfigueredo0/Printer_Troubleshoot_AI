"""
tests/test_data.py

Unit tests for the data pipeline:
  - TroubleshootingDataset loading from JSONL
  - Filter operations
  - DataLoader batching and shuffling
  - train/val/test split invariants
  - Transforms (normalise_symptoms, normalise_platform, compose, to_agent_input)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from zt411_agent.data.dataset import SampleCase, TroubleshootingDataset
from zt411_agent.data.loader import DataLoader
from zt411_agent.data.split import split_dataset
from zt411_agent.data.transforms import (
    compose,
    default_transform,
    normalise_platform,
    normalise_symptoms,
    to_agent_input,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_jsonl(cases: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for c in cases:
            fh.write(json.dumps(c) + "\n")


def _minimal_case(**kwargs) -> dict:
    base = {
        "case_id": "test-001",
        "description": "Printer offline",
        "symptoms": ["offline", "cannot print"],
        "os_platform": "windows",
        "device_ip": "192.168.1.1",
        "expected_resolution": "network",
        "expected_steps": 2,
        "expected_actions": ["ping"],
        "resolution_notes": "Port blocked.",
        "risk_class": "safe",
    }
    base.update(kwargs)
    return base


_SAMPLE_PATH = Path("data/sample/sample_cases.jsonl")


# ---------------------------------------------------------------------------
# TroubleshootingDataset
# ---------------------------------------------------------------------------


class TestTroubleshootingDataset:
    def test_load_from_valid_jsonl(self, tmp_path):
        f = tmp_path / "cases.jsonl"
        _write_jsonl([_minimal_case(), _minimal_case(case_id="test-002")], f)
        ds = TroubleshootingDataset.from_jsonl(f)
        assert len(ds) == 2

    def test_case_fields_preserved(self, tmp_path):
        f = tmp_path / "cases.jsonl"
        _write_jsonl([_minimal_case(case_id="abc-123", expected_resolution="cups")], f)
        ds = TroubleshootingDataset.from_jsonl(f)
        assert ds[0].case_id == "abc-123"
        assert ds[0].expected_resolution == "cups"
        assert ds[0].symptoms == ["offline", "cannot print"]

    def test_file_not_found_raises(self):
        with pytest.raises(FileNotFoundError):
            TroubleshootingDataset.from_jsonl("/nonexistent/path/cases.jsonl")

    def test_invalid_json_raises(self, tmp_path):
        f = tmp_path / "bad.jsonl"
        f.write_text("not-valid-json\n")
        with pytest.raises(ValueError, match="Invalid JSONL"):
            TroubleshootingDataset.from_jsonl(f)

    def test_blank_lines_skipped(self, tmp_path):
        f = tmp_path / "cases.jsonl"
        f.write_text(
            json.dumps(_minimal_case()) + "\n\n"
            + json.dumps(_minimal_case(case_id="t2")) + "\n"
        )
        ds = TroubleshootingDataset.from_jsonl(f)
        assert len(ds) == 2

    def test_filter_by_platform(self, tmp_path):
        f = tmp_path / "cases.jsonl"
        _write_jsonl([
            _minimal_case(case_id="w1", os_platform="windows"),
            _minimal_case(case_id="l1", os_platform="linux"),
            _minimal_case(case_id="w2", os_platform="windows"),
        ], f)
        ds = TroubleshootingDataset.from_jsonl(f)
        win = ds.filter_by_platform("windows")
        assert len(win) == 2
        assert all(c.os_platform == "windows" for c in win)

    def test_filter_by_resolution(self, tmp_path):
        f = tmp_path / "cases.jsonl"
        _write_jsonl([
            _minimal_case(case_id="n1", expected_resolution="network"),
            _minimal_case(case_id="d1", expected_resolution="device"),
            _minimal_case(case_id="n2", expected_resolution="network"),
        ], f)
        ds = TroubleshootingDataset.from_jsonl(f)
        net = ds.filter_by_resolution("network")
        assert len(net) == 2

    def test_case_ids_list(self, tmp_path):
        f = tmp_path / "cases.jsonl"
        _write_jsonl([_minimal_case(case_id="a"), _minimal_case(case_id="b")], f)
        ds = TroubleshootingDataset.from_jsonl(f)
        assert ds.case_ids() == ["a", "b"]

    def test_to_list_roundtrip(self, tmp_path):
        f = tmp_path / "cases.jsonl"
        original = _minimal_case()
        _write_jsonl([original], f)
        ds = TroubleshootingDataset.from_jsonl(f)
        result = ds.to_list()[0]
        assert result["case_id"] == original["case_id"]
        assert result["symptoms"] == original["symptoms"]

    def test_iteration(self, tmp_path):
        f = tmp_path / "cases.jsonl"
        _write_jsonl([_minimal_case(case_id=str(i)) for i in range(5)], f)
        ds = TroubleshootingDataset.from_jsonl(f)
        ids = [c.case_id for c in ds]
        assert len(ids) == 5

    @pytest.mark.skipif(not _SAMPLE_PATH.exists(), reason="sample_cases.jsonl not present")
    def test_real_sample_file_loads(self):
        ds = TroubleshootingDataset.from_jsonl(_SAMPLE_PATH)
        assert len(ds) >= 1
        for case in ds:
            assert case.case_id
            assert isinstance(case.symptoms, list)


# ---------------------------------------------------------------------------
# DataLoader
# ---------------------------------------------------------------------------


class TestDataLoader:
    def _ds(self, n: int = 10) -> TroubleshootingDataset:
        cases = [SampleCase(case_id=str(i), description="test", device_ip="x") for i in range(n)]
        return TroubleshootingDataset(cases)

    def test_default_batch_size_1(self):
        loader = DataLoader(self._ds(5))
        batches = list(loader)
        assert len(batches) == 5
        assert all(len(b) == 1 for b in batches)

    def test_batch_size_3(self):
        loader = DataLoader(self._ds(10), batch_size=3)
        batches = list(loader)
        assert len(batches) == 4  # 3+3+3+1

    def test_all_items_yielded(self):
        ds = self._ds(7)
        loader = DataLoader(ds, batch_size=3)
        total = sum(len(b) for b in loader)
        assert total == 7

    def test_shuffle_deterministic_with_seed(self):
        ds = self._ds(20)
        order_a = [c.case_id for b in DataLoader(ds, batch_size=1, shuffle=True, seed=99) for c in b]
        order_b = [c.case_id for b in DataLoader(ds, batch_size=1, shuffle=True, seed=99) for c in b]
        assert order_a == order_b

    def test_len_equals_num_batches(self):
        loader = DataLoader(self._ds(10), batch_size=3)
        assert len(loader) == len(list(loader))


# ---------------------------------------------------------------------------
# split_dataset
# ---------------------------------------------------------------------------


class TestSplitDataset:
    def _ds(self, n: int = 20) -> TroubleshootingDataset:
        cases = [SampleCase(case_id=str(i), description="t", device_ip="x") for i in range(n)]
        return TroubleshootingDataset(cases)

    def test_sizes_sum_to_total(self):
        ds = self._ds(20)
        split = split_dataset(ds, train_ratio=0.7, val_ratio=0.15, seed=1)
        total = len(split.train) + len(split.val) + len(split.test)
        assert total == 20

    def test_train_larger_than_val_and_test(self):
        ds = self._ds(20)
        split = split_dataset(ds, train_ratio=0.7, val_ratio=0.15, seed=1)
        assert len(split.train) > len(split.val)
        assert len(split.train) > len(split.test)

    def test_no_overlap_between_splits(self):
        ds = self._ds(30)
        split = split_dataset(ds, train_ratio=0.7, val_ratio=0.15, seed=42)
        train_ids = {c.case_id for c in split.train}
        val_ids = {c.case_id for c in split.val}
        test_ids = {c.case_id for c in split.test}
        assert train_ids.isdisjoint(val_ids)
        assert train_ids.isdisjoint(test_ids)
        assert val_ids.isdisjoint(test_ids)

    def test_reproducible_with_same_seed(self):
        ds = self._ds(20)
        s1 = split_dataset(ds, seed=5)
        s2 = split_dataset(ds, seed=5)
        assert [c.case_id for c in s1.train] == [c.case_id for c in s2.train]

    def test_invalid_ratios_raise(self):
        ds = self._ds(10)
        with pytest.raises(ValueError):
            split_dataset(ds, train_ratio=0.8, val_ratio=0.3)


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------


class TestTransforms:
    def _case(self, **overrides) -> SampleCase:
        c = SampleCase(
            case_id="t1",
            description="test",
            symptoms=["  Ribbon OUT ", "OFFLINE"],
            os_platform="Windows",
            device_ip="1.2.3.4",
        )
        for k, v in overrides.items():
            object.__setattr__(c, k, v)
        return c

    def test_normalise_symptoms_lowercases(self):
        result = normalise_symptoms(self._case())
        assert all(s == s.lower() for s in result.symptoms)

    def test_normalise_symptoms_strips_whitespace(self):
        result = normalise_symptoms(self._case())
        assert all(s == s.strip() for s in result.symptoms)

    def test_normalise_symptoms_does_not_mutate_original(self):
        case = self._case()
        _ = normalise_symptoms(case)
        assert case.symptoms[0] == "  Ribbon OUT "

    def test_normalise_platform_windows(self):
        assert normalise_platform(self._case(os_platform="Windows")).os_platform == "windows"

    def test_normalise_platform_unknown_for_garbage(self):
        assert normalise_platform(self._case(os_platform="AmigaOS")).os_platform == "unknown"

    def test_compose_applies_both(self):
        fn = compose(normalise_symptoms, normalise_platform)
        result = fn(self._case(os_platform="Linux"))
        assert result.os_platform == "linux"
        assert all(s == s.lower() for s in result.symptoms)

    def test_to_agent_input_maps_platform(self):
        from zt411_agent.state import OSPlatform
        ai = to_agent_input(self._case(os_platform="windows"))
        assert ai["os_platform"] == OSPlatform.WINDOWS

    def test_to_agent_input_passes_symptoms(self):
        ai = to_agent_input(self._case(symptoms=["error", "jam"]))
        assert ai["symptoms"] == ["error", "jam"]

    def test_default_transform_produces_lowercase_symptoms(self):
        case = self._case()
        result = default_transform(case)
        assert all(s == s.lower() for s in result.symptoms)
