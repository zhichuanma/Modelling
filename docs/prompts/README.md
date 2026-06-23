# Modelling Prompt Archive

This directory separates active modelling prompt templates from historical task
prompts. At present, all recovered modelling prompts are historical and live in
`archive/`.

## Historical Prompt Groups

- `01_*` to `11_*`: bus/coach rebuild, review-fix, annual simulation, and depot
  curve task prompts.
- `private_*`, `privatecar_*`, `scotland_*`: private-car simulation and geography
  prompts.
- `ev_assignment_soc_refactor_plan*`: bus EV assignment and SOC carry-over
  planning notes.
- `bus_depot_only_sample_refactor_prompt_cn*`: historical depot-only bus sample
  pipeline prompts; use with caution because later review notes corrected some
  assumptions.

New reusable prompts should be added next to this README with a clear filename,
scope, required inputs, forbidden changes, and verification checklist.
