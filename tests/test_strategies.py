"""Tests for strategy providers."""
import pytest
from lab.strategies import (
    SearchSpace, ParamBound, Trial,
    RandomStrategy, PerturbStrategy, BayesianStrategy, TreeStrategy,
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


class TestTreeStrategy:
    def test_tree_ask_returns_valid_params(self, simple_space):
        strategy = TreeStrategy(seed=42)
        params = strategy.ask(simple_space, [])
        # Should contain the space params (plus __tree_meta)
        assert "x" in params
        assert 0.0 <= params["x"] <= 10.0
        assert 0.1 <= params["y"] <= 1.0
        assert isinstance(params["z"], int)
        assert 1 <= params["z"] <= 100

    def test_tree_branches_from_best(self, simple_space):
        # Build a history with metadata so tree can reason about it
        history = [
            Trial(params={"x": 5.0, "y": 0.5, "z": 50}, score=0.9,
                  strategy="tree", metadata={"parent_idx": None, "depth": 0, "action": "branch"}),
            Trial(params={"x": 2.0, "y": 0.2, "z": 20}, score=0.3,
                  strategy="tree", metadata={"parent_idx": None, "depth": 0, "action": "branch"}),
        ]
        # Force no branching so we improve the best node
        strategy = TreeStrategy(branch_prob=0.0, seed=42)
        params = strategy.ask(simple_space, history)
        assert "x" in params
        assert 0.0 <= params["x"] <= 10.0

    def test_tree_metadata_tracks_lineage(self, simple_space):
        strategy = TreeStrategy(seed=42)
        # First call — root node
        params = strategy.ask(simple_space, [])
        meta = params.pop("__tree_meta")
        assert meta["parent_idx"] is None
        assert meta["depth"] == 0
        assert meta["action"] in ("branch", "improve")

        # Second call with history
        history = [
            Trial(params=params, score=0.7, strategy="tree", metadata=meta),
        ]
        params2 = strategy.ask(simple_space, history)
        meta2 = params2.pop("__tree_meta")
        assert "parent_idx" in meta2
        assert "depth" in meta2
        assert meta2["action"] in ("branch", "improve")

    def test_tree_registered(self):
        assert "tree" in STRATEGIES
        assert STRATEGIES["tree"] is TreeStrategy

    def test_tree_respects_bounds(self, simple_space):
        strategy = TreeStrategy(seed=7)
        history: list[Trial] = []
        for i in range(20):
            params = strategy.ask(simple_space, history)
            meta = params.pop("__tree_meta")
            assert 0.0 <= params["x"] <= 10.0
            assert 0.1 <= params["y"] <= 1.0
            assert isinstance(params["z"], int)
            assert 1 <= params["z"] <= 100
            history.append(Trial(
                params=params, score=float(i) / 20,
                strategy="tree", metadata=meta,
            ))


class TestGetStrategy:
    def test_known_strategies(self):
        for name in STRATEGIES:
            strategy = get_strategy(name)
            assert strategy is not None

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown strategy"):
            get_strategy("nonexistent")
