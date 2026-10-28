"""Keep the assumption registry, the code and the docs in step.

assumptions.py is the source of truth. This fails when:
- an assumption is missing from the README's verification table (or its confidence or status
  there differs), or from HARDWARE_VALIDATION.md
- the docs or code cite an id that isn't in the registry (a typo, or a removed assumption)
- an assumption is cited nowhere in the code or tests (nothing depends on it, so why keep it?)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from harmony_mcp import registry
from harmony_mcp.assumptions import ASSUMPTIONS, BY_ID, Assumption, contradicted, unverified
from harmony_mcp.doctor import Report, render

ROOT = Path(__file__).parent.parent
ID = re.compile(r"\bH-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")
DOCS = ("README.md", "HARDWARE_VALIDATION.md")


def cited_in(paths: list[Path]) -> set[str]:
    return {m for path in paths for m in ID.findall(path.read_text())}


def test_ids_are_unique_and_well_formed():
    ids = [a.id for a in ASSUMPTIONS]
    assert len(ids) == len(set(ids))
    assert all(ID.fullmatch(i) for i in ids)


@pytest.mark.parametrize("doc", DOCS)
def test_every_assumption_is_in_each_doc(doc):
    assert set(BY_ID) <= cited_in([ROOT / doc]), set(BY_ID) - cited_in([ROOT / doc])


@pytest.mark.parametrize("doc", DOCS)
def test_docs_cite_only_real_ids(doc):
    assert cited_in([ROOT / doc]) <= set(BY_ID)


def test_readme_table_matches_the_registry():
    rows = re.findall(r"^\| `(H-[A-Z0-9-]+)`\s*\| (\w+)\s*\| ([\w-]+)\s*\|$", (ROOT / "README.md").read_text(), re.M)
    assert {r[0]: (r[1], r[2]) for r in rows} == {a.id: (a.confidence, a.status) for a in ASSUMPTIONS}


def test_code_cites_only_real_ids_and_every_id_is_used():
    code = sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "tests").glob("test_*.py"))
    code = [p for p in code if p.name not in ("assumptions.py", "test_assumptions.py")]
    cited = cited_in(code)
    assert cited <= set(BY_ID), cited - set(BY_ID)
    assert set(BY_ID) <= cited, f"assumptions nothing cites: {set(BY_ID) - cited}"


# --- the shared registry (registry.py) is the machinery ---------------------------------------
SAMPLE = (
    Assumption("H-X", "claim", "source", "low"),
    Assumption("H-Y", "claim", "source", "high", status="hardware-contradicted", note="saw otherwise"),
    Assumption("H-Z", "claim", "source", "high", status="hardware-verified"),
)


def test_the_record_is_the_shared_one():
    assert Assumption is registry.Assumption
    assert registry.by_id(ASSUMPTIONS) == BY_ID


def test_a_contradicted_assumption_is_not_unverified():
    # The local query this replaced counted everything but hardware-verified, contradicted included.
    assert [a.id for a in unverified(SAMPLE)] == ["H-X"]
    assert [a.id for a in contradicted(SAMPLE)] == ["H-Y"]


def test_queries_default_to_this_servers_list():
    assert unverified() == [a for a in ASSUMPTIONS if a.status == "simulator-only"]
    assert contradicted() == [a for a in ASSUMPTIONS if a.status == "hardware-contradicted"]


def test_a_typo_in_a_status_fails_at_import_time():
    with pytest.raises(ValueError, match="hardware-verfied"):
        Assumption("H-X", "claim", "source", "low", status="hardware-verfied")  # type: ignore[arg-type]


def test_doctor_lists_contradicted_apart_from_unverified():
    report = Report({}, [], [], [], unverified_assumptions=["H-X"], contradicted_assumptions=["H-Y"])
    text = render(report)
    assert "1 protocol assumptions not yet confirmed on hardware: H-X" in text
    assert "1 contradicted on hardware, no longer relied on: H-Y" in text
    assert "contradicted" not in render(Report({}, [], [], [], unverified_assumptions=["H-X"]))
