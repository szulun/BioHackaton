"""
Monomer Bio Hackathon - Track A
Closed-Loop Bayesian Optimization Agent

Goal:
    Find the media composition that maximizes Vibrio natriegens growth
    in the fewest number of experimental iterations.

Loop:
    Suggest compositions -> Robot executes -> Read OD600 -> Update model -> Repeat
"""

import json
import numpy as np
import pandas as pd
from dataclasses import dataclass
from skopt import Optimizer
from skopt.space import Real

# Competition-day imports (uncomment only when the Monomer package/environment is available)
# from monomer.mcp_client import McpClient
# from monomer.workflows import register_workflow, instantiate_workflow


# ============================================================
# 1. CONFIGURATION
# ============================================================

# Reagent plate layout (well position -> reagent name)
REAGENT_WELLS = {
    "A1": "Glucose",
    "B1": "NaCl",
    "C1": "Tryptone",
    "D1": "base_media",
}

# Search space (uL added per 180 uL well)
# These are conservative starting ranges for round 1.
SEARCH_SPACE = {
    "Glucose":  (0.0, 30.0),
    "NaCl":     (0.0, 20.0),
    "Tryptone": (0.0, 25.0),
}

# Fixed system parameters
TOTAL_WELL_VOLUME = 180.0
CELL_VOLUME = 20.0
BASE_MEDIA_WELL = "D1"
CELL_SOURCE_WELL = "A1"

WELLS_PER_ITERATION = 8
MAX_TRANSFERS = 40
MIN_TRANSFER_VOLUME = 1.0  # Important: skip any transfer smaller than this

TEAM_NAME = "YOUR_TEAM_NAME"
EXPERIMENT_PLATE = f"{TEAM_NAME}_EXPERIMENT"
REAGENT_PLATE_TAG = f"{TEAM_NAME}_REAGENTS"
CELL_STOCK_PLATE = f"{TEAM_NAME}_CELLS"

SIMULATION_MODE = True  # Set to False on competition day when real APIs are enabled


# ============================================================
# 2. DATA STRUCTURES
# ============================================================

@dataclass
class ExperimentResult:
    iteration: int
    well: str
    composition: dict
    od600_readings: list
    delta_od600: float


# ============================================================
# 3. BAYESIAN OPTIMIZATION AGENT
# ============================================================

class BayesianAgent:
    """
    Closed-loop Bayesian Optimization agent.

    It proposes candidate media compositions, then updates its internal model
    after receiving experimental feedback (delta OD600).
    """

    def __init__(self):
        self.reagent_names = list(SEARCH_SPACE.keys())
        self.dimensions = [Real(*SEARCH_SPACE[r], name=r) for r in self.reagent_names]

        self.optimizer = Optimizer(
            dimensions=self.dimensions,
            base_estimator="GP",
            acq_func="EI",           # Expected Improvement
            n_initial_points=8,
            random_state=42,
        )

        self.history = []

    def suggest_next_batch(self, n=WELLS_PER_ITERATION):
        """
        Ask BO for the next batch of candidate compositions.
        """
        raw_suggestions = self.optimizer.ask(n_points=n)
        cleaned = []

        for s in raw_suggestions:
            comp = {}
            for name, val in zip(self.reagent_names, s):
                # Round to 0.1 uL for readability
                v = round(float(val), 1)

                # To avoid invalid robot transfers, treat tiny values as zero
                if v < MIN_TRANSFER_VOLUME:
                    v = 0.0

                comp[name] = v

            cleaned.append(comp)

        return cleaned

    def update(self, compositions, delta_od600_values):
        """
        Update the BO model with observed results.

        skopt minimizes by default, so we negate delta OD600.
        """
        for comp, delta in zip(compositions, delta_od600_values):
            x = [comp[name] for name in self.reagent_names]
            self.optimizer.tell(x, -float(delta))

    def best_so_far(self):
        if not self.history:
            return None
        return max(self.history, key=lambda r: r.delta_od600)


# ============================================================
# 4. HELPER FUNCTIONS
# ============================================================

def compute_base_media_volume(comp):
    """
    Base media fills the remaining volume after reagents + cells.
    """
    reagent_total = sum(comp.values())
    return round(TOTAL_WELL_VOLUME - CELL_VOLUME - reagent_total, 1)


def normalize_if_needed(comp):
    """
    If reagent total exceeds allowed capacity, scale it down.
    """
    max_reagent_volume = TOTAL_WELL_VOLUME - CELL_VOLUME
    reagent_total = sum(comp.values())

    if reagent_total <= max_reagent_volume:
        return comp

    scale = max_reagent_volume / (reagent_total + 1e-9) * 0.95
    scaled = {k: round(v * scale, 1) for k, v in comp.items()}

    # Remove tiny transfers after scaling
    for k in scaled:
        if scaled[k] < MIN_TRANSFER_VOLUME:
            scaled[k] = 0.0

    return scaled


def get_reagent_source_well(reagent_name):
    for well, name in REAGENT_WELLS.items():
        if name == reagent_name:
            return well
    return None


def compute_delta_od600(od_readings):
    """
    Metric used for optimization:
        delta_OD600 = max(OD600) - initial_OD600
    """
    if not od_readings or len(od_readings) < 2:
        return 0.0
    return float(max(od_readings) - od_readings[0])


# ============================================================
# 5. TRANSFER ARRAY BUILDER
# ============================================================

def build_transfer_array(compositions, iteration):
    """
    Convert a list of candidate compositions into robot transfer commands.

    Each well receives:
        - base media
        - selected reagent volumes
        - fixed cell volume
    """
    transfers = []
    wells_used = []

    # Column 1 often reserved; use iteration+1
    col = iteration + 1
    rows = ["A", "B", "C", "D", "E", "F", "G", "H"]

    for i, original_comp in enumerate(compositions):
        if i >= len(rows):
            break

        dst_well = f"{rows[i]}{col}"
        wells_used.append(dst_well)

        comp = normalize_if_needed(original_comp.copy())
        base_vol = compute_base_media_volume(comp)

        # Step 1: base media
        if base_vol >= MIN_TRANSFER_VOLUME:
            transfers.append({
                "src_plate": "reagent",
                "src_well": BASE_MEDIA_WELL,
                "dst_plate": "experiment",
                "dst_well": dst_well,
                "volume": base_vol,
                "new_tip": "once",
                "blow_out": True,
            })

        # Step 2: variable reagents
        for reagent_name, vol in comp.items():
            if vol < MIN_TRANSFER_VOLUME:
                continue

            src_well = get_reagent_source_well(reagent_name)
            if src_well is None:
                print(f"Warning: reagent '{reagent_name}' not found in REAGENT_WELLS.")
                continue

            transfers.append({
                "src_plate": "reagent",
                "src_well": src_well,
                "dst_plate": "experiment",
                "dst_well": dst_well,
                "volume": vol,
                "new_tip": "once",
                "blow_out": True,
            })

        # Step 3: seed cells
        transfers.append({
            "src_plate": "cell_culture_stock",
            "src_well": CELL_SOURCE_WELL,
            "dst_plate": "experiment",
            "dst_well": dst_well,
            "volume": CELL_VOLUME,
            "new_tip": "always",
            "blow_out": False,
            "post_mix_volume": 40,
            "post_mix_reps": 3,
        })

    return transfers, wells_used


def validate_transfer_array(transfers):
    """
    Validate transfer commands before submitting to the workcell.
    """
    print(f"Validating {len(transfers)} transfers...")

    if len(transfers) > MAX_TRANSFERS:
        print(f"FAIL: {len(transfers)} transfers exceeds limit of {MAX_TRANSFERS}")
        return False

    valid_src_plates = {"reagent", "experiment", "cell_culture_stock"}

    for i, t in enumerate(transfers):
        src_plate = t.get("src_plate")
        volume = t.get("volume", 0)

        if src_plate not in valid_src_plates:
            print(f"FAIL: transfer {i}: invalid src_plate={src_plate}")
            return False

        if not (MIN_TRANSFER_VOLUME <= volume <= 1000):
            print(f"FAIL: transfer {i}: invalid volume={volume}")
            return False

    print("OK: Transfer array is valid.")
    return True


# ============================================================
# 6. DATA FETCHING
# ============================================================

def fetch_od600_data(plate_barcode, wells):
    """
    Fetch OD600 time-series data.

    In simulation mode, generate fake but realistic-looking growth curves.
    """
    if SIMULATION_MODE:
        print(f"[Simulation] Reading OD600 for wells: {wells}")
        fake_data = {}

        for well in wells:
            baseline = 0.05 + np.random.uniform(0.0, 0.02)
            growth_strength = np.random.uniform(0.1, 0.8)

            curve = []
            for t in range(0, 91, 10):
                val = baseline + growth_strength * (1 - np.exp(-t / 30)) + np.random.normal(0, 0.01)
                curve.append(max(0.0, round(float(val), 4)))

            fake_data[well] = curve

        return fake_data

    # Competition-day example placeholder
    # import requests
    # resp = requests.post(...)
    # return parse_od600_response(resp.json(), wells)

    raise NotImplementedError("Real OD600 API fetch is not enabled.")


# ============================================================
# 7. SAVING / REPORTING
# ============================================================

def save_results(results, iteration):
    rows = []

    for r in results:
        row = {
            "iteration": r.iteration,
            "well": r.well,
            "delta_od600": r.delta_od600,
            "od600_readings": json.dumps(r.od600_readings),
        }
        row.update(r.composition)
        rows.append(row)

    df = pd.DataFrame(rows)
    filename = f"results_iter{iteration}.csv"
    df.to_csv(filename, index=False)
    print(f"[Saved] {filename}")


def print_batch_summary(compositions):
    print("Suggested compositions:")
    for i, comp in enumerate(compositions, start=1):
        base_vol = compute_base_media_volume(comp)
        print(f"  #{i}: {comp} | base_media={base_vol} uL | cells={CELL_VOLUME} uL")


def print_iteration_results(wells_used, compositions, od_data):
    delta_values = []

    for well, comp in zip(wells_used, compositions):
        readings = od_data.get(well, [0.0])
        delta = compute_delta_od600(readings)
        delta_values.append(delta)
        print(f"  {well}: delta_OD600={delta:.4f} | {comp}")

    return delta_values


# ============================================================
# 8. MAIN LOOP
# ============================================================

def run_agent(max_iterations=3):
    print("=" * 70)
    print("Monomer Bio Hackathon - Closed-Loop Bayesian Optimization Agent")
    print("=" * 70)

    agent = BayesianAgent()
    all_results = []

    # Competition-day setup example
    # client = McpClient("http://YOUR_WORKCELL_ENDPOINT")
    # def_id = register_workflow(client, Path("examples/workflow_definition_template.py"))

    for iteration in range(1, max_iterations + 1):
        print("\n" + "=" * 70)
        print(f"Iteration {iteration}/{max_iterations}")
        print("=" * 70)

        # Step 1: Ask BO for next candidates
        compositions = agent.suggest_next_batch(n=WELLS_PER_ITERATION)
        print_batch_summary(compositions)

        # Step 2: Convert to transfer array
        transfers, wells_used = build_transfer_array(compositions, iteration)
        print(f"\nBuilt {len(transfers)} transfers for wells: {wells_used}")

        if not validate_transfer_array(transfers):
            print("Stopping because transfer array is invalid.")
            return all_results

        # Step 3: Submit to workcell on competition day
        # uuid = instantiate_workflow(...)
        # wait for workflow to complete...

        # Step 4: Fetch OD600 results
        od_data = fetch_od600_data(EXPERIMENT_PLATE, wells_used)

        # Step 5: Convert to ExperimentResult objects
        delta_values = []
        for well, comp in zip(wells_used, compositions):
            readings = od_data.get(well, [0.0])
            delta = compute_delta_od600(readings)
            delta_values.append(delta)

            result = ExperimentResult(
                iteration=iteration,
                well=well,
                composition=comp,
                od600_readings=readings,
                delta_od600=delta,
            )
            all_results.append(result)
            agent.history.append(result)

        # Step 6: Update BO
        agent.update(compositions, delta_values)

        # Step 7: Print round summary
        print("\nRound results:")
        print_iteration_results(wells_used, compositions, od_data)

        best = agent.best_so_far()
        if best is not None:
            print("\nBest so far:")
            print(f"  Iteration:   {best.iteration}")
            print(f"  Well:        {best.well}")
            print(f"  delta_OD600: {best.delta_od600:.4f}")
            print(f"  Composition: {best.composition}")

        save_results(all_results, iteration)

    print("\n" + "=" * 70)
    print("Experiment complete.")
    best = agent.best_so_far()
    if best is not None:
        print("Final best condition:")
        print(f"  Iteration:   {best.iteration}")
        print(f"  Well:        {best.well}")
        print(f"  delta_OD600: {best.delta_od600:.4f}")
        print(f"  Composition: {best.composition}")
    print("=" * 70)

    return all_results


def preview_iteration(iteration=1):
    print(f"\nPreviewing iteration {iteration}...")
    agent = BayesianAgent()
    compositions = agent.suggest_next_batch(n=WELLS_PER_ITERATION)
    transfers, wells = build_transfer_array(compositions, iteration)

    print(f"Wells: {wells}")
    print(f"Transfers: {len(transfers)}")
    print("First 5 transfers:")
    for t in transfers[:5]:
        print(f"  {t}")

    validate_transfer_array(transfers)
    return transfers, wells


# ============================================================
# 9. ENTRY POINT
# ============================================================

if __name__ == "__main__":
    preview_iteration(iteration=1)

    print("\n" + "=" * 70)
    print("Running in simulation mode...")
    print("=" * 70)

    run_agent(max_iterations=3)