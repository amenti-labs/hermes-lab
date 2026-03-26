"""Tests for the runner module (unit tests, no dispatch needed)."""
import json
import pytest
from lab.runner import _deep_merge_params, RunConfig


class TestDeepMergeParams:
    def test_flat_merge(self):
        config = {"a": 1, "b": 2}
        _deep_merge_params(config, {"a": 10, "b": 20})
        assert config == {"a": 10, "b": 20}

    def test_nested_merge(self):
        config = {
            "model": {"d_model": 128, "n_layers": 2},
            "training": {"lr": 0.001, "weight_decay": 1.0},
        }
        _deep_merge_params(config, {"weight_decay": 3.0, "lr": 0.01})
        assert config["training"]["weight_decay"] == 3.0
        assert config["training"]["lr"] == 0.01

    def test_nested_not_found_goes_top_level(self):
        config = {"model": {"x": 1}}
        _deep_merge_params(config, {"new_param": 42})
        assert config["new_param"] == 42

    def test_top_level_takes_priority(self):
        config = {"x": 1, "nested": {"x": 2}}
        _deep_merge_params(config, {"x": 10})
        assert config["x"] == 10
        assert config["nested"]["x"] == 2  # untouched

    def test_nested_config_merge(self):
        config = {
            "model": {"d_model": 128, "n_heads": 4, "dropout": 0.0},
            "training": {"learning_rate": 1e-3, "weight_decay": 1.0},
            "noise_injection": {"gradient_noise_std": 0.0, "label_smoothing": 0.0},
        }
        flat = {
            "weight_decay": 3.0,
            "learning_rate": 0.005,
            "gradient_noise_std": 0.1,
            "dropout": 0.1,
        }
        _deep_merge_params(config, flat)
        assert config["training"]["weight_decay"] == 3.0
        assert config["training"]["learning_rate"] == 0.005
        assert config["noise_injection"]["gradient_noise_std"] == 0.1
        assert config["model"]["dropout"] == 0.1


class TestRunConfig:
    def test_from_dict(self):
        config = RunConfig.from_dict({
            "experiment": "test",
            "mode": "burst",
            "iterations": 20,
            "strategy": "bayesian",
        })
        assert config.experiment == "test"
        assert config.iterations == 20
        assert config.strategy == "bayesian"

    def test_defaults(self):
        config = RunConfig(experiment="test")
        assert config.mode == "burst"
        assert config.iterations == 10
        assert config.strategy == "llm"
