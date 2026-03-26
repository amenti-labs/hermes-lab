"""Strategy providers for Hermes Lab experiment search.

Each strategy implements the ask/tell interface:
  - ask(history, config) -> suggested params (dict)
  - tell(params, score) -> update internal state

Strategies:
  - llm: Current behavior — agent reads history and proposes (pass-through)
  - random: Random perturbation within defined bounds
  - bayesian: Optuna TPE-guided suggestions
  - evolution: Nevergrad CMA-ES optimization
"""
from __future__ import annotations

import json
import math
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ParamBound:
    """Single parameter search bound."""
    name: str
    low: float
    high: float
    log_scale: bool = False
    step: float | None = None  # discretize if set
    param_type: str = "float"  # float, int


@dataclass
class SearchSpace:
    """Parameter search space definition."""
    params: list[ParamBound] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SearchSpace:
        """Parse from dict like:
        {
            "weight_decay": {"low": 0.1, "high": 10.0, "log": true},
            "learning_rate": {"low": 1e-5, "high": 1e-2, "log": true},
            "hidden_dim": {"low": 64, "high": 512, "type": "int"}
        }
        """
        params = []
        for name, spec in data.items():
            params.append(ParamBound(
                name=name,
                low=float(spec["low"]),
                high=float(spec["high"]),
                log_scale=bool(spec.get("log", False)),
                step=float(spec["step"]) if "step" in spec else None,
                param_type=str(spec.get("type", "float")),
            ))
        return cls(params=params)

    @classmethod
    def from_json_file(cls, path: Path) -> SearchSpace:
        data = json.loads(path.read_text())
        return cls.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        return {
            p.name: {
                "low": p.low, "high": p.high,
                "log": p.log_scale, "type": p.param_type,
                **({"step": p.step} if p.step else {}),
            }
            for p in self.params
        }


@dataclass
class Trial:
    """One experiment trial."""
    params: dict[str, Any]
    score: float | None = None
    accepted: bool = False
    strategy: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class Strategy(ABC):
    """Base strategy interface."""
    name: str = "base"

    @abstractmethod
    def ask(self, space: SearchSpace, history: list[Trial]) -> dict[str, Any]:
        """Suggest next parameters."""
        ...

    @abstractmethod
    def tell(self, params: dict[str, Any], score: float) -> None:
        """Report result of a trial."""
        ...


class RandomStrategy(Strategy):
    """Random sampling within bounds."""
    name = "random"

    def __init__(self, seed: int | None = None):
        self._rng = random.Random(seed)

    def ask(self, space: SearchSpace, history: list[Trial]) -> dict[str, Any]:
        params = {}
        for p in space.params:
            if p.log_scale:
                log_low = math.log(p.low)
                log_high = math.log(p.high)
                val = math.exp(self._rng.uniform(log_low, log_high))
            else:
                val = self._rng.uniform(p.low, p.high)
            if p.step:
                val = round(val / p.step) * p.step
            if p.param_type == "int":
                val = int(round(val))
            params[p.name] = val
        return params

    def tell(self, params: dict[str, Any], score: float) -> None:
        pass  # random doesn't learn


class PerturbStrategy(Strategy):
    """Perturb the current best by a small amount. PBT-inspired."""
    name = "perturb"

    def __init__(self, perturb_factor: float = 0.2, seed: int | None = None):
        self._factor = perturb_factor
        self._rng = random.Random(seed)

    def ask(self, space: SearchSpace, history: list[Trial]) -> dict[str, Any]:
        # Find best trial
        best = None
        for t in history:
            if t.score is not None:
                if best is None or t.score > best.score:
                    best = t

        if best is None:
            # No history, fall back to random
            return RandomStrategy(seed=self._rng.randint(0, 2**31)).ask(space, history)

        params = dict(best.params)
        for p in space.params:
            if p.name not in params:
                continue
            current = float(params[p.name])
            if p.log_scale:
                log_val = math.log(max(current, 1e-12))
                noise = self._rng.gauss(0, self._factor)
                val = math.exp(log_val + noise)
            else:
                range_size = p.high - p.low
                noise = self._rng.gauss(0, self._factor * range_size)
                val = current + noise
            val = max(p.low, min(p.high, val))
            if p.step:
                val = round(val / p.step) * p.step
            if p.param_type == "int":
                val = int(round(val))
            params[p.name] = val
        return params

    def tell(self, params: dict[str, Any], score: float) -> None:
        pass


class BayesianStrategy(Strategy):
    """Optuna TPE-guided suggestions. Requires optuna."""
    name = "bayesian"

    def __init__(self, seed: int | None = None, direction: str = "maximize"):
        self._seed = seed
        self._direction = direction
        self._study = None

    def _ensure_study(self):
        if self._study is not None:
            return
        try:
            import optuna
            optuna.logging.set_verbosity(optuna.logging.WARNING)
        except ImportError:
            raise RuntimeError("optuna not installed. Run: pip install optuna")
        self._study = optuna.create_study(
            direction=self._direction,
            sampler=optuna.samplers.TPESampler(seed=self._seed),
        )

    def ask(self, space: SearchSpace, history: list[Trial]) -> dict[str, Any]:
        self._ensure_study()
        import optuna

        # Replay history into study if needed
        existing_ids = {t.number for t in self._study.trials}
        for i, trial in enumerate(history):
            if i not in existing_ids and trial.score is not None:
                t = optuna.trial.create_trial(
                    params={p.name: trial.params.get(p.name, 0) for p in space.params},
                    distributions={
                        p.name: (
                            optuna.distributions.FloatDistribution(p.low, p.high, log=p.log_scale, step=p.step)
                            if p.param_type == "float"
                            else optuna.distributions.IntDistribution(int(p.low), int(p.high), log=p.log_scale)
                        )
                        for p in space.params
                    },
                    values=[trial.score],
                )
                self._study.add_trial(t)

        # Ask for next suggestion
        trial = self._study.ask({
            p.name: (
                optuna.distributions.FloatDistribution(p.low, p.high, log=p.log_scale, step=p.step)
                if p.param_type == "float"
                else optuna.distributions.IntDistribution(int(p.low), int(p.high), log=p.log_scale)
            )
            for p in space.params
        })
        self._last_trial = trial
        return trial.params

    def tell(self, params: dict[str, Any], score: float) -> None:
        self._ensure_study()
        if hasattr(self, '_last_trial'):
            self._study.tell(self._last_trial, score)


class EvolutionStrategy(Strategy):
    """Nevergrad CMA-ES optimization. Requires nevergrad."""
    name = "evolution"

    def __init__(self, seed: int | None = None, budget: int = 100):
        self._seed = seed
        self._budget = budget
        self._optimizer = None
        self._pending: dict[str, Any] | None = None

    def _ensure_optimizer(self, space: SearchSpace):
        if self._optimizer is not None:
            return
        try:
            import nevergrad as ng
        except ImportError:
            raise RuntimeError("nevergrad not installed. Run: pip install nevergrad")

        params = {}
        for p in space.params:
            if p.log_scale:
                param = ng.p.Log(lower=p.low, upper=p.high)
            else:
                param = ng.p.Scalar(lower=p.low, upper=p.high)
            if p.param_type == "int":
                param = param.set_integer_casting()
            params[p.name] = param

        instrumentation = ng.p.Instrumentation(**params)
        self._optimizer = ng.optimizers.CMA(
            parametrization=instrumentation,
            budget=self._budget,
        )
        if self._seed is not None:
            self._optimizer.parametrization.random_state.seed(self._seed)

    def ask(self, space: SearchSpace, history: list[Trial]) -> dict[str, Any]:
        self._ensure_optimizer(space)
        candidate = self._optimizer.ask()
        self._pending = candidate
        return dict(candidate.kwargs)

    def tell(self, params: dict[str, Any], score: float) -> None:
        if self._pending is not None:
            # Nevergrad minimizes by default, negate for maximize
            self._optimizer.tell(self._pending, -score)
            self._pending = None


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------

STRATEGIES: dict[str, type[Strategy]] = {
    "random": RandomStrategy,
    "perturb": PerturbStrategy,
    "bayesian": BayesianStrategy,
    "evolution": EvolutionStrategy,
}


def get_strategy(name: str, **kwargs) -> Strategy:
    """Get a strategy by name."""
    cls = STRATEGIES.get(name)
    if cls is None:
        raise ValueError(f"Unknown strategy: {name}. Available: {list(STRATEGIES.keys())}")
    return cls(**kwargs)
