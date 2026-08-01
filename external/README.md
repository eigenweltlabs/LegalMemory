# Inspected upstream repositories

These shallow clones are local study material and are excluded from git. They are not
vendored or shipped with Knowledge Index. Recreate them with `git clone`, then check
out the exact commits below when auditing an adapter.

| Local path | Upstream | Inspected commit | License boundary |
|---|---|---|---|
| `external/onyx` | `https://github.com/onyx-dot-app/onyx.git` | `f6a7d963e3103d030b7e8d5e32e980dc9918b042` | MIT outside `ee/`; all `ee/` paths excluded |
| `external/docling` | `https://github.com/docling-project/docling.git` | `d5fc616045899f6ed09edb474fb0106aae8a6a54` | MIT |
| `external/fastmcp` | `https://github.com/PrefectHQ/fastmcp.git` | `1d932cc778a24cc0bf46fc4baad8306d4fed9c4b` | Apache-2.0 |
| `external/harvey-labs` | `https://github.com/harveyai/harvey-labs.git` | `73feb91d63d53b1a44151d99329779c4defcdb72` | MIT |
| `external/hatchet` | `https://github.com/hatchet-dev/hatchet.git` | `8f6e443bf18eff1b3a71ab6e24d771ee67b43933` | MIT |

Pins were recorded on 2026-07-17. Licenses and enterprise carve-outs must be checked
again before upgrading or copying code.
