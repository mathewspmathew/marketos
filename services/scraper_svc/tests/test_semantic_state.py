import uuid
from unittest.mock import patch
from dotenv import load_dotenv; load_dotenv()
from sqlalchemy import text
from services.common.db import get_db
import services.scraper_svc.semantics as sem


def _mk_product(session, shop, status="PENDING", version=0, claimed_sql="NULL"):
    pid = f"gid://test/Product/{uuid.uuid4()}"
    session.execute(text(f'''
      INSERT INTO "ShopifyProduct" (id,"shopDomain",title,"semanticStatus","semanticVersion","semanticClaimedAt","updatedAt")
      VALUES (:id,:sd,'t',:st,:v,{claimed_sql},NOW())'''), {"id": pid, "sd": shop, "st": status, "v": version})
    return pid


def _setup_shop(session):
    shop = f"claimtest-{uuid.uuid4()}.myshopify.com"
    session.execute(text('INSERT INTO "ShopifyUser" ("shopDomain") VALUES (:s)'), {"s": shop})
    return shop


def test_claim_specific_ids_only_pending():
    with get_db() as s:
        shop = _setup_shop(s)
        p_pending = _mk_product(s, shop, "PENDING")
        p_queued  = _mk_product(s, shop, "QUEUED")
        s.commit()
        with patch.object(sem.app, "send_task") as send:
            claimed = sem.claim_and_enqueue_semantics(s, ids=[p_pending, p_queued])
        assert claimed == [p_pending]
        send.assert_called_once_with("scraper.generate_shopify_variant_semantics",
                                     args=[p_pending], queue="shopify_semantic_queue")
        row = s.execute(text('SELECT "semanticStatus","semanticAttempts" FROM "ShopifyProduct" WHERE id=:i'),
                        {"i": p_pending}).first()
        assert row[0] == "QUEUED" and row[1] == 1
        s.execute(text('DELETE FROM "ShopifyProduct" WHERE "shopDomain"=:s'), {"s": shop})
        s.execute(text('DELETE FROM "ShopifyUser" WHERE "shopDomain"=:s'), {"s": shop}); s.commit()


def test_claim_beat_includes_stale_queued():
    with get_db() as s:
        shop = _setup_shop(s)
        p_pending = _mk_product(s, shop, "PENDING")
        p_fresh   = _mk_product(s, shop, "QUEUED", claimed_sql="NOW()")
        p_stale   = _mk_product(s, shop, "QUEUED", claimed_sql="NOW() - INTERVAL '20 minutes'")
        p_done    = _mk_product(s, shop, "DONE")
        s.commit()
        with patch.object(sem.app, "send_task"):
            claimed = set(sem.claim_and_enqueue_semantics(s, ids=None))
        assert p_pending in claimed and p_stale in claimed
        assert p_fresh not in claimed and p_done not in claimed
        s.execute(text('DELETE FROM "ShopifyProduct" WHERE "shopDomain"=:s'), {"s": shop})
        s.execute(text('DELETE FROM "ShopifyUser" WHERE "shopDomain"=:s'), {"s": shop}); s.commit()


def test_finalize_done_cas_version_mismatch_discards():
    with get_db() as s:
        shop = _setup_shop(s)
        pid = _mk_product(s, shop, "QUEUED", version=7); s.commit()
        rc = sem._finalize_semantic_done(s, pid, version_read=6); s.commit()
        assert rc == 0
        st = s.execute(text('SELECT "semanticStatus" FROM "ShopifyProduct" WHERE id=:i'), {"i": pid}).scalar()
        assert st == "QUEUED"
        rc2 = sem._finalize_semantic_done(s, pid, version_read=7); s.commit()
        assert rc2 == 1
        st2 = s.execute(text('SELECT "semanticStatus" FROM "ShopifyProduct" WHERE id=:i'), {"i": pid}).scalar()
        assert st2 == "DONE"
        s.execute(text('DELETE FROM "ShopifyProduct" WHERE "shopDomain"=:s'), {"s": shop})
        s.execute(text('DELETE FROM "ShopifyUser" WHERE "shopDomain"=:s'), {"s": shop}); s.commit()


def test_task_discards_when_version_moved(monkeypatch):
    import uuid as _uuid
    from sqlalchemy import text as _t
    with get_db() as s:
        shop = _setup_shop(s)
        pid = _mk_product(s, shop, "QUEUED", version=1)
        vid = f"gid://test/Variant/{_uuid.uuid4()}"
        s.execute(_t('''INSERT INTO "ShopifyVariant" (id,"productId",title,"currentPrice","semanticText","updatedAt")
                        VALUES (:i,:p,'v',10,NULL,NOW())'''), {"i": vid, "p": pid})
        s.commit()

    monkeypatch.setattr(sem, "_groq_semantic_call", lambda *a, **k: {sem._short_id(vid): "fp text"})
    monkeypatch.setattr(sem, "_groq_search_query", lambda **k: "q")
    orig_finalize = sem._finalize_semantic_done
    def bump_then_finalize(session, product_id, version_read):
        with get_db() as s2:
            s2.execute(_t('UPDATE "ShopifyProduct" SET "semanticVersion"=99 WHERE id=:i'), {"i": product_id}); s2.commit()
        return orig_finalize(session, product_id, version_read)
    monkeypatch.setattr(sem, "_finalize_semantic_done", bump_then_finalize)
    monkeypatch.setattr(sem.app, "send_task", lambda *a, **k: None)

    sem.generate_shopify_variant_semantics.run(pid)

    with get_db() as s:
        st, semt = s.execute(_t('''SELECT p."semanticStatus", v."semanticText"
                                   FROM "ShopifyProduct" p JOIN "ShopifyVariant" v ON v."productId"=p.id
                                   WHERE p.id=:i'''), {"i": pid}).first()
        assert st != "DONE"
        assert semt is None
        s.execute(_t('DELETE FROM "ShopifyVariant" WHERE "productId"=:p'), {"p": pid})
        s.execute(_t('DELETE FROM "ShopifyProduct" WHERE "shopDomain"=:s'), {"s": shop})
        s.execute(_t('DELETE FROM "ShopifyUser" WHERE "shopDomain"=:s'), {"s": shop}); s.commit()


def test_task_incomplete_groq_not_marked_done(monkeypatch):
    import uuid as _uuid
    from sqlalchemy import text as _t
    from celery.exceptions import Retry
    import pytest
    with get_db() as s:
        shop = _setup_shop(s)
        pid = _mk_product(s, shop, "QUEUED", version=1)
        vid = f"gid://test/Variant/{_uuid.uuid4()}"
        s.execute(_t('''INSERT INTO "ShopifyVariant" (id,"productId",title,"currentPrice","semanticText","updatedAt")
                        VALUES (:i,:p,'v',10,NULL,NOW())'''), {"i": vid, "p": pid})
        s.commit()
    monkeypatch.setattr(sem, "_groq_semantic_call", lambda *a, **k: {})  # Groq returns nothing
    monkeypatch.setattr(sem.app, "send_task", lambda *a, **k: None)
    with pytest.raises(Retry):
        sem.generate_shopify_variant_semantics.run(pid)
    with get_db() as s:
        st, semt = s.execute(_t('''SELECT p."semanticStatus", v."semanticText"
            FROM "ShopifyProduct" p JOIN "ShopifyVariant" v ON v."productId"=p.id WHERE p.id=:i'''), {"i": pid}).first()
        assert st != "DONE"
        assert semt is None
        s.execute(_t('DELETE FROM "ShopifyVariant" WHERE "productId"=:p'), {"p": pid})
        s.execute(_t('DELETE FROM "ShopifyProduct" WHERE "shopDomain"=:s'), {"s": shop})
        s.execute(_t('DELETE FROM "ShopifyUser" WHERE "shopDomain"=:s'), {"s": shop}); s.commit()


def test_beat_backfill_claims_once(monkeypatch):
    import services.scraper_svc.celery_beat as beat
    from sqlalchemy import text as _t
    with get_db() as s:
        shop = _setup_shop(s)
        pids = [_mk_product(s, shop, "PENDING") for _ in range(3)]
        s.commit()
    calls = []
    monkeypatch.setattr(sem.app, "send_task", lambda *a, **k: calls.append(k.get("args") or (a[1] if len(a) > 1 else a)))
    beat._shopify_semantic_backfill()
    beat._shopify_semantic_backfill()   # second tick: all now QUEUED -> no new enqueues
    assert len(calls) == 3
    with get_db() as s:
        s.execute(_t('DELETE FROM "ShopifyProduct" WHERE "shopDomain"=:s'), {"s": shop})
        s.execute(_t('DELETE FROM "ShopifyUser" WHERE "shopDomain"=:s'), {"s": shop}); s.commit()
