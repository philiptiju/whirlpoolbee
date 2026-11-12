"""Azure DevOps REST API client + sync orchestration.

Links local Claude Code sessions to Azure Repos pull requests/commits so the
dashboard can report real "AI-assisted PR" stats (feature 5). Sync
auto-discovers every project and Git repository directly from the Azure
DevOps API (no manual working-directory -> repo mapping required) and links
sessions to PRs via a time-window + author-email heuristic (see
`Database.link_sessions_to_prs`), not commit-hash matching.

Auth: HTTP Basic with an empty username and a Personal Access Token (PAT) as
the password, per Azure DevOps REST API convention. The PAT is read only
from `Config.AZURE_DEVOPS_PAT` (an environment variable) — it is never
accepted via the UI/API, stored in the database, or logged.

Docs: https://learn.microsoft.com/en-us/rest/api/azure/devops/git/
"""
import httpx

from src.utils.config import Config


class AzureDevOpsError(Exception):
    pass


# Azure DevOps reviewer vote codes -> human labels.
# https://learn.microsoft.com/en-us/rest/api/azure/devops/git/pull-requests/get-pull-request
VOTE_LABELS = {
    10: "Approved",
    5: "Approved with suggestions",
    0: "No vote",
    -5: "Waiting for author",
    -10: "Rejected",
}


def vote_label(vote) -> str:
    try:
        return VOTE_LABELS.get(int(vote), f"Vote {vote}")
    except (TypeError, ValueError):
        return "No vote"


class AzureDevOpsClient:
    def __init__(self, org_url: str = None, pat: str = None, api_version: str = None):
        self.org_url = (org_url or Config.AZURE_DEVOPS_ORG_URL).rstrip("/")
        self.pat = pat or Config.AZURE_DEVOPS_PAT
        self.api_version = api_version or Config.AZURE_DEVOPS_API_VERSION
        if not self.org_url or not self.pat:
            raise AzureDevOpsError(
                "Azure DevOps is not configured. Set AZURE_DEVOPS_ORG_URL and "
                "AZURE_DEVOPS_PAT as environment variables."
            )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(auth=("", self.pat), timeout=30.0)

    async def list_projects(self) -> list:
        """Fetch all projects in the org (used by sync_azure_devops to
        auto-discover everything to sync, with no manual config needed)."""
        url = f"{self.org_url}/_apis/projects?api-version={self.api_version}&$top=1000"
        async with self._client() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json().get("value", [])

    async def list_repositories(self, project: str) -> list:
        """Fetch all Git repositories within a project."""
        url = f"{self.org_url}/{project}/_apis/git/repositories?api-version={self.api_version}"
        async with self._client() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json().get("value", [])

    async def list_pull_requests(self, project: str, repository: str, status: str = "all", top: int = 200) -> list:
        """Fetch PRs for a repository. status: 'active' | 'completed' | 'abandoned' | 'all'."""
        url = (
            f"{self.org_url}/{project}/_apis/git/repositories/{repository}/pullrequests"
            f"?searchCriteria.status={status}&$top={top}&api-version={self.api_version}"
        )
        async with self._client() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json().get("value", [])

    async def list_pr_commits(self, project: str, repository: str, pull_request_id: int) -> list:
        url = (
            f"{self.org_url}/{project}/_apis/git/repositories/{repository}/pullRequests/"
            f"{pull_request_id}/commits?api-version={self.api_version}"
        )
        async with self._client() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json().get("value", [])

    async def get_commit_changes(self, project: str, repository: str, commit_id: str, top: int = 500) -> list:
        """Fetch the file paths changed by a single commit. Called on-demand
        by the PR 'View Details' report, NOT during the bulk org sync --
        doing this for every commit across thousands of synced PRs would be
        far too many extra API calls just to populate a list nobody may
        ever look at."""
        url = (
            f"{self.org_url}/{project}/_apis/git/repositories/{repository}/commits/"
            f"{commit_id}/changes?$top={top}&api-version={self.api_version}"
        )
        async with self._client() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            changes = []
            for ch in resp.json().get("changes", []):
                item = ch.get("item", {})
                path = item.get("path")
                if not path or item.get("isFolder"):
                    continue
                changes.append({"path": path, "change_type": ch.get("changeType", "edit")})
            return changes

    async def list_pr_threads(self, project: str, repository: str, pull_request_id: int) -> list:
        """Fetch a PR's comment threads -- used on-demand by the 'View
        Details' report for two things: (1) human review feedback/comments,
        and (2) reviewer vote events (system-generated threads tagged
        CodeReviewThreadType=VoteResult), which is the only place Azure
        DevOps exposes a *timestamp* for when someone approved/rejected a
        PR (the /reviewers endpoint only has the current vote, no history).
        Not called during the bulk org sync for the same cost reason as
        get_commit_changes."""
        url = (
            f"{self.org_url}/{project}/_apis/git/repositories/{repository}/pullRequests/"
            f"{pull_request_id}/threads?api-version={self.api_version}"
        )
        async with self._client() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json().get("value", [])


async def sync_azure_devops(db, status: str = "all") -> dict:
    """Auto-discover every project and Git repository in the configured
    Azure DevOps org, pull + persist their PRs and commits, then re-run the
    session<->PR linking heuristic. No manual repo mapping is required —
    this reads the entire org directly from the Azure DevOps API. Returns a
    summary dict. Raises AzureDevOpsError if not configured."""
    client = AzureDevOpsClient()  # raises AzureDevOpsError if unconfigured

    try:
        projects = await client.list_projects()
    except httpx.HTTPError as e:
        raise AzureDevOpsError(f"Could not list Azure DevOps projects: {e}")

    prs_synced, commits_synced, repos_synced, errors = 0, 0, 0, []
    for proj in projects:
        project = proj["name"]
        try:
            repos = await client.list_repositories(project)
        except httpx.HTTPError as e:
            errors.append(f"{project}: could not list repositories: {e}")
            continue
        for repo in repos:
            repository = repo["name"]
            repos_synced += 1
            try:
                prs = await client.list_pull_requests(project, repository, status=status)
            except httpx.HTTPError as e:
                errors.append(f"{project}/{repository}: {e}")
                continue
            for pr in prs:
                pr_row_id = await db.upsert_ado_pull_request(project, repository, pr)
                prs_synced += 1
                try:
                    commits = await client.list_pr_commits(project, repository, pr["pullRequestId"])
                except httpx.HTTPError as e:
                    errors.append(f"{project}/{repository}#{pr['pullRequestId']} commits: {e}")
                    continue
                for c in commits:
                    await db.upsert_ado_pr_commit(pr_row_id, c)
                    commits_synced += 1

    links_created = await db.link_sessions_to_prs()
    return {
        "repos_synced": repos_synced,
        "prs_synced": prs_synced,
        "commits_synced": commits_synced,
        "links_created": links_created,
        "errors": errors,
    }
