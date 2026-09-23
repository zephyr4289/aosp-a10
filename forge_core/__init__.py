"""ROMForge — a universal, zero-cost, GitHub-Actions-native ROM build system.

Subsystems:
  env      runner detection / disk strategy / watchdog helpers
  config   ROM + device + version-capability profiles (YAML)
  chunker  streaming split/compress archives (zstd preferred, gzip fallback)
  store    hybrid state store: GitHub Releases (permanent) / Artifacts / local FS
  syncer   shallow manifest sync + device repos + content-addressed snapshots
  relay    exact-resume out/ state relay (the fix for linear slice redo)
  engine   the slice build engine (process-group watchdog, budget, classify)
  turbo    parallel partition prewarm planner + merge
  gate     14-point anti-brick hard verification gate
"""

__version__ = "1.0.0"
