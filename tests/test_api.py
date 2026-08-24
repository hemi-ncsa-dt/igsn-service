"""HTTP surface: auth, tenant scoping, allocation, publish, admin."""

import httpx
import pytest
import respx

from app.services import allocator


class TestAuth:
    async def test_no_token_is_401(self, client):
        response = await client.post("/api/v1/identifiers", json={"prefix": "JHAMAA"})
        assert response.status_code == 401

    async def test_bad_token_is_401(self, client):
        response = await client.get(
            "/api/v1/whoami", headers={"Authorization": "Bearer nope"}
        )
        assert response.status_code == 401

    async def test_non_bearer_scheme_is_401(self, client, tenant):
        response = await client.get(
            "/api/v1/whoami", headers={"Authorization": f"Basic {tenant['_token']}"}
        )
        assert response.status_code == 401

    async def test_inactive_tenant_is_401(self, client, database, tenant, auth):
        await database["tenants"].update_one(
            {"slug": tenant["slug"]}, {"$set": {"active": False}}
        )
        assert (await client.get("/api/v1/whoami", headers=auth)).status_code == 401

    async def test_whoami(self, client, auth):
        response = await client.get("/api/v1/whoami", headers=auth)
        assert response.status_code == 200
        assert response.json() == {
            "slug": "test-tenant",
            "name": "Test Tenant",
            "allowed_institutions": ["JH", "NW"],
            "doi_prefix": "10.83961",
        }


class TestAllocateEndpoint:
    async def test_allocate_one(self, client, auth, metadata):
        response = await client.post(
            "/api/v1/identifiers",
            headers=auth,
            json={"prefix": "JHAMAA", "metadata": metadata},
        )
        assert response.status_code == 201
        records = response.json()["records"]
        assert len(records) == 1
        assert records[0]["igsn"] == "JHAMAA00001"
        assert records[0]["doi"] == "10.83961/jhamaa00001"
        assert records[0]["status"] == "reserved"
        assert records[0]["resolver_url"] == "https://igsn.example.org/JHAMAA00001"

    async def test_allocate_many(self, client, auth, metadata):
        response = await client.post(
            "/api/v1/identifiers",
            headers=auth,
            json={"prefix": "JHAMAA", "count": 4, "metadata": metadata},
        )
        assert [r["igsn"] for r in response.json()["records"]] == [
            f"JHAMAA{n:05d}" for n in range(1, 5)
        ]

    async def test_external_ref_round_trips(self, client, auth, metadata):
        response = await client.post(
            "/api/v1/identifiers",
            headers=auth,
            json={
                "prefix": "JHAMAA",
                "metadata": metadata,
                "external_ref": {
                    "system": "girder",
                    "instance": "data.htmdec.org",
                    "id": "66b1c0ffee",
                },
            },
        )
        assert response.json()["records"][0]["external_ref"]["id"] == "66b1c0ffee"

    async def test_bad_prefix_is_400(self, client, auth):
        response = await client.post(
            "/api/v1/identifiers", headers=auth, json={"prefix": "ZZZZZZ"}
        )
        assert response.status_code == 400
        assert "Invalid institution" in response.json()["detail"]

    async def test_forbidden_institution_is_400(self, client, auth):
        response = await client.post(
            "/api/v1/identifiers", headers=auth, json={"prefix": "TMAMAA"}
        )
        assert response.status_code == 400
        assert "not permitted" in response.json()["detail"]

    async def test_bad_metadata_is_422(self, client, auth):
        response = await client.post(
            "/api/v1/identifiers",
            headers=auth,
            json={"prefix": "JHAMAA", "metadata": {"titles": "wrong"}},
        )
        assert response.status_code == 422


class TestChildrenEndpoint:
    @pytest.fixture
    async def parent(self, client, auth, metadata):
        response = await client.post(
            "/api/v1/identifiers",
            headers=auth,
            json={"prefix": "JHAMAA", "metadata": metadata},
        )
        return response.json()["records"][0]

    async def test_explicit_indices(self, client, auth, parent):
        response = await client.post(
            f"/api/v1/identifiers/{parent['igsn']}/children",
            headers=auth,
            json={"indices": ["001", "002"]},
        )
        assert response.status_code == 201
        assert [r["igsn"] for r in response.json()["records"]] == [
            "JHAMAA00001-001",
            "JHAMAA00001-002",
        ]

    async def test_count(self, client, auth, parent):
        response = await client.post(
            f"/api/v1/identifiers/{parent['igsn']}/children",
            headers=auth,
            json={"count": 2},
        )
        assert [r["igsn"] for r in response.json()["records"]] == [
            "JHAMAA00001-001",
            "JHAMAA00001-002",
        ]

    async def test_both_indices_and_count_is_422(self, client, auth, parent):
        response = await client.post(
            f"/api/v1/identifiers/{parent['igsn']}/children",
            headers=auth,
            json={"count": 2, "indices": ["001"]},
        )
        assert response.status_code == 422

    async def test_neither_is_422(self, client, auth, parent):
        response = await client.post(
            f"/api/v1/identifiers/{parent['igsn']}/children", headers=auth, json={}
        )
        assert response.status_code == 422

    async def test_duplicate_is_409(self, client, auth, parent):
        await client.post(
            f"/api/v1/identifiers/{parent['igsn']}/children",
            headers=auth,
            json={"indices": ["001"]},
        )
        response = await client.post(
            f"/api/v1/identifiers/{parent['igsn']}/children",
            headers=auth,
            json={"indices": ["001"]},
        )
        assert response.status_code == 409

    async def test_unknown_parent_is_409(self, client, auth):
        response = await client.post(
            "/api/v1/identifiers/JHAMAA99999/children",
            headers=auth,
            json={"indices": ["001"]},
        )
        assert response.status_code == 409


class TestRecordEndpoints:
    @pytest.fixture
    async def record(self, database, tenant, metadata):
        docs = await allocator.allocate("JHAMAA", 1, tenant, metadata=metadata)
        return docs[0]

    async def test_get(self, client, auth, record):
        response = await client.get(f"/api/v1/records/{record['igsn']}", headers=auth)
        assert response.status_code == 200
        assert response.json()["igsn"] == record["igsn"]

    async def test_get_is_case_insensitive(self, client, auth, record):
        response = await client.get(
            f"/api/v1/records/{record['igsn'].lower()}", headers=auth
        )
        assert response.status_code == 200

    async def test_unknown_is_404(self, client, auth):
        assert (
            await client.get("/api/v1/records/JHAMAA99999", headers=auth)
        ).status_code == 404

    async def test_other_tenants_record_is_404_not_403(
        self, client, database, other_tenant, record
    ):
        """A tenant must not be able to probe another tenant's namespace."""
        headers = {"Authorization": f"Bearer {other_tenant['_token']}"}
        response = await client.get(f"/api/v1/records/{record['igsn']}", headers=headers)
        assert response.status_code == 404

    async def test_list_is_tenant_scoped(self, client, database, tenant, other_tenant, record):
        await allocator.allocate("TMAMAA", 1, other_tenant, metadata={})
        response = await client.get(
            "/api/v1/records", headers={"Authorization": f"Bearer {tenant['_token']}"}
        )
        assert [r["igsn"] for r in response.json()] == ["JHAMAA00001"]

    async def test_list_filters(self, client, auth, database, tenant, metadata, record):
        await allocator.allocate_children(record["igsn"], tenant, indices=["001"])
        response = await client.get(
            "/api/v1/records", headers=auth, params={"parent": record["igsn"]}
        )
        assert [r["igsn"] for r in response.json()] == ["JHAMAA00001-001"]

        response = await client.get(
            "/api/v1/records", headers=auth, params={"status": "findable"}
        )
        assert response.json() == []

    async def test_update_metadata(self, client, auth, record, metadata):
        metadata["titles"] = [{"title": "Renamed"}]
        response = await client.put(
            f"/api/v1/records/{record['igsn']}", headers=auth, json={"metadata": metadata}
        )
        assert response.status_code == 200
        assert response.json()["metadata"]["titles"] == [{"title": "Renamed"}]
        # doi is re-asserted from the record, not taken from the client.
        assert response.json()["metadata"]["doi"] == record["doi"]

    async def test_update_landing_page(self, client, auth, record):
        response = await client.put(
            f"/api/v1/records/{record['igsn']}",
            headers=auth,
            json={"landing_page": "https://data.htmdec.org/#igsn/JHAMAA00001"},
        )
        assert response.json()["landing_page"] == "https://data.htmdec.org/#igsn/JHAMAA00001"

    async def test_update_bad_metadata_is_422(self, client, auth, record):
        response = await client.put(
            f"/api/v1/records/{record['igsn']}",
            headers=auth,
            json={"metadata": {"titles": "wrong"}},
        )
        assert response.status_code == 422

    async def test_updating_published_record_queues_a_datacite_update(
        self, client, auth, database, record, metadata
    ):
        await database["records"].update_one(
            {"igsn": record["igsn"]}, {"$set": {"status": "findable"}}
        )
        metadata["titles"] = [{"title": "Corrected"}]
        await client.put(
            f"/api/v1/records/{record['igsn']}", headers=auth, json={"metadata": metadata}
        )
        queued = await database["publication_attempts"].find_one({"igsn": record["igsn"]})
        assert queued is not None
        assert queued["target"] == "findable"


class TestPublishEndpoint:
    @pytest.fixture
    async def record(self, database, tenant, metadata):
        docs = await allocator.allocate("JHAMAA", 1, tenant, metadata=metadata)
        return docs[0]

    async def test_publish_queues(self, client, auth, record, database):
        response = await client.post(
            f"/api/v1/records/{record['igsn']}/publish", headers=auth, json={}
        )
        assert response.status_code == 200
        assert response.json()["queued"] == 1
        # Queued, not yet published: the caller polls for the outcome.
        stored = await database["records"].find_one({"igsn": record["igsn"]})
        assert stored["status"] == "reserved"

    async def test_publish_recurse(self, client, auth, tenant, record):
        await allocator.allocate_children(record["igsn"], tenant, indices=["001", "002"])
        response = await client.post(
            f"/api/v1/records/{record['igsn']}/publish",
            headers=auth,
            json={"recurse": True},
        )
        assert response.json()["queued"] == 3

    async def test_publish_without_metadata_is_422(self, client, auth, database, tenant):
        docs = await allocator.allocate("JHAMAA", 1, tenant)
        response = await client.post(
            f"/api/v1/records/{docs[0]['igsn']}/publish", headers=auth, json={}
        )
        assert response.status_code == 422

    @respx.mock
    async def test_end_to_end_publish(self, client, auth, database, record):
        """Queue via HTTP, drain the queue, and see the record become findable."""
        from app.services import publisher

        respx.post("https://api.test.datacite.org/dois").mock(
            return_value=httpx.Response(201, json={})
        )
        await client.post(f"/api/v1/records/{record['igsn']}/publish", headers=auth, json={})
        await publisher.drain_once(database)

        response = await client.get(f"/api/v1/records/{record['igsn']}", headers=auth)
        assert response.json()["status"] == "findable"


class TestRevoke:
    @pytest.fixture
    async def record(self, database, tenant, metadata):
        docs = await allocator.allocate("JHAMAA", 1, tenant, metadata=metadata)
        return docs[0]

    async def test_revoke_reserved(self, client, auth, record):
        response = await client.post(
            f"/api/v1/records/{record['igsn']}/revoke", headers=auth
        )
        assert response.status_code == 200
        assert response.json()["status"] == "revoked"

    async def test_revoke_does_not_free_the_number(self, client, auth, database, tenant, record):
        await client.post(f"/api/v1/records/{record['igsn']}/revoke", headers=auth)
        docs = await allocator.allocate("JHAMAA", 1, tenant, metadata={})
        assert docs[0]["igsn"] == "JHAMAA00002", "a revoked IGSN must never be reissued"

    async def test_cannot_revoke_published(self, client, auth, database, record):
        await database["records"].update_one(
            {"igsn": record["igsn"]}, {"$set": {"status": "findable"}}
        )
        response = await client.post(
            f"/api/v1/records/{record['igsn']}/revoke", headers=auth
        )
        assert response.status_code == 409


class TestVocabularies:
    async def test_read(self, client, auth):
        response = await client.get("/api/v1/vocabularies", headers=auth)
        assert response.status_code == 200
        body = response.json()
        # Seeded from girder-jsonforms' own defaults, so validation agrees on day one.
        assert "JH" in body["institutions"]
        assert "MA" in body["materials"]
        assert response.headers["ETag"]

    async def test_validate_prefix(self, client, auth):
        response = await client.get(
            "/api/v1/prefixes/validate", headers=auth, params={"prefix": "JHAMAL"}
        )
        assert response.json() == {
            "prefix": "JHAMAL",
            "institution": "Johns Hopkins University",
            "lab": "Hopkins Extreme Materials Institute",
            "material": "metals and alloys",
            "submaterial": "Ti-containing",
        }

    async def test_validate_consumes_no_sequence(self, client, auth, database):
        await client.get(
            "/api/v1/prefixes/validate", headers=auth, params={"prefix": "JHAMAL"}
        )
        assert await database["counters"].count_documents({}) == 0

    async def test_validate_rejects_bad_prefix(self, client, auth):
        response = await client.get(
            "/api/v1/prefixes/validate", headers=auth, params={"prefix": "JHZMAL"}
        )
        assert response.status_code == 400


class TestAdmin:
    async def test_requires_admin_token(self, client, auth):
        assert (await client.get("/api/v1/admin/tenants", headers=auth)).status_code == 401

    async def test_create_tenant_returns_token_once(self, client, admin_auth, database):
        response = await client.post(
            "/api/v1/admin/tenants",
            headers=admin_auth,
            json={
                "slug": "new-tenant",
                "name": "New Tenant",
                "allowed_institutions": ["CM"],
                "datacite": {
                    "repository_id": "cmu.igsn",
                    "doi_prefix": "10.12345",
                    "password_env": "DATACITE_PW_CMU",
                },
            },
        )
        assert response.status_code == 201
        token = response.json()["token"]
        assert token

        # The plaintext is never stored.
        stored = await database["tenants"].find_one({"slug": "new-tenant"})
        assert "token" not in stored
        assert stored["token_sha256"] != token

        # And it works.
        whoami = await client.get(
            "/api/v1/whoami", headers={"Authorization": f"Bearer {token}"}
        )
        assert whoami.json()["slug"] == "new-tenant"

    async def test_duplicate_tenant_is_409(self, client, admin_auth, tenant):
        response = await client.post(
            "/api/v1/admin/tenants",
            headers=admin_auth,
            json={
                "slug": tenant["slug"],
                "name": "dup",
                "datacite": {
                    "repository_id": "x",
                    "doi_prefix": "10.1",
                    "password_env": "X",
                },
            },
        )
        assert response.status_code == 409

    async def test_rotate_token_invalidates_the_old_one(self, client, admin_auth, tenant):
        old = {"Authorization": f"Bearer {tenant['_token']}"}
        response = await client.post(
            f"/api/v1/admin/tenants/{tenant['slug']}/rotate-token", headers=admin_auth
        )
        new_token = response.json()["token"]
        assert (await client.get("/api/v1/whoami", headers=old)).status_code == 401
        assert (
            await client.get(
                "/api/v1/whoami", headers={"Authorization": f"Bearer {new_token}"}
            )
        ).status_code == 200

    async def test_counters_report_drift(self, client, admin_auth, database, tenant, metadata):
        await allocator.allocate("JHAMAA", 3, tenant, metadata=metadata)
        response = await client.get("/api/v1/admin/counters", headers=admin_auth)
        assert response.json() == [
            {"prefix": "JHAMAA", "seq": 3, "highest_record": 3, "behind": False}
        ]

        await database["counters"].update_one({"_id": "JHAMAA"}, {"$set": {"seq": 1}})
        response = await client.get("/api/v1/admin/counters", headers=admin_auth)
        assert response.json()[0]["behind"] is True

    async def test_seed_counter_refuses_to_lower(self, client, admin_auth, tenant, metadata):
        """Lowering a counter is how you mint a duplicate. The API says no."""
        await allocator.allocate("JHAMAA", 5, tenant, metadata=metadata)
        response = await client.put(
            "/api/v1/admin/counters/JHAMAA", headers=admin_auth, params={"seq": 2}
        )
        assert response.status_code == 409
        assert "refusing to lower" in response.json()["detail"]

    async def test_seed_counter_raises(self, client, admin_auth, database):
        response = await client.put(
            "/api/v1/admin/counters/JHAMAA", headers=admin_auth, params={"seq": 900}
        )
        assert response.status_code == 200
        counter = await database["counters"].find_one({"_id": "JHAMAA"})
        assert counter["seq"] == 900


class TestAttemptsEndpoint:
    """Debugging a failed publish without needing database access."""

    @pytest.fixture
    async def record(self, database, tenant, metadata):
        docs = await allocator.allocate("JHAMAA", 1, tenant, metadata=metadata)
        return docs[0]

    async def test_empty_before_any_publish(self, client, auth, record):
        response = await client.get(
            f"/api/v1/records/{record['igsn']}/attempts", headers=auth
        )
        assert response.status_code == 200
        assert response.json() == []

    @respx.mock
    async def test_failed_attempt_explains_itself(self, client, auth, database, record):
        """The whole point: a 422 traceable to the value that caused it."""
        from app.services import publisher

        respx.post("https://api.test.datacite.org/dois").mock(
            return_value=httpx.Response(
                422,
                text='{"errors":[{"source":"url","title":"URL is not valid"}]}',
            )
        )
        await client.post(f"/api/v1/records/{record['igsn']}/publish", headers=auth, json={})
        await publisher.drain_once(database)

        response = await client.get(
            f"/api/v1/records/{record['igsn']}/attempts", headers=auth
        )
        assert response.status_code == 200
        attempt = response.json()[0]
        assert attempt["status"] == "failed"
        assert "URL is not valid" in attempt["last_error"]
        assert attempt["last_request"]["url"] == f"https://igsn.example.org/{record['igsn']}"
        # And the message names what we sent, which DataCite's does not.
        assert "we sent url=" in attempt["last_error"]

    @respx.mock
    async def test_record_surfaces_the_last_request(self, client, auth, database, record):
        from app.services import publisher

        respx.post("https://api.test.datacite.org/dois").mock(
            return_value=httpx.Response(422, text='{"errors":[{"source":"url"}]}')
        )
        await client.post(f"/api/v1/records/{record['igsn']}/publish", headers=auth, json={})
        await publisher.drain_once(database)

        response = await client.get(f"/api/v1/records/{record['igsn']}", headers=auth)
        datacite_state = response.json()["datacite"]
        assert datacite_state["last_request"]["method"] == "POST"
        assert datacite_state["attempts"] == 1

    async def test_other_tenants_record_is_404(self, client, other_tenant, record):
        headers = {"Authorization": f"Bearer {other_tenant['_token']}"}
        response = await client.get(
            f"/api/v1/records/{record['igsn']}/attempts", headers=headers
        )
        assert response.status_code == 404

    async def test_requires_auth(self, client, record):
        response = await client.get(f"/api/v1/records/{record['igsn']}/attempts")
        assert response.status_code == 401


class TestLandingPageValidation:
    """Reject an unusable landing page at the door, not at publish time."""

    @pytest.fixture
    async def record(self, database, tenant, metadata):
        docs = await allocator.allocate("JHAMAA", 1, tenant, metadata=metadata)
        return docs[0]

    async def test_put_rejects_a_bare_fragment(self, client, auth, record):
        """This is exactly what Girder sent when its server_root was unset."""
        response = await client.put(
            f"/api/v1/records/{record['igsn']}",
            headers=auth,
            json={"landing_page": "#igsn/JHAMAA00001"},
        )
        assert response.status_code == 422
        assert "#igsn/JHAMAA00001" in response.json()["detail"]
        assert "server-root" in response.json()["detail"]

    async def test_put_rejects_localhost(self, client, auth, record):
        response = await client.put(
            f"/api/v1/records/{record['igsn']}",
            headers=auth,
            json={"landing_page": "http://localhost:8080/x"},
        )
        assert response.status_code == 422

    async def test_the_bad_value_is_not_stored(self, client, auth, database, record):
        await client.put(
            f"/api/v1/records/{record['igsn']}",
            headers=auth,
            json={"landing_page": "#igsn/JHAMAA00001"},
        )
        stored = await database["records"].find_one({"igsn": record["igsn"]})
        assert stored["landing_page"] != "#igsn/JHAMAA00001"

    async def test_put_accepts_a_real_url(self, client, auth, record):
        response = await client.put(
            f"/api/v1/records/{record['igsn']}",
            headers=auth,
            json={"landing_page": "https://data.htmdec.org/#igsn/JHAMAA00001"},
        )
        assert response.status_code == 200
        assert response.json()["landing_page"] == (
            "https://data.htmdec.org/#igsn/JHAMAA00001"
        )

    async def test_allocate_rejects_a_bad_landing_page(self, client, auth, metadata):
        response = await client.post(
            "/api/v1/identifiers",
            headers=auth,
            json={
                "prefix": "JHAMAA",
                "metadata": metadata,
                "landing_page": "#igsn/whatever",
            },
        )
        assert response.status_code == 422

    async def test_a_bad_tenant_template_is_caught_at_allocation(
        self, client, database, tenant, metadata
    ):
        """A misconfigured template should fail loudly on the first mint."""
        await database["tenants"].update_one(
            {"slug": tenant["slug"]}, {"$set": {"landing_template": "#igsn/{igsn}"}}
        )
        response = await client.post(
            "/api/v1/identifiers",
            headers={"Authorization": f"Bearer {tenant['_token']}"},
            json={"prefix": "JHAMAA", "metadata": metadata},
        )
        assert response.status_code == 422
        assert "#igsn/JHAMAA" in response.json()["detail"]
