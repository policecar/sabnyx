"""Guard against the next command module silently falling out of tmp_vault.

2026-10-20 security review: `tmp_vault` (tests/conftest.py) only redirects a
fixed, hand-maintained list of modules. `lint.py` and `reconcile.py` (at
least) were missing — any test exercising them without ALSO manually
monkeypatching DATA_ROOT would read/write under the real default (DATA_ROOT
== ROOT, or the operator's live vault via .env) instead of a tmp dir.

This test derives the "must be patched" module list straight from the
source tree (every module-level `from ..paths import X` where X is a
DATA_ROOT-derived name) and asserts it is fully covered by
`conftest.PATH_BINDING_MODULES`, so a new command module that imports e.g.
`DATA_ROOT` at module level fails this test instead of silently writing
outside any tmp dir.
"""

from __future__ import annotations

import conftest


def test_tmp_vault_patches_every_module_level_data_root_importer() -> None:
    discovered = conftest.modules_requiring_direct_patch()
    patched = {mod.__name__ for mod in conftest.PATH_BINDING_MODULES}

    missing = set(discovered) - patched
    assert not missing, (
        "tmp_vault does not directly patch module(s) with a module-level "
        "DATA_ROOT-derived import — add them to PATH_BINDING_MODULES in "
        f"tests/conftest.py: { {m: sorted(discovered[m]) for m in sorted(missing)} }"
    )


def test_discovery_actually_finds_known_importers() -> None:
    """Sanity check on the AST scanner itself: if this regresses to an empty
    dict, the first test above would pass vacuously and prove nothing."""
    discovered = conftest.modules_requiring_direct_patch()
    assert "sabnyx.commands.lint" in discovered
    assert "sabnyx.commands.reconcile" in discovered
    assert "sabnyx.commands.triage" in discovered
    assert "sabnyx.commands.new" in discovered
    assert "sabnyx.sources" in discovered
    assert "sabnyx.ledger" in discovered
    # ROOT is never redirected — a module importing only ROOT must NOT show up.
    assert "sabnyx.config" not in discovered
    assert "sabnyx.llm" not in discovered


def test_data_root_derived_names_is_nonempty() -> None:
    """Sanity check: if `_data_root_mapping` ever stops returning the usual
    names, the discovery scanner above would also pass vacuously."""
    assert "DATA_ROOT" in conftest.DATA_ROOT_DERIVED_NAMES
    assert "ROOT" not in conftest.DATA_ROOT_DERIVED_NAMES
