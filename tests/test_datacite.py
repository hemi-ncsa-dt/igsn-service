"""DataCite payload construction and error classification.

The payload assertions are pinned against what the hand-run
``htmdec/datacite/publish_prod.py`` and ``update_prod.py`` scripts sent, since
those are known to work against the live API.
"""

import datetime
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
    async def test_previously_synced_record_is_put(self, tenant, record):
        """The deciding field is last_sync, not status."""
        record["status"] = "findable"
        record["datacite"] = {"last_sync": datetime.datetime.now(datetime.UTC)}
        route = respx.put(f"https://api.test.datacite.org/dois/{record['doi']}").mock(
            return_value=httpx.Response(200, json={})
        )
        await datacite.submit(record, tenant, "findable")
        assert route.called

    @respx.mock
    async def test_never_synced_failed_record_is_posted(self, tenant, record):
        """A record whose first POST failed must not be stuck PUTting forever.

        Keying the decision off `status` meant a "failed" record PUT a DOI that
        was never created, got a 404, and could never recover.
        """
        record["status"] = "failed"
        record["datacite"] = {"last_sync": None, "last_error": "earlier failure"}
        post = respx.post("https://api.test.datacite.org/dois").mock(
            return_value=httpx.Response(201, json={})
        )
        await datacite.submit(record, tenant, "findable")
        assert post.called

    @respx.mock
    async def test_put_that_404s_falls_back_to_post(self, tenant, record):
        """Recovers if DataCite lost the DOI, or last_sync is optimistic."""
        record["status"] = "findable"
        record["datacite"] = {"last_sync": datetime.datetime.now(datetime.UTC)}
        respx.put(f"https://api.test.datacite.org/dois/{record['doi']}").mock(
            return_value=httpx.Response(404, json={})
        )
        post = respx.post("https://api.test.datacite.org/dois").mock(
            return_value=httpx.Response(201, json={})
        )
        result = await datacite.submit(record, tenant, "findable")
        assert post.called
        assert result["status"] == "findable"

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


class TestLandingUrlPreflight:
    """The failure this exists for.

    Girder pushed ``landing_page="#igsn/JHBBMX00001"`` -- what
    ``os.path.join('', '#igsn', igsn)`` produces when Girder's server-root
    setting is empty. DataCite answered
    ``{"source":"url","title":"URL is not valid"}`` with no echo of the value,
    so the record just said "failed" and nothing pointed at the cause.
    """

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("#igsn/JHBBMX00001", "not an absolute http"),
            ("igsn/JHBBMX00001", "not an absolute http"),
            ("/igsn/JHBBMX00001", "not an absolute http"),
            ("ftp://example.org/x", "not an absolute http"),
            ("", "is empty"),
            ("   ", "is empty"),
            (None, "is empty"),
            ("http://localhost:8000/JHBBMX00001", "will not accept"),
            ("https://girder/JHBBMX00001", "will not accept"),
        ],
    )
    def test_rejected(self, url, expected):
        with pytest.raises(datacite.InvalidLandingUrl, match=expected):
            datacite.validate_landing_url(url)

    def test_the_error_names_the_offending_value(self):
        with pytest.raises(datacite.InvalidLandingUrl) as excinfo:
            datacite.validate_landing_url("#igsn/JHBBMX00001")
        message = str(excinfo.value)
        assert "#igsn/JHBBMX00001" in message
        # And points at the likely cause rather than leaving it to guesswork.
        assert "server-root" in message

    @pytest.mark.parametrize(
        "url",
        [
            "https://data.htmdec.org/#igsn/JHAMAA00001",
            "https://girder.local.xarthisius.xyz/#igsn/JHAMAA00001",
            "http://example.org/igsn/X",
            "https://igsn.example.org/JHAMAA00001",
        ],
    )
    def test_accepted(self, url):
        assert datacite.validate_landing_url(url) == url

    def test_whitespace_is_trimmed(self):
        assert datacite.validate_landing_url("  https://example.org/x  ") == (
            "https://example.org/x"
        )

    @respx.mock
    async def test_submit_refuses_before_calling_datacite(self, tenant, record):
        """Fail locally with a useful message instead of relaying a 422."""
        record["landing_page"] = "#igsn/JHAMAA00001"
        route = respx.post("https://api.test.datacite.org/dois")
        with pytest.raises(datacite.InvalidLandingUrl, match="#igsn/JHAMAA00001"):
            await datacite.submit(record, tenant, "findable")
        assert not route.called

    async def test_the_refusal_is_terminal(self, tenant, record):
        record["landing_page"] = "#igsn/JHAMAA00001"
        with pytest.raises(datacite.InvalidLandingUrl) as excinfo:
            await datacite.submit(record, tenant, "findable")
        # Retrying a malformed URL forever would just burn attempts.
        assert excinfo.value.retryable is False
        assert excinfo.value.status_code == 422


class TestRequestDiagnostics:
    """What was sent, kept alongside what came back."""

    def test_summary_captures_the_fields_datacite_rejects(self, tenant, record):
        attributes = datacite.build_attributes(record, tenant, "findable")
        summary = datacite.summarize_request(
            "POST", "https://api.test.datacite.org/dois", attributes
        )
        assert summary["method"] == "POST"
        assert summary["endpoint"] == "https://api.test.datacite.org/dois"
        assert summary["url"] == "https://igsn.example.org/JHAMAA00001"
        assert summary["doi"] == "10.83961/jhamaa00001"
        assert summary["event"] == "publish"
        assert summary["counts"]["creators"] == 1
        assert summary["counts"]["titles"] == 1

    def test_summary_omits_credentials(self, tenant, record):
        attributes = datacite.build_attributes(record, tenant, "findable")
        summary = datacite.summarize_request("POST", "https://x/dois", attributes)
        blob = json.dumps(summary)
        assert "s3cret" not in blob
        assert "password" not in blob.lower()

    @respx.mock
    async def test_error_carries_the_request(self, tenant, record):
        respx.post("https://api.test.datacite.org/dois").mock(
            return_value=httpx.Response(
                422, text='{"errors":[{"source":"publisher","title":"is missing"}]}'
            )
        )
        with pytest.raises(datacite.DataCiteError) as excinfo:
            await datacite.submit(record, tenant, "findable")
        request = excinfo.value.request
        assert request["method"] == "POST"
        assert request["doi"] == "10.83961/jhamaa00001"
        assert request["url"] == "https://igsn.example.org/JHAMAA00001"

    @respx.mock
    async def test_error_message_echoes_the_field_datacite_named(self, tenant, record):
        """The exact failure the user hit, made self-explanatory.

        DataCite says the url is invalid without saying which url; the message
        now appends the value we sent.
        """
        record["landing_page"] = "https://ok.example.org/x"
        respx.post("https://api.test.datacite.org/dois").mock(
            return_value=httpx.Response(
                422,
                text='{"errors":[{"source":"url","title":"URL is not valid",'
                '"uid":"10.83961/jhamaa00001"}]}',
            )
        )
        with pytest.raises(datacite.DataCiteError) as excinfo:
            await datacite.submit(record, tenant, "findable")
        assert "we sent url='https://ok.example.org/x'" in str(excinfo.value)

    @respx.mock
    async def test_success_returns_the_request(self, tenant, record):
        respx.post("https://api.test.datacite.org/dois").mock(
            return_value=httpx.Response(201, json={})
        )
        result = await datacite.submit(record, tenant, "findable")
        assert result["request"]["method"] == "POST"

    @respx.mock
    async def test_outgoing_request_is_logged_without_the_auth_header(
        self, tenant, record, caplog
    ):
        respx.post("https://api.test.datacite.org/dois").mock(
            return_value=httpx.Response(201, json={})
        )
        with caplog.at_level("INFO", logger="app.services.datacite"):
            await datacite.submit(record, tenant, "findable")
        logged = caplog.text
        assert "DataCite POST" in logged
        assert "JHAMAA00001" in logged
        assert "https://igsn.example.org/JHAMAA00001" in logged
        assert "s3cret" not in logged

    @respx.mock
    async def test_rejection_is_logged_with_the_response_body(
        self, tenant, record, caplog
    ):
        respx.post("https://api.test.datacite.org/dois").mock(
            return_value=httpx.Response(422, text='{"errors":[{"source":"url"}]}')
        )
        with caplog.at_level("WARNING", logger="app.services.datacite"):
            with pytest.raises(datacite.DataCiteError):
                await datacite.submit(record, tenant, "findable")
        logged = caplog.text
        assert "rejected" in logged
        assert "source" in logged

    async def test_preflight_failure_still_reports_what_it_rejected(
        self, tenant, record
    ):
        """A preflight rejection has to be as legible as a DataCite one."""
        record["landing_page"] = "#igsn/JHAMAA00001"
        with pytest.raises(datacite.InvalidLandingUrl) as excinfo:
            await datacite.submit(record, tenant, "findable")
        request = excinfo.value.request
        assert request["url"] == "#igsn/JHAMAA00001"
        assert request["doi"] == "10.83961/jhamaa00001"
        assert request["rejected_by"] == "preflight"
