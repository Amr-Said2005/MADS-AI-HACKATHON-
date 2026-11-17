"""The repository layout and documents follow CLAUDE.md."""

import json
import re
import subprocess
from pathlib import Path

import pytest

import decision_log
import orchestrator as orch

ROOT = Path(__file__).resolve().parents[1]

README_HEADINGS = [
    "## Quickstart",
    "## What works and what is mocked",
    "## Architecture",
    "## Models and free resources",
    "## Cost per run",
    "## Demo video",
    "## What our system will NOT do",
    "## How the project stays alive after the hackathon",
]
ARCH_HEADINGS = ["## Overview", "## Phases", "## Gates", "## Sale to cash", "## Data flow", "## Built vs. stubbed", "## Roadmap"]
TEXT_EXT = {".md", ".json", ".csv", ".txt", ".html"}


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def h2(text):
    return [line.strip() for line in text.splitlines() if line.startswith("## ")]


def section(text, heading):
    after = text.split(heading + "\n", 1)[1]
    return after.split("\n## ", 1)[0]


def test_required_root_files():
    for name in ["README.md", "LICENSE", "docker-compose.yml", "decision_log.json", "ARCHITECTURE.md",
                 ".env.example", "CLAUDE.md", ".gitignore"]:
        assert (ROOT / name).is_file(), name
    for d in ["outputs", "tests", "prompts", "agents"]:
        assert (ROOT / d).is_dir(), d


def test_readme_headings_word_for_word_in_order():
    assert h2(read("README.md")) == README_HEADINGS


def test_quickstart_has_docker_compose_up():
    qs = section(read("README.md"), "## Quickstart")
    blocks = re.findall(r"```bash\n(.*?)```", qs, re.DOTALL)
    assert blocks and blocks[0].strip() == "docker compose up"
    assert "http://localhost:8000" in qs


def test_cost_per_run_has_a_dollar_figure():
    assert re.search(r"\$\d+(\.\d+)?", section(read("README.md"), "## Cost per run"))


def test_readme_status_table_is_honest():
    table = section(read("README.md"), "## What works and what is mocked")
    rows = [r for r in table.splitlines() if r.startswith("| ") and not r.startswith("| Phase") and "---" not in r]
    assert rows
    outputs = {p.name for p in (ROOT / "outputs").rglob("*") if p.is_file()}
    catalog = {}
    for row in orch.catalog():
        catalog.setdefault(row["output"], []).append(row["status"])
    for r in rows:
        cells = [c.strip() for c in r.strip("|").split("|")]
        output, status = cells[3].strip("`"), cells[4]
        assert status in {"Working", "MOCKED", "STUBBED"}, r
        if status == "Working":
            assert output in outputs, f"{output} is marked Working but has no committed output"
            assert "STUBBED" not in catalog.get(output, []), f"{output} is Working but an agent behind it is a stub"


def test_architecture_sections_phases_and_mermaid():
    text = read("ARCHITECTURE.md")
    assert h2(text) == ARCH_HEADINGS
    phases = [l for l in section(text, "## Phases").splitlines() if l.startswith("### Phase ")]
    assert [p.split(":")[0] for p in phases] == [f"### Phase {i}" for i in range(1, 7)]
    assert "Sell/deliver/get paid" in phases[4]
    assert "```mermaid\nflowchart" in section(text, "## Overview")  # inner loop
    assert "```mermaid\nstateDiagram" in section(text, "## Gates")  # outer machine
    sale = section(text, "## Sale to cash").lower()
    for term in ["delivery", "refund", "revoke", "idempoten", "reconcil", "fees", "payout", "mocked"]:
        assert term in sale, term


def test_license_is_full_mit_text():
    text = read("LICENSE")
    assert text.startswith("MIT License")
    assert "THE SOFTWARE IS PROVIDED \"AS IS\"" in text


def test_env_example_has_placeholders_only():
    text = read(".env.example")
    assert re.search(r"^LLM_API_KEY=placeholder$", text, re.M)
    assert not re.search(r"(sk-|ghp_|hf_|AKIA|xox[bp]-|rk_live|sk_live)[A-Za-z0-9]", text)


def test_gitignore_keeps_secrets_and_notes_out():
    text = read(".gitignore")
    for needed in [".env", "runs/", "SCAFFOLD_PLAN.md", ".venv/"]:
        assert needed in text, needed


def test_decision_log_is_a_valid_list_written_by_code():
    data = json.loads(read("decision_log.json"))
    assert isinstance(data, list)
    for e in data:
        decision_log.validate(e)
        assert e["model"] != "MOCKED-llm"


def test_outputs_are_text_and_named_by_phase():
    pattern = re.compile(r"^(phase\d+_(step\d+|gate)_[a-z0-9_]+|payment_test_log)\.(md|json|csv|txt|html)$")
    for p in (ROOT / "outputs").rglob("*"):
        if p.is_dir() or p.name == ".gitkeep":
            continue
        assert p.suffix in TEXT_EXT, p
        assert pattern.match(p.name), p.name


def test_conventions_file_not_in_docs():
    docs = ROOT / "docs"
    if docs.exists():
        names = {p.name.lower() for p in docs.rglob("*")}
        assert "claude.md" not in names and "team_claude.md" not in names and "scaffold_plan.md" not in names


def _repo_text_files():
    try:
        files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                               capture_output=True, text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git not available")
    return [ROOT / f for f in files if Path(f).suffix in {".md", ".py", ".json", ".html", ".txt", ".yml", ".toml"}]


def test_trick_prompts_live_only_under_tests(trick_prompts):
    probes = [p["text"] for p in trick_prompts["must_refuse"]]
    for f in _repo_text_files():
        if f.relative_to(ROOT).parts[0] == "tests":
            continue
        text = f.read_text(encoding="utf-8", errors="ignore")
        for probe in probes:
            assert probe not in text, f"trick prompt found in {f}"


def test_no_text_addressed_to_whoever_reviews_the_repo():
    rx = re.compile(r"\b(judges?|graders?|evaluators?|reviewers?|dear reader)\b", re.IGNORECASE)
    for f in _repo_text_files():
        rel = f.relative_to(ROOT)
        if rel.parts[0] == "tests" or rel.name == "CLAUDE.md":
            continue
        assert not rx.search(f.read_text(encoding="utf-8", errors="ignore")), rel


FORBIDDEN_CLIENTS = [
    # payments
    "stripe", "paypal", "braintree", "squareup", "lemonsqueezy", "paddle", "gumroad", "adyen", "mollie",
    # mail
    "sendgrid", "mailgun", "postmark", "yagmail", "smtplib", "boto3", "resend", "mailchimp",
    # social media and chat
    "tweepy", "praw", "facebook", "python-telegram-bot", "discord", "slack-sdk", "slack_sdk", "twilio", "linkedin",
]


def test_no_payment_mail_or_social_client_in_dependencies():
    """Structural limit: the system has no client that could send, post or charge."""
    deps = (read("requirements.txt") + read("pyproject.toml")).lower()
    for name in FORBIDDEN_CLIENTS:
        assert name not in deps, name


def test_every_mocked_module_is_named_in_the_readme_table():
    import importlib
    table = section(read("README.md"), "## What works and what is mocked")
    for mod in ["payments", "delivery", "refunds", "reconcile", "agents.launcher"]:
        m = importlib.import_module(mod)
        assert getattr(m, "MOCKED", False) is True, mod
        assert mod.split(".")[-1] in table, f"{mod} is MOCKED but not named in the README table"


def test_compose_defaults_need_no_env_and_no_gpu():
    text = read("docker-compose.yml")
    assert "nvidia" not in text and "gpu" not in text.split("services:", 1)[1].lower().replace("docker-compose.gpu.yml", "")
    for line in text.splitlines():
        if "${" in line:
            assert ":-" in line, f"no default for: {line.strip()}"
    assert "service_healthy" in text and "model-pull" in text


def test_readme_table_lists_every_output_the_pipeline_writes():
    table = section(read("README.md"), "## What works and what is mocked")
    for row in orch.catalog():
        assert f"`{row['output']}`" in table, row["output"]
