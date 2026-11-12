import sqlite3
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from src.integrations.azure_devops import vote_label, AzureDevOpsClient
from src.utils.config import Config


@pytest.fixture
def admin_client(api_app, database):
    with sqlite3.connect(database.db_path) as conn:
        conn.execute("INSERT INTO admin_users (email) VALUES ('owner@example.com')")
    with TestClient(api_app) as client:
        yield client


@pytest.fixture
def configured_ado(monkeypatch):
    monkeypatch.setattr(Config, "AZURE_DEVOPS_ORG_URL", "https://dev.azure.com/example-org")
    monkeypatch.setattr(Config, "AZURE_DEVOPS_PAT", "fake-pat")
    monkeypatch.setattr(Config, "AZURE_DEVOPS_ENABLED", True)


def test_sync_endpoint_requires_admin(api_app, configured_ado):
    with TestClient(api_app) as client:
        assert client.post("/api/integrations/ado/sync").status_code == 403


def test_sync_endpoint_not_configured(admin_client):
    # AZURE_DEVOPS_ORG_URL/PAT are empty by default in the test environment.
    response = admin_client.post("/api/integrations/ado/sync")
    assert response.status_code == 400
    assert "not configured" in response.json()["error"]


def test_mapping_endpoints_no_longer_exist(admin_client):
    """Regression test: the manual working-directory -> project/repository
    mapping feature was removed in favor of auto-discovering the entire
    Azure DevOps org, so none of its endpoints should be reachable."""
    assert admin_client.get("/api/integrations/ado/mappings").status_code == 404
    assert admin_client.post("/api/integrations/ado/mappings", json={}).status_code == 404
    assert admin_client.delete("/api/integrations/ado/mappings/1").status_code == 404
    assert admin_client.get("/api/integrations/ado/projects").status_code == 404
    assert admin_client.get("/api/integrations/ado/repositories?project=Foo").status_code == 404


def test_git_pull_requests_page_is_not_admin_gated(api_app, configured_ado):
    """Regression test: unlike /integrations/azure-devops (admin-only org
    control plane), the user-facing Git Pull Requests page under Monitor
    must be reachable by any authenticated user -- auth is disabled in
    tests, so request.state.is_admin is forced True either way, but this
    guards against ever adding an is_admin gate to this specific route."""
    with TestClient(api_app) as client:
        assert client.get("/git-pull-requests").status_code == 200


@pytest.mark.asyncio
async def test_sync_auto_discovers_every_project_and_repository(database, monkeypatch, configured_ado):
    """Regression test: sync must read the entire org directly from the
    Azure DevOps API (list every project, then every repository in each
    project) instead of relying on any admin-configured mapping."""
    from src.integrations import azure_devops as ado_module

    async def fake_list_projects(self):
        return [{"id": "p1", "name": "ProjectA"}, {"id": "p2", "name": "ProjectB"}]

    async def fake_list_repositories(self, project):
        return {
            "ProjectA": [{"id": "r1", "name": "repo-one"}],
            "ProjectB": [{"id": "r2", "name": "repo-two"}],
        }[project]

    async def fake_list_pull_requests(self, project, repository, status="all", top=200):
        return [{
            "pullRequestId": 101, "title": f"PR in {repository}",
            "createdBy": {"displayName": "Alice"}, "status": "completed",
            "creationDate": "2026-10-01T00:00:00Z", "closedDate": "2026-10-01T01:00:00Z",
            "sourceRefName": "refs/heads/feature", "targetRefName": "refs/heads/main",
            "url": f"https://dev.azure.com/example-org/{project}/_git/{repository}/pullrequest/101",
        }]

    async def fake_list_pr_commits(self, project, repository, pull_request_id):
        return [{
            "commitId": "abc123", "author": {"email": "alice@example.com", "date": "2026-10-01T00:30:00Z"},
            "comment": "fix bug",
        }]

    monkeypatch.setattr(ado_module.AzureDevOpsClient, "list_projects", fake_list_projects)
    monkeypatch.setattr(ado_module.AzureDevOpsClient, "list_repositories", fake_list_repositories)
    monkeypatch.setattr(ado_module.AzureDevOpsClient, "list_pull_requests", fake_list_pull_requests)
    monkeypatch.setattr(ado_module.AzureDevOpsClient, "list_pr_commits", fake_list_pr_commits)

    summary = await ado_module.sync_azure_devops(database)

    assert summary["repos_synced"] == 2
    assert summary["prs_synced"] == 2
    assert summary["commits_synced"] == 2
    assert summary["errors"] == []

    prs = await database.get_ado_pull_requests()
    assert {(p["ado_project"], p["ado_repository"]) for p in prs} == {
        ("ProjectA", "repo-one"), ("ProjectB", "repo-two"),
    }


@pytest.mark.asyncio
async def test_sync_surfaces_upstream_project_listing_errors(database, monkeypatch, configured_ado):
    from src.integrations import azure_devops as ado_module
    from src.integrations.azure_devops import AzureDevOpsError

    async def fake_list_projects(self):
        raise httpx.HTTPStatusError("boom", request=httpx.Request("GET", "https://example"),
                                     response=httpx.Response(401))

    monkeypatch.setattr(ado_module.AzureDevOpsClient, "list_projects", fake_list_projects)

    with pytest.raises(AzureDevOpsError):
        await ado_module.sync_azure_devops(database)


@pytest.mark.parametrize("identity,expected", [
    ("Jane Doe <jane.doe@example.com>", "jane.doe"),
    ("jane.doe@example-corp.com", "jane.doe"),
    ("Jane.Doe@example-corp.com", "jane.doe"),
    ("john.smith", "john.smith"),
    (None, None),
    ("", None),
])
def test_identity_local_part_extraction(identity, expected):
    """Regression test for a live-production bug where 0% of sessions were
    ever linked to PRs: logged_in_user is stored as 'Name <email>' (and the
    login email's domain, e.g. example.com, commonly differs from the git
    commit author email's domain, e.g. example-corp.com) -- the naive
    `logged_in_user.split('@')[0]` used to produce garbage like
    'Jane Doe <jane.doe' instead of 'jane.doe'."""
    from src.database import Database

    assert Database._identity_local_part(identity) == expected


@pytest.mark.asyncio
async def test_link_sessions_to_prs_matches_by_user_and_time_no_working_directory(database):
    """Regression test: linking no longer depends on a session's
    working_directory or any repo mapping -- it matches purely on the
    session's logged-in user against commit author emails within the
    session's time window (+/- buffer), across every synced repo."""
    now = datetime(2026, 10, 1, 12, 0, 0)
    with sqlite3.connect(database.db_path) as conn:
        conn.execute(
            """INSERT INTO machines (machine_id, machine_name, logged_in_user, first_seen, last_seen)
               VALUES ('m1', 'Macbook', 'Alice Smith <alice.smith@example.com>', ?, ?)""",
            (now.isoformat(), now.isoformat()),
        )
        # No working_directory at all -- must still be linkable. Note the
        # commit's author_email domain (example-corp.com) deliberately
        # differs from the logged-in user's login email domain
        # (example.com) -- only the local part ("alice.smith") should need
        # to match.
        conn.execute(
            """INSERT INTO sessions (session_id, machine_id, start_time, end_time, working_directory)
               VALUES ('sess-1', 'm1', ?, ?, NULL)""",
            (now.isoformat(), (now + timedelta(minutes=30)).isoformat()),
        )
        conn.execute(
            """INSERT INTO ado_pull_requests
               (ado_pr_id, ado_project, ado_repository, title, author, status, created_date)
               VALUES (101, 'ProjectA', 'repo-one', 'Fix bug', 'Alice', 'completed', ?)""",
            (now.isoformat(),),
        )
        pr_row_id = conn.execute(
            "SELECT id FROM ado_pull_requests WHERE ado_pr_id=101"
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO ado_pr_commits (pr_row_id, commit_sha, author_email, commit_date, comment)
               VALUES (?, 'abc123', 'Alice.Smith@example-corp.com', ?, 'fix bug')""",
            (pr_row_id, (now + timedelta(minutes=15)).isoformat()),
        )
        conn.commit()

    created = await database.link_sessions_to_prs()
    assert created == 1

    prs = await database.get_ado_pull_requests()
    assert prs[0]["linked_sessions"] == 1
    assert prs[0]["total_commits"] == 1
    assert prs[0]["ai_commits"] == 1
    assert prs[0]["ai_commit_pct"] == 100.0
    assert prs[0]["is_ai_assisted"] is True


@pytest.mark.asyncio
async def test_ai_commit_pct_is_ratio_of_matched_to_total_commits(database):
    """A PR's AI % should reflect commits-in-an-AI-session / total commits
    in that PR, not just a yes/no flag -- e.g. 1 of 4 commits matched
    should report 25%, not 100%."""
    now = datetime(2026, 10, 1, 12, 0, 0)
    with sqlite3.connect(database.db_path) as conn:
        conn.execute(
            """INSERT INTO machines (machine_id, machine_name, logged_in_user, first_seen, last_seen)
               VALUES ('m1', 'Macbook', 'alice.smith@example.com', ?, ?)""",
            (now.isoformat(), now.isoformat()),
        )
        conn.execute(
            """INSERT INTO sessions (session_id, machine_id, start_time, end_time)
               VALUES ('sess-1', 'm1', ?, ?)""",
            (now.isoformat(), (now + timedelta(minutes=30)).isoformat()),
        )
        conn.execute(
            """INSERT INTO ado_pull_requests
               (ado_pr_id, ado_project, ado_repository, title, author, status, created_date)
               VALUES (101, 'ProjectA', 'repo-one', 'Fix bug', 'Alice', 'completed', ?)""",
            (now.isoformat(),),
        )
        pr_row_id = conn.execute(
            "SELECT id FROM ado_pull_requests WHERE ado_pr_id=101"
        ).fetchone()[0]
        # 1 commit inside the session window (matches), 3 commits far
        # outside any session window (do not match).
        conn.execute(
            """INSERT INTO ado_pr_commits (pr_row_id, commit_sha, author_email, commit_date, comment)
               VALUES (?, 'c1', 'alice.smith@example-corp.com', ?, 'm1')""",
            (pr_row_id, (now + timedelta(minutes=15)).isoformat()),
        )
        for i, sha in enumerate(["c2", "c3", "c4"]):
            conn.execute(
                """INSERT INTO ado_pr_commits (pr_row_id, commit_sha, author_email, commit_date, comment)
                   VALUES (?, ?, 'alice.smith@example-corp.com', ?, ?)""",
                (pr_row_id, sha, (now - timedelta(days=10 + i)).isoformat(), sha),
            )
        conn.commit()

    await database.link_sessions_to_prs()

    prs = await database.get_ado_pull_requests()
    assert prs[0]["total_commits"] == 4
    assert prs[0]["ai_commits"] == 1
    assert prs[0]["ai_commit_pct"] == 25.0
    assert prs[0]["is_ai_assisted"] is True


@pytest.mark.asyncio
async def test_ai_only_filter_excludes_non_ai_assisted_prs(database):
    now = datetime(2026, 10, 1, 12, 0, 0)
    with sqlite3.connect(database.db_path) as conn:
        conn.execute(
            """INSERT INTO ado_pull_requests
               (ado_pr_id, ado_project, ado_repository, title, author, status, created_date)
               VALUES (101, 'ProjectA', 'repo-one', 'No AI here', 'Bob', 'completed', ?)""",
            (now.isoformat(),),
        )
        conn.commit()

    all_prs = await database.get_ado_pull_requests()
    assert len(all_prs) == 1
    assert all_prs[0]["is_ai_assisted"] is False

    ai_only_prs = await database.get_ado_pull_requests(ai_only=True)
    assert ai_only_prs == []


@pytest.mark.asyncio
async def test_get_ado_pull_requests_supports_pagination(database):
    now = datetime(2026, 10, 1, 12, 0, 0)
    with sqlite3.connect(database.db_path) as conn:
        for i in range(5):
            conn.execute(
                """INSERT INTO ado_pull_requests
                   (ado_pr_id, ado_project, ado_repository, title, author, status, created_date)
                   VALUES (?, 'ProjectA', 'repo-one', 'PR', 'Bob', 'completed', ?)""",
                (i, (now - timedelta(minutes=i)).isoformat()),
            )
        conn.commit()

    page1 = await database.get_ado_pull_requests(limit=2, offset=0)
    page2 = await database.get_ado_pull_requests(limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert {p["ado_pr_id"] for p in page1}.isdisjoint({p["ado_pr_id"] for p in page2})


@pytest.mark.asyncio
async def test_machine_id_scoping_restricts_prs_to_selected_users_commits(database):
    """Regression test for the user-facing Git Pull Requests page: passing
    machine_id must scope results down to only PRs with a commit authored
    by that machine's logged_in_user (matched by email local-part, exactly
    like session<->PR linking), not the entire org -- that remains the
    admin Azure DevOps page's job (machine_id=None)."""
    now = datetime(2026, 10, 1, 12, 0, 0)
    with sqlite3.connect(database.db_path) as conn:
        conn.execute(
            """INSERT INTO machines (machine_id, machine_name, logged_in_user, first_seen, last_seen)
               VALUES ('m1', 'Macbook', 'Alice Smith <alice.smith@example.com>', ?, ?)""",
            (now.isoformat(), now.isoformat()),
        )
        conn.execute(
            """INSERT INTO ado_pull_requests
               (ado_pr_id, ado_project, ado_repository, title, author, status, created_date)
               VALUES (101, 'ProjectA', 'repo-one', 'Alice PR', 'Alice', 'completed', ?)""",
            (now.isoformat(),),
        )
        conn.execute(
            """INSERT INTO ado_pull_requests
               (ado_pr_id, ado_project, ado_repository, title, author, status, created_date)
               VALUES (102, 'ProjectA', 'repo-one', 'Bob PR', 'Bob', 'completed', ?)""",
            (now.isoformat(),),
        )
        alice_pr_id = conn.execute("SELECT id FROM ado_pull_requests WHERE ado_pr_id=101").fetchone()[0]
        bob_pr_id = conn.execute("SELECT id FROM ado_pull_requests WHERE ado_pr_id=102").fetchone()[0]
        conn.execute(
            """INSERT INTO ado_pr_commits (pr_row_id, commit_sha, author_email, commit_date, comment)
               VALUES (?, 'c1', 'alice.smith@example-corp.com', ?, 'x')""",
            (alice_pr_id, now.isoformat()),
        )
        conn.execute(
            """INSERT INTO ado_pr_commits (pr_row_id, commit_sha, author_email, commit_date, comment)
               VALUES (?, 'c2', 'bob@example-corp.com', ?, 'x')""",
            (bob_pr_id, now.isoformat()),
        )
        conn.commit()

    unscoped = await database.get_ado_pull_requests()
    assert len(unscoped) == 2

    scoped = await database.get_ado_pull_requests(machine_id="m1")
    assert [p["ado_pr_id"] for p in scoped] == [101]

    stats_unscoped = await database.get_ai_assisted_pr_stats()
    assert stats_unscoped["total_prs"] == 2

    stats_scoped = await database.get_ai_assisted_pr_stats(machine_id="m1")
    assert stats_scoped["total_prs"] == 1

    # A machine with no known identity (or that doesn't exist) should
    # return nothing rather than silently falling back to the full org.
    assert await database.get_ado_pull_requests(machine_id="does-not-exist") == []
    no_stats = await database.get_ai_assisted_pr_stats(machine_id="does-not-exist")
    assert no_stats["total_prs"] == 0


@pytest.mark.asyncio
async def test_known_users_only_excludes_prs_with_no_claude_code_author(database):
    """Regression test: the user-facing Git Pull Requests page must never
    show the entire Azure DevOps org, even in its default/unscoped 'all
    users' view. Only PRs authored by a recognized Claude Code user
    (matched the same way as machine_id scoping, just across every known
    machine instead of one) should appear. The admin Azure DevOps page
    does not set known_users_only, so it keeps seeing everything."""
    now = datetime(2026, 10, 1, 12, 0, 0)
    with sqlite3.connect(database.db_path) as conn:
        conn.execute(
            """INSERT INTO machines (machine_id, machine_name, logged_in_user, first_seen, last_seen)
               VALUES ('m1', 'Macbook', 'Alice Smith <alice.smith@example.com>', ?, ?)""",
            (now.isoformat(), now.isoformat()),
        )
        conn.execute(
            """INSERT INTO ado_pull_requests
               (ado_pr_id, ado_project, ado_repository, title, author, status, created_date)
               VALUES (201, 'ProjectA', 'repo-one', 'Alice PR', 'Alice', 'completed', ?)""",
            (now.isoformat(),),
        )
        conn.execute(
            """INSERT INTO ado_pull_requests
               (ado_pr_id, ado_project, ado_repository, title, author, status, created_date)
               VALUES (202, 'ProjectA', 'repo-one', 'Stranger PR', 'Someone Else', 'completed', ?)""",
            (now.isoformat(),),
        )
        alice_pr_id = conn.execute("SELECT id FROM ado_pull_requests WHERE ado_pr_id=201").fetchone()[0]
        stranger_pr_id = conn.execute("SELECT id FROM ado_pull_requests WHERE ado_pr_id=202").fetchone()[0]
        conn.execute(
            """INSERT INTO ado_pr_commits (pr_row_id, commit_sha, author_email, commit_date, comment)
               VALUES (?, 'c1', 'alice.smith@example-corp.com', ?, 'x')""",
            (alice_pr_id, now.isoformat()),
        )
        conn.execute(
            """INSERT INTO ado_pr_commits (pr_row_id, commit_sha, author_email, commit_date, comment)
               VALUES (?, 'c2', 'someone.else@example-corp.com', ?, 'x')""",
            (stranger_pr_id, now.isoformat()),
        )
        conn.commit()

    # Admin view (no known_users_only): sees both PRs, including the one
    # with no Claude Code author at all.
    admin_view = await database.get_ado_pull_requests()
    assert {p["ado_pr_id"] for p in admin_view} == {201, 202}

    # User-facing default view: only the PR with a recognized author.
    scoped_view = await database.get_ado_pull_requests(known_users_only=True)
    assert [p["ado_pr_id"] for p in scoped_view] == [201]

    admin_stats = await database.get_ai_assisted_pr_stats()
    assert admin_stats["total_prs"] == 2

    scoped_stats = await database.get_ai_assisted_pr_stats(known_users_only=True)
    assert scoped_stats["total_prs"] == 1


@pytest.mark.asyncio
async def test_get_login_identities_resolves_machine_scope(database):
    now = datetime(2026, 10, 1, 12, 0, 0)
    with sqlite3.connect(database.db_path) as conn:
        conn.execute(
            """INSERT INTO machines (machine_id, machine_name, logged_in_user, first_seen, last_seen)
               VALUES ('m1', 'Macbook', 'Alice Smith <alice.smith@example.com>', ?, ?)""",
            (now.isoformat(), now.isoformat()),
        )
        conn.execute(
            """INSERT INTO machines (machine_id, machine_name, logged_in_user, first_seen, last_seen)
               VALUES ('m2', 'Workstation', 'bob', ?, ?)""",
            (now.isoformat(), now.isoformat()),
        )
        conn.commit()

    assert await database.get_login_identities(machine_id="m1") == ["alice.smith"]
    assert await database.get_login_identities(machine_id="all") == ["alice.smith", "bob"]
    assert await database.get_login_identities(machine_id=None) == ["alice.smith", "bob"]


@pytest.mark.asyncio
async def test_link_sessions_to_prs_does_not_match_different_author(database):
    now = datetime(2026, 10, 1, 12, 0, 0)
    with sqlite3.connect(database.db_path) as conn:
        conn.execute(
            """INSERT INTO machines (machine_id, machine_name, logged_in_user, first_seen, last_seen)
               VALUES ('m1', 'Macbook', 'bob', ?, ?)""",
            (now.isoformat(), now.isoformat()),
        )
        conn.execute(
            """INSERT INTO sessions (session_id, machine_id, start_time, end_time)
               VALUES ('sess-1', 'm1', ?, ?)""",
            (now.isoformat(), (now + timedelta(minutes=30)).isoformat()),
        )
        conn.execute(
            """INSERT INTO ado_pull_requests
               (ado_pr_id, ado_project, ado_repository, title, author, status, created_date)
               VALUES (101, 'ProjectA', 'repo-one', 'Fix bug', 'Alice', 'completed', ?)""",
            (now.isoformat(),),
        )
        pr_row_id = conn.execute(
            "SELECT id FROM ado_pull_requests WHERE ado_pr_id=101"
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO ado_pr_commits (pr_row_id, commit_sha, author_email, commit_date, comment)
               VALUES (?, 'abc123', 'alice@example.com', ?, 'fix bug')""",
            (pr_row_id, (now + timedelta(minutes=15)).isoformat()),
        )
        conn.commit()

    created = await database.link_sessions_to_prs()
    assert created == 0


@pytest.mark.asyncio
async def test_get_ado_pull_requests_supports_project_and_author_filters(database):
    """Regression test for the PR report filter UI on the admin Azure
    DevOps page: project (an exact 'Project/Repo' combo) and author
    (case-sensitive substring, matching SQLite LIKE) should each narrow
    the result set independently."""
    now = datetime(2026, 10, 1, 12, 0, 0)
    with sqlite3.connect(database.db_path) as conn:
        conn.execute(
            """INSERT INTO ado_pull_requests
               (ado_pr_id, ado_project, ado_repository, title, author, status, created_date)
               VALUES (101, 'ProjectA', 'repo-one', 'Alice PR', 'Alice Smith', 'completed', ?)""",
            (now.isoformat(),),
        )
        conn.execute(
            """INSERT INTO ado_pull_requests
               (ado_pr_id, ado_project, ado_repository, title, author, status, created_date)
               VALUES (102, 'ProjectB', 'repo-two', 'Bob PR', 'Bob Jones', 'completed', ?)""",
            (now.isoformat(),),
        )
        conn.commit()

    by_project = await database.get_ado_pull_requests(project="ProjectA/repo-one")
    assert [p["ado_pr_id"] for p in by_project] == [101]

    by_author = await database.get_ado_pull_requests(author="Jones")
    assert [p["ado_pr_id"] for p in by_author] == [102]

    no_match = await database.get_ado_pull_requests(project="ProjectA/repo-one", author="Jones")
    assert no_match == []

    all_prs = await database.get_ado_pull_requests(project="all")
    assert len(all_prs) == 2


@pytest.mark.asyncio
async def test_get_ado_projects_repos_returns_distinct_sorted_combos(database):
    now = datetime(2026, 10, 1, 12, 0, 0)
    with sqlite3.connect(database.db_path) as conn:
        for pr_id, project, repo in [(101, 'ProjectB', 'repo-two'), (102, 'ProjectA', 'repo-one'),
                                      (103, 'ProjectA', 'repo-one')]:
            conn.execute(
                """INSERT INTO ado_pull_requests
                   (ado_pr_id, ado_project, ado_repository, title, author, status, created_date)
                   VALUES (?, ?, ?, 'PR', 'Someone', 'completed', ?)""",
                (pr_id, project, repo, now.isoformat()),
            )
        conn.commit()

    combos = await database.get_ado_projects_repos()
    assert combos == ["ProjectA/repo-one", "ProjectB/repo-two"]


def test_ado_integration_page_hides_machine_topbar(admin_client):
    """Regression test: the admin ADO page is an org-wide control plane
    with no team/machine scoping, so the global machine/team selector
    topbar (meant for per-user/per-team scoped pages) must not render."""
    response = admin_client.get("/integrations/azure-devops")
    assert response.status_code == 200
    assert 'id="machineTopbar"' not in response.text


def test_ado_pr_projects_endpoint_returns_list(admin_client):
    response = admin_client.get("/api/integrations/ado/pr-projects")
    assert response.status_code == 200
    assert response.json() == {"projects": []}


def test_vote_label_mapping():
    assert vote_label(10) == "Approved"
    assert vote_label(5) == "Approved with suggestions"
    assert vote_label(0) == "No vote"
    assert vote_label(-5) == "Waiting for author"
    assert vote_label(-10) == "Rejected"
    assert vote_label(None) == "No vote"


@pytest.mark.asyncio
async def test_upsert_ado_pull_request_persists_closed_by_and_reviewers(database):
    """closedBy/reviewers are already present in the list-pull-requests
    response the sync loop fetches -- they should be stored, not
    discarded, so the PR detail report can show who completed the PR and
    its reviewer/approval state without any extra API calls."""
    pr_data = {
        "pullRequestId": 201,
        "title": "Add feature",
        "createdBy": {"displayName": "Alice Smith"},
        "status": "completed",
        "creationDate": "2026-10-01T12:00:00Z",
        "closedDate": "2026-10-02T12:00:00Z",
        "sourceRefName": "refs/heads/feature",
        "targetRefName": "refs/heads/main",
        "url": "https://example/pr/201",
        "closedBy": {"displayName": "Bob Jones"},
        "reviewers": [
            {"displayName": "Carol Lee", "vote": 10, "isRequired": True},
            {"displayName": "Dan Park", "vote": -5, "isRequired": False},
        ],
    }
    pr_row_id = await database.upsert_ado_pull_request("ProjectA", "repo-one", pr_data)
    detail = await database.get_ado_pr_detail(pr_row_id)
    assert detail["closed_by"] == "Bob Jones"
    assert detail["reviewers"] == [
        {"display_name": "Carol Lee", "vote": 10, "vote_label": "Approved", "is_required": True},
        {"display_name": "Dan Park", "vote": -5, "vote_label": "Waiting for author", "is_required": False},
    ]
    assert detail["time_to_complete_hours"] == 24.0


@pytest.mark.asyncio
async def test_get_ado_pr_detail_derives_co_authors_from_commits(database):
    """Co-authors are derived from distinct commit authors other than the
    PR's primary author -- no extra data source required."""
    now = datetime(2026, 10, 1, 12, 0, 0)
    with sqlite3.connect(database.db_path) as conn:
        conn.execute(
            """INSERT INTO ado_pull_requests
               (ado_pr_id, ado_project, ado_repository, title, author, status, created_date)
               VALUES (301, 'ProjectA', 'repo-one', 'Multi-author PR', 'alice.smith', 'completed', ?)""",
            (now.isoformat(),),
        )
        pr_row_id = conn.execute("SELECT id FROM ado_pull_requests WHERE ado_pr_id=301").fetchone()[0]
        conn.execute(
            """INSERT INTO ado_pr_commits (pr_row_id, commit_sha, author_email, commit_date, comment)
               VALUES (?, 'c1', 'alice.smith@example-corp.com', ?, 'first')""",
            (pr_row_id, now.isoformat()),
        )
        conn.execute(
            """INSERT INTO ado_pr_commits (pr_row_id, commit_sha, author_email, commit_date, comment)
               VALUES (?, 'c2', 'bob.jones@example-corp.com', ?, 'second')""",
            (pr_row_id, now.isoformat()),
        )
        conn.commit()

    detail = await database.get_ado_pr_detail(pr_row_id)
    assert detail["co_authors"] == ["bob.jones@example-corp.com"]


@pytest.mark.asyncio
async def test_get_ado_pr_detail_returns_none_for_missing_pr(database):
    assert await database.get_ado_pr_detail(9999) is None


def test_parse_pr_threads_extracts_feedback_and_vote_events():
    from src.api.main import _parse_pr_threads

    threads = [
        {
            "properties": {"CodeReviewVoteResult": {"$value": 10}},
            "comments": [{
                "author": {"displayName": "Carol Lee"},
                "publishedDate": "2026-10-01T15:00:00Z",
                "commentType": "system",
            }],
        },
        {
            "properties": {},
            "comments": [{
                "author": {"displayName": "Dan Park"},
                "publishedDate": "2026-10-01T16:00:00Z",
                "commentType": "text",
                "content": "Looks good, nice refactor.",
            }],
        },
        {
            "properties": {},
            "comments": [{
                "author": {"displayName": "System"},
                "publishedDate": "2026-10-01T16:05:00Z",
                "commentType": "system",
                "content": "",
            }],
        },
    ]
    result = _parse_pr_threads(threads)
    assert result["vote_events"] == [
        {"display_name": "Carol Lee", "vote": 10, "vote_label": "Approved", "voted_at": "2026-10-01T15:00:00Z"},
    ]
    assert result["feedback"] == [
        {"author": "Dan Park", "content": "Looks good, nice refactor.",
         "published_date": "2026-10-01T16:00:00Z", "is_deleted": False},
    ]
    assert result["first_approval_at"] == "2026-10-01T15:00:00Z"


def test_parse_pr_threads_handles_empty_input():
    from src.api.main import _parse_pr_threads

    result = _parse_pr_threads([])
    assert result == {"feedback": [], "vote_events": [], "first_approval_at": None}


@pytest.mark.asyncio
async def test_pr_details_endpoint_includes_people_and_timeline_data(admin_client, database, configured_ado, monkeypatch):
    now = datetime(2026, 10, 1, 12, 0, 0)
    pr_data = {
        "pullRequestId": 401,
        "title": "Detailed PR",
        "createdBy": {"displayName": "alice.smith"},
        "status": "completed",
        "creationDate": now.isoformat(),
        "closedDate": (now + timedelta(hours=5)).isoformat(),
        "sourceRefName": "refs/heads/feature",
        "targetRefName": "refs/heads/main",
        "url": "https://example/pr/401",
        "closedBy": {"displayName": "Bob Jones"},
        "reviewers": [{"displayName": "Carol Lee", "vote": 10, "isRequired": True}],
    }
    pr_row_id = await database.upsert_ado_pull_request("ProjectA", "repo-one", pr_data)
    await database.upsert_ado_pr_commit(pr_row_id, {
        "commitId": "c1",
        "author": {"email": "alice.smith@example-corp.com", "date": now.isoformat()},
        "comment": "first commit",
    })

    async def fake_get_commit_changes(self, project, repository, commit_id, top=500):
        return [{"path": "/src/api/main.py", "change_type": "edit"}]

    async def fake_list_pr_threads(self, project, repository, pull_request_id):
        return [{
            "properties": {"CodeReviewVoteResult": {"$value": 10}},
            "comments": [{
                "author": {"displayName": "Carol Lee"},
                "publishedDate": (now + timedelta(hours=1)).isoformat(),
                "commentType": "system",
            }],
        }]

    monkeypatch.setattr(AzureDevOpsClient, "get_commit_changes", fake_get_commit_changes)
    monkeypatch.setattr(AzureDevOpsClient, "list_pr_threads", fake_list_pr_threads)

    response = admin_client.get(f"/api/integrations/ado/pull-requests/{pr_row_id}/details")

    assert response.status_code == 200
    data = response.json()
    assert data["closed_by"] == "Bob Jones"
    assert data["reviewers"][0]["vote_label"] == "Approved"
    assert data["time_to_complete_hours"] == 5.0
    assert data["time_to_approve_hours"] == 1.0
    assert data["code_areas"][0]["area"] == "src/api"


def test_pr_details_endpoint_404_for_unknown_pr(admin_client, configured_ado):
    response = admin_client.get("/api/integrations/ado/pull-requests/9999/details")
    assert response.status_code == 404

