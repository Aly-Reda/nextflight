# Contributing to nextflight

Thanks for considering a contribution — this project depends on real-world Next.js pages behaving in ways the maintainer hasn't seen yet, so outside eyes are genuinely valuable here.

## Easiest ways to help

- **Found a page nextflight parses incorrectly?** Open an issue with an anonymized snippet of the offending `self.__next_f.push(...)` payload (strip anything identifying/proprietary first). These become regression tests.
- **Next.js shipped a release that changed something?** A short issue with the version number and what broke is enough — see `next_version_hint()` in the README for how this gets tracked.
- **Docs unclear or missing an example?** PRs to `README.md` or `docs/` are welcome without needing to touch any code.

## Code contributions

1. Fork and clone the repo.
2. `pip install -e ".[all]"` to install with every optional extra.
3. Run the test suite: `pytest`.
4. Run benchmarks if your change touches parsing hot paths: `python benchmarks/run_benchmarks.py`.
5. Open a PR describing the real-world case your change handles — a link to a (sanitized) example page helps reviewers a lot.

## Roadmap

See the "Roadmap and design proposals" section in the README for larger, unclaimed directions (plugin system, JS/TS port, Playwright integration) if you're looking for a bigger project to take on.

## Code of conduct

Be respectful, assume good faith, and keep discussion focused on the technical problem. That's it.
