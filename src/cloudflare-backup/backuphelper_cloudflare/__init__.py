"""Cloudflare backup/restore/diff extension for the BackupHelper engine.

Ships three things a consuming image installs on top of the central engine:

* a ``cloudflare`` **Source** (``source.py``) that exports the live Cloudflare
  account to Terraform HCL via ``cf-terraforming`` and returns it as one bundled
  component — the engine then hashes, deterministically bundles, optionally
  encrypts, uploads off-site and applies retention;
* a ``cloudflare`` **command group** (``commands.py``) adding the
  Cloudflare/Terraform-specific verbs the generic engine lacks —
  ``diff`` (compare two snapshots), ``apply`` (push HCL back to Cloudflare,
  plan-gated) and ``drift`` (what changed since the last backup);
* the engine-independent core (``config``/``cfapi``/``tofu``/``cfterraforming``/
  ``export``/``diff``/``resources``) that carries all the logic and unit-tests
  without the engine installed.
"""

__version__ = "0.0.0"
