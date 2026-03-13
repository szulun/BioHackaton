"""
Monomer Bio Hackathon - Track A
Closed-Loop Bayesian Optimization Agent (v5 fixed)

Changes from v5:
- Bug fix: propose_batch now compares in scaled space correctly
- Readability restored: all functions fully expanded, no one-liners
- compute_well_metrics expanded back to explicit steps
- evaluate_plate expanded with clearer structure
- load_history / save functions restored to descriptive names
- All helper logic has inline comments explaining the why

Adaptive plate strategy:
    Round 1 (60 wells) : LHS + manual media panel + pH / NaCl / Carbon gradients
    Round 2 (48 wells) : focused Bayesian Optimization
    Round 3+ (32 wells): convergence run

Workflow:
    1. Load previous results
    2. Compute mu_max per well
    3. Fit GP model on scaled factors
    4. Propose next plate  (adaptive strategy)
    5. Export plate_design CSV + Monomer transfer_array JSON
    6. Execute on workcell, collect OD, repeat
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy.stats import linregress, norm, qmc
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import ConstantKernel, Matern, WhiteKernel
from sklearn.preprocessing import StandardScaler


# ============================================================
# 1. CONFIGURATION
# ============================================================

RANDOM_SEED = 42
np.random.seed(RANDOM_SEED)
random.seed(RANDOM_SEED)

TEAM_NAME        = "YOUR_TEAM_NAME"
EXPERIMENT_PLATE = f"{TEAM_NAME}_EXPERIMENT"
REAGENT_PLATE    = f"{TEAM_NAME}_REAGENTS"
CELL_STOCK_PLATE = f"{TEAM_NAME}_CELLS"

# ── Volumes (uL) ──────────────────────────────────────────────────────────────
FINAL_WELL_VOLUME_UL   = 200.0
CELL_VOLUME_UL         = 20.0
MEDIA_TARGET_VOLUME_UL = FINAL_WELL_VOLUME_UL - CELL_VOLUME_UL   # 180 uL

MIN_TRANSFER_UL = 1.0
MAX_TRANSFER_UL = 180.0

# ── Well layout ───────────────────────────────────────────────────────────────
# Interior wells only — edge wells evaporate 2-3x faster and corrupt OD readings
INTERIOR_WELLS = [
    f"{r}{c}"
    for c in range(2, 12)       # columns 2–11
    for r in list("BCDEFG")     # rows B–G
]
# Total interior wells available = 60

# ── Adaptive well budget per round ────────────────────────────────────────────
# Round 1: maximum exploration (all 60 interior wells)
# Round 2: focused BO after we have data
# Round 3+: convergence — save time for more iterations
ROUND_WELL_BUDGET: Dict[int, int] = {
    1: 60,
    2: 48,
    3: 32,
}
DEFAULT_WELL_BUDGET = 32   # rounds 4 and beyond

# ── Adaptive plate composition per round ──────────────────────────────────────
# Round 1 uses manual exploration panels (gradients + known media).
# Rounds 2+ switch to pure Bayesian Optimization candidates.
ROUND_COMPOSITION: Dict[int, Dict[str, int]] = {
    1: dict(
        n_lhs          = 24,   # Latin Hypercube candidates
        n_media_panel  =  8,   # classic V. natriegens reference media
        n_ph_grad      =  8,   # pH sweep for buffer calibration
        n_nacl_grad    =  6,   # NaCl boundary sweep
        n_carbon_grad  =  4,   # carbon source boundary sweep
        n_bo           =  0,   # no BO in round 1 (no data yet)
        n_repeat       =  0,
        n_center       =  4,
        n_baseline     =  4,
        n_blanks       =  2,
    ),
    2: dict(
        n_lhs          =  0,
        n_media_panel  =  0,
        n_ph_grad      =  0,
        n_nacl_grad    =  0,
        n_carbon_grad  =  0,
        n_bo           = 24,   # BO takes over from round 2
        n_repeat       =  8,
        n_center       =  8,
        n_baseline     =  4,
        n_blanks       =  4,
    ),
    3: dict(
        n_lhs          =  0,
        n_media_panel  =  0,
        n_ph_grad      =  0,
        n_nacl_grad    =  0,
        n_carbon_grad  =  0,
        n_bo           = 16,   # fewer candidates — converging
        n_repeat       =  6,
        n_center       =  4,
        n_baseline     =  4,
        n_blanks       =  2,
    ),
}
DEFAULT_COMPOSITION = ROUND_COMPOSITION[3]   # reuse round-3 template for round 4+

# ── Search space: FINAL concentrations in the experiment well ─────────────────
# pH is included as a 7th factor and converted to acid/base volume at execution time.
# Upper limit of 8.5 matches the precipitation threshold from the reference paper.
FACTOR_CONFIG: Dict[str, Tuple[float, float]] = {
    "NaCl_mM":      ( 85.0, 340.0),
    "MOPS_mM":      ( 20.0,  40.0),
    "Phosphate_mM": (  2.0,  10.0),
    "MgSO4_mM":     (  0.5,   2.0),
    "NH4SO4_mM":    (  5.0,  20.0),
    "Carbon_pct":   (  0.10,  0.50),
    "pH":           (  7.0,   8.5),
}

FACTOR_NAMES = list(FACTOR_CONFIG.keys())

# ── Reagent stocks and source wells ───────────────────────────────────────────
STOCKS: Dict[str, Dict] = {
    "NaCl_mM":      {"stock_conc": 5000.0, "src_well": "A1"},
    "MOPS_mM":      {"stock_conc": 1000.0, "src_well": "A2"},
    "Phosphate_mM": {"stock_conc": 1000.0, "src_well": "A3"},
    "MgSO4_mM":     {"stock_conc":  500.0, "src_well": "A4"},
    "NH4SO4_mM":    {"stock_conc": 1000.0, "src_well": "A5"},
    "Carbon_pct":   {"stock_conc":   10.0, "src_well": "A6"},
    "NaOH":         {"stock_conc": 1000.0, "src_well": "A7"},   # 1 M NaOH for pH up
    "HCl":          {"stock_conc": 1000.0, "src_well": "A8"},   # 1 M HCl  for pH down
    "BaseMedia":    {"stock_conc":    1.0, "src_well": "B1"},   # filler medium
}

CELL_SOURCE_WELL = "A1"   # on the cell stock plate

# ── pH calibration ────────────────────────────────────────────────────────────
# How many uL of 1 M NaOH are needed to raise 180 uL of base media by 1.0 pH unit.
# *** THIS MUST BE MEASURED EMPIRICALLY BEFORE THE FIRST RUN ON COMPETITION DAY ***
# Procedure: add known volumes of 1 M NaOH to 180 uL base media, measure pH each time,
# fit a line, and read off the slope.
BUFFER_CAPACITY_UL_PER_PH_UNIT = 8.0   # placeholder — calibrate before use!
BASE_PH = 7.5                           # pH of base media before any adjustment

# ── Optional cost constraint ─────────────────────────────────────────────────
# Relative cost weighting per unit concentration.
# Set MAX_COST_PER_WELL = None to disable the budget filter entirely.
REAGENT_COST: Dict[str, float] = {
    "NaCl_mM":      0.001,
    "MOPS_mM":      0.050,
    "Phosphate_mM": 0.005,
    "MgSO4_mM":     0.010,
    "NH4SO4_mM":    0.008,
    "Carbon_pct":   0.030,
    "pH":           0.000,   # pH adjustment cost not modelled here
}
MAX_COST_PER_WELL: Optional[float] = None   # e.g. set to 0.5 to enable

# ── Precipitation detection threshold ────────────────────────────────────────
# If the first OD reading of a well exceeds this value, the well is flagged.
# Paper shows precipitation causes anomalously high initial absorbance.
PRECIPITATION_OD_THRESHOLD = 0.30

# ── Reference conditions ──────────────────────────────────────────────────────
BASELINE_CONDITION: Dict[str, float] = {
    "NaCl_mM":      200.0,
    "MOPS_mM":       30.0,
    "Phosphate_mM":   5.0,
    "MgSO4_mM":       1.0,
    "NH4SO4_mM":     10.0,
    "Carbon_pct":     0.20,
    "pH":             7.5,
}

CENTER_POINT: Dict[str, float] = {
    factor: round((lo + hi) / 2.0, 4)
    for factor, (lo, hi) in FACTOR_CONFIG.items()
}

# ── Manual media panel for round 1 ───────────────────────────────────────────
# Eight classic V. natriegens / marine bacteria conditions from literature.
# These anchor the GP with biologically meaningful reference points before
# any BO data is collected.
MANUAL_MEDIA_PANEL: List[Dict[str, float]] = [
    # LB3 standard — the most common lab condition for V. natriegens
    {
        "NaCl_mM": 200.0, "MOPS_mM": 30.0, "Phosphate_mM": 5.0,
        "MgSO4_mM": 1.0,  "NH4SO4_mM": 10.0, "Carbon_pct": 0.20, "pH": 7.5,
    },
    # Marine Broth — high salt mimics ocean environment
    {
        "NaCl_mM": 340.0, "MOPS_mM": 30.0, "Phosphate_mM": 5.0,
        "MgSO4_mM": 2.0,  "NH4SO4_mM": 10.0, "Carbon_pct": 0.20, "pH": 7.5,
    },
    # Low Salt — tests minimum Na+ requirement
    {
        "NaCl_mM":  85.0, "MOPS_mM": 30.0, "Phosphate_mM": 5.0,
        "MgSO4_mM": 1.0,  "NH4SO4_mM": 10.0, "Carbon_pct": 0.20, "pH": 7.5,
    },
    # High Carbon — tests whether excess carbon source accelerates growth
    {
        "NaCl_mM": 200.0, "MOPS_mM": 30.0, "Phosphate_mM": 5.0,
        "MgSO4_mM": 1.0,  "NH4SO4_mM": 10.0, "Carbon_pct": 0.45, "pH": 7.5,
    },
    # High Nitrogen — tests nitrogen-limited growth regime
    {
        "NaCl_mM": 200.0, "MOPS_mM": 30.0, "Phosphate_mM": 8.0,
        "MgSO4_mM": 1.0,  "NH4SO4_mM": 18.0, "Carbon_pct": 0.20, "pH": 7.5,
    },
    # Minimal — lowest possible nutrients, tests growth floor
    {
        "NaCl_mM": 200.0, "MOPS_mM": 20.0, "Phosphate_mM": 2.0,
        "MgSO4_mM": 0.5,  "NH4SO4_mM":  5.0, "Carbon_pct": 0.10, "pH": 7.5,
    },
    # Rich — all factors pushed toward upper range
    {
        "NaCl_mM": 300.0, "MOPS_mM": 40.0, "Phosphate_mM": 8.0,
        "MgSO4_mM": 1.8,  "NH4SO4_mM": 18.0, "Carbon_pct": 0.40, "pH": 7.8,
    },
    # Optimal pH — same as baseline but at pH 7.8 (literature optimum for V. natriegens)
    {
        "NaCl_mM": 200.0, "MOPS_mM": 30.0, "Phosphate_mM": 5.0,
        "MgSO4_mM": 1.0,  "NH4SO4_mM": 10.0, "Carbon_pct": 0.20, "pH": 7.8,
    },
]

SIMULATION_MODE = True


# ============================================================
# 2. DATA STRUCTURES
# ============================================================

@dataclass
class WellDesign:
    well:           str
    condition_type: str    # lhs | media_panel | ph_grad | nacl_grad | carbon_grad |
                           # candidate | repeat_best | center | baseline | blank
    composition:    Dict[str, float]
    source_note:    str = ""


@dataclass
class WellResult:
    iteration:          int
    well:               str
    condition_type:     str
    composition:        Dict[str, float]
    mu_max_per_hr:      float
    doubling_time_hr:   Optional[float]
    auc:                float
    endpoint_od:        float
    od_readings:        List[float]
    precipitation_flag: bool = False


# ============================================================
# 3. GROWTH METRICS
# ============================================================

def compute_mu_max(time_hours: np.ndarray,
                   od:         np.ndarray,
                   od_min:     float = 0.03,
                   window:     int   = 4) -> float:
    """
    Estimate mu_max (per hour) as the steepest local slope of ln(OD) vs time.

    Algorithm:
        - Mask out non-finite values and OD below od_min (noise floor)
        - Slide a window of length `window` across the log-OD curve
        - Fit a linear regression in each window
        - Accept the window only if slope > 0 and R² > 0.85
        - Return the maximum accepted slope = mu_max
    """
    time_hours = np.asarray(time_hours, dtype=float)
    od         = np.asarray(od,         dtype=float)

    # Remove invalid or sub-threshold points
    mask       = np.isfinite(time_hours) & np.isfinite(od) & (od > od_min)
    time_hours = time_hours[mask]
    od         = od[mask]

    if len(time_hours) < window + 1:
        return float("nan")

    log_od = np.log(od)
    slopes = []

    for i in range(len(time_hours) - window + 1):
        t_window = time_hours[i : i + window]
        y_window = log_od[i : i + window]

        if np.any(~np.isfinite(y_window)):
            continue

        slope, _, r_value, _, _ = linregress(t_window, y_window)

        # Only accept positive slopes with a good local fit
        if slope > 0 and (r_value ** 2) > 0.85:
            slopes.append(slope)

    if not slopes:
        return float("nan")

    return float(np.max(slopes))


def compute_auc(time_hours: np.ndarray, od: np.ndarray) -> float:
    """Area under the OD curve using the trapezoidal rule."""
    mask = np.isfinite(time_hours) & np.isfinite(od)
    if mask.sum() < 2:
        return float("nan")
    return float(np.trapz(od[mask], time_hours[mask]))


def compute_well_metrics(time_hours: np.ndarray,
                          od:         np.ndarray) -> Dict[str, float]:
    """
    Compute all growth metrics for a single well.

    Returns:
        mu_max_per_hr    — maximum specific growth rate (hr⁻¹)
        doubling_time_hr — ln(2) / mu_max (hr)
        auc              — area under OD curve (OD·hr)
        endpoint_od      — last valid OD reading
    """
    mu_max = compute_mu_max(time_hours, od)

    if np.isfinite(mu_max) and mu_max > 0:
        doubling_time = float(np.log(2) / mu_max)
    else:
        doubling_time = float("nan")

    auc = compute_auc(time_hours, od)

    valid_od = od[np.isfinite(od)]
    endpoint = float(valid_od[-1]) if len(valid_od) > 0 else float("nan")

    return {
        "mu_max_per_hr":    mu_max,
        "doubling_time_hr": doubling_time,
        "auc":              auc,
        "endpoint_od":      endpoint,
    }


# ============================================================
# 4. pH ADJUSTMENT LOGIC
# ============================================================

def ph_adjustment_volume(target_ph: float,
                          base_ph:   float = BASE_PH,
                          ) -> Tuple[str, float]:
    """
    Calculate how much NaOH or HCl to add to shift the base media pH.

    Uses a linear buffer capacity model:
        volume (uL) = |delta_pH| × BUFFER_CAPACITY_UL_PER_PH_UNIT

    Returns:
        (reagent_name, volume_uL)
        reagent_name is 'NaOH', 'HCl', or 'none' (if adjustment < MIN_TRANSFER_UL)

    IMPORTANT: BUFFER_CAPACITY_UL_PER_PH_UNIT must be measured empirically
               with the actual base media before the experiment starts.
    """
    delta_ph = target_ph - base_ph
    volume   = round(abs(delta_ph) * BUFFER_CAPACITY_UL_PER_PH_UNIT, 1)

    if volume < MIN_TRANSFER_UL:
        # Adjustment too small to pipette — skip it
        return "none", 0.0

    if volume > MAX_TRANSFER_UL:
        print(
            f"  WARNING: pH shift of {delta_ph:+.2f} units requires {volume:.1f} uL — "
            f"clamped to {MAX_TRANSFER_UL:.0f} uL. "
            f"Check BUFFER_CAPACITY_UL_PER_PH_UNIT or narrow the pH search range."
        )
        volume = MAX_TRANSFER_UL

    reagent = "NaOH" if delta_ph > 0 else "HCl"
    return reagent, volume


# ============================================================
# 5. DESIGN CONSTRAINTS
# ============================================================

def is_valid_condition(c: Dict[str, float]) -> bool:
    """
    Returns False if the condition violates any chemistry, biology, or cost rule.

    Rules (with justification):
        1. Hard bounds    — keeps factors within the defined search space
        2. NaCl minimum   — V. natriegens needs Na⁺ for membrane transport
        3. pH ceiling     — paper shows precipitation above pH 8.5
        4. pH + phosphate — high pH + high phosphate causes Ca/Mg phosphate precipitation
        5. Phosphate + Mg — high phosphate + high Mg causes MgHPO4 precipitation
        6. Osmolarity     — excessive salt / ammonium stresses the cell
        7. Cost budget    — optional; disabled when MAX_COST_PER_WELL is None
    """
    # Rule 1: Hard bounds
    for factor, (lo, hi) in FACTOR_CONFIG.items():
        if c[factor] < lo or c[factor] > hi:
            return False

    # Rule 2: NaCl minimum
    if c["NaCl_mM"] < 85:
        return False

    # Rule 3: pH ceiling
    if c.get("pH", BASE_PH) > 8.5:
        return False

    # Rule 4: High pH + high phosphate → calcium/magnesium phosphate precipitation
    if c.get("pH", BASE_PH) > 8.0 and c["Phosphate_mM"] > 6.0:
        return False

    # Rule 5: High phosphate + high Mg → MgHPO4 precipitation (pH-independent)
    if c["Phosphate_mM"] > 8.0 and c["MgSO4_mM"] > 1.5:
        return False

    # Rule 6: Osmolarity sanity check
    if c["NaCl_mM"] + 2.0 * c["NH4SO4_mM"] > 420:
        return False

    # Rule 7: Optional cost budget
    if MAX_COST_PER_WELL is not None:
        cost = sum(REAGENT_COST.get(f, 0.0) * c[f] for f in FACTOR_NAMES)
        if cost > MAX_COST_PER_WELL:
            return False

    return True


# ============================================================
# 6. FACTOR SCALING
# ============================================================

def _build_scaler() -> StandardScaler:
    """
    Fit a StandardScaler on the corners of the search space.

    Why this matters:
        NaCl range = 255 mM  vs  pH range = 1.5
        Without scaling, the GP treats NaCl as 170× more important than pH.
        After scaling, all 7 factors contribute equally to distance calculations.
    """
    corners = np.array([
        [lo for lo, _  in FACTOR_CONFIG.values()],
        [hi for _,  hi in FACTOR_CONFIG.values()],
    ])
    scaler = StandardScaler()
    scaler.fit(corners)
    return scaler


# Build once at module level — reused in all GP calls
SCALER: StandardScaler = _build_scaler()


def condition_to_vector(c: Dict[str, float]) -> np.ndarray:
    """Convert a condition dict to a raw (unscaled) numpy vector."""
    return np.array([c[f] for f in FACTOR_NAMES], dtype=float)


def vector_to_condition(x: np.ndarray) -> Dict[str, float]:
    """Convert a raw numpy vector back to a condition dict."""
    return {f: float(v) for f, v in zip(FACTOR_NAMES, x)}


def unique_conditions(conditions: List[Dict[str, float]],
                      decimals:   int = 4) -> List[Dict[str, float]]:
    """Remove duplicate conditions (rounded to `decimals` decimal places)."""
    seen   = set()
    unique = []
    for c in conditions:
        key = tuple(round(c[f], decimals) for f in FACTOR_NAMES)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


# ============================================================
# 7. SAMPLING HELPERS
# ============================================================

def _sample_random_conditions(n: int) -> List[Dict[str, float]]:
    """Generate n purely random conditions (uniform over FACTOR_CONFIG bounds)."""
    conditions = []
    for _ in range(n):
        c = {
            factor: float(np.random.uniform(lo, hi))
            for factor, (lo, hi) in FACTOR_CONFIG.items()
        }
        conditions.append(c)
    return conditions


def latin_hypercube_sample(n_points: int) -> List[Dict[str, float]]:
    """
    Generate initial conditions using Latin Hypercube Sampling (LHS).

    LHS guarantees that each factor is uniformly covered across the search
    space — far better than pure random sampling for a first round where
    we have no prior model.

    If constraint filtering removes too many points, random valid conditions
    are appended as fallback.
    """
    sampler     = qmc.LatinHypercube(d=len(FACTOR_NAMES), seed=RANDOM_SEED)
    # Oversample to allow for constraint filtering
    raw_samples = sampler.random(n=n_points * 3)

    bounds_low  = np.array([FACTOR_CONFIG[f][0] for f in FACTOR_NAMES])
    bounds_high = np.array([FACTOR_CONFIG[f][1] for f in FACTOR_NAMES])
    scaled      = qmc.scale(raw_samples, bounds_low, bounds_high)

    conditions = []
    for row in scaled:
        c = vector_to_condition(row)
        if is_valid_condition(c):
            conditions.append(c)
        if len(conditions) >= n_points:
            break

    # Fallback: fill remaining slots with random valid conditions
    if len(conditions) < n_points:
        extras = [
            c for c in _sample_random_conditions(500)
            if is_valid_condition(c)
        ]
        conditions.extend(extras[: n_points - len(conditions)])

    return unique_conditions(conditions)[:n_points]


def ph_gradient(n_points: int = 8) -> List[Dict[str, float]]:
    """
    Single-factor pH sweep, all other factors fixed at BASELINE_CONDITION.

    Serves two purposes in round 1:
        (a) Identifies the pH optimum for V. natriegens
        (b) Provides data to calibrate BUFFER_CAPACITY_UL_PER_PH_UNIT
    """
    lo, hi    = FACTOR_CONFIG["pH"]
    ph_values = np.linspace(lo, hi, n_points)
    return [
        {**BASELINE_CONDITION, "pH": round(float(ph), 3)}
        for ph in ph_values
    ]


def nacl_gradient(n_points: int = 6) -> List[Dict[str, float]]:
    """
    Single-factor NaCl sweep.
    Confirms the salinity boundary for V. natriegens growth.
    """
    lo, hi = FACTOR_CONFIG["NaCl_mM"]
    values = np.linspace(lo, hi, n_points)
    return [
        {**BASELINE_CONDITION, "NaCl_mM": round(float(v), 1)}
        for v in values
    ]


def carbon_gradient(n_points: int = 4) -> List[Dict[str, float]]:
    """
    Single-factor carbon source sweep.
    Confirms the carbon optimum.
    """
    lo, hi = FACTOR_CONFIG["Carbon_pct"]
    values = np.linspace(lo, hi, n_points)
    return [
        {**BASELINE_CONDITION, "Carbon_pct": round(float(v), 3)}
        for v in values
    ]


# ============================================================
# 8. GP MODEL + ACQUISITION
# ============================================================

def build_gp() -> GaussianProcessRegressor:
    """
    Build a Gaussian Process regressor with a Matern kernel.

    Kernel components:
        ConstantKernel  — overall signal amplitude
        Matern (nu=2.5) — smooth but not infinitely differentiable;
                          appropriate for biological response surfaces
        WhiteKernel     — models measurement noise explicitly
    """
    n_factors = len(FACTOR_NAMES)
    kernel = (
        ConstantKernel(1.0, constant_value_bounds=(1e-3, 1e3))
        * Matern(length_scale=np.ones(n_factors), nu=2.5)
        + WhiteKernel(noise_level=1e-4, noise_level_bounds=(1e-8, 1e0))
    )
    return GaussianProcessRegressor(
        kernel=kernel,
        normalize_y=True,
        n_restarts_optimizer=8,
        random_state=RANDOM_SEED,
    )


def expected_improvement(X_scaled: np.ndarray,
                          model:    GaussianProcessRegressor,
                          y_best:   float,
                          xi:       float = 0.01) -> np.ndarray:
    """
    Standard Expected Improvement acquisition function.

    EI(x) = (mu(x) - y_best - xi) * Phi(z) + sigma(x) * phi(z)
    where z = (mu(x) - y_best - xi) / sigma(x)

    xi controls exploration vs exploitation:
        large xi  → favour exploring uncertain regions
        small xi  → favour exploiting the current best
    """
    mu, sigma = model.predict(X_scaled, return_std=True)
    sigma     = np.maximum(sigma, 1e-9)   # numerical safety

    improvement = mu - y_best - xi
    z           = improvement / sigma
    ei          = improvement * norm.cdf(z) + sigma * norm.pdf(z)

    # Zero out points with negligible uncertainty
    ei[sigma <= 1e-9] = 0.0
    return ei


def get_xi(iteration: int, total_iterations: int) -> float:
    """
    Dynamic exploration-exploitation schedule.

    xi decays linearly from max_xi (explore) to min_xi (exploit)
    as the experiment progresses.

    Example with 4 iterations:
        Round 2: xi = 0.10   (maximum exploration)
        Round 3: xi = 0.063
        Round 4: xi = 0.027
        Round 5: xi = 0.01   (maximum exploitation)
    """
    max_xi = 0.10
    min_xi = 0.01

    if total_iterations <= 1:
        return max_xi

    progress = (iteration - 1) / (total_iterations - 1)
    xi       = max_xi - (max_xi - min_xi) * progress
    return round(xi, 4)


# ============================================================
# 9. BATCH PROPOSAL (BO rounds 2+)
# ============================================================

def propose_batch(model:         GaussianProcessRegressor,
                  X_hist_scaled: np.ndarray,
                  y_hist:        np.ndarray,
                  n_candidates:  int,
                  xi:            float = 0.01,
                  ) -> List[Dict[str, float]]:
    """
    Heuristic batch Bayesian Optimization.

    Steps:
        1. Sample a large random pool and filter by constraints
        2. Scale all candidates using SCALER
        3. Score each candidate by Expected Improvement (in scaled space)
        4. Greedily pick the highest-EI candidates while enforcing
           a minimum separation distance (also in scaled space)
        5. Fallback: relax diversity constraint if too few survive

    BUG FIX vs v5:
        Comparison against history is now done in SCALED space throughout.
        v5 was incorrectly calling inverse_transform before comparison.
    """
    # Step 1: Build candidate pool
    pool = [
        c for c in _sample_random_conditions(5000)
        if is_valid_condition(c)
    ]
    pool = unique_conditions(pool)

    if not pool:
        print("  [BO] Candidate pool empty after constraint filtering — falling back to LHS.")
        return latin_hypercube_sample(n_candidates)

    # Step 2: Scale candidates
    X_pool_raw    = np.vstack([condition_to_vector(c) for c in pool])
    X_pool_scaled = SCALER.transform(X_pool_raw)

    # Step 3: Score by EI
    y_best = float(np.nanmax(y_hist))
    ei     = expected_improvement(X_pool_scaled, model, y_best, xi=xi)

    # Rank from highest to lowest EI
    ranked_indices = np.argsort(ei)[::-1]

    selected        = []
    selected_scaled = []
    # min_separation in SCALED space (≈ standard deviations)
    # prevents the agent from proposing near-duplicate conditions
    min_separation  = 0.3

    for idx in ranked_indices:
        c        = pool[idx]
        x_scaled = X_pool_scaled[idx]

        # Skip if this point is too close to any historical observation (scaled space)
        too_close_to_history = any(
            np.linalg.norm(x_scaled - h_scaled) < 1e-6
            for h_scaled in X_hist_scaled
        )
        if too_close_to_history:
            continue

        # Skip if this point is too close to an already-selected candidate (scaled space)
        too_close_to_selected = any(
            np.linalg.norm(x_scaled - s_scaled) < min_separation
            for s_scaled in selected_scaled
        )
        if too_close_to_selected:
            continue

        selected.append(c)
        selected_scaled.append(x_scaled)

        if len(selected) >= n_candidates:
            break

    # Fallback: relax diversity if not enough candidates were found
    if len(selected) < n_candidates:
        print(f"  [BO] Only {len(selected)} diverse candidates found — "
              f"relaxing diversity to fill {n_candidates}.")
        for idx in ranked_indices:
            c        = pool[idx]
            x_scaled = X_pool_scaled[idx]

            already_selected = any(
                np.allclose(x_scaled, s_scaled, atol=1e-4)
                for s_scaled in selected_scaled
            )
            if already_selected:
                continue

            selected.append(c)
            selected_scaled.append(x_scaled)

            if len(selected) >= n_candidates:
                break

    return selected[:n_candidates]


# ============================================================
# 10. TRANSFER CALCULATIONS
# ============================================================

def calc_transfer_ul(final_conc:     float,
                     stock_conc:     float,
                     final_volume_ul: float = FINAL_WELL_VOLUME_UL) -> float:
    """
    C1V1 = C2V2 dilution formula.
    Works as long as final_conc and stock_conc share the same units.
    """
    return (final_conc * final_volume_ul) / stock_conc


def composition_to_transfers(dst_well:       str,
                               composition:    Dict[str, float],
                               condition_type: str,
                               ) -> List[Dict]:
    """
    Convert a desired final concentration dict into Monomer transfer commands.

    Three-step dispensing order (important for mixing and pH accuracy):
        Step A — nutrient reagents + base media filler
                 (well is at BASE_PH after this step)
        Step B — pH adjustment (NaOH raises pH, HCl lowers pH)
                 (well reaches target_ph after this step)
        Step C — seed cells
                 (cells are added last to avoid pH shock during mixing)

    Volume balance:
        base_media_fill = MEDIA_TARGET_VOLUME_UL
                          - sum(nutrient volumes)
                          - pH adjustment volume
        This ensures total media = exactly MEDIA_TARGET_VOLUME_UL before cell addition,
        and total well volume = exactly FINAL_WELL_VOLUME_UL after cell addition.

    Blank wells receive only base media — no nutrients and no cells.
    """
    transfers    = []
    nutrient_vol = 0.0

    target_ph             = composition.get("pH", BASE_PH)
    ph_reagent, ph_vol_ul = ph_adjustment_volume(target_ph, BASE_PH)

    # ── Step A: Nutrient reagents ─────────────────────────────────────────────
    if condition_type != "blank":
        for factor in FACTOR_NAMES:
            if factor == "pH":
                continue   # pH is handled separately in Step B

            final_conc = composition[factor]
            stock_info = STOCKS[factor]
            vol        = calc_transfer_ul(final_conc, stock_info["stock_conc"])

            if vol < MIN_TRANSFER_UL:
                continue   # too small to pipette reliably — skip

            if vol > MAX_TRANSFER_UL:
                raise ValueError(
                    f"Transfer too large: {factor} → {dst_well} = {vol:.2f} uL "
                    f"(max {MAX_TRANSFER_UL} uL). Reduce stock concentration or target."
                )

            transfers.append({
                "src_plate": "reagent",
                "src_well":  stock_info["src_well"],
                "dst_plate": "experiment",
                "dst_well":  dst_well,
                "volume":    round(vol, 2),
                "new_tip":   "once",
                "blow_out":  True,
                "step":      "A_nutrients",
            })
            nutrient_vol += vol

    # Base media filler
    # Must account for pH adjustment volume to keep total volume balanced
    media_fill = MEDIA_TARGET_VOLUME_UL - nutrient_vol - ph_vol_ul

    if media_fill < -1e-6:
        raise ValueError(
            f"Negative base media fill for {dst_well}: "
            f"nutrients={nutrient_vol:.1f} uL + pH_adj={ph_vol_ul:.1f} uL "
            f"> MEDIA_TARGET={MEDIA_TARGET_VOLUME_UL:.1f} uL. "
            f"Lower stock concentrations or reduce the number of factors."
        )

    if media_fill >= MIN_TRANSFER_UL:
        transfers.append({
            "src_plate": "reagent",
            "src_well":  STOCKS["BaseMedia"]["src_well"],
            "dst_plate": "experiment",
            "dst_well":  dst_well,
            "volume":    round(media_fill, 2),
            "new_tip":   "once",
            "blow_out":  True,
            "step":      "A_base_media_fill",
        })

    # ── Step B: pH adjustment ─────────────────────────────────────────────────
    if condition_type != "blank" and ph_reagent != "none":
        transfers.append({
            "src_plate": "reagent",
            "src_well":  STOCKS[ph_reagent]["src_well"],
            "dst_plate": "experiment",
            "dst_well":  dst_well,
            "volume":    round(ph_vol_ul, 2),
            "new_tip":   "once",
            "blow_out":  True,
            "step":      "B_pH_adjust",
            "comment":   f"Target pH {target_ph:.2f} — adding {ph_reagent} {ph_vol_ul:.1f} uL",
        })

    # ── Step C: Seed cells ────────────────────────────────────────────────────
    if condition_type != "blank":
        transfers.append({
            "src_plate":       "cell_culture_stock",
            "src_well":        CELL_SOURCE_WELL,
            "dst_plate":       "experiment",
            "dst_well":        dst_well,
            "volume":          round(CELL_VOLUME_UL, 2),
            "new_tip":         "always",    # fresh tip per well to avoid cross-contamination
            "blow_out":        False,
            "post_mix_volume": 40,          # mix after seeding to homogenise
            "post_mix_reps":   3,
            "step":            "C_seed_cells",
        })

    return transfers


def validate_transfers(transfers: List[Dict]) -> None:
    """Raise ValueError if any transfer command is malformed."""
    valid_src_plates = {"reagent", "cell_culture_stock", "experiment"}
    for i, t in enumerate(transfers):
        if t["src_plate"] not in valid_src_plates:
            raise ValueError(
                f"Transfer {i}: invalid src_plate '{t['src_plate']}'. "
                f"Must be one of {valid_src_plates}."
            )
        vol = t.get("volume", 0)
        if not (MIN_TRANSFER_UL <= vol <= 1000):
            raise ValueError(
                f"Transfer {i}: volume {vol:.2f} uL is out of range "
                f"[{MIN_TRANSFER_UL}, 1000]."
            )


# ============================================================
# 11. PLATE DESIGN
# ============================================================

def select_best_unique_conditions(results_df: pd.DataFrame,
                                   n:          int,
                                   ) -> List[Dict[str, float]]:
    """
    Find the top-n unique conditions by median mu_max across replicates.
    Used to pick conditions worth repeating in the next round.
    """
    if results_df.empty:
        return []

    grouped = (
        results_df
        .groupby(FACTOR_NAMES, dropna=False)["mu_max_per_hr"]
        .median()
        .reset_index()
        .sort_values("mu_max_per_hr", ascending=False)
    )
    return grouped.head(n)[FACTOR_NAMES].to_dict(orient="records")


def build_plate_design(iteration:        int,
                        total_iterations: int,
                        model:            Optional[GaussianProcessRegressor],
                        history_df:       pd.DataFrame,
                        X_hist_scaled:    Optional[np.ndarray] = None,
                        y_hist:           Optional[np.ndarray] = None,
                        ) -> List[WellDesign]:
    """
    Build an adaptive plate for the current iteration.

    Round 1: manual exploration (gradients + known media + LHS)
    Round 2: focused BO with 48 wells
    Round 3+: convergence BO with 32 wells
    """
    comp   = ROUND_COMPOSITION.get(iteration, DEFAULT_COMPOSITION)
    budget = ROUND_WELL_BUDGET.get(iteration, DEFAULT_WELL_BUDGET)
    wells  = INTERIOR_WELLS[:budget]

    designs: List[WellDesign] = []

    # ── Round 1: Manual exploration panels ───────────────────────────────────

    if comp["n_lhs"] > 0:
        print(f"  [Design] Round {iteration}: Latin Hypercube Sampling "
              f"({comp['n_lhs']} points).")
        lhs_conditions = latin_hypercube_sample(comp["n_lhs"])
        for c in lhs_conditions:
            designs.append(WellDesign(
                well="", condition_type="lhs",
                composition=c, source_note="lhs_round1",
            ))

    if comp["n_media_panel"] > 0:
        panel = MANUAL_MEDIA_PANEL[: comp["n_media_panel"]]
        for c in panel:
            designs.append(WellDesign(
                well="", condition_type="media_panel",
                composition=c, source_note="manual_media_panel",
            ))

    if comp["n_ph_grad"] > 0:
        for c in ph_gradient(comp["n_ph_grad"]):
            designs.append(WellDesign(
                well="", condition_type="ph_grad",
                composition=c, source_note="ph_gradient_round1",
            ))

    if comp["n_nacl_grad"] > 0:
        for c in nacl_gradient(comp["n_nacl_grad"]):
            designs.append(WellDesign(
                well="", condition_type="nacl_grad",
                composition=c, source_note="nacl_gradient_round1",
            ))

    if comp["n_carbon_grad"] > 0:
        for c in carbon_gradient(comp["n_carbon_grad"]):
            designs.append(WellDesign(
                well="", condition_type="carbon_grad",
                composition=c, source_note="carbon_gradient_round1",
            ))

    # ── Rounds 2+: Bayesian Optimization candidates ───────────────────────────

    if comp["n_bo"] > 0:
        if model is None or history_df.empty:
            # No model yet — fall back to LHS
            print(f"  [Design] Round {iteration}: No GP model yet — using LHS fallback.")
            bo_conditions = latin_hypercube_sample(comp["n_bo"])
            source = "lhs_fallback"
        else:
            xi = get_xi(iteration, total_iterations)
            print(f"  [Design] Round {iteration}: Bayesian Optimization "
                  f"(xi={xi:.4f}, explore→exploit).")
            bo_conditions = propose_batch(
                model, X_hist_scaled, y_hist, comp["n_bo"], xi=xi
            )
            source = f"bo_iter{iteration}_xi{xi:.3f}"

        for c in bo_conditions:
            designs.append(WellDesign(
                well="", condition_type="candidate",
                composition=c, source_note=source,
            ))

    # ── Repeat best conditions ────────────────────────────────────────────────

    if comp["n_repeat"] > 0:
        best_conditions = select_best_unique_conditions(
            history_df, max(1, min(4, comp["n_repeat"]))
        )
        if not best_conditions:
            best_conditions = [BASELINE_CONDITION]

        # Tile to fill the required number of repeat wells
        repeat_pool = []
        while len(repeat_pool) < comp["n_repeat"]:
            repeat_pool.extend(best_conditions)

        for c in repeat_pool[: comp["n_repeat"]]:
            designs.append(WellDesign(
                well="", condition_type="repeat_best",
                composition=c, source_note="top_previous_condition",
            ))

    # ── Center points ─────────────────────────────────────────────────────────

    for _ in range(comp["n_center"]):
        designs.append(WellDesign(
            well="", condition_type="center",
            composition=CENTER_POINT.copy(), source_note="center_point",
        ))

    # ── Baseline controls ─────────────────────────────────────────────────────

    for _ in range(comp["n_baseline"]):
        designs.append(WellDesign(
            well="", condition_type="baseline",
            composition=BASELINE_CONDITION.copy(), source_note="baseline_control",
        ))

    # ── Blanks (no cells — OD background reference) ───────────────────────────

    blank_composition = {f: 0.0 for f in FACTOR_NAMES}
    for _ in range(comp["n_blanks"]):
        designs.append(WellDesign(
            well="", condition_type="blank",
            composition=blank_composition.copy(), source_note="blank_no_cells",
        ))

    # ── Assign physical well positions ────────────────────────────────────────

    if len(designs) > len(wells):
        raise ValueError(
            f"Round {iteration}: {len(designs)} designs exceed "
            f"{len(wells)} available wells."
        )

    for i, d in enumerate(designs):
        d.well = wells[i]

    return designs


def build_transfer_array_from_design(designs: List[WellDesign]) -> List[Dict]:
    """Convert all WellDesigns to a flat list of Monomer transfer commands."""
    transfers = []
    for d in designs:
        well_transfers = composition_to_transfers(
            dst_well       = d.well,
            composition    = d.composition,
            condition_type = d.condition_type,
        )
        transfers.extend(well_transfers)
    validate_transfers(transfers)
    return transfers


# ============================================================
# 12. ANOMALY DETECTION
# ============================================================

def flag_precipitation_anomaly(od: np.ndarray) -> bool:
    """
    Return True if the initial OD reading suggests precipitation.

    When precipitation forms, particulates scatter light and inflate the
    apparent OD from the very first reading — before any cell growth can occur.
    A high initial OD is therefore a reliable precipitation signal.

    Consequence:
        - The well is still recorded in history for traceability
        - The well is excluded from GP model training (see fit_model)
        - The condition_type / pH / phosphate combination is implicitly
          penalised by the chemistry constraints in is_valid_condition
    """
    if len(od) == 0 or not np.isfinite(od[0]):
        return False
    return float(od[0]) > PRECIPITATION_OD_THRESHOLD


# ============================================================
# 13. INPUT / OUTPUT
# ============================================================

def save_plate_design(designs: List[WellDesign], out_csv: Path) -> None:
    """Save the plate layout to a CSV file for human review and traceability."""
    rows = []
    for d in designs:
        row = {
            "well":           d.well,
            "condition_type": d.condition_type,
            "source_note":    d.source_note,
        }
        row.update(d.composition)
        rows.append(row)
    pd.DataFrame(rows).to_csv(out_csv, index=False)


def save_transfer_array(transfers: List[Dict], out_json: Path) -> None:
    """Save the transfer array JSON for submission to the Monomer MCP."""
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(transfers, f, indent=2)


def load_history_results(results_csv: Path) -> pd.DataFrame:
    """Load the rolling history CSV, or return an empty DataFrame if it does not exist."""
    if not results_csv.exists():
        return pd.DataFrame(columns=FACTOR_NAMES + [
            "iteration", "well", "condition_type",
            "mu_max_per_hr", "doubling_time_hr", "auc", "endpoint_od",
            "precipitation_flag",
        ])
    return pd.read_csv(results_csv)


# ============================================================
# 14. OD SIMULATION
# ============================================================

def simulate_od_curves(designs:      List[WellDesign],
                        duration_min: int = 120,
                        interval_min: int =  10) -> pd.DataFrame:
    """
    Generate synthetic OD time-series for local testing.

    Growth strength is a function of the composition so that the GP can
    learn meaningful structure during simulation.

    Hidden optima (matches V. natriegens literature approximately):
        NaCl_mM      ~ 220 mM
        MOPS_mM      ~ 30 mM
        Phosphate_mM ~ 5.5 mM
        MgSO4_mM     ~ 1.1 mM
        NH4SO4_mM    ~ 11 mM
        Carbon_pct   ~ 0.28 %
        pH           ~ 7.8
    """
    times = np.arange(0, duration_min + interval_min, interval_min)
    data  = {"time_min": times}

    for d in designs:
        if d.condition_type == "blank":
            baseline = 0.03 + np.random.normal(0, 0.002)
            noise    = np.random.normal(0, 0.002, size=len(times))
            data[d.well] = np.clip(baseline + noise, 0, None)
            continue

        c = d.composition
        growth_strength = (
            0.70
            - 0.000012 * (c["NaCl_mM"]            - 220 ) ** 2
            - 0.001500 * (c["MOPS_mM"]             -  30 ) ** 2
            - 0.006000 * (c["Phosphate_mM"]        -   5.5) ** 2
            - 0.030000 * (c["MgSO4_mM"]            -   1.1) ** 2
            - 0.001800 * (c["NH4SO4_mM"]           -  11 ) ** 2
            - 1.200000 * (c["Carbon_pct"]          -   0.28) ** 2
            - 0.150000 * (c.get("pH", BASE_PH)     -   7.8) ** 2
        )
        growth_strength = max(0.05, growth_strength)
        baseline        = 0.05 + np.random.uniform(0.0, 0.01)

        curve = []
        for t in times:
            t_hr = t / 60.0
            val  = baseline + growth_strength * (1 - np.exp(-1.8 * t_hr))
            val += np.random.normal(0, 0.008)
            curve.append(max(0.0, val))

        data[d.well] = curve

    return pd.DataFrame(data)


def evaluate_plate_from_od(designs:   List[WellDesign],
                            od_df:     pd.DataFrame,
                            iteration: int) -> pd.DataFrame:
    """
    Compute growth metrics for every well and flag precipitation anomalies.

    od_df must have a 'time_min' column and one column per well.
    Flagged wells are still saved to history but excluded from GP training.
    """
    if "time_min" not in od_df.columns:
        raise ValueError("OD dataframe must contain a 'time_min' column.")

    time_hr = od_df["time_min"].values / 60.0
    rows    = []

    for d in designs:
        if d.well not in od_df.columns:
            raise ValueError(f"Missing OD column for well {d.well} in OD dataframe.")

        od     = od_df[d.well].values.astype(float)
        precip = flag_precipitation_anomaly(od)

        if precip:
            print(
                f"  [PRECIP FLAG] {d.well}: initial OD={od[0]:.3f} "
                f"(threshold={PRECIPITATION_OD_THRESHOLD})  "
                f"pH={d.composition.get('pH', 'N/A')}  "
                f"Phosphate={d.composition.get('Phosphate_mM', 'N/A')} mM"
            )

        metrics = compute_well_metrics(time_hr, od)

        rows.append({
            "iteration":          iteration,
            "well":               d.well,
            "condition_type":     d.condition_type,
            **d.composition,
            **metrics,
            "precipitation_flag": precip,
            "od_readings":        json.dumps([round(float(x), 5) for x in od]),
        })

    return pd.DataFrame(rows)


# ============================================================
# 15. MODEL FITTING
# ============================================================

def fit_model_from_history(history_df: pd.DataFrame,
                            ) -> Tuple[Optional[GaussianProcessRegressor],
                                       Optional[np.ndarray],
                                       Optional[np.ndarray]]:
    """
    Fit the GP model on clean historical data.

    Exclusions:
        - Blank wells (no cells — not relevant for growth modelling)
        - Wells flagged for precipitation (OD data is unreliable)

    Requires at least 8 clean data points to fit.

    Returns:
        model         — fitted GaussianProcessRegressor, or None
        X_hist_scaled — scaled factor matrix (used for diversity checks in propose_batch)
        y_hist        — mu_max values (used for EI calculation)
    """
    # Keep only well types that represent actual growth experiments
    valid_types = [
        "lhs", "media_panel", "ph_grad", "nacl_grad", "carbon_grad",
        "candidate", "repeat_best", "center", "baseline",
    ]
    usable = history_df[history_df["condition_type"].isin(valid_types)].copy()

    # Remove precipitation-flagged wells
    if "precipitation_flag" in usable.columns:
        n_flagged = int(usable["precipitation_flag"].sum())
        if n_flagged > 0:
            print(f"  [GP] Excluding {n_flagged} precipitation-flagged wells from training.")
        usable = usable[~usable["precipitation_flag"].astype(bool)]

    # Remove rows where mu_max could not be estimated
    usable = usable[np.isfinite(usable["mu_max_per_hr"])]

    if len(usable) < 8:
        print(f"  [GP] Only {len(usable)} clean data points — skipping model fit.")
        return None, None, None

    X_raw     = usable[FACTOR_NAMES].values.astype(float)
    y         = usable["mu_max_per_hr"].values.astype(float)
    X_scaled  = SCALER.transform(X_raw)

    model = build_gp()
    model.fit(X_scaled, y)
    print(f"  [GP] Model fitted on {len(usable)} clean data points.")
    return model, X_scaled, y


def summarize_best_conditions(history_df: pd.DataFrame,
                               n:          int = 10) -> pd.DataFrame:
    """Return the top-n conditions by mu_max, excluding blanks and artefacts."""
    usable = history_df[history_df["condition_type"] != "blank"].copy()

    if "precipitation_flag" in usable.columns:
        usable = usable[~usable["precipitation_flag"].astype(bool)]

    usable = usable[np.isfinite(usable["mu_max_per_hr"])]
    return usable.sort_values("mu_max_per_hr", ascending=False).head(n)


# ============================================================
# 16. MAIN LOOP
# ============================================================

def run_closed_loop(n_iterations: int = 4,
                    output_dir:   str = "bo_outputs") -> None:
    """
    Main closed-loop Bayesian Optimization experiment.

    Each iteration:
        1. Fit GP on accumulated history
        2. Propose next plate (adaptive strategy)
        3. Export design CSV + transfer JSON
        4. Execute on workcell (or simulate)
        5. Evaluate OD curves → compute mu_max
        6. Append results to rolling history CSV
        7. Report best condition so far
    """
    outdir       = Path(output_dir)
    outdir.mkdir(parents=True, exist_ok=True)

    history_path = outdir / "all_results.csv"
    history_df   = load_history_results(history_path)

    print("=" * 72)
    print("Monomer Bio Hackathon — Closed-Loop Bayesian Optimization Agent v5 fixed")
    print(f"Factors         : {FACTOR_NAMES}")
    print(f"pH range        : {FACTOR_CONFIG['pH']}   base_pH = {BASE_PH}")
    print(
        f"Buffer capacity : {BUFFER_CAPACITY_UL_PER_PH_UNIT} uL / pH unit  "
        f"(*** calibrate before first run ***)"
    )
    print(
        f"Well budget     : "
        f"Round 1 = {ROUND_WELL_BUDGET[1]}  "
        f"Round 2 = {ROUND_WELL_BUDGET[2]}  "
        f"Round 3+ = {DEFAULT_WELL_BUDGET}"
    )
    print("=" * 72)

    for iteration in range(1, n_iterations + 1):
        well_budget = ROUND_WELL_BUDGET.get(iteration, DEFAULT_WELL_BUDGET)
        print(f"\n{'=' * 72}")
        print(f"Iteration {iteration} / {n_iterations}  "
              f"(well budget: {well_budget})")
        print(f"{'=' * 72}")

        # Step 1: Fit model
        model, X_hist_scaled, y_hist = fit_model_from_history(history_df)

        # Step 2: Build plate
        designs = build_plate_design(
            iteration        = iteration,
            total_iterations = n_iterations,
            model            = model,
            history_df       = history_df,
            X_hist_scaled    = X_hist_scaled,
            y_hist           = y_hist,
        )

        # Step 3: Build transfer array
        transfers = build_transfer_array_from_design(designs)

        design_csv    = outdir / f"plate_design_iter{iteration}.csv"
        transfer_json = outdir / f"transfer_array_iter{iteration}.json"
        save_plate_design(designs, design_csv)
        save_transfer_array(transfers, transfer_json)

        print(f"Saved design:    {design_csv}  ({len(designs)} wells)")
        print(f"Saved transfers: {transfer_json}  ({len(transfers)} transfers)")

        # Step 4: Execute on workcell
        # ── Competition day (uncomment and adapt) ──────────────────────────
        # from monomer.mcp_client import McpClient
        # from monomer.workflows import register_workflow, instantiate_workflow
        # client = McpClient("http://YOUR_WORKCELL_ENDPOINT")
        # workflow_id = register_workflow(client, Path("workflow_template.py"))
        # run_id = instantiate_workflow(client, workflow_id, transfer_json)
        # wait_for_completion(client, run_id)
        # od_df = fetch_od_from_elnora(EXPERIMENT_PLATE, [d.well for d in designs])
        # ───────────────────────────────────────────────────────────────────
        if SIMULATION_MODE:
            od_df = simulate_od_curves(designs)
        else:
            raise NotImplementedError(
                "Replace this block with live Monomer / Elnora OD fetch."
            )

        od_csv = outdir / f"od_iter{iteration}.csv"
        od_df.to_csv(od_csv, index=False)
        print(f"Saved OD data:   {od_csv}")

        # Step 5: Evaluate
        iter_results = evaluate_plate_from_od(designs, od_df, iteration)
        iter_csv     = outdir / f"results_iter{iteration}.csv"
        iter_results.to_csv(iter_csv, index=False)
        print(f"Saved results:   {iter_csv}")

        # Step 6: Append to rolling history
        history_df = pd.concat([history_df, iter_results], ignore_index=True)
        history_df.to_csv(history_path, index=False)

        # Step 7: Report best condition so far
        best_df = summarize_best_conditions(history_df, n=1)
        if not best_df.empty:
            row = best_df.iloc[0]
            print("\nBest condition so far:")
            print(f"  iteration      : {int(row['iteration'])}")
            print(f"  well           : {row['well']}")
            print(f"  condition_type : {row['condition_type']}")
            print(f"  mu_max_per_hr  : {row['mu_max_per_hr']:.4f}")
            print(f"  doubling_time  : {row['doubling_time_hr']:.4f} hr")
            print("  composition    :")
            for f in FACTOR_NAMES:
                unit = "mM" if "mM" in f else ("%" if "pct" in f else "")
                print(f"    {f:<18}: {row[f]:.4f} {unit}")

    print(f"\n{'=' * 72}")
    print("Experiment complete.")
    print(f"Full history saved to: {history_path}")
    print(f"{'=' * 72}")


# ============================================================
# 17. ENTRY POINT
# ============================================================

if __name__ == "__main__":
    run_closed_loop(n_iterations=4, output_dir="bo_outputs")