"""Minimal closed-loop gradient descent agent for Track 2A.

Runs a full multi-iteration experiment:
  1. Register the workflow definition template (once, at session start)
  2. For each iteration:
     a. Generate transfer array from current media composition
     b. Instantiate the workflow with this iteration's inputs
     c. Poll until the workflow completes
     d. Fetch OD600 results and compute the gradient
     e. Update the center point for the next iteration

Usage:
    python examples/basic_agent.py --plate GD-R1-20260314 --iterations 5
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from monomer.datasets import fetch_absorbance_results, parse_od_results
from monomer.mcp_client import McpClient
from monomer.transfers import (
    ROWS,
    SUPPLEMENT_NAMES,
    apply_constraints,
    generate_transfer_array,
)
from monomer.workflows import (
    instantiate_workflow,
    poll_workflow_completion,
    register_workflow,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────

WORKFLOW_TEMPLATE = Path(__file__).parent / "workflow_definition_template.py"

# ── Agent parameters ─────────────────────────────────────────────────────────

LEARNING_RATE = 5    # µL to adjust per unit gradient
DELTA_UL = 10        # perturbation size for gradient estimation


def run_agent(
    plate_barcode: str,
    n_iterations: int = 5,
    workcell_url: str = "http://192.168.68.55:8080",
):
    client = McpClient(workcell_url)

    # ── Register workflow definition ONCE ────────────────────────────────────
    # The same definition is reused for every iteration; each instantiation
    # passes its own transfer_array, dest_wells, etc. as inputs.
    log.info("Registering workflow definition...")
    def_id = register_workflow(
        client,
        WORKFLOW_TEMPLATE,
        name=f"Hackathon GD Agent — {plate_barcode}",
    )
    log.info("Registered workflow definition ID: %d", def_id)

    # ── Starting composition ─────────────────────────────────────────────────
    center = apply_constraints({"Glucose": 20, "NaCl": 10, "MgSO4": 15})
    log.info("Starting composition: %s", center)

    # Track wells used so far for cumulative OD600 monitoring
    monitoring_wells: list[str] = []
    history = []

    for iteration in range(1, n_iterations + 1):
        log.info("=== Iteration %d / %d ===", iteration, n_iterations)
        log.info("Center: %s", center)

        # Column 1 is reserved for seed wells; experiments start at column 2.
        column_index = iteration + 1
        if column_index > 12:
            log.warning("Plate full after iteration %d — stopping", iteration - 1)
            break

        # Seed well advances one row per iteration: A1, B1, C1, ...
        seed_well = f"{ROWS[iteration - 1]}1"
        next_seed_well = f"{ROWS[iteration]}1" if iteration < len(ROWS) else ""

        # Destination wells for this iteration (one full column)
        dest_wells = [f"{r}{column_index}" for r in ROWS]

        # Cumulative monitoring: all wells used so far + this iteration's wells
        monitoring_wells = monitoring_wells + dest_wells

        # ── Step 1: Generate transfer array ──────────────────────────────────
        transfers = generate_transfer_array(
            center, column_index=column_index, delta=DELTA_UL
        )
        log.info(
            "Generated %d transfers → column %d (seed=%s, next=%s)",
            len(transfers), column_index, seed_well, next_seed_well or "none",
        )

        # ── Step 2: Instantiate the workflow ─────────────────────────────────
        uuid = instantiate_workflow(
            client,
            definition_id=def_id,
            plate_barcode=plate_barcode,
            extra_inputs={
                "transfer_array":    json.dumps(transfers),
                "dest_wells":        json.dumps(dest_wells),
                "monitoring_wells":  json.dumps(monitoring_wells),
                "seed_well":         seed_well,
                "next_seed_well":    next_seed_well,
            },
            reason=(
                f"GD iteration {iteration}/{n_iterations}, "
                f"column={column_index}, center={json.dumps(center)}"
            ),
        )
        log.info("Instantiated workflow: %s (pending operator approval)", uuid)

        # ── Step 3: Wait for completion ───────────────────────────────────────
        log.info("Polling for completion — typical runtime 60–90 min...")
        result = poll_workflow_completion(
            client,
            uuid,
            timeout_minutes=180,
            on_status=lambda s, t: log.info("  %dm elapsed: status=%s", t // 60, s),
        )
        log.info("Workflow completed: status=%s", result.get("status"))

        # ── Step 4: Fetch OD600 results ───────────────────────────────────────
        raw = fetch_absorbance_results(client, plate_barcode, column_index=column_index)
        parsed = parse_od_results(raw, column_index=column_index)

        log.info(
            "Results — control: %.3f | center: %.3f",
            parsed["control_od"],
            parsed["center_od"],
        )
        for supp, (r1, r2) in parsed["perturbed_ods"].items():
            log.info(
                "  %s: %.3f, %.3f (avg %.3f)", supp, r1, r2, (r1 + r2) / 2
            )

        history.append({
            "iteration":  iteration,
            "column":     column_index,
            "seed_well":  seed_well,
            "center":     dict(center),
            "parsed":     parsed,
        })

        # ── Step 5: Gradient update ───────────────────────────────────────────
        new_center = dict(center)
        for supp in SUPPLEMENT_NAMES:
            r1, r2 = parsed["perturbed_ods"][supp]
            avg_perturbed = (r1 + r2) / 2
            gradient = avg_perturbed - parsed["center_od"]
            adjustment = int(LEARNING_RATE * gradient)
            new_center[supp] = center[supp] + adjustment
            log.info(
                "  Gradient %s: %.3f → adjust %+d µL", supp, gradient, adjustment
            )

        center = apply_constraints(new_center)
        log.info("Updated center: %s", center)

    log.info("=== Agent finished after %d iterations ===", len(history))
    log.info("Final center composition: %s", center)

    # Save run history
    output_dir = Path("runs")
    output_dir.mkdir(exist_ok=True)
    (output_dir / "history.json").write_text(json.dumps(history, indent=2))
    log.info("History saved to runs/history.json")

    return center, history


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Gradient descent media optimization agent"
    )
    parser.add_argument(
        "--plate", required=True, help="Plate barcode (e.g. GD-R1-20260314)"
    )
    parser.add_argument(
        "--iterations", type=int, default=5, help="Number of iterations to run"
    )
    parser.add_argument(
        "--workcell",
        default="http://192.168.68.55:8080",
        help="Workcell base URL",
    )
    args = parser.parse_args()

    run_agent(
        plate_barcode=args.plate,
        n_iterations=args.iterations,
        workcell_url=args.workcell,
    )
