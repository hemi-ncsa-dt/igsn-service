"""The public resolver.

This is what a DataCite DOI ultimately dereferences to, so its behavior is a
published contract rather than an implementation detail.
"""

import pytest

from app.services import allocator

BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


@pytest.fixture
async def record(database, tenant, metadata):
    docs = await allocator.allocate("JHAMAA", 1, tenant, metadata=metadata)
    return docs[0]


class TestHtml:
    async def test_renders_landing_page(self, client, record):
        response = await client.get(f"/{record['igsn']}", headers={"accept": BROWSER_ACCEPT})
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "Test Sample" in response.text
        assert record["igsn"] in response.text
        assert record["doi"] in response.text

    async def test_redirects_to_tenant_landing_page(self, client, database, record):
        """Already-published DOIs must keep resolving where they always did."""
        await database["records"].update_one(
            {"igsn": record["igsn"]},
            {"$set": {"landing_page": "https://data.htmdec.org/#igsn/JHAMAA00001"}},
        )
        response = await client.get(f"/{record['igsn']}", headers={"accept": BROWSER_ACCEPT})
        assert response.status_code == 302
        assert response.headers["location"] == "https://data.htmdec.org/#igsn/JHAMAA00001"

    async def test_lowercase_igsn_resolves(self, client, record):
        response = await client.get(
            f"/{record['igsn'].lower()}", headers={"accept": BROWSER_ACCEPT}
        )
        assert response.status_code == 200

    async def test_unknown_igsn_is_404(self, client, database):
        response = await client.get("/JHAMAA99999", headers={"accept": BROWSER_ACCEPT})
        assert response.status_code == 404

    async def test_non_igsn_path_is_404(self, client, database):
        assert (await client.get("/not-an-igsn")).status_code == 404

    async def test_resolver_does_not_shadow_api_or_docs(self, client, database, auth):
        """The catch-all /{igsn} route must not swallow the rest of the app."""
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/api/v1/whoami", headers=auth)).status_code == 200
        assert (await client.get("/docs")).status_code == 200
        assert (await client.get("/openapi.json")).status_code == 200

    async def test_index_page(self, client, database):
        response = await client.get("/")
        assert response.status_code == 200
        assert "IGSN" in response.text

    async def test_child_links_to_parent(self, client, database, tenant, record):
        await allocator.allocate_children(record["igsn"], tenant, indices=["001"])
        response = await client.get(
            f"/{record['igsn']}-001", headers={"accept": BROWSER_ACCEPT}
        )
        assert f'href="/{record["igsn"]}"' in response.text

    async def test_record_without_metadata_still_renders(self, client, database, tenant):
        docs = await allocator.allocate("JHAMAA", 1, tenant)
        response = await client.get(
            f"/{docs[0]['igsn']}", headers={"accept": BROWSER_ACCEPT}
        )
        assert response.status_code == 200
        assert docs[0]["igsn"] in response.text


class TestJson:
    async def test_json_accept_returns_metadata(self, client, record):
        response = await client.get(
            f"/{record['igsn']}", headers={"accept": "application/json"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["igsn"] == record["igsn"]
        assert body["doi"] == record["doi"]
        assert body["attributes"]["titles"] == [{"title": "Test Sample"}]

    async def test_datacite_media_type(self, client, record):
        response = await client.get(
            f"/{record['igsn']}",
            headers={"accept": "application/vnd.datacite.datacite+json"},
        )
        assert response.status_code == 200
        assert response.json()["igsn"] == record["igsn"]

    async def test_browser_accept_prefers_html(self, client, record):
        """A browser sends */* alongside text/html; that must not yield JSON."""
        response = await client.get(f"/{record['igsn']}", headers={"accept": BROWSER_ACCEPT})
        assert "text/html" in response.headers["content-type"]

    async def test_json_ignores_tenant_landing_page(self, client, database, record):
        """Machine clients want the metadata, not a redirect into a web app."""
        await database["records"].update_one(
            {"igsn": record["igsn"]},
            {"$set": {"landing_page": "https://data.htmdec.org/#igsn/JHAMAA00001"}},
        )
        response = await client.get(
            f"/{record['igsn']}", headers={"accept": "application/json"}
        )
        assert response.status_code == 200
        assert response.json()["landing_page"] == "https://data.htmdec.org/#igsn/JHAMAA00001"


class TestRevoked:
    async def test_html_tombstone_is_410(self, client, database, record):
        await database["records"].update_one(
            {"igsn": record["igsn"]}, {"$set": {"status": "revoked"}}
        )
        response = await client.get(f"/{record['igsn']}", headers={"accept": BROWSER_ACCEPT})
        assert response.status_code == 410
        assert "withdrawn" in response.text
        assert "never be reassigned" in response.text

    async def test_json_tombstone_is_410(self, client, database, record):
        await database["records"].update_one(
            {"igsn": record["igsn"]}, {"$set": {"status": "revoked"}}
        )
        response = await client.get(
            f"/{record['igsn']}", headers={"accept": "application/json"}
        )
        assert response.status_code == 410
        assert response.json()["status"] == "revoked"


class TestNoAuthRequired:
    async def test_resolver_is_public(self, client, record):
        """No Authorization header at all -- a DOI must resolve for anyone."""
        response = await client.get(f"/{record['igsn']}", headers={"accept": "application/json"})
        assert response.status_code == 200
