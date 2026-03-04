# Reagent Plate Setup

Before your first run, design your stock plate and hand this doc to a Monomer team member.
They will prepare the solutions, load the plate into the workcell, and give you a
`reagent_name` tag to use in your workflow.

---

## Step 1 — Choose your reagents

Pick from the available compounds:

| Compound | Notes |
|----------|-------|
| Glucose | Carbon source |
| Sodium Chloride (NaCl) | Osmolarity |
| Magnesium Sulfate (MgSO4) | Cofactor |
| Potassium Phosphate monobasic (KH₂PO₄) | Buffer |
| Potassium Phosphate dibasic (K₂HPO₄) | Buffer |
| Potassium Chloride (KCl) | Osmolarity |
| Calcium Chloride (CaCl₂) | Cofactor |
| Ammonium Sulfate ((NH₄)₂SO₄) | Nitrogen source |
| MOPS | Buffer |
| Glycerol | Carbon source |
| Tryptone | Rich carbon/nitrogen |
| Yeast Extract | Rich carbon/nitrogen |
| Trace metals solution | Micronutrients |

Novel Bio (base media) is always pre-loaded in **D1** — you don't need to include it.
It fills the remaining volume in each experimental well automatically.

---

## Step 2 — Assign wells and concentrations

Fill in the table below and hand it to the Monomer team.
Use wells A1–D6 on the 24-well deep well plate (modeled as a 96-well grid).
Leave unused rows blank.

> **Stock concentration determines your working concentration range.**
> Transfer volume max is 90 µL per reagent per well.
> Working concentration = stock concentration × (transfer volume / 180 µL).

| Well | Reagent | Stock Concentration | Volume Needed |
|------|---------|---------------------|---------------|
| A1   |         |                     | 2 mL          |
| B1   |         |                     | 2 mL          |
| C1   |         |                     | 2 mL          |
| D1   | Novel Bio (base media) | pre-loaded | —        |
| A2   | NM+Cells | pre-loaded         | —             |
| B2   |         |                     | 2 mL          |
| C2   |         |                     | 2 mL          |
| D2   |         |                     | 2 mL          |

---

## Step 3 — Get your `reagent_name` tag

After the Monomer team loads your plate, ask for your `reagent_name` tag.
It will look something like `"Team Alpha Stock Plate"`.

Use this string in every `instantiate_workflow()` call:

```python
extra_inputs={
    "reagent_name": "Team Alpha Stock Plate",   # ← your tag here
    "transfer_array": json.dumps(my_transfers),
    ...
}
```

---

## Transfer array format reference

Your agent produces a **list of dicts**. Each dict is one transfer step.
The routine supports three named plate types: `"reagent"`, `"experiment"`, `"cell_culture_stock"`.

```python
# Example: fill two experiment wells from reagent plate, then seed cells
# A1=Glucose, D1=Novel Bio on reagent plate | A1=cells on cell_culture_stock plate
transfer_array = [
    # Base media first — new_tip "once" reuses one tip for all D1 transfers
    {"src_plate": "reagent", "src_well": "D1", "dst_plate": "experiment", "dst_well": "A2",
     "volume": 180, "new_tip": "once", "blow_out": True},
    {"src_plate": "reagent", "src_well": "D1", "dst_plate": "experiment", "dst_well": "B2",
     "volume": 160, "new_tip": "once", "blow_out": True},
    # Supplement — one tip reused for all transfers from the same source well
    {"src_plate": "reagent", "src_well": "A1", "dst_plate": "experiment", "dst_well": "B2",
     "volume":  20, "new_tip": "once", "blow_out": True},
    # Seed cells — always use a fresh tip, mix after dispensing
    {"src_plate": "cell_culture_stock", "src_well": "A1", "dst_plate": "experiment", "dst_well": "A2",
     "volume": 20, "post_mix_volume": 10, "post_mix_reps": 5, "new_tip": "always", "blow_out": False},
]
```

**Each entry fields:**
| Field | Required | Values |
|-------|----------|--------|
| `src_plate` | yes | `"reagent"` \| `"experiment"` \| `"cell_culture_stock"` |
| `src_well` | yes | e.g. `"A1"` |
| `dst_plate` | yes | same options |
| `dst_well` | yes | e.g. `"B3"` |
| `volume` | yes | µL (int or float) |
| `new_tip` | no | `"always"` (default) \| `"once"` \| `"never"` |
| `blow_out` | no | `true` (default) |
| `pre_mix_volume` / `pre_mix_reps` | no | mix at source before aspirating |
| `post_mix_volume` / `post_mix_reps` | no | mix at dest after dispensing |

**Tip policies:**
- `"always"` — fresh tip per transfer
- `"once"` — one tip per unique `(src_plate, src_well)` pair; reused for all transfers from that source
- `"never"` — keep whatever tip is currently held

**Volume → pipette:** P50: 1–50 µL | P200: 51–200 µL | P1000: 201–1000 µL

---

## Warm-up note

Your stock plate lives in the **4°C fridge** between uses. Cold reagents cause a
30–60 min growth lag. Pull the plate out while your current iteration is running
(~90 min monitoring window) so it warms to room temp before the next run.
