# ADR 0001: src layout, one package, one image

**Status:** accepted (2026-10-16)

**Context.** The spec lists top-level directories (`engine/`, `models/`, `world/`, …). As top-level Python packages those names collide with common third-party modules and make imports ambiguous.

**Decision.**
- Use a single package, `src/vlsim/`, with sub-packages mirroring the spec's areas.
- Build one Docker image that serves both `sim-engine` (`vlsim run`) and `tms-service` (uvicorn `vlsim.tms.api:app`).

**Consequences.** One lockfile and one build. The engine/TMS boundary is enforced by an interface (ADR 0002), not by packaging. The image is larger than the TMS service alone needs; this is acceptable at MVP scale.
