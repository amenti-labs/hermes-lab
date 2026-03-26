"""Tests for strategy providers."""
import pytest
from lab.strategies import (
    SearchSpace, ParamBound, Trial,
    RandomStrategy, PerturbStrategy, BayesianStrategy,
    get_strategy, STRATEGIES,
)


@pytest.fixture
def simple_space():
    return SearchSpace(params=[
        ParamBound("x", 0.0, 10.0),
        ParamBound("y", 0.1, 1.0, log_scale=True),
        ParamBound("z", 1, 100, param_type="int"),
    ])


@pytest.fixture
def history():
    return [
        Trial(params={"x": 5.0, "y": 0.5, "z": 50}, score=0.8, strategy="test"),
        Trial(params={"x": 3.0, "y": 0.3, "z": 30}, score=0.6, strategy="test"),
    ]


class TestSearchSpace:
    def test_from_dict(self):
        data = {
            "lr": {"low": 1e-5, "high": 1e-2, "log": True},
            "layers": {"low": 1, "high": 10, "type": "int"},
        }
        space = SearchSpace.from_dict(data)
        assert len(space.params) == 2
        assert space.params[0].name == "lr"
        assert space.params[0].log_scale is True
        assert space.params[1].param_type == "int"

    def test_to_dict(self):
        space = SearchSpace(params=[
            ParamBound("x", 0.0, 1.0, log_scale=True),
        ])
        d = space.to_dict()
        assert "x" in d
        assert d["x"]["log"] is True


class TestRandomStrategy:
    def test_ask_returns_valid_params(self, simple_space):
        strategy = RandomStrategy(seed=42)
        params = strategy.ask(simple_space, [])
        assert "x" in params
        assert 0.0 <= params["x"] <= 10.0
        assert 0.1 <= params["y"] <= 1.0
        assert isinstance(params["z"], int)
        assert 1 <= params["z"] <= 100

    def test_deterministic_with_seed(self, simple_space):
        s1 = RandomStrategy(seed=42)
        s2 = RandomStrategy(seed=42)
        assert s1.ask(simple_space, []) == s2.ask(simple_space, [])

    def test_different_seeds_differ(self, simple_space):
        s1 = RandomStrategy(seed=1)
        s2 = RandomStrategy(seed=2)
        # Very unlikely to be equal
        p1 = s1.ask(simple_space, [])
        p2 = s2.ask(simple_space, [])
        assert p1 != p2


class TestPerturbStrategy:
    def test_perturbs_best(self, simple_space, history):
        strategy = PerturbStrategy(seed=42)
        params = strategy.ask(simple_space, history)
        # Should be near the best (x=5.0, y=0.5, z=50)
        assert "x" in params
        assert 0.0 <= params["x"] <= 10.0

    def test_falls_back_to_random_with_no_history(self, simple_space):
        strategy = PerturbStrategy(seed=42)
        params = strategy.ask(simple_space, [])
        assert "x" in params

    def test_respects_bounds(self, simple_space, history):
        strategy = PerturbStrategy(perturb_factor=10.0, seed=42)
        for _ in range(20):
            params = strategy.ask(simple_space, history)
            assert 0.0 <= params["x"] <= 10.0
            assert 0.1 <= params["y"] <= 1.0


class TestBayesianStrategy:
    def test_ask_returns_valid_params(self, simple_space):
        try:
            strategy = BayesianStrategy(seed=42)
            params = strategy.ask(simple_space, [])
            assert "x" in params
            assert 0.0 <= params["x"] <= 10.0
        except RuntimeError as e:
            if "optuna not installed" in str(e):
                pytest.skip("optuna not installed")
            raise

    def test_tell_updates_study(self, simple_space):
        try:
            strategy = BayesianStrategy(seed=42)
            params = strategy.ask(simple_space, [])
            strategy.tell(params, 0.5)
            # Should not raise
        except RuntimeError as e:
            if "optuna not installed" in str(e):
                pytest.skip("optuna not installed")
            raise


class TestGetStrategy:
    def test_known_strategies(self):
        for name in STRATEGIES:
            strategy = get_strategy(name)
            assert strategy is not None

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown strategy"):
            get_strategy("nonexistent")
