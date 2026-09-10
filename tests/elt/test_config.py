"""Tests that the committed config/*.yaml files parse into the expected
shape (catches config regressions without hitting the network)."""

from __future__ import annotations

from src.elt.config import load_harmonisation_config, load_pipeline_config


def test_load_pipeline_config_has_all_nine_domains():
    cfg = load_pipeline_config()
    assert set(cfg.domains) == {"QCL", "TM", "FBS", "FS", "CAHD", "RFM", "CP", "GT", "RL"}


def test_load_pipeline_config_core_vs_supporting_split():
    cfg = load_pipeline_config()
    core = {d for d, spec in cfg.domains.items() if spec.priority == "core"}
    supporting = {d for d, spec in cfg.domains.items() if spec.priority == "supporting"}
    assert core == {"QCL", "TM", "FBS", "FS", "CAHD"}
    assert supporting == {"CP", "GT", "RL", "RFM"}


def test_resolve_item_codes_qcl_crops_only_matches_food_commodities():
    cfg = load_pipeline_config()
    assert cfg.resolve_item_codes("QCL") == cfg.resolve_item_codes("TM")


def test_resolve_item_codes_all_scope_returns_none():
    cfg = load_pipeline_config()
    assert cfg.resolve_item_codes("FBS") is None


def test_load_harmonisation_config_covers_every_domain_duplicate_key():
    harmonisation = load_harmonisation_config()
    cfg = load_pipeline_config()
    assert set(harmonisation.duplicate_natural_keys) == set(cfg.domains)
