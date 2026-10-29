# Contributing

Thanks for taking a look. This project values **small, boring, verifiable changes**.

## Ground rules

1. **Standard library only.** No new runtime dependencies. A PR that adds one needs an ADR.
2. **The store stays plain Markdown + git.** Anything that makes the on-disk truth less readable
   (`cat atoms/...` must still be pleasant) will be rejected.
3. **Atoms are immutable.** No code path may rewrite or delete an atom file. Corrections are
   superseding atoms; deletions are tombstones.
4. **Deterministic output.** `reindex --force` twice in a row must produce byte-identical files.
   CI does not enforce this yet — do it by hand and say so in the PR.
5. **No personal data in the repo.** Documentation uses `server` / `laptop` / `ci` and RFC 5737
   addresses (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`). Run
   `python3 tools/privacy-scan.py --strict` before pushing.

## Workflow

```bash
python3 -m unittest discover -s tests -v      # 57 tests, no dependencies
python3 tools/privacy-scan.py --strict        # must be clean
python3 tools/release.py                      # version/changelog gate (vendor-neutral checks)
```

Commits: `<type>(<scope>): <what>` with `type ∈ feat|fix|refactor|docs|test|chore|perf|build|ci`.
Explain *why* in the body. One logical change per commit.

A pre-commit hook lives in `.githooks/`; enable it once with `git config core.hooksPath .githooks`.

## Adding a feature

1. Open an issue describing the failure mode you hit, not the API you want.
2. If it changes the data format, the API, or an invariant → write an ADR in `docs/ADR.md`
   (background / decision / consequences) before writing code.
3. Update `docs/SPEC-api.md` in the same PR as the code. Docs and code drift is the default state;
   the only defence is keeping them in one commit.
4. Add a test that fails without your change.

## Release

`docs/VERSIONING.md` has the full policy. Short version: bump `brainlib/__init__.py:__version__`,
move the `[Unreleased]` section of `CHANGELOG.md` to the new version, run
`python3 tools/release.py --apply`.
