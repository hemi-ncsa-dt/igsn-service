"""DataCite payload construction and error classification.

The payload assertions are pinned against what the hand-run
``htmdec/datacite/publish_prod.py`` and ``update_prod.py`` scripts sent, since
those are known to work against the live API.
"""

import json

import httpx
import pytest
import respx

from app.services import datacite


@pytest.fixture
def record():
    return {
        "igsn": "JHAMAA00001",
        "doi": "10.83961/jhamaa00001",
        "prefix": "JHAMAA",
        "status": "reserved",
        "landing_page": None,
        "metadata": {
            "titles": [{"title": "Test Sample"}],
            "creators": [{"name": "Doe, Jane"}],
            "publisher": {"name": "Test Institute"},
            "publicationYear": "2026",
            "types": {"resourceTypeGeneral": "PhysicalObject"},
            "schemaVersion": "http://datacite.org/schema/kernel-4",
        },
    }


class TestBuildAttributes:
    def test_shape_matches_publish_prod(self, tenant, record):
        attributes = datacite.build_attributes(record, tenant, "findable")
        # publish_prod.py set exactly these three on top of the metadata.
        assert attributes["prefix"] == "10.83961"
        assert attributes["doi"] == "10.83961/jhamaa00001"
        assert attributes["url"]
        assert attributes["titles"] == [{"title": "Test Sample"}]

    def test_event_mapping(self, tenant, record):
        assert datacite.build_attributes(record, tenant, "findable")["event"] == "publish"
        assert datacite.build_attributes(record, tenant, "registered")["event"] == "register"
        assert "event" not in datacite.build_attributes(record, tenant, "draft")

    def test_url_prefers_tenant_landing_page(self, tenant, record):
        """Already-published DOIs must keep pointing where they always did."""
        record["landing_page"] = "https://data.htmdec.org/#igsn/JHAMAA00001"
        attributes = datacite.build_attributes(record, tenant, "findable")
        assert attributes["url"] == "https://data.htmdec.org/#igsn/JHAMAA00001"

    def test_url_falls_back_to_central_resolver(self, tenant, record):
        attributes = datacite.build_attributes(record, tenant, "findable")
        assert attributes["url"] == "https://igsn.example.org/JHAMAA00001"


class TestRelatedIdentifierRewriting:
    """DataCite rejects IGSN-typed related identifiers.

    girder-jsonforms emits them for every batch IsPartOf/HasPart edge, so without
    this every batch would fail to publish.
    """

    def test_igsn_becomes_lowercase_doi(self):
        out = datacite.rewrite_related_identifiers(
            [
                {
                    "relationType": "IsPartOf",
                    "relatedIdentifier": "JHAMAA00001",
                    "relatedIdentifierType": "IGSN",
                }
            ],
            "10.82581",
        )
        assert out == [
            {
                "relationType": "IsPartOf",
                "relatedIdentifier": "10.82581/jhamaa00001",
                "relatedIdentifierType": "DOI",
            }
        ]

    def test_other_types_are_untouched(self):
        original = [
            {
                "relationType": "HasMetadata",
                "relatedIdentifier": "https://data.htmdec.org/api/v1/entry/abc",
                "relatedIdentifierType": "URL",
            }
        ]
        assert datacite.rewrite_related_identifiers(original, "10.82581") == original

    def test_already_qualified_igsn_is_not_double_prefixed(self):
        out = datacite.rewrite_related_identifiers(
            [
                {
                    "relationType": "IsPartOf",
                    "relatedIdentifier": "10.82581/JHAMAA00001",
                    "relatedIdentifierType": "IGSN",
                }
            ],
            "10.82581",
        )
        assert out[0]["relatedIdentifier"] == "10.82581/jhamaa00001"

    def test_applied_through_build_attributes(self, tenant, record):
        record["metadata"]["relatedIdentifiers"] = [
            {
                "relationType": "HasPart",
                "relatedIdentifier": "JHAMAA00001-001",
                "relatedIdentifierType": "IGSN",
            }
        ]
        attributes = datacite.build_attributes(record, tenant, "findable")
        assert attributes["relatedIdentifiers"][0] == {
            "relationType": "HasPart",
            "relatedIdentifier": "10.83961/jhamaa00001-001",
            "relatedIdentifierType": "DOI",
        }

    def test_source_metadata_is_not_mutated(self, tenant, record):
        record["metadata"]["relatedIdentifiers"] = [
            {
                "relationType": "IsPartOf",
                "relatedIdentifier": "JHAMAA00001",
                "relatedIdentifierType": "IGSN",
            }
        ]
        datacite.build_attributes(record, tenant, "findable")
        assert record["metadata"]["relatedIdentifiers"][0]["relatedIdentifierType"] == "IGSN"


class TestSubmit:
    @respx.mock
    async def test_reserved_record_is_posted(self, tenant, record):
        route = respx.post("https://api.test.datacite.org/dois").mock(
            return_value=httpx.Response(201, json={"data": {"id": record["doi"]}})
        )
        result = await datacite.submit(record, tenant, "findable")
        assert result["status"] == "findable"
        assert route.called
        payload = json.loads(route.calls[0].request.read())
        assert payload["data"]["type"] == "dois"
        assert payload["data"]["attributes"]["event"] == "publish"
        assert payload["data"]["attributes"]["doi"] == "10.83961/jhamaa00001"

    @respx.mock
    async def test_published_record_is_put(self, tenant, record):
        record["status"] = "findable"
        route = respx.put(f"https://api.test.datacite.org/dois/{record['doi']}").mock(
            return_value=httpx.Response(200, json={})
        )
        await datacite.submit(record, tenant, "findable")
        assert route.called

    @respx.mock
    async def test_post_falls_back_to_put_when_doi_exists(self, tenant, record):
        """Re-publishing must be safe -- publish_prod.py was re-run by hand often."""
        respx.post("https://api.test.datacite.org/dois").mock(
            return_value=httpx.Response(
                422, json={"errors": [{"title": "This DOI has already been taken"}]}
            )
        )
        put = respx.put(f"https://api.test.datacite.org/dois/{record['doi']}").mock(
            return_value=httpx.Response(200, json={})
        )
        result = await datacite.submit(record, tenant, "findable")
        assert put.called
        assert result["status"] == "findable"

    @respx.mock
    async def test_validation_error_is_terminal(self, tenant, record):
        respx.post("https://api.test.datacite.org/dois").mock(
            return_value=httpx.Response(422, json={"errors": [{"title": "bad publisher"}]})
        )
        with pytest.raises(datacite.DataCiteError) as excinfo:
            await datacite.submit(record, tenant, "findable")
        assert excinfo.value.retryable is False

    @respx.mock
    @pytest.mark.parametrize("status_code", [429, 500, 502, 503])
    async def test_transient_errors_are_retryable(self, tenant, record, status_code):
        respx.post("https://api.test.datacite.org/dois").mock(
            return_value=httpx.Response(status_code, text="nope")
        )
        with pytest.raises(datacite.DataCiteError) as excinfo:
            await datacite.submit(record, tenant, "findable")
        assert excinfo.value.retryable is True

    @respx.mock
    async def test_network_failure_is_retryable(self, tenant, record):
        respx.post("https://api.test.datacite.org/dois").mock(
            side_effect=httpx.ConnectError("no route to host")
        )
        with pytest.raises(datacite.DataCiteError) as excinfo:
            await datacite.submit(record, tenant, "findable")
        assert excinfo.value.retryable is True

    async def test_missing_credentials_is_terminal(self, tenant, record, monkeypatch):
        monkeypatch.delenv("DATACITE_PW_TEST", raising=False)
        with pytest.raises(datacite.DataCiteNotConfigured) as excinfo:
            await datacite.submit(record, tenant, "findable")
        assert excinfo.value.retryable is False

    @respx.mock
    async def test_uses_tenant_api_base_and_credentials(self, tenant, record):
        route = respx.post("https://api.test.datacite.org/dois").mock(
            return_value=httpx.Response(201, json={})
        )
        await datacite.submit(record, tenant, "findable")
        auth_header = route.calls[0].request.headers["authorization"]
        assert auth_header.startswith("Basic ")
