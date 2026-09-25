"""``hermes update`` skips the editable reinstall when the pull can't affect it.

``uv pip install -e .`` never audits an editable target — it reinstalls on
every invocation and rewrites the console-script shims each time. On Windows
that rewrite is the only reason the running ``hermes.exe`` gets quarantined,
and a quarantine that loses its race is the ``os error 32`` family. The gate
under test removes the reinstall (and therefore the rename) for any update
that touches none of the files defining the install.

Two independent conditions must hold for that skip to be safe:

1. the pull touched no file that defines the install (the ``git diff``
   pathspec below), and
2. the venv's editable finder still maps every top-level name the checkout
   exposes — the finder's ``MAPPING`` is a snapshot taken at build time, so a
   pull that *adds* a root module or package invalidates it while touching no
   install-defining file at all (``setup.py::_root_py_modules`` derives root
   modules from the tree; pyproject dropped its static list for exactly this
   drift). Skipping the reinstall then is #119466: the update reports success
   and the gateway crash-loops on ``ModuleNotFoundError: No module named
   'hermes_platform'``.

These tests drive a REAL git repository. The predicate is a ``git diff``
pathspec against the pre-pull SHA; mocking git would assert our idea of what
git prints rather than what it does.
"""

import shutil
import subprocess

import pytest

from hermes_cli.update_cmd import _editable_install_is_current

GIT = ["git"]


def _write_editable_venv(repo, mapping, *, version="0.0.0"):
    """Write the venv state a previous ``pip install -e .`` left behind: an active ``.pth``
    plus the finder module it imports, whose ``MAPPING`` is a name -> target-path snapshot
    in the exact shape setuptools emits (a root module's target carries no ``.py`` suffix)."""
    site_packages = repo / "venv" / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    module = f"__editable___hermes_agent_{version.replace('.', '_')}_finder"
    entries = "".join(f"    {name!r}: {str(target)!r},\n" for name, target in mapping.items())
    (site_packages / f"{module}.py").write_text(f"MAPPING = {{\n{entries}}}\n")
    (site_packages / f"__editable__.hermes_agent-{version}.pth").write_text(
        f"import {module}; {module}.install()\n"
    )


@pytest.fixture
def repo(tmp_path):
    """A repo with one commit, standing in for the pre-pull checkout, plus the editable
    install of that commit — the state the venv is in when ``hermes update`` starts."""
    subprocess.run(GIT + ["init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        GIT + ["config", "user.email", "t@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(GIT + ["config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'hermes'\nversion = '0.0.0'\n\n"
        "[tool.setuptools.packages.find]\n"
        "include = ['agent', 'agent.*', 'platformkit', 'platformkit.*']\n"
    )
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "__init__.py").write_text("")
    (tmp_path / "cli.py").write_text("x = 1\n")
    subprocess.run(GIT + ["add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(GIT + ["commit", "-qm", "base"], cwd=tmp_path, check=True)
    _write_editable_venv(tmp_path, {"agent": tmp_path / "agent", "cli": tmp_path / "cli"})
    return tmp_path


def _head(cwd):
    return subprocess.run(
        GIT + ["rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(cwd, message):
    subprocess.run(GIT + ["add", "-A"], cwd=cwd, check=True)
    subprocess.run(GIT + ["commit", "-qm", message], cwd=cwd, check=True)


def test_source_only_pull_skips_the_reinstall(repo):
    """The common update: .py churn inside already-mapped packages."""
    before = _head(repo)
    (repo / "cli.py").write_text("x = 2\n")
    (repo / "agent" / "loop.py").write_text("y = 1\n")
    _commit(repo, "source churn")

    assert _editable_install_is_current(GIT, repo, before) is True


def test_new_submodule_in_mapped_package_skips_the_reinstall(repo):
    """A new file inside an existing package resolves through its __path__."""
    before = _head(repo)
    (repo / "agent" / "brand_new.py").write_text("z = 1\n")
    _commit(repo, "new submodule")

    assert _editable_install_is_current(GIT, repo, before) is True


@pytest.mark.parametrize(
    "filename",
    ["pyproject.toml", "setup.py", "setup.cfg", "MANIFEST.in", "uv.lock"],
)
def test_touching_a_file_that_defines_the_install_forces_the_reinstall(repo, filename):
    """Dependencies, entry points and the static module list all live here."""
    before = _head(repo)
    (repo / filename).write_text("# changed\n")
    _commit(repo, f"touch {filename}")

    assert _editable_install_is_current(GIT, repo, before) is False


def test_source_churn_alongside_a_pyproject_edit_still_reinstalls(repo):
    """The gate must not be fooled by burying the pyproject diff in noise."""
    before = _head(repo)
    (repo / "cli.py").write_text("x = 3\n")
    (repo / "pyproject.toml").write_text("[project]\nname = 'hermes'\ndeps = []\n")
    _commit(repo, "mixed")

    assert _editable_install_is_current(GIT, repo, before) is False


def test_missing_pre_pull_sha_fails_closed(repo):
    """No SHA (ZIP swap, capture failure) means we cannot prove anything."""
    assert _editable_install_is_current(GIT, repo, None) is False
    assert _editable_install_is_current(GIT, repo, "") is False


def test_unresolvable_pre_pull_sha_fails_closed(repo):
    """A shallow checkout whose base commit isn't present reinstalls as before."""
    assert _editable_install_is_current(GIT, repo, "0" * 40) is False


def test_unusable_git_fails_closed(repo):
    """A git that cannot be executed must not be read as 'nothing changed'."""
    before = _head(repo)
    assert _editable_install_is_current(["definitely-not-git"], repo, before) is False


# --- Condition 2: the finder's MAPPING still covers the checkout (#119466) ---


def test_new_root_module_stales_the_mapping(repo, capsys):
    """A root ``.py`` added by the pull has no key in the frozen ``MAPPING``.

    ``setup.py::_root_py_modules`` derives that list from the tree *at build time*,
    so the pull changes no install-defining file — the diff gate alone calls the
    editable install current and the gateway dies on the import.
    """
    before = _head(repo)
    (repo / "hermes_brand_new.py").write_text("z = 1\n")
    _commit(repo, "new root module")

    assert _editable_install_is_current(GIT, repo, before) is False
    out = capsys.readouterr().out
    assert "stale" in out
    assert "hermes_brand_new" in out


def test_new_top_level_package_stales_the_mapping(repo):
    """A new package the discovery include already covers is mapped by a fresh build."""
    before = _head(repo)
    (repo / "platformkit").mkdir()
    (repo / "platformkit" / "__init__.py").write_text("")
    _commit(repo, "new package")

    assert _editable_install_is_current(GIT, repo, before) is False


def test_stale_mapping_without_any_pull(repo):
    """Coverage is checked against the tree on disk, not only against the diff: a mapping
    left stale by an interrupted install still cannot skip the reinstall."""
    before = _head(repo)
    _write_editable_venv(repo, {"cli": repo / "cli"})  # `agent` dropped from the snapshot

    assert _editable_install_is_current(GIT, repo, before) is False


def test_mapping_target_that_no_longer_resolves(repo):
    """The snapshot maps names to a checkout path; a deleted or moved copy keeps the key
    but can never produce a spec."""
    before = _head(repo)
    _write_editable_venv(
        repo, {"agent": repo / "agent", "cli": repo / "vanished"}
    )

    assert _editable_install_is_current(GIT, repo, before) is False


def test_names_resolve_through_any_active_finder(repo):
    """A leftover finder from an older install still has its ``.pth``, so names resolve
    through whichever active finder maps them — the coverage check must not pick one."""
    before = _head(repo)
    _write_editable_venv(repo, {"agent": repo / "agent"})
    _write_editable_venv(repo, {"cli": repo / "cli"}, version="0.0.1")

    assert _editable_install_is_current(GIT, repo, before) is True


def test_venv_without_an_editable_install_fails_closed(repo):
    """No finder to read means nothing can be proven about the mapping."""
    before = _head(repo)
    shutil.rmtree(repo / "venv")

    assert _editable_install_is_current(GIT, repo, before) is False


def test_active_finder_that_cannot_be_read_fails_closed(repo):
    """A ``.pth`` importing a finder that is gone or malformed proves nothing."""
    before = _head(repo)
    site_packages = repo / "venv" / "lib" / "python3.12" / "site-packages"
    (site_packages / "__editable___hermes_agent_0_0_0_finder.py").unlink()

    assert _editable_install_is_current(GIT, repo, before) is False


def test_compat_mode_editable_pth_counts_as_covered(repo):
    """The compat editable mode puts the checkout itself on sys.path through the ``.pth``,
    so every name resolves from the tree — there is no snapshot that could go stale."""
    before = _head(repo)
    shutil.rmtree(repo / "venv")
    site_packages = repo / "venv" / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    (site_packages / "__editable__.hermes_agent-0.0.0.pth").write_text(f"{repo}\n")

    assert _editable_install_is_current(GIT, repo, before) is True


def test_unreadable_package_discovery_fails_closed(repo):
    """Without ``packages.find`` there is no build rule to check the mapping against."""
    before = _head(repo)
    (repo / "pyproject.toml").write_text("[project]\nname = 'hermes'\n")

    assert _editable_install_is_current(GIT, repo, before) is False

