"""Tests for generate_embeddings' matcher-dispatch guard and retry-exhaustion
handling.

Before this fix, a failed matcher dispatch after a successful embedding
write was completely unhandled (no log, no retry — just an uncaught
exception), and the retry path never checked for exhausted retries the way
its sibling task (generate_shopify_embeddings) does.
"""
from unittest.mock import MagicMock

import services.embedding_svc.main as emb_mod


def test_matcher_dispatch_failure_is_logged_not_raised(monkeypatch):
    monkeypatch.setattr(emb_mod, "_generate", lambda pid: None)
    fake_logger = MagicMock()
    monkeypatch.setattr(emb_mod, "logger", fake_logger)
    send = MagicMock(side_effect=RuntimeError("broker down"))
    monkeypatch.setattr(emb_mod.app, "send_task", send)

    # Must not raise.
    emb_mod.generate_embeddings.run("prod1")

    send.assert_called_once()
    fake_logger.exception.assert_called_once_with("match_dispatch_failed", product_id="prod1")


def test_matcher_dispatch_success_no_log(monkeypatch):
    monkeypatch.setattr(emb_mod, "_generate", lambda pid: None)
    fake_logger = MagicMock()
    monkeypatch.setattr(emb_mod, "logger", fake_logger)
    send = MagicMock()
    monkeypatch.setattr(emb_mod.app, "send_task", send)

    emb_mod.generate_embeddings.run("prod1")

    send.assert_called_once()
    fake_logger.exception.assert_not_called()


def test_permanently_failed_after_retries_exhausted_returns_cleanly(monkeypatch):
    def boom(product_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(emb_mod, "_generate", boom)
    fake_logger = MagicMock()
    monkeypatch.setattr(emb_mod, "logger", fake_logger)
    send = MagicMock()
    monkeypatch.setattr(emb_mod.app, "send_task", send)

    task = emb_mod.generate_embeddings
    task.push_request(retries=task.max_retries)
    try:
        result = task.run("prod1")
    finally:
        task.pop_request()

    assert result is None
    fake_logger.exception.assert_any_call(
        "embedding_generation_permanently_failed", product_id="prod1"
    )
    send.assert_not_called()
