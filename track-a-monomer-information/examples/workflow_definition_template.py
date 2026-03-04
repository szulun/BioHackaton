"""Hackathon closed-loop workflow definition template — Track 2A.

IMPORTANT: This file is uploaded to and executed on the workcell, not run locally.
The imports below (src.platform, src.workflows) only resolve inside the workcell
Docker environment — running this file directly on your laptop will fail with
ImportError. Use register_workflow() to upload and validate it instead.

HOW TO USE
----------
1. Register this file ONCE at the start of your session:

       from monomer.mcp_client import McpClient
       from monomer.workflows import register_workflow

       client = McpClient("http://192.168.68.55:8080")
       def_id = register_workflow(client, Path("workflow_definition_template.py"))

2. Each iteration, instantiate with your agent's outputs:

       from monomer.workflows import instantiate_workflow

       uuid = instantiate_workflow(
           client,
           definition_id=def_id,
           plate_barcode="TEAM-R1-20260314",
           extra_inputs={
               "transfer_array":   json.dumps(my_transfers),
               "monitoring_wells": json.dumps(all_wells_so_far),
               "reagent_name":     "Team Alpha Stock Plate",
               "cell_culture_stock_plate_barcode": "CELLS-20260314",
           },
           reason="Iteration 1: testing Glucose=20µL center",
       )

WHAT YOUR AGENT MUST PRODUCE EACH ITERATION
--------------------------------------------
  transfer_array    List of transfer dicts (JSON string).
                    Each entry: {src_plate, src_well, dst_plate, dst_well, volume,
                                 new_tip?, blow_out?, pre_mix_volume?, pre_mix_reps?,
                                 post_mix_volume?, post_mix_reps?}
                    plate names: "reagent" | "experiment" | "cell_culture_stock"
                    new_tip: "always" | "once" | "never"
                    Max 40 entries. See REAGENT_PLATE.md for the full field reference.

  monitoring_wells  JSON list of ALL experiment plate wells to read via OD600.
                    CUMULATIVE — include every well from all prior iterations too.
                    e.g. after 2 iterations: ["A2",...,"H2","A3",...,"H3"]

  reagent_name      Tag identifying your stock plate in the Monomer system.
                    Must match the value registered when the Monomer team loaded
                    your plate. Coordinate with a team member to get this string.

  cell_culture_stock_plate_barcode
                    Barcode of the 24-well flat-bottom cell culture stock plate
                    (warm, in the 37°C incubator). Required if any transfer in
                    transfer_array uses src_plate="cell_culture_stock".

PLATE LAYOUT
------------
  Reagent plate (24-well deep well, cold storage):
    Your layout — defined in REAGENT_PLATE.md and registered by the Monomer team.
    D1 = Novel Bio (base media) — always present, reuse tip with new_tip="once"

  Experiment plate (96-well flat, warm incubator):
    Col 1  = reserved for seed wells — do not write here in transfer_array
    Col 2  = iteration 1 results
    Col 3  = iteration 2 results
    ...
    Col 11 = iteration 10 results

  Cell culture stock plate (24-well flat-bottom, warm incubator):
    Your cell stock — transfer to experiment wells for seeding.

WELL REUSE WARNING
------------------
This template does NOT check for well conflicts across iterations.
Track which columns you have used and do not repeat dest wells.
If you restart and need to recover state, query OD600 observations —
any well with a reading has already been inoculated.
"""

from __future__ import annotations

import json

from src.platform.core_domain.units import Time
from src.workflows.workflow_definition_dsl.workflow_definition_descriptor import (
    MoreThanConstraint,
    RoutineReference,
    WorkflowDefinitionDescriptor,
)

# Hard cap on reagent transfer steps per iteration
_MAX_TRANSFERS = 40


def _validate(transfers: list[dict], monitoring_well_list: list[str]) -> None:
    """Validate iteration parameters before the workflow is queued.

    Raises AssertionError with a descriptive message if any constraint is violated.

    :param transfers: Parsed transfer array (list of dicts)
    :param monitoring_well_list: Wells to include in OD600 monitoring
    """
    assert len(transfers) <= _MAX_TRANSFERS, (
        f"Too many transfers ({len(transfers)}): max is {_MAX_TRANSFERS}. "
        "Reduce conditions or reagents per iteration."
    )

    assert len(monitoring_well_list) > 0, (
        "monitoring_wells is empty. Include the dest wells from this iteration."
    )

    valid_plates = {"reagent", "experiment", "cell_culture_stock"}
    for i, t in enumerate(transfers):
        assert isinstance(t, dict), (
            f"Transfer [{i}] is not a dict. "
            "transfer_array must be a JSON list of dicts — see REAGENT_PLATE.md."
        )
        vol = t.get("volume", 0)
        assert isinstance(vol, (int, float)) and vol > 0, (
            f"Transfer [{i}]: volume must be a positive number, got {vol!r}."
        )
        src = t.get("src_plate", "")
        dst = t.get("dst_plate", "")
        assert src in valid_plates, (
            f"Transfer [{i}]: unknown src_plate='{src}'. "
            f"Must be one of: {sorted(valid_plates)}."
        )
        assert dst in valid_plates, (
            f"Transfer [{i}]: unknown dst_plate='{dst}'. "
            f"Must be one of: {sorted(valid_plates)}."
        )


def build_definition(
    plate_barcode: str,
    # ── Agent outputs — set these each iteration ───────────────────────────
    transfer_array: str = "[]",
    monitoring_wells: str = '["A2","B2","C2","D2","E2","F2","G2","H2"]',
    # ── Plate selection ────────────────────────────────────────────────────
    reagent_name: str = "GD Compound Stock Plate",
    cell_culture_stock_plate_barcode: str = "",
    # ── Monitoring window ──────────────────────────────────────────────────
    monitoring_readings: int = 9,
    monitoring_interval_minutes: int = 10,
) -> WorkflowDefinitionDescriptor:
    """Hackathon closed-loop iteration: liquid handling → OD600 monitoring.

    Register this definition once per session; instantiate it per iteration
    by passing fresh inputs to instantiate_workflow().

    One complete iteration:
      Phase 1 — Liquid handling: execute transfer_array on the Opentrons Flex.
                 Plates are unlid only if referenced in transfer_array.
      Phase 2 — OD600 monitoring: read absorbance at fixed intervals for
                 the duration of the monitoring window.

    :param plate_barcode: Barcode of the experiment plate (96-well flat).
    :param transfer_array: JSON string — list of transfer dicts.
        Each dict: {src_plate, src_well, dst_plate, dst_well, volume,
                    new_tip?, blow_out?, pre_mix_volume?, post_mix_volume?, ...}
        src_plate / dst_plate: "reagent" | "experiment" | "cell_culture_stock"
        new_tip: "always" (default) | "once" | "never"
        Max 40 entries. See REAGENT_PLATE.md for the full schema.
    :param monitoring_wells: JSON list of ALL experiment plate wells to measure.
        Cumulative — grows by ~8 wells each iteration.
    :param reagent_name: Tag identifying the stock plate in the Monomer system.
        Must match the value registered by the Monomer team for your plate.
    :param cell_culture_stock_plate_barcode: Barcode of the cell culture stock
        plate (24-well flat-bottom, warm). Required if any transfer uses
        src_plate="cell_culture_stock".
    :param monitoring_readings: Number of OD600 reads (default 9 = 90 min at 10-min intervals).
    :param monitoring_interval_minutes: Minutes between OD600 reads (min 5).
    """
    # ── Parse JSON inputs ────────────────────────────────────────────────────
    transfers: list[dict] = json.loads(transfer_array) if transfer_array else []
    monitoring_well_list: list[str] = json.loads(monitoring_wells)

    # ── Validate ─────────────────────────────────────────────────────────────
    _validate(transfers, monitoring_well_list)

    # ── Build workflow ────────────────────────────────────────────────────────
    workflow = WorkflowDefinitionDescriptor(
        description=(
            f"Hackathon iteration: {len(transfers)} transfers, "
            f"{len(monitoring_well_list)} monitored wells"
        ),
    )

    # Phase 1: Liquid handling
    # Executes the transfer array on the Opentrons Flex. Supports any combination
    # of reagent → experiment, cell_culture_stock → experiment, or intra-plate
    # transfers. Only plates referenced in the array will be unlid.
    liquid_handling = RoutineReference(
        routine_name="hackathon_transfer_samples",
        routine_parameters={
            "reagent_name":                      reagent_name,
            "experiment_plate_barcode":          plate_barcode,
            "cell_culture_stock_plate_barcode":  cell_culture_stock_plate_barcode,
            "transfer_array":                    json.dumps(transfers),
        },
    )
    workflow.add_routine("liquid_handling", liquid_handling)

    # Phase 2: OD600 monitoring loop
    # Reads all wells (cumulative across iterations) at fixed intervals.
    monitoring_keys: list[str] = []
    for i in range(monitoring_readings):
        key = f"od600_{i + 1}"
        workflow.add_routine(
            key,
            RoutineReference(
                routine_name="Measure Absorbance",
                routine_parameters={
                    "culture_plate_barcode": plate_barcode,
                    "method_name":           "96wp_od600",
                    "wells_to_process":      monitoring_well_list,
                },
            ),
        )
        monitoring_keys.append(key)

    # Space monitoring reads evenly across the window
    workflow.space_out_routines(
        monitoring_keys,
        Time(f"{monitoring_interval_minutes} minutes"),
    )

    # First read starts 30 s after liquid handling completes
    workflow.add_time_constraint(
        MoreThanConstraint(
            from_start="liquid_handling",
            to_start=monitoring_keys[0],
            value=Time("30 seconds"),
        )
    )

    return workflow
