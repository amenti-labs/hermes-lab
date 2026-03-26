"""Tests for the SQLite blackboard."""
import pytest
import tempfile
import os
from pathlib import Path
from lab.blackboard import Blackboard


@pytest.fixture
def bb(tmp_path):
    db_path = tmp_path / "test.db"
    board = Blackboard(db_path)
    yield board
    board.close()


class TestTrials:
    def test_submit_and_query(self, bb):
        tid = bb.submit("exp-1", "random", {"x": 1.0, "y": 2.0})
        assert tid > 0
        trials = bb.query("exp-1")
        assert len(trials) == 1
        assert trials[0].params == {"x": 1.0, "y": 2.0}
        assert trials[0].strategy == "random"

    def test_update_score(self, bb):
        tid = bb.submit("exp-1", "bayesian", {"lr": 0.01})
        bb.update(tid, score=0.85, accepted=True, status="completed")
        trials = bb.query("exp-1")
        assert trials[0].score == 0.85
        assert trials[0].accepted is True
        assert trials[0].status == "completed"
        assert trials[0].finished_at is not None

    def test_best(self, bb):
        t1 = bb.submit("exp-1", "a", {"x": 1})
        bb.update(t1, score=0.5, status="completed")
        t2 = bb.submit("exp-1", "b", {"x": 2})
        bb.update(t2, score=0.9, status="completed")
        t3 = bb.submit("exp-1", "c", {"x": 3})
        bb.update(t3, score=0.7, status="completed")

        best = bb.best("exp-1")
        assert best is not None
        assert best.score == 0.9
        assert best.params == {"x": 2}

    def test_best_minimize(self, bb):
        t1 = bb.submit("exp-1", "a", {"x": 1})
        bb.update(t1, score=0.5, status="completed")
        t2 = bb.submit("exp-1", "b", {"x": 2})
        bb.update(t2, score=0.1, status="completed")

        best = bb.best("exp-1", direction="minimize")
        assert best.score == 0.1

    def test_count(self, bb):
        bb.submit("exp-1", "a", {})
        bb.submit("exp-1", "b", {})
        bb.submit("exp-2", "a", {})
        assert bb.count("exp-1") == 2
        assert bb.count("exp-2") == 1

    def test_history_ordered(self, bb):
        t1 = bb.submit("exp-1", "a", {"i": 1})
        t2 = bb.submit("exp-1", "b", {"i": 2})
        t3 = bb.submit("exp-1", "c", {"i": 3})
        history = bb.history("exp-1")
        assert [t.params["i"] for t in history] == [1, 2, 3]

    def test_parent_lineage(self, bb):
        t1 = bb.submit("exp-1", "a", {"gen": 1})
        t2 = bb.submit("exp-1", "a", {"gen": 2}, parent_id=t1)
        trials = bb.query("exp-1", order_by="id ASC")
        assert trials[1].parent_id == t1

    def test_query_by_strategy(self, bb):
        bb.submit("exp-1", "random", {"x": 1})
        bb.submit("exp-1", "bayesian", {"x": 2})
        bb.submit("exp-1", "random", {"x": 3})
        random_trials = bb.query("exp-1", strategy="random")
        assert len(random_trials) == 2


class TestClaims:
    def test_claim_and_list(self, bb):
        cid = bb.claim("exp-1", "worker-1", "trying lr=0.01", ttl_seconds=60)
        assert cid > 0
        claims = bb.active_claims("exp-1")
        assert len(claims) == 1
        assert claims[0].description == "trying lr=0.01"

    def test_expired_claims_filtered(self, bb):
        # Create expired claim (ttl=0)
        bb.claim("exp-1", "worker-1", "old claim", ttl_seconds=0)
        import time
        time.sleep(0.1)
        claims = bb.active_claims("exp-1")
        assert len(claims) == 0

    def test_clear_expired(self, bb):
        bb.claim("exp-1", "w", "old", ttl_seconds=0)
        import time
        time.sleep(0.1)
        removed = bb.clear_expired_claims("exp-1")
        assert removed >= 1


class TestFeed:
    def test_post_and_read(self, bb):
        pid = bb.post("exp-1", "Found something interesting", worker="agent-1")
        assert pid > 0
        posts = bb.recent_posts("exp-1")
        assert len(posts) == 1
        assert posts[0]["content"] == "Found something interesting"

    def test_post_with_trial(self, bb):
        tid = bb.submit("exp-1", "a", {})
        bb.post("exp-1", "This trial was great", trial_id=tid)
        posts = bb.recent_posts("exp-1")
        assert posts[0]["trial_id"] == tid


class TestSummary:
    def test_empty_summary(self, bb):
        summary = bb.summary("exp-1")
        assert "No completed trials" in summary

    def test_summary_with_trials(self, bb):
        t1 = bb.submit("exp-1", "random", {"x": 1}, reasoning="first try")
        bb.update(t1, score=0.5, status="completed")
        t2 = bb.submit("exp-1", "bayesian", {"x": 2}, reasoning="TPE guided")
        bb.update(t2, score=0.8, accepted=True, status="completed")

        summary = bb.summary("exp-1")
        assert "2 trials" in summary
        assert "0.800000" in summary
        assert "bayesian" in summary
