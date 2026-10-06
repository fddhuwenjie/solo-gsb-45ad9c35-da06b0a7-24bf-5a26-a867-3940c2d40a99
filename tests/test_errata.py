import threading

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import ErrataRequest, CorrectionSpec, create_app, logic_submit_errata

CATEGORIES = ["cat", "dog", "bird"]
SAMPLES = [
    {"code": "S01", "true_label": "cat"},
    {"code": "S02", "true_label": "dog"},
    {"code": "S03", "true_label": "bird"},
]
# reviewer1 matches the recorded truth; reviewer2 differs on S03
R1_LABELS = {"S01": "cat", "S02": "dog", "S03": "bird"}
R2_LABELS = {"S01": "cat", "S02": "dog", "S03": "cat"}


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def sealed(client):
    r = client.post("/batches", json={"allowed_categories": CATEGORIES, "samples": SAMPLES})
    assert r.status_code == 201, r.text
    batch = r.json()
    bid = batch["batch_id"]
    client.post(
        f"/batches/{bid}/submissions",
        json={"labels": R1_LABELS},
        headers={"X-Reviewer-Token": batch["reviewer1_token"]},
    )
    client.post(
        f"/batches/{bid}/submissions",
        json={"labels": R2_LABELS},
        headers={"X-Reviewer-Token": batch["reviewer2_token"]},
    )
    client.post(
        f"/batches/{bid}/adjudications",
        json={"decisions": {"S03": "bird"}},
        headers={"X-Arbiter-Token": batch["arbiter_token"]},
    )
    return batch


def errata(client, bid, token, expected_revision, corrections):
    return client.post(
        f"/batches/{bid}/errata",
        json={"expected_revision": expected_revision, "corrections": corrections},
        headers={"X-Arbiter-Token": token},
    )


def test_errata_only_after_sealing(client):
    r = client.post("/batches", json={"allowed_categories": CATEGORIES, "samples": SAMPLES})
    batch = r.json()
    bid = batch["batch_id"]

    # collecting -> 409
    r = errata(client, bid, batch["arbiter_token"], 0, [{"code": "S01", "true_label": "dog"}])
    assert r.status_code == 409
    # index/version queries are unavailable too
    assert client.get(f"/batches/{bid}/errata").status_code == 409
    assert client.get(f"/batches/{bid}/errata/0").status_code == 409


def test_errata_requires_arbiter_token(client, sealed):
    bid = sealed["batch_id"]
    body = {"expected_revision": 0, "corrections": [{"code": "S01", "true_label": "dog"}]}

    assert client.post(f"/batches/{bid}/errata", json=body).status_code == 401
    assert client.post(f"/batches/{bid}/errata", json=body,
                       headers={"X-Arbiter-Token": "nope"}).status_code == 401
    # reviewer credentials cannot operate errata
    assert client.post(f"/batches/{bid}/errata", json=body,
                       headers={"X-Arbiter-Token": sealed["reviewer1_token"]}).status_code == 401
    assert client.post(f"/batches/{bid}/errata", json=body,
                       headers={"X-Arbiter-Token": sealed["reviewer2_token"]}).status_code == 401


def test_version_zero_is_sealed_original(client, sealed):
    bid = sealed["batch_id"]
    original = client.get(f"/batches/{bid}/results").json()

    r = client.get(f"/batches/{bid}/errata/0")
    assert r.status_code == 200
    v0 = r.json()
    assert v0["version"] == 0
    assert v0["current_revision"] == 0
    assert v0["corrected_samples"] == []
    # classifications, truth and correctness reproduce the sealed original
    assert v0["results"] == [
        {**row, "final_correct": row["final_label"] == row["true_label"]}
        for row in original["results"]
    ]
    assert v0["summary"] == original["summary"]

    # unknown versions are 404 but disclose the current revision
    r = client.get(f"/batches/{bid}/errata/1")
    assert r.status_code == 404
    assert r.json()["detail"]["current_revision"] == 0
    r = client.get(f"/batches/{bid}/errata/-1")
    assert r.status_code == 404


def test_correction_recomputes_results_and_keeps_classifications(client, sealed):
    bid = sealed["batch_id"]
    arb = sealed["arbiter_token"]

    # S01's recorded truth "cat" was wrong: it is really "dog".
    # final=cat, r1=cat, r2=cat on S01 -> all three flip from correct to wrong.
    r = errata(client, bid, arb, 0, [{"code": "S01", "true_label": "dog"}])
    assert r.status_code == 201, r.text
    v1 = r.json()
    assert v1["version"] == 1
    assert v1["current_revision"] == 1
    assert v1["corrected_samples"] == [
        {"code": "S01", "old_true_label": "cat", "new_true_label": "dog"}
    ]
    by_code = {row["code"]: row for row in v1["results"]}
    assert by_code["S01"] == {
        "code": "S01",
        "final_label": "cat",       # original final classification preserved
        "true_label": "dog",        # only the truth changed
        "reviewer1_label": "cat",
        "reviewer2_label": "cat",
        "agreed": True,
        "reviewer1_correct": False,
        "reviewer2_correct": False,
        "final_correct": False,
    }
    assert by_code["S02"]["true_label"] == "dog"
    assert by_code["S03"]["true_label"] == "bird"
    assert v1["summary"] == {
        "sample_count": 3,
        "disagreement_count": 1,
        "reviewer1_correct": 2,
        "reviewer2_correct": 1,
        "final_correct": 2,
    }

    # the same content is queryable later
    assert client.get(f"/batches/{bid}/errata/1").json() == v1

    # original queries still return the sealed original
    original = client.get(f"/batches/{bid}/results").json()
    assert client.get(f"/batches/{bid}").json() == original
    by_code0 = {row["code"]: row for row in original["results"]}
    assert by_code0["S01"]["true_label"] == "cat"
    assert original["summary"]["reviewer1_correct"] == 3
    # v0 is still the original as well
    assert client.get(f"/batches/{bid}/errata/0").json()["results"] == [
        {**row, "final_correct": row["final_label"] == row["true_label"]}
        for row in original["results"]
    ]


def test_validation_rejects_whole_request(client, sealed):
    bid = sealed["batch_id"]
    arb = sealed["arbiter_token"]

    def post(corrections, expected=0):
        return errata(client, bid, arb, expected, corrections)

    assert post([]).status_code == 422  # empty
    r = post([  # duplicate code within one request
        {"code": "S01", "true_label": "dog"},
        {"code": "S01", "true_label": "bird"},
    ])
    assert r.status_code == 422 and r.json()["detail"]["duplicated"] == ["S01"]
    assert post([{"code": "S99", "true_label": "cat"}]).status_code == 422  # unknown code
    assert post([{"code": "S01", "true_label": "fish"}]).status_code == 422  # bad category
    assert post([{"code": "S01", "true_label": "cat"}]).status_code == 422  # no actual change
    assert post([
        {"code": "S01", "true_label": "cat"},   # one no-op among real changes
        {"code": "S02", "true_label": "cat"},
    ]).status_code == 422
    assert post([{"code": "S01", "true_label": "dog"}], expected=-1).status_code == 422

    # none of the failed attempts created a version
    idx = client.get(f"/batches/{bid}/errata").json()
    assert idx["current_revision"] == 0
    assert [v["version"] for v in idx["versions"]] == [0]


def test_stale_revision_is_rejected_with_current_revision_then_succeeds(client, sealed):
    bid = sealed["batch_id"]
    arb = sealed["arbiter_token"]

    assert errata(client, bid, arb, 0, [{"code": "S01", "true_label": "dog"}]).status_code == 201

    # another request still expecting revision 0 is stale
    r = errata(client, bid, arb, 0, [{"code": "S02", "true_label": "cat"}])
    assert r.status_code == 409
    assert r.json()["detail"]["current_revision"] == 1

    # retrying with the reported revision works
    r = errata(client, bid, arb, 1, [{"code": "S02", "true_label": "cat"}])
    assert r.status_code == 201
    assert r.json()["version"] == 2


def test_multiple_versions_chain_and_list_changes(client, sealed):
    bid = sealed["batch_id"]
    arb = sealed["arbiter_token"]

    v1 = errata(client, bid, arb, 0, [{"code": "S01", "true_label": "dog"}]).json()
    v2 = errata(client, bid, arb, 1, [
        {"code": "S02", "true_label": "cat"},
        {"code": "S01", "true_label": "bird"},  # correct S01 again
    ]).json()
    assert v2["version"] == 2
    assert v2["corrected_samples"] == [
        {"code": "S01", "old_true_label": "dog", "new_true_label": "bird"},
        {"code": "S02", "old_true_label": "dog", "new_true_label": "cat"},
    ]
    by_code = {r["code"]: r for r in v2["results"]}
    assert by_code["S01"]["true_label"] == "bird"
    assert by_code["S02"]["true_label"] == "cat"
    assert by_code["S03"]["true_label"] == "bird"
    # v1's content is unchanged (only the live "current_revision" pointer moved)
    fetched_v1 = client.get(f"/batches/{bid}/errata/1").json()
    assert fetched_v1["current_revision"] == 2
    assert {k: val for k, val in fetched_v1.items() if k != "current_revision"} == {
        k: val for k, val in v1.items() if k != "current_revision"
    }

    idx = client.get(f"/batches/{bid}/errata").json()
    assert idx["current_revision"] == 2
    assert [v["version"] for v in idx["versions"]] == [0, 1, 2]
    assert idx["versions"][1]["correction_count"] == 1
    assert idx["versions"][2]["correction_count"] == 2
    assert [s["code"] for s in idx["versions"][2]["corrected_samples"]] == ["S01", "S02"]


def test_concurrent_errata_apply_in_revision_order(tmp_path):
    """Concurrent corrections with the same expected revision: exactly one wins."""
    app = create_app(str(tmp_path / "race.db"))
    store = app.state.store
    with TestClient(app) as client:
        sealed_data = None
        r = client.post("/batches", json={"allowed_categories": CATEGORIES, "samples": SAMPLES})
        sealed_data = r.json()
        bid = sealed_data["batch_id"]
        client.post(f"/batches/{bid}/submissions", json={"labels": R1_LABELS},
                    headers={"X-Reviewer-Token": sealed_data["reviewer1_token"]})
        client.post(f"/batches/{bid}/submissions", json={"labels": R2_LABELS},
                    headers={"X-Reviewer-Token": sealed_data["reviewer2_token"]})
        client.post(f"/batches/{bid}/adjudications", json={"decisions": {"S03": "bird"}},
                    headers={"X-Arbiter-Token": sealed_data["arbiter_token"]})
        token = sealed_data["arbiter_token"]

        outcomes = []

        def worker(new_label):
            payload = ErrataRequest(
                expected_revision=0,
                corrections=[CorrectionSpec(code="S01", true_label=new_label)],
            )
            try:
                logic_submit_errata(store, bid, token, payload)
                outcomes.append("ok")
            except HTTPException as exc:
                outcomes.append(exc.status_code)

        threads = [
            threading.Thread(target=worker, kwargs={"new_label": label})
            for label in ("dog", "bird")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert outcomes.count("ok") == 1
        assert outcomes.count(409) == 1
        idx = client.get(f"/batches/{bid}/errata").json()
        assert idx["current_revision"] == 1
        assert idx["versions"][1]["correction_count"] == 1
        # the loser can now retry against the new revision with a different change
        r = client.post(
            f"/batches/{bid}/errata",
            json={"expected_revision": 1,
                  "corrections": [{"code": "S02", "true_label": "cat"}]},
            headers={"X-Arbiter-Token": token},
        )
        assert r.status_code == 201 and r.json()["version"] == 2


def test_submissions_and_adjudications_still_closed_after_errata(client, sealed):
    bid = sealed["batch_id"]
    errata(client, bid, sealed["arbiter_token"], 0, [{"code": "S01", "true_label": "dog"}])

    assert client.post(
        f"/batches/{bid}/submissions",
        json={"labels": R1_LABELS},
        headers={"X-Reviewer-Token": sealed["reviewer1_token"]},
    ).status_code == 409
    assert client.post(
        f"/batches/{bid}/adjudications",
        json={"decisions": {"S03": "cat"}},
        headers={"X-Arbiter-Token": sealed["arbiter_token"]},
    ).status_code == 409
    assert client.get(
        f"/batches/{bid}/disagreements",
        headers={"X-Arbiter-Token": sealed["arbiter_token"]},
    ).status_code == 409


def test_errata_persist_across_restart(tmp_path):
    db = str(tmp_path / "persist.db")
    app1 = create_app(db)
    with TestClient(app1) as client1:
        r = client1.post("/batches", json={"allowed_categories": CATEGORIES, "samples": SAMPLES})
        batch = r.json()
        bid = batch["batch_id"]
        client1.post(f"/batches/{bid}/submissions", json={"labels": R1_LABELS},
                     headers={"X-Reviewer-Token": batch["reviewer1_token"]})
        client1.post(f"/batches/{bid}/submissions", json={"labels": R2_LABELS},
                     headers={"X-Reviewer-Token": batch["reviewer2_token"]})
        client1.post(f"/batches/{bid}/adjudications", json={"decisions": {"S03": "bird"}},
                     headers={"X-Arbiter-Token": batch["arbiter_token"]})
        original = client1.get(f"/batches/{bid}/results").json()
        v1 = errata(client1, bid, batch["arbiter_token"], 0,
                    [{"code": "S01", "true_label": "dog"}]).json()

    app2 = create_app(db)
    with TestClient(app2) as client2:
        assert client2.get(f"/batches/{bid}/errata/1").json() == v1
        assert client2.get(f"/batches/{bid}/results").json() == original
        assert client2.get(f"/batches/{bid}").json() == original
        idx = client2.get(f"/batches/{bid}/errata").json()
        assert idx["current_revision"] == 1
        # the next erratum chains on after restart
        r = errata(client2, bid, batch["arbiter_token"], 1,
                   [{"code": "S02", "true_label": "bird"}])
        assert r.status_code == 201 and r.json()["version"] == 2
