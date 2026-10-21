import warnings
import random
from collections import Counter
from datetime import datetime, timezone, timedelta
import logging
import aiohttp
import asyncio
import os
import ssl
import certifi
from typing import Dict, Any, Optional
from urllib.parse import quote_plus

from aiohttp import ClientResponse

from async_helpers import (
    get_all_commit_stats,
    GitCommitLocal,
)
from contributor import Contributor, ContributorCollection
from folders import FolderType, FolderSettings
from organization import (
    ORGANIZATION,
    organization_by_company,
)

logger = logging.getLogger(__name__)


class AsyncGitHubRepoSummary:
    """Asynchronous GitHub repository analyzer for code ownership generation.

    This class handles the analysis of GitHub repositories to determine code
    ownership based on commit history and contributor activity. It uses the
    GitHub API to gather information about contributors and their commit
    patterns.

    Attributes:
        MAX_CONCURRENT_API_REQUESTS: Maximum number of concurrent API
        requests. MAX_UNRESOLVED_COMMITS: Maximum number of commits to queue
        for resolution.
        COMMIT_RESOLVE_WORKERS: Number of worker tasks for commit resolution.
        GITHUB_API_ENDPOINT: Base URL for GitHub API.
        GITHUB_API_TOKENS_ENV_VAR: Environment variable name for GitHub tokens.
        GITHUB_API_TOKENS: List of GitHub API tokens for authentication.
    """

    MAX_CONCURRENT_API_REQUESTS = 1000
    MAX_UNRESOLVED_COMMITS = 1000
    COMMIT_RESOLVE_WORKERS = 64

    GITHUB_API_ENDPOINT = "https://api.github.com/"
    GITHUB_API_TOKENS_ENV_VAR = "GITHUB_API_TOKENS"
    GITHUB_API_TOKENS = [
        token
        for token in map(
            str.strip, os.environ.get(GITHUB_API_TOKENS_ENV_VAR, "").split(",")
        )
        if token
    ]

    def build_api_headers(self) -> Dict[str, str]:
        """Build HTTP headers for GitHub API requests.

        Returns:
            Dict[str, str]: Headers dictionary with authentication and API
            version.
        """
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.GITHUB_API_TOKENS:
            token = random.choice(self.GITHUB_API_TOKENS)
            headers["Authorization"] = f"token {token}"
        return headers

    async def check_api_rate(self, response: ClientResponse):
        """Check and handle GitHub API rate limiting.

        Implements exponential backoff for rate limit handling. If rate limit
        is exceeded, waits for the appropriate time before retrying.

        Args:
            response: The HTTP response from GitHub API.
        """
        now = datetime.now(timezone.utc).timestamp()
        if (
            now - self.expo_wait_last_update > self.expo_wait_time_incr_wait
            and self.expo_wait_time < 3600
        ):
            self.expo_wait_time = min(self.expo_wait_time * 2, 3600)
            self.expo_wait_last_update = now
            logger.warning(
                f"Doubling the additional wait time to: {self.expo_wait_time}s"
            )

        if response.status == 429:
            logger.warning("Got 429 error, too many requests")
        if response.headers.get("retry-after"):
            sleep_duration = self.expo_wait_time + int(
                response.headers["retry-after"]
            )
        else:
            # Handle the rate limit on GitHub
            if int(response.headers["x-ratelimit-remaining"]) > 0:
                # Error when the remaining limit is not 0,
                # return the error state
                raise ValueError(
                    "Invalid 403 response while rate limit is not over"
                )
            reset_time_utc_epoch = int(response.headers["x-ratelimit-reset"])
            ts_now_utc_epoch = datetime.now(timezone.utc).timestamp()
            sleep_duration = (
                max(reset_time_utc_epoch - ts_now_utc_epoch, 0)
                + self.expo_wait_time
            )
        wake_time = datetime.now() + timedelta(seconds=sleep_duration)

        logger.warning(
            f"{asyncio.current_task().get_name()} "
            "GitHub rate limit exceeded, sleeping for "
            f"{int(sleep_duration)} seconds until "
            f"{wake_time}",
        )
        await asyncio.sleep(sleep_duration)

    async def send_github_api_request(
        self, url: str, params: Dict[str, str] = None
    ) -> Optional[Any]:
        """Send a request to the GitHub API with rate limiting and retry logic.

        Args:
            url: The GitHub API URL to request.
            params: Optional query parameters for the request.

        Returns:
            Optional[Any]: JSON response from the API, or None if failed.

        Raises:
            ValueError: If the API returns a non-200 status code.
        """
        # limit the number of requests
        headers = self.build_api_headers()
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=self.ssl_context)
        ) as session:
            response = None
            async with self.github_api_sem:
                while True:
                    if response is not None:
                        # We came back where after 403 error
                        # check if we need to wait for
                        # the API cooldown
                        await self.check_api_rate(response)
                    async with session.get(
                        url=url,
                        headers=headers,
                        params=params,
                    ) as response:
                        if response.status == 403 or response.status == 429:
                            continue
                        if response.status != 200:
                            raise ValueError(
                                f"Bad API response: {response} "
                                f"for {url} {params}"
                            )
                        self.expo_wait_time = 1
                        self.expo_wait_last_update = datetime.now(
                            timezone.utc
                        ).timestamp()
                        return await response.json()

    async def github_id_lookup(self, github_id: int) -> Dict[str, Any]:
        """Look up GitHub user information by user ID.

        Args:
            github_id: The GitHub user ID to look up.

        Returns:
            Dict[str, Any]: Dictionary containing user information with keys:
                login, id, name, email, company.
        """
        if github_id == -1:
            return {
                "login": "non-github-bundle",
                "id": -1,
                "name": "Non-found emails bundle",
                "email": None,
                "company": None,
            }
        try:
            return self.gh_id_lookup_cache[github_id]
        except KeyError:
            pass
        response = await self.send_github_api_request(
            f"{AsyncGitHubRepoSummary.GITHUB_API_ENDPOINT}"
            f"user/{int(github_id)}",
        )
        self.gh_id_lookup_cache[github_id] = result = {
            k: response[k] for k in ["login", "id", "name", "email", "company"]
        }
        return result

    async def github_login_lookup(self, github_login: str) -> Dict[str, Any]:
        """Look up GitHub user information by username.

        Args:
            github_login: The GitHub username to look up.

        Returns:
            Dict[str, Any]: Dictionary containing user information with keys:
                login, id, name, email, company.
        """
        try:
            return self.gh_login_lookup_cache[github_login]
        except KeyError:
            pass
        response = await self.send_github_api_request(
            f"{AsyncGitHubRepoSummary.GITHUB_API_ENDPOINT}"
            f"users/{quote_plus(github_login)}",
        )
        self.gh_login_lookup_cache[github_login] = result = {
            k: response[k] for k in ["login", "id", "name", "email", "company"]
        }
        return result

    async def github_commit_author_id_lookup(self, commit_hash: str) -> int:
        """Look up the GitHub user ID of a commit's author.

        Args:
            commit_hash: The Git commit hash to look up.

        Returns:
            int: The GitHub user ID of the commit author, or -1 if not found.

        Raises:
            ValueError: If the commit is not found in the repository.
            RuntimeError: If the API request fails.
        """
        try:
            return (
                await self.send_github_api_request(
                    f"{AsyncGitHubRepoSummary.GITHUB_API_ENDPOINT}"
                    f"repos/{quote_plus(self.owner)}/{quote_plus(self.repo)}"
                    f"/commits/{quote_plus(commit_hash)}"
                )
            )["author"]["id"]
        except TypeError:
            return -1

    def __init__(self):
        """Initialize the AsyncGitHubRepoSummary instance.

        Sets up caches, SSL context, and rate limiting parameters.
        """
        self.gh_login_lookup_cache = dict()
        self.gh_id_lookup_cache = dict()

        self.ssl_context = None
        self.github_api_sem = None
        self.to_resolve_commit_queue = None
        self.connector = None
        self.resolved_commit_queue = None

        # API wait params if hitting a rate limit
        self.expo_wait_time = 1  # current wait file
        self.expo_wait_last_update = datetime.now(timezone.utc).timestamp()
        self.expo_wait_time_incr_wait = 10  # after 10 s double the wait rime

        if not self.GITHUB_API_TOKENS:
            warnings.warn(
                "No GitHub tokens passed in "
                f"{self.GITHUB_API_TOKENS_ENV_VAR} env var. "
                "Only 60 GitHub requests per hour allowed: "
                "https://docs.github.com/en/rest/using-the-rest-api/"
                "rate-limits-for-the-rest-api?apiVersion=2022-11-28"
            )

    async def _initialize(
        self,
        contributors: ContributorCollection,
        repo_folders: Dict[str, FolderSettings],
        repo_path: str,
        owner: str,
        repo: str,
        active_after: datetime,
        max_owners: int,
    ):
        """Initialize the repository analysis with configuration parameters.

        Args:
            contributors: Collection of contributors to analyze.
            repo_folders: Dictionary mapping repo folder paths
                          to their settings.
            repo_path: Path to the local repository.
            owner: GitHub repository owner.
            repo: GitHub repository name.
            active_after: Cutoff date for considering contributors active.
            max_owners: Maximum number of owners per folder.
        """
        self.contributors = contributors
        self.repo_folders = repo_folders
        self.repo_folders_stats = {
            folder: Counter() for folder in repo_folders
        }
        # Perform async operations here
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())
        self.github_api_sem = asyncio.Semaphore(
            AsyncGitHubRepoSummary.MAX_CONCURRENT_API_REQUESTS
        )
        self.connector = aiohttp.TCPConnector(ssl=self.ssl_context)
        self.repo_path = repo_path
        self.owner = owner
        self.repo = repo
        self.active_after = active_after
        self.max_owners = max_owners

        self.resolved_commit_queue = asyncio.Queue()
        self.to_resolve_commit_queue = asyncio.Queue(
            maxsize=AsyncGitHubRepoSummary.MAX_UNRESOLVED_COMMITS
        )

        # API wait params if hitting a rate limit
        self.expo_wait_time = 1  # current wait file
        self.expo_wait_last_update = datetime.now(timezone.utc).timestamp()
        self.expo_wait_time_incr_wait = 10  # after 10 s double the wait rime

    async def process_repository(
        self,
        contributors: ContributorCollection,
        preset_folders: Dict[str, FolderSettings],
        repo_folders: Dict[str, FolderSettings],
        repo_path: str,
        total_commit_count: int,
        owner: str,
        repo_name: str,
        active_after: datetime,
        max_owners: int,
    ):
        """Process a repository to determine code ownership.

        Analyzes all commits in the repository, resolves contributor
        information, and assigns owners to folders based on commit
        activity.

        Args:
            contributors: Collection of contributors to analyze.
            preset_folders: Dictionary mapping folder presets
                            to their settings.
            repo_folders: Dictionary mapping folder paths to their settings.
            repo_path: Path to the local repository.
            total_commit_count: Total number of commits in the repository.
            owner: GitHub repository owner.
            repo_name: GitHub repository name.
            active_after: Cutoff date for considering contributors active.
            max_owners: Maximum number of owners per folder.
        """
        await self._initialize(
            contributors,
            repo_folders,
            repo_path,
            owner,
            repo_name,
            active_after,
            max_owners,
        )
        cnt = 0
        backlog_workers = [
            asyncio.create_task(self.resolve_commit())
            for _ in range(AsyncGitHubRepoSummary.COMMIT_RESOLVE_WORKERS)
        ]
        async for commit in get_all_commit_stats(repo_path):
            while not self.resolved_commit_queue.empty():
                _, _ = await self.resolved_commit_queue.get()
                self.resolved_commit_queue.task_done()
                cnt += 1
                if cnt % 100 == 0:
                    logger.debug(
                        f"Processed {cnt} of {total_commit_count} commits"
                    )
                if cnt % 1000 == 0:
                    await self.contributors.save_to_file()

            await self.to_resolve_commit_queue.put(commit)
        # send a sentinel to all workers to stop
        for _ in range(AsyncGitHubRepoSummary.COMMIT_RESOLVE_WORKERS):
            await self.to_resolve_commit_queue.put(None)
        await asyncio.gather(*backlog_workers)

        # Collect the remaining commits
        while not self.resolved_commit_queue.empty():
            _, _ = await self.resolved_commit_queue.get()
            self.resolved_commit_queue.task_done()
            cnt += 1
            if cnt % 100 == 0:
                logger.debug(
                    f"Processed {cnt} of {total_commit_count} commits"
                )
            if cnt % 1000 == 0:
                await self.contributors.save_to_file()

        # process the active contributors
        for contributor in self.contributors.contributors:
            if (
                contributor.last_commit_ts is not None
                and contributor.last_commit_ts >= self.active_after
                and contributor.available_to_review
            ):
                # if the contributor's last commit was after
                # the cutoff date, add the commit stats to the
                # repo folders
                for commit in contributor.commits:
                    for folder, change_count in commit.changes.items():
                        folder = os.sep + folder
                        # Apply the changes from the folder up
                        while True:
                            try:
                                if (
                                    preset_folders[folder].folder_type
                                    == FolderType.IGNORE
                                ):
                                    # do not account for the data in
                                    # the Ignore subfolders
                                    break
                            except KeyError:
                                # Ignore non-existent folders
                                pass
                            try:
                                if (
                                    self.repo_folders[folder].folder_type
                                    != FolderType.CLOSED_OWNERS
                                ):
                                    # Unless the owners are already defined,
                                    # count the statistics
                                    self.repo_folders_stats[folder][
                                        contributor
                                    ] += change_count
                            except KeyError:
                                # Ignore non-existent folders
                                pass
                            if folder == os.sep:
                                break
                            folder = os.path.dirname(folder)

        # select contributors for each folder
        for folder, contributor_stat in sorted(
            self.repo_folders_stats.items()
        ):
            folder_settings = self.repo_folders[folder]
            if folder_settings.folder_type in [
                FolderType.OPEN_OWNERS,
                FolderType.REGULAR,
            ]:
                need_extra_owners = max(
                    0, (self.max_owners - len(folder_settings.owners))
                )
                if need_extra_owners > 0 and contributor_stat:
                    # Select all contributors to complement to the self.max_owners
                    # if there is a tie in the number of changes, select all of them
                    it_contributors = iter(
                        contributor_stat.most_common(self.max_owners)
                    )
                    contributor, previous_contributor_changes = next(
                        it_contributors
                    )
                    folder_settings.owners[contributor.github_login] = (
                        previous_contributor_changes
                    )
                    for contributor, contributor_changes in it_contributors:
                        if (
                            len(folder_settings.owners) >= self.max_owners
                            and contributor_changes
                            < previous_contributor_changes
                        ):
                            # if found enough contributors and the new contributor has less changes, stop
                            break
                        folder_settings.owners[contributor.github_login] = (
                            contributor_changes
                        )
                        previous_contributor_changes = contributor_changes

    async def resolve_commit(self):
        """Worker task to resolve commit information and update contributors.

        Processes commits from the queue, looks up contributor information,
        and updates the contributor collection with new commit data.
        """
        while True:
            commit = await self.to_resolve_commit_queue.get()
            self.to_resolve_commit_queue.task_done()
            if commit is None:
                # done processing
                return
            contributor = self.contributors.by_email.get(commit.email)
            if not contributor:
                contributor = await self.build_contributor(commit)
            contributor.commits.append(commit)
            if (
                contributor.last_commit_ts is None
                or contributor.last_commit_ts < commit.ts
            ):
                contributor.last_commit_ts = commit.ts

            await self.resolved_commit_queue.put((commit, contributor))

    async def build_contributor(self, commit: GitCommitLocal) -> Contributor:
        """Build a contributor object from commit information.

        Tries to look for the commit information in GitHub and build the
        candidate contributor object for later adding to the contributor
        collection.

        Args:
            commit: GitCommit instance with local commit info.

        Returns:
            Contributor: The built contributor instance.
        """
        author_id = await self.github_commit_author_id_lookup(
            commit.commit_hash
        )
        if author_id == -1:
            github_info = await self.github_info_by_email(commit.email)
        else:
            github_info = await self.github_id_lookup(author_id)

        if not github_info:
            logger.warning(
                f"Commit {commit.commit_hash}, {commit.name}, "
                f"{commit.email}, unable to determine authors' GitHub id. "
                "Adding to the bundled contributor with GitHub id = -1"
            )
            github_info = await self.github_id_lookup(author_id)

        emails = {commit.email.lower()}
        if github_info["email"]:
            emails.add(github_info["email"].lower())
        organization = None
        if github_info["company"]:
            organization = organization_by_company(github_info["company"])
            if organization == ORGANIZATION.OTHER:
                organization = None
        person_name = github_info["name"]
        if person_name is None:
            person_name = commit.name

        contributor = Contributor(
            name=person_name,
            emails=emails,
            organization=organization,
            github_id=github_info["id"],
            github_login=github_info["login"],
        )
        return self.contributors.add_update_contributor(contributor)

    async def github_info_by_email(self, email: str) -> Dict[str, Any]:
        """Look up GitHub user information by email addresses.

        Parses GitHub noreply emails
        (like 29677895+nikamirrr@users.noreply.github.com)
        to extract GitHub user ID and login information.

        Args:
            email: Email address to search for GitHub information.

        Returns:
            Dict[str, Any]: Dictionary containing GitHub user information, or
            empty dict if not found.
        """
        local_part, domain = email.split("@")
        if domain == "users.noreply.github.com":
            try:
                id_str, github_login = local_part.split("+")
                return await self.github_id_lookup(int(id_str))
            except ValueError:
                # Try using the entire local part as login
                try:
                    return await self.github_login_lookup(local_part)
                except ValueError:
                    pass
        return {}
