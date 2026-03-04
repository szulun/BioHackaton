"""Hackathon tutorial workflow: seed 6 conditions × 3 replicates, then monitor.

OVERVIEW
--------
This is the tutorial workflow for Step 1 of the hackathon. It:
  1. Takes a baseline OD600 read of the cell stock plate (24-well)
  2. Takes a blank read of the experiment plate (96-well)
  3. Seeds 6 seeding density conditions in triplicate into the experiment plate
  4. Monitors growth every 10 minutes for 90 minutes post-seeding

PLATE LAYOUT (experiment plate, 96-well flat)
---------------------------------------------
  Columns 1–6 → one seeding density condition each
  Rows    A–C → three replicates per condition

SEEDING DENSITY GRADIENT
-------------------------
Media and cells are transferred from the same source wells every time:
  - Novel Media  → from reagent plate A1   (Team Swamp Marsh Reagent Plate)
  - Cell stock   → from cell stock plate A1 (Team Swamp Marsh Cell Stock Plate)

Total volume per well is always 200 µL. Cell stock % decreases left to right:

  Col 1: 100 µL media + 100 µL cells  (50.0 % cells)
  Col 2: 119 µL media +  81 µL cells  (40.5 %)
  Col 3: 138 µL media +  62 µL cells  (31.0 %)
  Col 4: 157 µL media +  43 µL cells  (21.5 %)
  Col 5: 176 µL media +  24 µL cells  (12.0 %)
  Col 6: 195 µL media +   5 µL cells  ( 2.5 %)

HOW TO USE
----------
Register this file once at the start of your session:

    from monomer.mcp_client import McpClient
    from monomer.workflows import register_workflow

    client = McpClient("http://192.168.68.55:8080")
    def_id = register_workflow(client, Path("hackathon_tutorial_workflow_definition_template.py"))

Then instantiate it with your plate barcodes:

    from monomer.workflows import instantiate_workflow

    uuid = instantiate_workflow(
        client,
        definition_id=def_id,
        plate_barcode="SWAMP_Tutorial_Experiment",
        cell_culture_stock_plate_barcode="Team Swamp Marsh Cell Stock Plate",
        reagent_name="Team Swamp Marsh Reagent Plate",
        reason="Tutorial: seeding density screen",
    )
"""

from __future__ import annotations

import json

from src.platform.core_domain.units import Time
from src.workflows.workflow_definition_dsl.workflow_definition_descriptor import (
    MoreThanConstraint,
    RoutineReference,
    WorkflowDefinitionDescriptor,
)

# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------

_ROWS = ["A", "B", "C"]  # 3 replicates per condition
_NUM_CONDITIONS = 6  # columns 1–6

# Linear seeding gradient: cells go from 100 µL down to 5 µL across 6 conditions.
# Total volume is always 200 µL — media fills the remainder.
_CELL_UL: list[int] = [
    round(100 + (5 - 100) * i / (_NUM_CONDITIONS - 1)) for i in range(_NUM_CONDITIONS)
]  # [100, 81, 62, 43, 24, 5]
_MEDIA_UL: list[int] = [200 - v for v in _CELL_UL]  # [100, 119, 138, 157, 176, 195]

# All 18 experiment wells that will be seeded — used for post-seeding monitoring
_SEEDED_WELLS: list[str] = [
    f"{row}{col}" for col in range(1, _NUM_CONDITIONS + 1) for row in _ROWS
]

# Post-seeding monitoring: 9 reads × 10 min = 90 min
_MONITORING_READINGS = 9
_MONITORING_INTERVAL_MINUTES = 10


# ---------------------------------------------------------------------------
# Transfer array builder
# ---------------------------------------------------------------------------


def _build_transfer_array() -> list[dict]:
    """Build the 36-entry transfer array: media fills first, then cell seeding.

    Media fills use new_tip="once" (one shared tip from sterile stock A1).
    Cell seeding uses new_tip="always" (fresh tip per well, post-mix to homogenize).
    """
    transfers: list[dict] = []

    # Phase A: media fills (all 18 wells, one tip reused across all — same sterile source)
    for col_idx, media_vol in enumerate(_MEDIA_UL):
        col = col_idx + 1
        for row in _ROWS:
            transfers.append(
                {
                    "src_plate": "reagent",
                    "src_well": "A1",
                    "dst_plate": "experiment",
                    "dst_well": f"{row}{col}",
                    "volume": media_vol,
                    "new_tip": "once",
                    "blow_out": True,
                }
            )

    # Phase B: cell seeding (fresh tip per well, post-mix to distribute cells)
    for col_idx, cell_vol in enumerate(_CELL_UL):
        col = col_idx + 1
        for row in _ROWS:
            transfers.append(
                {
                    "src_plate": "cell_culture_stock",
                    "src_well": "A1",
                    "dst_plate": "experiment",
                    "dst_well": f"{row}{col}",
                    "volume": cell_vol,
                    "new_tip": "always",
                    "blow_out": True,
                    "post_mix_volume": 50,
                    "post_mix_reps": 3,
                }
            )

    return transfers


# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------


def build_definition(
    plate_barcode: str = "SWAMP_Tutorial_Experiment",
    reagent_name: str = "Team Swamp Marsh Reagent Plate",
    cell_culture_stock_plate_barcode: str = "Team Swamp Marsh Cell Stock Plate",
) -> WorkflowDefinitionDescriptor:
    """Tutorial seeding workflow: baseline reads → 6×3 density gradient → 90-min monitoring.

    :param plate_barcode: Barcode of the 96-well experiment plate (warm, STX220).
    :param reagent_name: Reagent name tag for the team's stock plate (LPX220 cold storage).
        Novel Media must be loaded in well A1.
    :param cell_culture_stock_plate_barcode: Barcode of the 24-well cell stock plate
        (warm, STX220). V. natriegens culture must be in well A1.
    """
    workflow = WorkflowDefinitionDescriptor(
        description=(
            f"Tutorial seeding: {_NUM_CONDITIONS} density conditions × {len(_ROWS)} replicates, "
            f"{_MONITORING_READINGS * _MONITORING_INTERVAL_MINUTES}-min OD600 monitoring"
        ),
    )

    transfer_array = _build_transfer_array()

    # -----------------------------------------------------------------------
    # Phase 1: Baseline reads
    # Read the cell stock plate (24-well) to capture baseline culture density.
    # Read the experiment plate (96-well) to capture the media blank.
    # Both reads happen before any liquid handling.
    # -----------------------------------------------------------------------

    workflow.add_routine(
        "read_cell_stock_baseline",
        RoutineReference(
            routine_name="Measure Absorbance",
            routine_parameters={
                "culture_plate_barcode": cell_culture_stock_plate_barcode,
                "method_name": "24wp_od600",
            },
        ),
    )

    workflow.add_routine(
        "read_experiment_blank",
        RoutineReference(
            routine_name="Measure Absorbance",
            routine_parameters={
                "culture_plate_barcode": plate_barcode,
                "method_name": "96wp_od600",
                "wells_to_process": _SEEDED_WELLS,
            },
        ),
    )

    # -----------------------------------------------------------------------
    # Phase 2: Seeding transfers
    # Media fills first (one shared tip), then cell seeding (fresh tip per well).
    # -----------------------------------------------------------------------

    workflow.add_routine(
        "seed_plate",
        RoutineReference(
            routine_name="Hackathon Transfer Samples",
            routine_parameters={
                "reagent_name": reagent_name,
                "experiment_plate_barcode": plate_barcode,
                "cell_culture_stock_plate_barcode": cell_culture_stock_plate_barcode,
                "transfer_array": json.dumps(transfer_array),
            },
        ),
    )

    # Seed plate must start after both baseline reads complete
    workflow.add_time_constraint(
        MoreThanConstraint(
            from_start="read_cell_stock_baseline",
            to_start="seed_plate",
            value=Time("0 minutes"),
        )
    )
    workflow.add_time_constraint(
        MoreThanConstraint(
            from_start="read_experiment_blank",
            to_start="seed_plate",
            value=Time("0 minutes"),
        )
    )

    # -----------------------------------------------------------------------
    # Phase 3: Post-seeding OD600 monitoring
    # 9 reads × 10 min = 90 minutes, watching all 18 seeded wells.
    # -----------------------------------------------------------------------

    monitoring_keys: list[str] = []
    for i in range(_MONITORING_READINGS):
        key = f"od600_{i + 1}"
        workflow.add_routine(
            key,
            RoutineReference(
                routine_name="Measure Absorbance",
                routine_parameters={
                    "culture_plate_barcode": plate_barcode,
                    "method_name": "96wp_od600",
                    "wells_to_process": _SEEDED_WELLS,
                },
            ),
        )
        monitoring_keys.append(key)

    workflow.space_out_routines(monitoring_keys, Time(f"{_MONITORING_INTERVAL_MINUTES} minutes"))

    # First monitoring read starts 30 seconds after seeding completes
    workflow.add_time_constraint(
        MoreThanConstraint(
            from_start="seed_plate",
            to_start=monitoring_keys[0],
            value=Time("30 seconds"),
        )
    )

    return workflow


if __name__ == "__main__":
    # ruff: noqa: T201
    workflow = build_definition()
    print(json.dumps(workflow.model_dump(), indent=2))
