# Phase 2 - Track A: Build an Autonomous Closed-Loop Agent

**Lead:** Carter Allen (carter@monomerbio.com) / Carmen Kivisild (Elnora)
**Goal:** Build an AI agent that runs media optimization experiments autonomously on real cells

---

## The Flow

### Step 0: Research (Phase 1 → Phase 2 handoff)

Use Elnora to research *V. natriegens* growth and identify which media components to test and at what concentrations. The [available components are listed in Notion](https://www.notion.so/monomer/2ff8d59ea9ff815a94c7d13e691fe6db?v=2ff8d59ea9ff81c89be7000c0ac066b6&source=copy_link) — you can choose from ~13 options including Glucose, Sodium Chloride, Magnesium Sulfate, Potassium Phosphate (mono/dibasic), Potassium Chloride, Calcium Chloride, Ammonium Sulfate, MOPS, Glycerol, Tryptone, Yeast Extract, and Trace metals solution.

Come out of Phase 1 with the arc of your experiment, which components, which wells they go in, and what stock concentrations you want.

### Step 1: Workcell Tutorial - Run a Workflow on the workcell using the Monomer MCP, to grow some cells in provided media
Monomer Staff will provide you with an empty 96-well plate, a Reagent Plate filled with Novel Media, and a 24-well Cell Culture Stock Plate with growing Vibrion Natriegens in it. 

You will follow the Tutorial below to use the MCP to run an experiment to determine the correct seeding density to get the most growth.


### Step 2: Design your stock plate

You'll get a **24-well deep well plate** to use as your stock plate. Assign one component or component mixture per well (e.g. A1 = Glucose 1M, B1 = KCl 2M, C1 = MOPS 500mM). The robot will pipette directly from these wells into your experiment plate, so stock concentration determines your working concentration range.

**Transfer limit: max 40 transfers per iteration.** With 8 experimental wells and 4 components, that's 32 transfers — leaving room for a control well of pure base media. Plan your well layout and dilutions before you start. This is to make sure the workcell is not locked up for extended periods of time

Fill out [`REAGENT_PLATE.md`](./REAGENT_PLATE.md) and hand it to a Monomer team member.
We'll prepare the stock solutions, load the plate, and give you a `reagent_type` tag to use in your workflow.

### Step 2: Run an Agentic Experimental Cycle

Your agent:
1. **Decides** — picks a media composition to test next (gradient descent, Bayesian optimization, etc.)
2. **Acts** — generates a transfer array and instantiates the workflow
3. **Waits** — the first few iterations require Monomer team approval (~a few minutes); later iterations may be pre-approved
4. **Observes** — reads OD600 growth data; platereader runs every 5–10 minutes during the ~90 min monitoring window
5. **Loops** — each iteration should be ~2 hours end-to-end; you get ~6–8 iterations over the hackathon



## Setting up your MCP Servers

There are two servers you will use during the hackathon:

1. **Monomer Cloud MCP** - this MCP is what you will use to interact with your plate data.
2. **Monomer Automation MCP** - this MCP is what you will use to interact with the automated workcell.

### Step 1: Onboarding to Monomer Bio
If you haven't already gotten onboarded onto Monomer, we will need your email. Find Monomer Staff to get you set up in an organization.

### Step 2: Connect to the MCP

### Option A: Claude Code / Claude API

Add this to your Claude MCP config (`~/.claude.json`):

```json
{
  "mcpServers": {
    "monomer-cloud": {
      "type": "http",
      "url": "https://backend-staging.monomerbio.com/mcp",
      "headers": {
        "Authorization": "Bearer <TOKEN_FROM_MONOMER_CLOUD>"
      }
    },
    "monomer-autoplat": {
        "type": "http",
        "url": "https://desktop-nrh3hvl.tapir-decibel.ts.net/mcp",
        "headers": {
        "Authorization": "Bearer <TOKEN_FROM_MONOMER_CLOUD>"
      }
    }
  }
}
```

### Option B: Cursor

1. Download [Cursor](https://cursor.com/download)
2. Open Settings → MCP
3. Add server: `http://192.168.68.55:8080/mcp` (no auth needed on local network)
4. For Monitor MCP (read-only cloud data), add: `https://backend-staging.monomerbio.com/mcp` with `Authorization: Bearer YOUR_TOKEN`


### Option C: Any MCP-compatible tool

The workcell speaks standard MCP (JSON-RPC 2.0 over HTTP POST). See `CLAUDE.md` for the full tool list and MCP Resources (DSL guides, schema references, and a working example workflow your AI can read directly).

---

## Tutorial

### Start: Explore the MCPs
Ask your tool (Claude Code, Cursor, etc.) to tell you about the cloud mcp and the autoplat mcp, and what it can learn about the use.

### Build: Generate a Simple Transfer Routine on your test plate
Ask Monomer Staff for the name of your cell stock plate and reagent plate
Use the prompt "build a workflow to transfer different volumes of Novel Media from <Reagent Plate>  and different percentages of cell stock solution from A1 of <Cell Culture Stock Plate Name> into different wells of the 96-well experiment plate using the hackathon_transfer_samples Routine, make triplicates of percentages from 50% to 5%; measure absorbance of the plate immediately before the transfer routine and then immediately after the transfer routine, then every 10 minutes. Instantiate the workflow once it is validated"

### Analyze: Ask Monomer Cloud for data and poll it to build a graph of Delta OD600 in Streamlit that continuously updates
We are trying to optimize for the biggest change in growth for a given media, not just Max OD600, so it behooves us to capture this delta. Once your plate has finished the liquid handling step, 

Stopping point for carter

## Workflow Definition Format

A workflow definition is a Python file with a `build_definition()` function. The function accepts typed parameters — your agent passes them at instantiation time, so you only ever upload the file once.

```python
def build_definition(
    plate_barcode: str,           # always required
    transfer_array: str = "[]",   # your reagent transfers this iteration
    dest_wells: str = "...",      # wells being filled
    monitoring_wells: str = "...",# cumulative — all wells measured so far
    seed_well: str = "A1",        # advances A1 → B1 → C1 ... each round
    next_seed_well: str = "B1",   # pre-warms the next seed well
    reagent_type: str = "...",    # identifies your stock plate
    monitoring_readings: int = 9, # 9 × 10 min = 90 min window
    ...
) -> WorkflowDefinitionDescriptor:
    # builds the routine sequence and returns it
```

The template validates your inputs (transfer count, well conflicts, volumes) before the workflow reaches the approval queue. See `examples/workflow_definition_template.py` for the full implementation and parameter docs.

---

## Workcell Constraints

- **Workflow approval:** Every workflow goes to `pending_approval` after instantiation. The first few iterations require manual approval from a Monomer team member (~a few minutes). `poll_workflow_completion()` blocks automatically; your agent just waits. If nothing happens after 10 minutes, flag a Monomer team member.

- **One workflow at a time:** The workcell runs workflows sequentially. Wait for the current one to complete before instantiating the next.

- **Tip and reagent tracking:** Handled internally by the workflow template. You don't need to count tips or reagent wells — the template computes consumption from your transfer array.

- **Workcell sharing:** Other teams may be using the workcell. If your workflow is queued but not starting, check with the Monomer team.

- **Volume limits:** P50 handles 1–50 µL, P200 handles 51–200 µL, P1000 handles 201–1000 µL. `apply_constraints()` enforces these in your transfer array.

- **Monitoring frequency:** Minimum 5 minutes between platereader reads. Default in the template is 10 minutes (`monitoring_interval_minutes=10`), which gives a 90-minute window with 9 reads. You can go down to 5 minutes for more granular data.

- **Reagent plate tag:** Your custom stock plate must be registered on the workcell with a specific `reagent_type` tag before you can use it. Coordinate with the Monomer team when you hand off your plate layout — they'll give you the tag string to use in your workflow.
