"""Portable projection and strict results-index publication tests."""

from __future__ import annotations

import copy
import json
import stat
from pathlib import Path

import pytest

from benchmarks import cluster_campaign as campaign
from benchmarks.campaign import results
from tests._platform import LINUX_ONLY_CAMPAIGN
from tests.test_cluster_campaign import _make_campaign, _worker_result

pytestmark = LINUX_ONLY_CAMPAIGN


@pytest.fixture(scope="module")
def publication_fixture(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict, dict, dict]:
    value = _make_campaign(tmp_path_factory.mktemp("campaign-publication"))
    worker_results = [_worker_result(value, job) for job in campaign._jobs(value)]
    merged = results.merge_campaign(value, worker_results)
    portable = results.project_campaign_results(merged)
    return value, merged, portable


def _resign_projection(value: dict) -> None:
    unsigned = {key: item for key, item in value.items() if key != "projection_sha256"}
    value["projection_sha256"] = campaign.sha256_json(unsigned)


def test_projection_is_explicit_path_free_and_freshly_digested(
    publication_fixture: tuple[dict, dict, dict],
) -> None:
    _value, merged, portable = publication_fixture
    validated = results.validate_portable_campaign_results(portable)

    assert validated["artifact_kind"] == "portable"
    assert validated["platform_key"] == "linux-x86_64"
    assert validated["source_results_sha256"] == merged["results_sha256"]
    assert "campaign" not in validated
    assert "campaign_path" not in validated
    assert "worker_results" not in validated
    assert "path" not in validated["candidate"]["wheel"]
    assert "input" not in validated["results"]["primary"]["mane"]
    assert "db_paths" not in validated["results"]["primary"]["mane"]
    assert "path" not in validated["results"]["bridges"]["bridge-gffbase-0.1.0-mane"]["input"]
    assert "/campaign-publication" not in json.dumps(validated)


@pytest.mark.parametrize("schema", ["campaign-results-v1", "2", None])
def test_portable_projection_rejects_stale_schemas(
    publication_fixture: tuple[dict, dict, dict], schema: object
) -> None:
    _value, _merged, portable = publication_fixture
    forged = copy.deepcopy(portable)
    forged["schema_version"] = schema
    _resign_projection(forged)

    with pytest.raises(campaign.CampaignError, match="schema"):
        results.validate_portable_campaign_results(forged)


def test_portable_projection_rejects_unknown_allowlist_fields(
    publication_fixture: tuple[dict, dict, dict],
) -> None:
    _value, _merged, portable = publication_fixture
    forged = copy.deepcopy(portable)
    forged["candidate"]["wheel"]["path"] = "/private/candidate.whl"
    _resign_projection(forged)

    with pytest.raises(campaign.CampaignError, match="private absolute path|unknown keys"):
        results.validate_portable_campaign_results(forged)


def test_portable_projection_rejects_forged_measurement_and_comparison_after_resigning(
    publication_fixture: tuple[dict, dict, dict],
) -> None:
    _value, _merged, portable = publication_fixture
    forged = copy.deepcopy(portable)
    forged["results"]["primary"]["mane"]["gffbase"]["wall_seconds"] = 0.0
    forged["comparisons"]["primary"]["mane"]["ratio"] = 999.0
    _resign_projection(forged)

    with pytest.raises(campaign.CampaignError, match="candidate is invalid"):
        results.validate_portable_campaign_results(forged)


def test_portable_projection_binds_canonical_corpus_registry(
    publication_fixture: tuple[dict, dict, dict],
) -> None:
    _value, _merged, portable = publication_fixture
    forged = copy.deepcopy(portable)
    forged["inputs"]["mane"]["sha256"] = "0" * 64
    forged["results"]["primary"]["mane"]["input_sha256"] = "0" * 64
    _resign_projection(forged)

    with pytest.raises(campaign.CampaignError, match="corpus registry"):
        results.validate_portable_campaign_results(forged)


def test_linux_platform_key_is_derived_not_caller_selected(
    publication_fixture: tuple[dict, dict, dict],
) -> None:
    _value, _merged, portable = publication_fixture
    forged = copy.deepcopy(portable)
    forged["platform_key"] = "linux-aarch64"
    _resign_projection(forged)
    with pytest.raises(campaign.CampaignError, match="platform key"):
        results.validate_portable_campaign_results(forged)

    with pytest.raises(campaign.CampaignError, match="requires recorded Linux"):
        results.derive_linux_platform_key("Darwin", "arm64")


def _seed_historical_mac(publish_root: Path) -> bytes:
    source = campaign.ROOT / "benchmarks" / "results" / "06_mega.json"
    raw = source.read_bytes()
    (publish_root / "06_mega.json").write_bytes(raw)
    return raw


def test_publisher_reloads_projection_index_and_every_reference(
    publication_fixture: tuple[dict, dict, dict], tmp_path: Path
) -> None:
    _value, merged, _portable = publication_fixture
    publish_root = tmp_path / "results"
    publish_root.mkdir()
    historical = _seed_historical_mac(publish_root)

    target = results.publish_campaign(merged, publish_root)
    first_index = (publish_root / "index.json").read_bytes()
    assert results.publish_campaign(merged, publish_root) == target
    assert (publish_root / "index.json").read_bytes() == first_index
    assert (publish_root / "06_mega.json").read_bytes() == historical

    index = json.loads(first_index)
    validated = results.validate_results_index(index, publish_root)
    assert validated["platforms"]["macos-arm64"]["kind"] == "legacy-opaque"
    linux = validated["platforms"]["linux-x86_64"]
    run = linux["runs"][linux["canonical_run_id"]]
    assert run["sha256"] == campaign._campaign_safe_io.sha256_regular_file(
        target, require_unique=True
    )


def test_publisher_accepts_normal_checkout_and_shared_repository_modes(
    publication_fixture: tuple[dict, dict, dict], tmp_path: Path
) -> None:
    _value, merged, _portable = publication_fixture
    publish_root = tmp_path / "results"
    publish_root.mkdir()
    _seed_historical_mac(publish_root)
    target = results.publish_campaign(merged, publish_root)

    for directory in (
        target.parent,
        target.parent.parent,
        target.parent.parent.parent,
    ):
        directory.chmod(0o2775)
    target.chmod(0o664)
    (publish_root / "index.json").chmod(0o664)

    assert results.publish_campaign(merged, publish_root) == target
    assert stat.S_IMODE(target.stat().st_mode) in {0o600, 0o644, 0o664}
    assert stat.S_IMODE((publish_root / "index.json").stat().st_mode) in {
        0o600,
        0o644,
        0o664,
    }


def test_publisher_works_beneath_a_setgid_repository_root(
    publication_fixture: tuple[dict, dict, dict], tmp_path: Path
) -> None:
    _value, merged, _portable = publication_fixture
    tmp_path.chmod(0o2775)
    publish_root = tmp_path / "results"
    publish_root.mkdir()
    publish_root.chmod(0o2775)
    _seed_historical_mac(publish_root)

    target = results.publish_campaign(merged, publish_root)

    assert target.is_file()
    assert results.publish_campaign(merged, publish_root) == target


def test_publisher_rejects_world_writable_public_json(
    publication_fixture: tuple[dict, dict, dict], tmp_path: Path
) -> None:
    _value, merged, _portable = publication_fixture
    publish_root = tmp_path / "results"
    publish_root.mkdir()
    target = results.publish_campaign(merged, publish_root)
    target.chmod(0o666)

    with pytest.raises(campaign.CampaignError, match="mode|permission"):
        results.publish_campaign(merged, publish_root)


def test_publisher_rejects_world_writable_publication_root(
    publication_fixture: tuple[dict, dict, dict], tmp_path: Path
) -> None:
    _value, merged, _portable = publication_fixture
    publish_root = tmp_path / "results"
    publish_root.mkdir()
    publish_root.chmod(0o777)

    with pytest.raises(campaign.CampaignError, match="mode|permission"):
        results.publish_campaign(merged, publish_root)


@pytest.mark.parametrize("schema", ["results-index-v1", "1", None])
def test_results_index_rejects_stale_schemas(
    publication_fixture: tuple[dict, dict, dict], tmp_path: Path, schema: object
) -> None:
    _value, merged, _portable = publication_fixture
    publish_root = tmp_path / "results"
    publish_root.mkdir()
    results.publish_campaign(merged, publish_root)
    index = json.loads((publish_root / "index.json").read_text())
    index["schema_version"] = schema

    with pytest.raises(campaign.CampaignError, match="schema"):
        results.validate_results_index(index, publish_root)


def test_results_index_rejects_arbitrary_old_platform_shapes(
    publication_fixture: tuple[dict, dict, dict], tmp_path: Path
) -> None:
    _value, merged, _portable = publication_fixture
    publish_root = tmp_path / "results"
    publish_root.mkdir()
    results.publish_campaign(merged, publish_root)
    index = json.loads((publish_root / "index.json").read_text())
    index["platforms"]["macos-arm64"] = {
        "canonical_run_id": "mac-old",
        "runs": {"mac-old": {"artifact": "06_mega.json", "sha256": "f" * 64}},
    }

    with pytest.raises(campaign.CampaignError, match="missing keys"):
        results.validate_results_index(index, publish_root)


def test_results_index_rejects_referenced_digest_drift(
    publication_fixture: tuple[dict, dict, dict], tmp_path: Path
) -> None:
    _value, merged, _portable = publication_fixture
    publish_root = tmp_path / "results"
    publish_root.mkdir()
    target = results.publish_campaign(merged, publish_root)
    index = json.loads((publish_root / "index.json").read_text())
    run = index["platforms"]["linux-x86_64"]["runs"][merged["run_id"]]
    run["sha256"] = "0" * 64

    with pytest.raises(campaign.CampaignError, match="artifact digest differs"):
        results.validate_results_index(index, publish_root)
    assert target.is_file()


def test_historical_mac_seed_requires_the_exact_preserved_bytes(
    publication_fixture: tuple[dict, dict, dict], tmp_path: Path
) -> None:
    _value, merged, _portable = publication_fixture
    publish_root = tmp_path / "results"
    publish_root.mkdir()
    historical = _seed_historical_mac(publish_root)
    (publish_root / "06_mega.json").write_bytes(historical + b"\n")

    with pytest.raises(campaign.CampaignError, match="byte-preservation digest"):
        results.publish_campaign(merged, publish_root)
    assert (publish_root / "06_mega.json").read_bytes() == historical + b"\n"
