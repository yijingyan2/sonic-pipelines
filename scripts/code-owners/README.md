# CodeOwners Generator

The script to run over the local copy of the GitHub repo, validate all contributors against
the GitHub and list the top contributors per folder in the CODEOWNERS format.

## Building the Wheel Package

Alternatively you can do just pip install

May need to do it from the virtual environment (see how to setup below)
To build the wheel package, use the following command:

```bash
pip3 install build
python3 -m build
```

This will create both a source distribution (`.tar.gz`) and a wheel distribution (`.whl`) in the `dist/` directory.

## Setting Up Virtual Environment

### Option 1: Using UV (Recommended)
1. Download and install UV: https://docs.astral.sh/uv/getting-started/installation/
2. Run the command from the directory with the code
```bash
cd sonic-pipelines/scripts/code-owners

uv run codeowners-cli --repo /local/workspace/repo/nmirin-sonic-mgmt --contributors_file /local/workspace/repo/nmirin-sonic-mgmt/.code-owners/contributors.yaml --folder_presets_file /local/workspace/repo/nmirin-sonic-mgmt/.code-owners/folder_presets.yaml
```

### Option 2: Using venv

```bash
# Create a virtual environment
python3 -m venv venv

# Activate the virtual environment
# On macOS/Linux:
source venv/bin/activate

# On Windows:
# venv\Scripts\activate
```

### Option 3: Using conda

```bash
# Create a conda environment
conda create -n codeowners python=3.8

# Activate the conda environment
conda activate codeowners
```

## Installation

### Install from Wheel Package

After building the wheel package, install it in your virtual environment:

```bash
# Make sure your virtual environment is activated
pip install dist/*.whl
```

### Install in Development Mode

For development purposes, you can install the package in editable mode:

```bash
# Make sure your virtual environment is activated
pip install -e .
```

### Updating the GitHub tokens

Without GitHub tokens the script will be only allowed 60 API requests
per hour. For initial runs or larger repos, recommended to provide the GitHub tokens 

Request a GitHub token(s) with access to public repositories (default setting).

https://github.com/settings/personal-access-tokens

(GitHub->Profile->Settings(Gear icon)->Developer Settings->Personalized Access Tokens->Fine Grained Tokens)

Several tokens are recommended for larger repo. One token allows to process about 20000 commits per hour, depending on how many unique/new contributors are in the repo
Pass the tokens as a comma-separated environment variable.
```shell
# example
env GITHUB_API_TOKENS = "github_pat_XXXXX,github_pat_YYYYY,github_pat_ZZZZZZ" codeowners-cli ....
```

When GitHub rate-limits requests, workers wait for `Retry-After` or the
`X-RateLimit-Reset` deadline plus an exponential backoff. The shared backoff
doubles at most once per ten seconds, is capped at 3,600 seconds, and resets
after a successful request. Concurrent worker warnings describe overlapping
waits, not cumulative delays.

Run the rate-limit regression tests from this directory:

```bash
python3 -m unittest discover -s tests -v
```


## Running the CLI

After wheel installation, you can use the `codeowners-cli` command:

```bash
# Basic usage
codeowners-cli --help

# Example: Analyze a repository
codeowners-cli --repo_name /path/to/your/repo_name --contributors-file contributors.yaml
```

After PIP installation
```bash
python3 main.py --help
```

## CLI Options

The `codeowners-cli` command supports various options:

- `--repo`: Path to the local Git repository
- `--contributors_file`: Path to the contributors YAML file (default: `contributors.yaml`)
- `--folder_presets_file`: YAML file with the preset folder information
- `--active_after`: Date from which to consider contributors active (YYYY-MM-DD, default: 730 days ago)
- `--max_owners`: Maximum number of owners per folder (default: 3)
- `--log_level`: Log level of the output (choices: debug, info, warning, error, critical)

## Output Format

The tool generates a YAML file mapping folder paths to code owners with their contribution weights:

```yaml
/:
  owner1: 1505
  owner2: 892
  owner3: 678
/src/:
  owner1: 2000
  owner4: 453
/tests/:
  owner2: 1200
  owner5: 987
```

**Weight Values:**
- Integer values represent the total number of changed lines (additions + deletions) from Git history
- Higher values indicate more significant contributions to that folder
- `.inf` indicates preset owners with infinite priority (from folder_presets.yaml)
- Calculated weights are always integers; manual preset weights can be integers or floats

**Folder Presets Format:**
The `folder_presets.yaml` file now uses a dictionary format for owners with weights:

```yaml
/.git:
  type: IGNORE
/tests/dash:
  owners:
    congh: .inf
    nikamirrr: .inf
  type: CLOSED_OWNERS
/tests/ntp:
  owners:
    nikamirrr: .inf
  type: OPEN_OWNERS
```

## Maintenance

After the first run or when the new ```contributors.yaml``` is created
review emails for the record ```github_id: -1```, if any of them match the 
known contributor elsewhere in the file, move them to the ```email:``` list 
of that user. Rerun the script.
__Be careful not to duplicate emails.__

## Requirements

- Python 3.8 or higher
- Git repository with commit history
- Contributors YAML file
- GitHub API access (for contributor validation)

## GitHub Workflow Integration

The tool includes GitHub Actions workflows for automatic reviewer assignment on pull requests.

### Setup

1. **Install workflow files:**
```bash
# Copy workflow to your repository
cp workflow_scripts/assignReviewers.yaml .github/workflows/
cp workflow_scripts/auto-assign.py .github/.code-reviewers/

# Generate reviewer index
codeowners-cli --repo . \
  --contributors_file contributors.yaml \
  --folder_presets_file folder_presets.yaml \
  > .github/.code-reviewers/pr_reviewer-by-files.yml
```

2. **Commit the files:**
```bash
git add .github/
git commit -m "Add auto-assign reviewers workflow"
git push
```

### Workflow Features

- **Automatic Trigger:** Activates on pull requests to `master`, `main`, or release branches
- **Smart Selection:** Uses BFS algorithm to find appropriate reviewers based on changed files
- **Hierarchical Search:** Traverses up directory tree if specific folder lacks reviewers
- **Tie Handling:** Optionally includes all contributors with equal scores
- **Configurable:** Adjust reviewer count and selection logic via environment variables

### Configuration Options

Edit `.github/workflows/assignReviewers.yaml` to customize:

- `REVIEWER_INDEX`: Path to the reviewer mapping file (default: `.github/.code-reviewers/pr_reviewer-by-files.yml`)
- `NEEDED_REVIEWER_COUNT`: Number of reviewers to assign (default: 3)
- `INCLUDE_CONTRIBUTORS_TIES`: Include tied contributors (default: True)

### How It Works

1. Analyzes all files changed in the pull request
2. Maps each changed file to folder paths in the reviewer index
3. Performs BFS up the directory tree to collect reviewer candidates
4. Ranks candidates by contribution count
5. Selects top reviewers (with optional tie-breaking)
6. Requests reviews from selected users automatically

## Dependencies

### Core Tool
- PyYAML >= 6.0.2
- aiofiles >= 24.1.0
- aiohttp >= 3.10.11
- Brotli >= 1.1.0 (optional)
- aiodns >= 3.2.0 (optional)

### GitHub Workflow (auto-assign.py)
- PyYAML (pyaml package)
- PyGithub
