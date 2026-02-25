"""Internal implementation core for Responses API parity.

This package is intentionally separate from `vtol.types`:
- `vtol.types` defines wire-contract Pydantic models and should remain importable without
  pulling in orchestration/state machines.
- `vtol.responses_core` owns internal normalization and contract-composition logic.
"""
