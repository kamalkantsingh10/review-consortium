"""Story 1.3: seeded Persona generation: every I/O matrix row, seeds, grid, quotas, cards."""

from __future__ import annotations

import ast
import filecmp
import hashlib
import json
import logging
import os
import random
import re
import shutil
from collections import Counter
from pathlib import Path

import pytest
from typer.testing import CliRunner

from consortium.cli import app
from consortium.config.load import load_card_wording, load_personas, load_study
from consortium.config.models import CardWording
from consortium.core.errors import ConsortiumError
from consortium.core.personas import TRAITS, Persona, generate_personas, render_card
from consortium.core.seeds import derive_seed
from consortium.stages.init import init_study
from consortium.stages.personas import canonical_index, generate

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "consortium"
runner = CliRunner()

# sha256 of panel/personas/index.json for the template Study (seed 1).
GOLDEN_INDEX_SHA256 = "10fe71765ceceb2535bbde0acbe748d68f3966c3b7fc8a21154db9b76af75131"
GOLDEN_P1 = (
    "You are a man, aged 18 to 29, from the Middle East or North Africa, with regular"
    " experience of robots.\n"
    "You prefer familiar things and practical, well-tried ways of doing them.\n"
    "You take things as they come and do not worry much about plans or details.\n"
    "You are quiet and reserved, and you prefer calm settings or small groups.\n"
    "You say what you think plainly, and you trust others once they have shown they are"
    " reliable.\n"
    "You stay calm under pressure and rarely worry for long.\n"
    "You feel comfortable around robots and would be at ease interacting with one.\n"
)


@pytest.fixture
def study(tmp_path: Path) -> Path:
    return init_study(tmp_path / "s")


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new, 1))


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def _cli(study: Path, *extra: str):
    return runner.invoke(app, ["personas", "generate", "--study", str(study), *extra])


# --------------------------------------------------------------------------- seeds


def test_derive_seed_fixed_vectors() -> None:
    # sha256("1:personas:gender")[:8] = 430c37c5
    assert derive_seed(1, "personas", "gender") == 0x430C37C5 == 1124874181
    # sha256("1:personas:cultural_region")[:8] = d2d08bb5; the top bit is masked off
    assert derive_seed(1, "personas", "cultural_region") == 0xD2D08BB5 & 0x7FFFFFFF
    assert derive_seed(1, "personas", "cultural_region") == 1389398965
    assert 0 <= derive_seed(2**63 - 1, "order", "t/p1-m1/r1") < 2**31


# --------------------------------------------------------------------------- default run


def test_default_generation(study: Path) -> None:
    result = _cli(study)
    assert result.exit_code == 0, result.output
    assert result.stdout == "64 personas -> panel/personas\n"
    files = set(_tree(study / "panel"))
    assert files == {f"personas/p{i}.md" for i in range(1, 65)} | {
        "personas/index.json",
        "personas/meta.json",
    }
    assert (study / "panel" / "personas" / "p1.md").read_text(encoding="utf-8") == GOLDEN_P1


def test_index_is_canonical_and_loads(study: Path) -> None:
    personas = generate(study)
    raw = (study / "panel" / "personas" / "index.json").read_bytes()
    data = json.loads(raw)
    assert raw == json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert not raw.endswith(b"\n")
    assert [d["id"] for d in data] == [f"p{i}" for i in range(1, 65)]
    assert set(data[0]) == {
        "id", "big_five", "nars", "age_band", "gender", "cultural_region", "robot_experience"
    }
    assert load_personas(study) == personas


def test_reproducible_across_folders(tmp_path: Path) -> None:
    a, b = init_study(tmp_path / "a"), init_study(tmp_path / "b")
    generate(a)
    generate(b)
    cmp = filecmp.dircmp(a / "panel", b / "panel")
    assert _tree(a / "panel") == _tree(b / "panel")
    assert not cmp.left_only and not cmp.right_only


def test_seed_changes_assignment(study: Path, tmp_path: Path) -> None:
    other = init_study(tmp_path / "o")
    _edit(other / "study.yaml", "seed: 1", "seed: 2")
    assert generate_personas(load_study(study)) != generate_personas(load_study(other))


# --------------------------------------------------------------------------- grid & quotas


def test_grid_is_32_profiles_times_each_band(study: Path) -> None:
    personas = generate_personas(load_study(study))
    assert len(personas) == 64
    profiles = [tuple(p.big_five[t] for t in TRAITS) for p in personas[::2]]
    assert len(set(profiles)) == 32
    # bit pattern order: low = 0, openness most significant
    assert profiles[0] == ("low",) * 5
    assert profiles[1] == ("low",) * 4 + ("high",)
    assert profiles[16] == ("high",) + ("low",) * 4
    assert profiles[31] == ("high",) * 5
    assert [p.nars for p in personas[:4]] == ["low", "high", "low", "high"]
    assert personas[0].big_five == personas[1].big_five


def test_single_band_gives_32(study: Path) -> None:
    _edit(study / "study.yaml", "nars_bands: [low, high]", "nars_bands: [high]")
    personas = generate_personas(load_study(study))
    assert [p.id for p in personas] == [f"p{i}" for i in range(1, 33)]
    assert {p.nars for p in personas} == {"high"}


def test_marginals_match_frame(study: Path) -> None:
    cfg = load_study(study)
    personas = generate_personas(cfg)
    assert Counter(p.age_band for p in personas) == {
        "18-29": 16, "30-44": 16, "45-59": 16, "60+": 16
    }
    assert Counter(p.gender for p in personas) == {"woman": 32, "man": 32}
    assert set(Counter(p.cultural_region for p in personas).values()) == {8}
    assert Counter(p.robot_experience for p in personas) == {
        "none": 22, "some": 21, "regular": 21
    }


def test_uneven_quota_extra_goes_to_first_listed(study: Path) -> None:
    path = study / "study.yaml"
    text = path.read_text()
    text, n = re.subn(
        r"    cultural_region:\n(      - \w+\n)+", "    cultural_region: [zeta, alpha, mid]\n", text
    )
    assert n == 1
    path.write_text(text)
    personas = generate_personas(load_study(study))
    assert Counter(p.cultural_region for p in personas) == {"zeta": 22, "alpha": 21, "mid": 21}


# --------------------------------------------------------------------------- cards


def test_cards_are_behaviour_only(study: Path) -> None:
    personas = generate(study)
    folder = study / "panel" / "personas"
    forbidden = [*TRAITS, "high", "low", "nars"]
    for persona in personas:
        raw = (folder / f"{persona.id}.md").read_bytes()
        text = raw.decode("utf-8")
        assert b"\r" not in raw and text.endswith("\n") and not text.endswith("\n\n")
        assert len(text.splitlines()) == 7
        words = {w.strip(".,").lower() for w in text.split()}
        for word in forbidden:
            assert word not in words, (persona.id, word)
        assert "nars" not in text.lower()
        assert persona.id not in text.split()
        assert not any(ch.isdigit() for ch in text.replace("18 to 29", "").replace(
            "30 to 44", "").replace("45 to 59", "").replace("60 or over", "")), persona.id


def test_card_layout_order(study: Path) -> None:
    wording = load_card_wording()
    persona = Persona(
        id="p9",
        big_five={
            "openness": "high", "conscientiousness": "low", "extraversion": "low",
            "agreeableness": "high", "neuroticism": "low",
        },
        nars="high",
        age_band="60+",
        gender="man",
        cultural_region="western_europe",
        robot_experience="none",
    )
    assert render_card(persona, wording) == (
        "You are a man, aged 60 or over, from Western Europe, with no experience of robots.\n"
        "You enjoy new ideas and unfamiliar experiences, and you like to try things a"
        " different way.\n"
        "You take things as they come and do not worry much about plans or details.\n"
        "You are quiet and reserved, and you prefer calm settings or small groups.\n"
        "You are warm and cooperative, and you usually give others the benefit of the doubt.\n"
        "You stay calm under pressure and rarely worry for long.\n"
        "You feel uneasy around robots and would rather keep some distance from them.\n"
    )


def test_wording_file_is_the_approved_draft() -> None:
    draft = (REPO / "_doc" / "implementation-artifacts" / "persona-card-wording-draft.md")
    if not draft.is_file():
        pytest.skip("planning artifacts not present")
    block = draft.read_text().split("## Proposed `wording.yaml`", 1)[1]
    block = block.split("```yaml\n", 1)[1].split("```", 1)[0]
    shipped = (SRC / "templates" / "persona_card" / "wording.yaml").read_text()
    assert shipped == block


def test_wording_missing_band_is_config_invalid(
    study: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import consortium.config.load as load

    cfg = load_study(study)
    wording = load_card_wording()
    trimmed = wording.model_copy(update={"nars": {"low": wording.nars["low"]}})
    monkeypatch.setattr(load, "_parse", lambda *a: trimmed)
    with pytest.raises(ConsortiumError) as info:
        load.load_card_wording(cfg)
    assert info.value.code == "config_invalid"
    assert info.value.message.startswith("nars.high:")
    with pytest.raises(ValueError, match="NARS band 'high'"):
        render_card(generate_personas(cfg)[1], trimmed)


def test_wording_rejects_labels_and_bad_templates() -> None:
    base = load_card_wording().model_dump()
    bad = json.loads(json.dumps(base))
    bad["traits"]["openness"]["high"] = "You score high on openness."
    with pytest.raises(ValueError, match="label"):
        CardWording.model_validate(bad)
    bad = json.loads(json.dumps(base))
    bad["demographic"] = "You are {gender} from {planet}."
    with pytest.raises(ValueError, match="placeholder"):
        CardWording.model_validate(bad)


# --------------------------------------------------------------------------- refusals


def test_panel_exists_refuses_and_changes_nothing(study: Path) -> None:
    generate(study)
    before = _tree(study / "panel")
    result = _cli(study)
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.strip() == "panel_exists: panel/personas already exists (use --force)"
    assert _tree(study / "panel") == before


def test_empty_personas_dir_is_not_a_panel(study: Path) -> None:
    (study / "panel" / "personas").mkdir(parents=True)
    assert _cli(study).exit_code == 0
    assert len(_tree(study / "panel")) == 66


def test_force_replaces_atomically(study: Path) -> None:
    generate(study)
    folder = study / "panel" / "personas"
    (folder / "stale.md").write_text("old")
    (folder / "p1.md").write_text("tampered")
    result = _cli(study, "--force")
    assert result.exit_code == 0, result.output
    files = _tree(study / "panel")
    assert "personas/stale.md" not in files
    assert files["personas/p1.md"].decode() == GOLDEN_P1
    assert sorted(p.name for p in (study / "panel").iterdir()) == ["personas"]


def test_failure_leaves_no_partial_panel(study: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import consortium.stages.personas as stage

    calls = {"n": 0}
    real = stage._write

    def flaky(path: Path, data: bytes) -> None:
        calls["n"] += 1
        if calls["n"] == 10:
            raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(stage, "_write", flaky)
    with pytest.raises(ConsortiumError) as info:
        generate(study)
    assert info.value.code == "personas_failed"
    assert list((study / "panel").iterdir()) == []


def test_force_failure_keeps_old_panel(study: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import consortium.stages.personas as stage

    generate(study)
    before = _tree(study / "panel")

    def boom(path: Path, data: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(stage, "_write", boom)
    with pytest.raises(ConsortiumError):
        generate(study, force=True)
    assert _tree(study / "panel") == before
    assert sorted(p.name for p in (study / "panel").iterdir()) == ["personas"]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("nars_bands: [low, high]", "nars_bands: []"),
        ("gender: [woman, man]", "gender: []"),
    ],
)
def test_bad_frame_writes_nothing(study: Path, old: str, new: str) -> None:
    _edit(study / "study.yaml", old, new)
    result = _cli(study)
    assert result.exit_code == 1
    assert result.stderr.startswith("config_invalid: personas.")
    assert not (study / "panel").exists()


def test_load_personas_missing_panel(study: Path) -> None:
    with pytest.raises(ConsortiumError) as info:
        load_personas(study)
    assert info.value.code == "panel_missing"


def test_load_personas_corrupt_index(study: Path) -> None:
    generate(study)
    (study / "panel" / "personas" / "index.json").write_text("{}")
    with pytest.raises(ConsortiumError) as info:
        load_personas(study)
    assert info.value.code == "panel_invalid"


# --------------------------------------------------------------------------- purity


@pytest.mark.parametrize("module", ["personas.py", "seeds.py"])
def test_core_modules_import_only_stdlib_and_pydantic(module: str) -> None:
    import sys

    tree = ast.parse((SRC / "core" / module).read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    third_party = roots - set(sys.stdlib_module_names) - {"__future__", "consortium"}
    assert third_party <= {"pydantic"}
    internal = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("consortium")
    }
    assert all(m.startswith("consortium.core.") for m in internal)


# --------------------------------------------------------------------------- review fixes


def test_golden_index_sha256(study: Path) -> None:
    generate(study)
    raw = (study / "panel" / "personas" / "index.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == GOLDEN_INDEX_SHA256


def test_shuffle_never_uses_random_shuffle(study: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_shuffle(*a, **k):
        raise AssertionError("random.shuffle is not version-stable")

    monkeypatch.setattr(random.Random, "shuffle", no_shuffle)
    generate_personas(load_study(study))


def _set_regions(study: Path, regions: str) -> None:
    path = study / "study.yaml"
    text, n = re.subn(
        r"    cultural_region:\n(      - \w+\n)+", f"    cultural_region: {regions}\n",
        path.read_text(),
    )
    assert n == 1
    path.write_text(text)


@pytest.mark.parametrize("bands", ["[low, high]", "[high, low]", "[low]"])
@pytest.mark.parametrize("regions", [None, "[zeta, alpha, mid]", "[a, b, c, d, e]"])
def test_quotas_stratified_by_nars_band(study: Path, bands: str, regions: str | None) -> None:
    _edit(study / "study.yaml", "nars_bands: [low, high]", f"nars_bands: {bands}")
    if regions:
        _set_regions(study, regions)
    cfg = load_study(study)
    personas = generate_personas(cfg)
    n = len(personas)
    for attr in ("age_band", "gender", "cultural_region", "robot_experience"):
        levels = getattr(cfg.personas.quotas, attr)
        k = len(levels)
        marginal = Counter(getattr(p, attr) for p in personas)
        assert [marginal[lv] for lv in levels] == [
            n // k + (1 if i < n % k else 0) for i in range(k)
        ], attr
        for band in cfg.personas.nars_bands:
            counts = Counter(getattr(p, attr) for p in personas if p.nars == band)
            per = [counts[lv] for lv in levels]
            assert max(per) - min(per) <= 1, (attr, band, per)


def test_meta_json_provenance(study: Path) -> None:
    generate(study)
    raw = (study / "panel" / "personas" / "meta.json").read_bytes()
    meta = json.loads(raw)
    assert raw == json.dumps(meta, sort_keys=True, separators=(",", ":")).encode()
    cfg = load_study(study)
    frame = json.dumps(
        cfg.personas.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    wording = (SRC / "templates" / "persona_card" / "wording.yaml").read_bytes()
    assert meta == {
        "seed": 1,
        "frame_sha256": hashlib.sha256(frame).hexdigest(),
        "wording_sha256": hashlib.sha256(wording).hexdigest(),
        "generator_version": "1",
        "design": {
            "fraction": "1", "replicates": 1, "profiles": 32, "resolution": None,
            "generators": [], "defining_relation": None, "aliasing": [],
        },
    }


def test_level_without_phrase_is_config_invalid(study: Path) -> None:
    _set_regions(study, "[zeta, alpha]")
    result = _cli(study)
    assert result.exit_code == 1
    assert result.stderr.strip() == (
        "config_invalid: level_phrases.cultural_region.zeta: no card phrase for this quota level"
    )
    assert not (study / "panel").exists()
    persona = generate_personas(load_study(study))[0]
    with pytest.raises(ValueError, match="no card phrase for cultural_region"):
        render_card(persona, load_card_wording())


def _wording(**changes) -> dict:
    data = json.loads(json.dumps(load_card_wording().model_dump()))
    for dotted, value in changes.items():
        node = data
        *path, last = dotted.split("__")
        for key in path:
            node = node[key]
        node[last] = value
    return data


@pytest.mark.parametrize(
    "sentence",
    [
        "You are very agreeable.",
        "You are a bit neurotic.",
        "You are introverted.",
        "You are Open-minded about most things.",
        "You like extroverts.",
        "You work conscientiously.",
        "You are an extravert.",
        "You score HIGH here.",
    ],
)
def test_wording_rejects_label_stems(sentence: str) -> None:
    with pytest.raises(ValueError, match="label"):
        CardWording.model_validate(_wording(traits__openness__high=sentence))


def test_approved_wording_passes_label_check() -> None:
    CardWording.model_validate(_wording())


@pytest.mark.parametrize("sep", ["\n", "\r", "\u2028", "\x85", "\x0b", "\x0c"])
def test_wording_rejects_multi_line_sentence(sep: str) -> None:
    with pytest.raises(ValueError, match="one line"):
        CardWording.model_validate(_wording(traits__openness__low=f"You rest.{sep}You sleep."))


def test_wording_rejects_label_in_nars_and_phrases() -> None:
    with pytest.raises(ValueError, match="label"):
        CardWording.model_validate(_wording(nars__high="Your NARS score is elevated."))
    with pytest.raises(ValueError, match="label"):
        CardWording.model_validate(_wording(level_phrases__robot_experience__none="low"))


@pytest.mark.parametrize(
    ("template", "match"),
    [
        ("You are {gender}, {age_band}, from {cultural_region}.", "missing placeholder"),
        (
            "You are {gender!r}, {age_band}, from {cultural_region}, {robot_experience}.",
            "conversion or format spec",
        ),
        (
            "You are {gender}, {age_band:>20}, from {cultural_region}, {robot_experience}.",
            "conversion or format spec",
        ),
    ],
)
def test_wording_demographic_template_checks(template: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        CardWording.model_validate(_wording(demographic=template))


def test_canonical_index_keeps_non_ascii() -> None:
    persona = Persona(
        id="p1",
        big_five=dict.fromkeys(TRAITS, "low"),
        nars="low",
        age_band="18-29",
        gender="woman",
        cultural_region="Île-de-France",
        robot_experience="none",
    )
    raw = canonical_index([persona])
    assert "Île-de-France".encode() in raw
    assert raw == json.dumps(
        [persona.model_dump(mode="json")], sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


@pytest.mark.parametrize(
    "index",
    [
        "{}",
        "[]",
        '[{"id":"p1"}]',
        "not json",
    ],
)
def test_load_personas_rejects_bad_index(study: Path, index: str) -> None:
    generate(study)
    (study / "panel" / "personas" / "index.json").write_text(index)
    with pytest.raises(ConsortiumError) as info:
        load_personas(study)
    assert info.value.code == "panel_invalid"


def _rewrite_index(study: Path, fn) -> None:
    path = study / "panel" / "personas" / "index.json"
    data = json.loads(path.read_text())
    fn(data)
    path.write_text(json.dumps(data))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.reverse(),
        lambda d: d[1].update(id="p1"),
        lambda d: d[0]["big_five"].pop("openness"),
        lambda d: d[0]["big_five"].update(openness="medium"),
        lambda d: d[0].update(nars="mid"),
        lambda d: d[0].update(gender=3),
    ],
)
def test_load_personas_rejects_corrupt_entries(study: Path, mutate) -> None:
    generate(study)
    _rewrite_index(study, mutate)
    with pytest.raises(ConsortiumError) as info:
        load_personas(study)
    assert info.value.code == "panel_invalid"


def test_load_personas_requires_every_card(study: Path) -> None:
    generate(study)
    (study / "panel" / "personas" / "p7.md").unlink()
    with pytest.raises(ConsortiumError) as info:
        load_personas(study)
    assert info.value.code == "panel_invalid"
    assert "p7.md" in info.value.message


def test_stale_work_dirs_are_swept(study: Path) -> None:
    generate(study)
    panel = study / "panel"
    (panel / ".personas-new-abc").mkdir()
    (panel / ".personas-new-abc" / "p1.md").write_text("x")
    (panel / ".personas-old-def" / "personas").mkdir(parents=True)
    generate(study, force=True)
    assert sorted(p.name for p in panel.iterdir()) == ["personas"]


def test_preserved_old_panel_kept_while_target_absent(study: Path) -> None:
    panel = study / "panel"
    kept = panel / ".personas-old-def" / "personas"
    kept.mkdir(parents=True)
    (kept / "p1.md").write_text("old")
    generate(study)
    assert (kept / "p1.md").read_text() == "old"


def test_panel_appearing_mid_run_is_not_overwritten(
    study: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import consortium.stages.personas as stage

    real = stage.generate_personas

    def racing(cfg):
        other = study / "panel" / "personas"
        other.mkdir(parents=True)
        (other / "p1.md").write_text("theirs")
        return real(cfg)

    monkeypatch.setattr(stage, "generate_personas", racing)
    with pytest.raises(ConsortiumError) as info:
        generate(study)
    assert info.value.code == "panel_exists"
    assert _tree(study / "panel") == {"personas/p1.md": b"theirs"}


def _failing_rename(monkeypatch: pytest.MonkeyPatch, prefixes: tuple[str, ...]) -> None:
    real = os.rename

    def rename(src, dst):
        if Path(src).name.startswith(prefixes) or Path(src).parent.name.startswith(prefixes):
            raise OSError("rename refused")
        real(src, dst)

    monkeypatch.setattr(os, "rename", rename)


def test_force_failure_at_final_rename_restores_old(
    study: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generate(study)
    (study / "panel" / "personas" / "p1.md").write_text("old card")
    before = _tree(study / "panel")
    _failing_rename(monkeypatch, (".personas-new-",))
    result = _cli(study, "--force")
    assert result.exit_code == 1
    assert result.stderr.startswith("personas_failed: ")
    assert _tree(study / "panel") == before
    assert sorted(p.name for p in (study / "panel").iterdir()) == ["personas"]


def test_force_failed_rollback_names_preserved_panel(
    study: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generate(study)
    before = _tree(study / "panel" / "personas")
    _failing_rename(monkeypatch, (".personas-new-", ".personas-old-"))
    with pytest.raises(ConsortiumError) as info:
        generate(study, force=True)
    assert info.value.code == "personas_failed"
    kept = study / info.value.path
    assert info.value.path.startswith("panel/.personas-old-")
    assert info.value.path in info.value.message
    assert _tree(kept) == before
    assert not (study / "panel" / "personas").exists()


def test_old_copy_removal_failure_is_logged(
    study: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import consortium.stages.personas as stage

    generate(study)
    real = stage.shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if Path(path).name.startswith(".personas-old-") and not kwargs.get("ignore_errors"):
            raise OSError("busy")
        real(path, *args, **kwargs)

    monkeypatch.setattr(stage.shutil, "rmtree", rmtree)
    with caplog.at_level(logging.WARNING, logger="consortium.stages.personas"):
        generate(study, force=True)
    assert any("could not remove the old Panel copy" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- story 2.4: design

TEMPLATE_BIG_FIVE = "  big_five:\n    fraction: 1\n    replicates: 1\n"


def _design(study: Path, fraction: str, replicates: int = 1) -> None:
    _edit(
        study / "study.yaml",
        TEMPLATE_BIG_FIVE,
        f"  big_five:\n    fraction: {fraction}\n    replicates: {replicates}\n",
    )


def _coded(persona: Persona) -> dict[str, int]:
    return {t[0].upper(): 1 if persona.big_five[t] == "high" else -1 for t in TRAITS}


def _profile_rows(personas: list[Persona]) -> list[dict[str, int]]:
    seen: list[tuple[str, ...]] = []
    rows = []
    for p in personas:
        key = tuple(p.big_five[t] for t in TRAITS)
        if key not in seen:
            seen.append(key)
            rows.append(_coded(p))
    return rows


def _balanced_and_orthogonal(rows: list[dict[str, int]]) -> None:
    letters = "OCEAN"
    for a in letters:
        assert sum(r[a] for r in rows) == 0, a
        for b in letters:
            if a < b:
                assert sum(r[a] * r[b] for r in rows) == 0, (a, b)


def test_template_uses_mapping_form(study: Path) -> None:
    assert TEMPLATE_BIG_FIVE in (study / "study.yaml").read_text()


def test_legacy_all_32_gives_golden_panel(study: Path, tmp_path: Path) -> None:
    _edit(study / "study.yaml", TEMPLATE_BIG_FIVE, "  big_five: all_32\n")
    cfg = load_study(study)
    assert (cfg.personas.big_five.fraction, cfg.personas.big_five.replicates) == ("1", 1)
    generate(study)
    template = init_study(tmp_path / "t")
    generate(template)
    a, b = _tree(study / "panel"), _tree(template / "panel")
    assert len(a) == 66
    assert {k: v for k, v in a.items() if k != "personas/meta.json"} == {
        k: v for k, v in b.items() if k != "personas/meta.json"
    }
    raw = (study / "panel" / "personas" / "index.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == GOLDEN_INDEX_SHA256


def test_half_fraction(study: Path) -> None:
    _design(study, "1/2")
    result = _cli(study)
    assert result.exit_code == 0, result.output
    assert result.stdout == "32 personas -> panel/personas\n"
    personas = load_personas(study)
    assert [p.id for p in personas] == [f"p{i}" for i in range(1, 33)]
    assert [p.nars for p in personas[:4]] == ["low", "high", "low", "high"]
    rows = _profile_rows(personas)
    assert len(rows) == 16
    for r in rows:
        assert r["N"] == r["O"] * r["C"] * r["E"] * r["A"]
    _balanced_and_orthogonal(rows)
    full = [
        tuple(p.big_five[t] for t in TRAITS)
        for p in generate_personas(load_study(init_study(study.parent / "full")))[::2]
    ]
    kept = [full.index(tuple(p.big_five[t] for t in TRAITS)) + 1 for p in personas[::2]]
    assert kept == [2, 3, 5, 8, 9, 12, 14, 15, 17, 20, 22, 23, 26, 27, 29, 32]


def test_quarter_fraction_times_three(study: Path) -> None:
    _design(study, "0.25", 3)
    personas = generate(study)
    assert len(personas) == 48
    assert [p.id for p in personas] == [f"p{i}" for i in range(1, 49)]
    rows = _profile_rows(personas)
    assert len(rows) == 8
    for r in rows:
        assert r["A"] == r["O"] * r["C"]
        assert r["N"] == r["O"] * r["E"]
    _balanced_and_orthogonal(rows)
    # order: profile, then replicate, then band (band fastest)
    for i, p in enumerate(personas):
        profile, rest = divmod(i, 6)
        assert _coded(p) == rows[profile]
        assert p.nars == ["low", "high"][rest % 2]
    # each replicate gets its own demographic draw
    draws = {
        tuple(getattr(personas[i + 2 * r], a) for r in range(3) for a in ("age_band", "gender"))
        for i in range(0, 48, 6)
    }
    assert len(draws) > 1
    full = [
        tuple(p.big_five[t] for t in TRAITS)
        for p in generate_personas(load_study(init_study(study.parent / "full")))[::2]
    ]
    kept = [full.index(tuple(p.big_five[t] for t in TRAITS)) + 1 for p in personas[::6]]
    assert kept == [4, 7, 10, 13, 17, 22, 27, 32]


@pytest.mark.parametrize(
    ("fraction", "replicates"),
    [("1", 1), ("1", 2), ("1", 3), ("1/2", 1), ("0.5", 2), ("1/4", 1), ("0.25", 3)],
)
@pytest.mark.parametrize("bands", ["[low, high]", "[high]"])
def test_per_band_marginals_with_replicates(
    study: Path, fraction: str, replicates: int, bands: str
) -> None:
    _design(study, fraction, replicates)
    _edit(study / "study.yaml", "nars_bands: [low, high]", f"nars_bands: {bands}")
    _set_regions(study, "[zeta, alpha, mid]")
    cfg = load_study(study)
    personas = generate_personas(cfg)
    profiles = {"1": 32, "1/2": 16, "1/4": 8}[cfg.personas.big_five.fraction]
    n = profiles * replicates * len(cfg.personas.nars_bands)
    assert [p.id for p in personas] == [f"p{i}" for i in range(1, n + 1)]
    for attr in ("age_band", "gender", "cultural_region", "robot_experience"):
        levels = getattr(cfg.personas.quotas, attr)
        k = len(levels)
        marginal = Counter(getattr(p, attr) for p in personas)
        assert [marginal[lv] for lv in levels] == [
            n // k + (1 if i < n % k else 0) for i in range(k)
        ], attr
        for band in cfg.personas.nars_bands:
            counts = Counter(getattr(p, attr) for p in personas if p.nars == band)
            per = [counts[lv] for lv in levels]
            assert max(per) - min(per) <= 1, (attr, band, per)


@pytest.mark.parametrize(
    ("fraction", "replicates"), [("1", 1), ("1/2", 2), ("1/4", 3), ("0.5", 1)]
)
def test_design_reproducible_across_folders(
    tmp_path: Path, fraction: str, replicates: int
) -> None:
    a, b = init_study(tmp_path / "a"), init_study(tmp_path / "b")
    for s in (a, b):
        _design(s, fraction, replicates)
        generate(s)
    assert _tree(a / "panel") == _tree(b / "panel")
    first = _tree(a / "panel")
    shutil.rmtree(a / "panel")
    generate(a)
    assert _tree(a / "panel") == first


@pytest.mark.parametrize(
    ("fraction", "design"),
    [
        (
            "1/2",
            {
                "fraction": "1/2", "replicates": 2, "profiles": 16, "resolution": "V",
                "generators": ["N=OCEA"], "defining_relation": "I=OCEAN",
                "aliasing": [
                    "O=CEAN", "C=OEAN", "E=OCAN", "A=OCEN", "N=OCEA", "OC=EAN", "OE=CAN",
                    "OA=CEN", "ON=CEA", "CE=OAN", "CA=OEN", "CN=OEA", "EA=OCN", "EN=OCA",
                    "AN=OCE",
                ],
            },
        ),
        (
            "0.25",
            {
                "fraction": "1/4", "replicates": 2, "profiles": 8, "resolution": "III",
                "generators": ["A=OC", "N=OE"], "defining_relation": "I=OCA=OEN=CEAN",
                "aliasing": [
                    "O=CA=EN=OCEAN", "C=OA=EAN=OCEN", "E=ON=CAN=OCEA", "A=OC=CEN=OEAN",
                    "N=OE=CEA=OCAN", "CE=AN=OCN=OEA", "CN=EA=OCE=OAN",
                ],
            },
        ),
    ],
)
def test_meta_json_design(study: Path, fraction: str, design: dict) -> None:
    _design(study, fraction, 2)
    generate(study)
    meta = json.loads((study / "panel" / "personas" / "meta.json").read_bytes())
    assert meta["design"] == design
    assert meta["generator_version"] == "1"


@pytest.mark.parametrize(
    ("fraction", "replicates"),
    [("0.3", 1), ("1", 4), ("1", 0), ("0.5", "two"), ("1/3", 1), ("true", 1), ('"0.5"', 1)],
)
def test_bad_design_writes_nothing(study: Path, fraction: str, replicates: object) -> None:
    _design(study, fraction, replicates)  # type: ignore[arg-type]
    result = _cli(study)
    assert result.exit_code == 1
    assert result.stderr.startswith("config_invalid: personas.big_five"), result.stderr
    assert not (study / "panel").exists()


def _board_with_trial(study: Path, with_trial: bool) -> None:
    from consortium.board.db import connect

    conn = connect(study)
    try:
        if with_trial:
            conn.execute(
                "INSERT INTO tests (name, kind, path, sha256, openable, registered_at)"
                " VALUES ('t', 'pilot', 'tests/t.yaml', 'x', 1, 'now')"
            )
            conn.execute(
                "INSERT INTO trials (trial_id, test, session_id, trial_index, instrument,"
                " clip_ids, prompt_variant, order_seed, repeat, agent_id, persona_id,"
                " model_id, seq) VALUES ('t1', 't', 's', 0, 'godspeed', '[]', 'default', 1,"
                " 1, 'a', 'p1', 'm1', 0)"
            )
        else:
            conn.execute(
                "INSERT INTO clips (clip_id, sha256, duration_s, size_bytes, width, height,"
                " fps, pushed_at) VALUES ('c_aaaaaaaa', 'x', 1, 1, 2, 2, 25, 'now')"
            )
    finally:
        conn.close()


@pytest.mark.parametrize("force", [True, False])
def test_panel_in_use_refuses_and_changes_nothing(study: Path, force: bool) -> None:
    generate(study)
    _board_with_trial(study, with_trial=True)
    _design(study, "1/2")
    before = _tree(study)
    result = _cli(study, *(["--force"] if force else []))
    assert result.exit_code == 1
    assert result.stderr.startswith("panel_in_use: "), result.stderr
    assert _tree(study) == before


def test_panel_in_use_even_when_panel_deleted(study: Path) -> None:
    generate(study)
    _board_with_trial(study, with_trial=True)
    shutil.rmtree(study / "panel")
    result = _cli(study)
    assert result.exit_code == 1
    assert result.stderr.startswith("panel_in_use: ")
    assert not (study / "panel").exists()


def test_board_without_trials_allows_regeneration(study: Path) -> None:
    generate(study)
    _board_with_trial(study, with_trial=False)
    _design(study, "1/4")
    result = _cli(study, "--force")
    assert result.exit_code == 0, result.output
    assert result.stdout == "16 personas -> panel/personas\n"


def test_any_trials_without_trials_table(tmp_path: Path) -> None:
    import sqlite3

    from consortium.board.trials import any_trials

    conn = sqlite3.connect(":memory:")
    assert any_trials(conn) is False
    conn.execute("CREATE TABLE trials (trial_id TEXT)")
    assert any_trials(conn) is False
    conn.execute("INSERT INTO trials VALUES ('t1')")
    assert any_trials(conn) is True


def test_panel_in_use_keeps_stale_work_folders(study: Path) -> None:
    generate(study)
    _board_with_trial(study, with_trial=True)
    stale = study / "panel" / ".personas-new-crashed"
    stale.mkdir()
    (stale / "p1.md").write_text("x")
    before = _tree(study)
    result = _cli(study, "--force")
    assert result.exit_code == 1
    assert result.stderr.startswith("panel_in_use: ")
    assert _tree(study) == before
    assert stale.is_dir()


@pytest.mark.parametrize("force", [True, False])
def test_panel_in_use_rechecked_before_rename(
    study: Path, monkeypatch: pytest.MonkeyPatch, force: bool
) -> None:
    import consortium.stages.personas as stage

    if force:
        generate(study)
    before = _tree(study)
    calls: list[int] = []

    def trials_appear(conn) -> bool:  # an ``open`` plans Trials after the first check
        calls.append(1)
        return len(calls) > 1

    monkeypatch.setattr(stage, "any_trials", trials_appear)
    _board_with_trial(study, with_trial=False)
    with pytest.raises(ConsortiumError) as info:
        generate(study, force=force)
    assert info.value.code == "panel_in_use"
    assert len(calls) == 2
    after = {k: v for k, v in _tree(study).items() if not k.startswith("board.db")}
    assert after == before
    assert not list((study / "panel").glob(".personas-*"))


def test_garbage_board_is_unreadable(study: Path) -> None:
    (study / "board.db").write_bytes(b"not a database" * 100)
    result = _cli(study)
    assert result.exit_code == 1
    assert result.stderr.startswith("board_unreadable: "), result.stderr
    assert not (study / "panel").exists()


def _old_board(study: Path, with_trial: bool) -> None:
    import sqlite3

    conn = sqlite3.connect(study / "board.db")
    try:
        conn.execute("CREATE TABLE clips (clip_id TEXT PRIMARY KEY)")
        if with_trial:
            conn.execute("CREATE TABLE trials (trial_id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO trials VALUES ('t1')")
            conn.execute("PRAGMA user_version = 3")
        else:
            conn.execute("PRAGMA user_version = 2")
        conn.commit()
    finally:
        conn.close()


def test_older_board_without_trials_generates(study: Path) -> None:
    _old_board(study, with_trial=False)
    before = (study / "board.db").read_bytes()
    result = _cli(study)
    assert result.exit_code == 0, result.output
    assert (study / "board.db").read_bytes() == before  # never migrated


def test_older_board_with_trials_is_in_use(study: Path) -> None:
    _old_board(study, with_trial=True)
    result = _cli(study)
    assert result.exit_code == 1
    assert result.stderr.startswith("panel_in_use: ")
    assert not (study / "panel").exists()


@pytest.mark.parametrize("fraction", ["1", "1/2", "1/4"])
@pytest.mark.parametrize("replicates", [2, 3])
def test_replicates_have_distinct_demographics(
    study: Path, fraction: str, replicates: int, caplog: pytest.LogCaptureFixture
) -> None:
    _design(study, fraction, replicates)
    cfg = load_study(study)
    with caplog.at_level(logging.WARNING):
        personas = generate_personas(cfg)
    assert "replicates_indistinct" not in caplog.text
    groups: dict[tuple, list[tuple]] = {}
    for p in personas:
        key = (tuple(p.big_five[t] for t in TRAITS), p.nars)
        groups.setdefault(key, []).append(
            (p.age_band, p.gender, p.cultural_region, p.robot_experience)
        )
    for key, tuples in groups.items():
        assert len(tuples) == replicates
        assert len(set(tuples)) == replicates, key
    # swaps stay within a band: per-band counts and marginals stay exact
    n = len(personas)
    for attr in ("age_band", "gender", "cultural_region", "robot_experience"):
        levels = getattr(cfg.personas.quotas, attr)
        k = len(levels)
        marginal = Counter(getattr(p, attr) for p in personas)
        assert [marginal[lv] for lv in levels] == [
            n // k + (1 if i < n % k else 0) for i in range(k)
        ]
        for band in cfg.personas.nars_bands:
            per = Counter(getattr(p, attr) for p in personas if p.nars == band)
            assert max(per[lv] for lv in levels) - min(per[lv] for lv in levels) <= 1


def test_replicates_indistinct_warns(study: Path, caplog: pytest.LogCaptureFixture) -> None:
    text = (study / "study.yaml").read_text()
    text = re.sub(r"    age_band: .*\n", "    age_band: [a]\n", text)
    text = text.replace("gender: [woman, man]", "gender: [woman]")
    path = study / "study.yaml"
    path.write_text(text)
    _set_regions(study, "[zeta]")
    _edit(path, "[none, some, regular]", "[none, some]")
    _design(study, "1/4", 3)
    with caplog.at_level(logging.WARNING):
        generate_personas(load_study(study))
    assert "replicates_indistinct: band low" in caplog.text


def test_quota_levels_empty_warns(study: Path, caplog: pytest.LogCaptureFixture) -> None:
    _set_regions(study, "[a, b, c, d, e, f, g, h, i, j]")
    _design(study, "1/4")
    with caplog.at_level(logging.WARNING):
        personas = generate_personas(load_study(study))
    assert "quota_levels_empty: cultural_region: band low has no Persona at" in caplog.text
    assert len(personas) == 16


def test_no_quota_warning_for_template(study: Path, caplog: pytest.LogCaptureFixture) -> None:
    _design(study, "1/4")
    with caplog.at_level(logging.WARNING):
        generate_personas(load_study(study))
    assert "quota_levels_empty" not in caplog.text
