"""Starter closed-loop agent for Track 2A.

Copy this file, fill in the two CUSTOMIZE sections, and run it.

The agent loops up to `--iterations` times:
  1. Design  — your strategy produces a transfer array for 8 wells
  2. Act     — the workflow is instantiated and sent for approval
  3. Wait    — polls until the workflow completes (~60–90 min)
  4. Observe — reads delta OD600 for each well
  5. Loop    — repeat with the full history available to your strategy

Transfer array format — each entry is a dict:
  {
    "src_plate": "reagent" | "experiment" | "cell_culture_stock",
    "src_well":  "A1",
    "dst_plate": "reagent" | "experiment" | "cell_culture_stock",
    "dst_well":  "B3",
    "volume":    25,                  # µL
    "new_tip":   "always",            # "always" | "once" | "never" (default: "always")
    "blow_out":  True,                # optional
    "post_mix_volume": 10,            # optional: mix after dispensing
    "post_mix_reps":   3,             # optional
  }

Tip policies:
  "always"  — fresh tip per transfer
  "once"    — one tip per unique (src_plate, src_well); reused for all transfers from that source
  "never"   — reuse whatever tip is currently held

Usage:
    python examples/starter_agent.py \\
        --plate TEAM-R1-20260314 \\
        --reagent-name "Team Alpha Stock Plate" \\
        --cell-stock CELLS-20260314 \\
        --iterations 5
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from monomer.datasets import fetch_absorbance_results
from monomer.mcp_client import McpClient
from monomer.transfers import ROWS
from monomer.workflows import (
    instantiate_workflow,
    poll_workflow_completion,
    register_workflow,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

WORKFLOW_TEMPLATE = Path(__file__).parent / "workflow_definition_template.py"


# ── CUSTOMIZE 1: Your stock plate layout ──────────────────────────────────────
#
# List the wells you filled on your stock plate (see REAGENT_PLATE.md).
# This is just a reference — update it to match what the Monomer team loaded.
# Your agent can import this dict to look up source wells by reagent name.
#
STOCK_PLATE = {
    "A1": "Glucose",   # e.g. 1M stock
    "B1": "NaCl",      # e.g. 5M stock
    "C1": "MgSO4",     # e.g. 1M stock
    "D1": "Novel Bio", # base media — always D1, always present
    # "E1": "KCl",
    # "F1": "MOPS",
}

WELL_VOLUME_UL = 180   # total volume per experimental well
BASE_MEDIA_WELL = "D1" # Novel Bio — one tip reused for all D1 transfers


# ── CUSTOMIZE 2: Your optimization strategy ───────────────────────────────────
#
# Return a transfer array for this iteration: list of dicts.
# See the module docstring for the full field reference.
#
# Rules:
#   - "reagent" src_plate refers to your stock plate (wells in STOCK_PLATE above)
#   - "experiment" dst_plate wells are on the 96-well culture plate
#   - "cell_culture_stock" src_plate is the warm cell stock plate (for seeding)
#   - each experiment dest well should total exactly 180 µL
#   - Novel Bio (BASE_MEDIA_WELL) fills remaining volume — put it first with new_tip "once"
#   - max 40 transfers per iteration
#   - P50: 1–50 µL | P200: 51–200 µL | P1000: 201–1000 µL
#
# `history` is a list of dicts from previous iterations:
#   [{"iteration": 1, "column_index": 2, "transfers": [...], "od": {"A2": 0.42, ...}}, ...]
#
def design_next_iteration(
    iteration: int,
    column_index: int,
    history: list[dict],
) -> list[dict]:
    """Return the transfer array for this iteration.

    Replace the body of this function with your optimization strategy.
    The default below fills every well with pure base media — a useful
    baseline sanity check, but not a strategy.
    """
    # TODO: implement your strategy here.
    #
    # Example: fill A{col} as control (all base media) and B{col} with 20 µL Glucose:
    #   transfers = [
    #       # Base media — new_tip "once" reuses one tip for all D1 transfers
    #       {"src_plate": "reagent", "src_well": BASE_MEDIA_WELL,
    #        "dst_plate": "experiment", "dst_well": f"A{column_index}",
    #        "volume": 180, "new_tip": "once", "blow_out": True},
    #       {"src_plate": "reagent", "src_well": BASE_MEDIA_WELL,
    #        "dst_plate": "experiment", "dst_well": f"B{column_index}",
    #        "volume": 160, "new_tip": "once", "blow_out": True},
    #       # Supplement
    #       {"src_plate": "reagent", "src_well": STOCK_PLATE["Glucose"],
    #        "dst_plate": "experiment", "dst_well": f"B{column_index}",
    #        "volume": 20, "new_tip": "once", "blow_out": True},
    #   ]

    dest_wells = [f"{row}{column_index}" for row in ROWS]
    return [
        {"src_plate": "reagent", "src_well": BASE_MEDIA_WELL,
         "dst_plate": "experiment", "dst_well": well,
         "volume": WELL_VOLUME_UL, "new_tip": "once", "blow_out": True}
        for well in dest_wells
    ]


# ── Boilerplate — you shouldn't need to change anything below ─────────────────

def run_agent(
    plate_barcode: str,
    reagent_name: str,
    cell_stock_barcode: str,
    n_iterations: int = 5,
    workcell_url: str = "http://192.168.68.55:8080",
) -> None:
    client = McpClient(workcell_url)

    log.info("Registering workflow definition...")
    def_id = register_workflow(
        client,
        WORKFLOW_TEMPLATE,
        name=f"Starter Agent — {plate_barcode}",
    )
    log.info("Definition ID: %d", def_id)

    history: list[dict] = []
    monitoring_wells: list[str] = []

    for iteration in range(1, n_iterations + 1):
        log.info("=== Iteration %d / %d ===", iteration, n_iterations)

        # Experiments start at column 2; column 1 is reserved for seed wells
        column_index = iteration + 1
        if column_index > 12:
            log.warning("Plate full — stopping after iteration %d", iteration - 1)
            break

        dest_wells       = [f"{row}{column_index}" for row in ROWS]
        monitoring_wells = monitoring_wells + dest_wells

        # ── Design ───────────────────────────────────────────────────────────
        transfers = design_next_iteration(iteration, column_index, history)
        log.info("%d transfers → column %d (%s–%s)", len(transfers), column_index,
                 dest_wells[0], dest_wells[-1])

        # ── Act ───────────────────────────────────────────────────────────────
        uuid = instantiate_workflow(
            client,
            definition_id=def_id,
            plate_barcode=plate_barcode,
            extra_inputs={
                "transfer_array":                json.dumps(transfers),
                "monitoring_wells":              json.dumps(monitoring_wells),
                "reagent_name":                  reagent_name,
                "cell_culture_stock_plate_barcode": cell_stock_barcode,
            },
            reason=f"Iteration {iteration}/{n_iterations}",
        )
        log.info("Workflow %s submitted — awaiting operator approval...", uuid)

        # ── Wait ──────────────────────────────────────────────────────────────
        poll_workflow_completion(
            client,
            uuid,
            timeout_minutes=180,
            on_status=lambda s, t: log.info("  %dm elapsed: %s", t // 60, s),
        )

        # ── Observe ───────────────────────────────────────────────────────────
        raw = fetch_absorbance_results(client, plate_barcode, column_index=column_index)
        od = {
            well: raw["endpoint"].get(well, 0.0) - raw["baseline"].get(well, 0.0)
            for well in dest_wells
        }
        log.info("Delta OD600: %s", {w: f"{v:.3f}" for w, v in od.items()})

        history.append({
            "iteration":    iteration,
            "column_index": column_index,
            "transfers":    transfers,
            "od":           od,
        })

        # Save after each iteration — safe to crash and restart
        Path("runs").mkdir(exist_ok=True)
        Path("runs/history.json").write_text(json.dumps(history, indent=2))
        log.info("History saved → runs/history.json")

    log.info("Done. %d iteration(s) complete.", len(history))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Track 2A starter closed-loop agent")
    parser.add_argument("--plate",        required=True,
                        help="Experiment plate barcode (e.g. TEAM-R1-20260314)")
    parser.add_argument("--reagent-name", required=True,
                        help="Stock plate reagent_name tag from the Monomer team")
    parser.add_argument("--cell-stock",   required=True,
                        help="Cell culture stock plate barcode")
    parser.add_argument("--iterations",   type=int, default=5,
                        help="Number of iterations to run (default 5)")
    parser.add_argument("--workcell",     default="http://192.168.68.55:8080",
                        help="Autoplat MCP base URL")
    args = parser.parse_args()

    run_agent(
        plate_barcode=args.plate,
        reagent_name=args.reagent_name,
        cell_stock_barcode=args.cell_stock,
        n_iterations=args.iterations,
        workcell_url=args.workcell,
    )
