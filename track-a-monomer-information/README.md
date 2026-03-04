# Track A: Build an Autonomous Closed-Loop Agent

**Leads:** Carter Allen (carter@monomerbio.com) / Carmen Kivisild (Elnora)

**Goal:** Build an AI agent that runs media optimization experiments autonomously on real cells

---

# Hackathon Flow

## Step 0: Research (Phase 1 → Phase 2 handoff)

Use Elnora to research *V. natriegens* growth and identify which media components to test and at what concentrations. The available components are [listed in Notion](https://www.notion.so/monomer/2ff8d59ea9ff815a94c7d13e691fe6db?v=2ff8d59ea9ff81c89be7000c0ac066b6&source=copy_link): you can choose from 13 options including Glucose, Sodium Chloride, Magnesium Sulfate, Potassium Phosphate (mono/dibasic), Potassium Chloride, Calcium Chloride, Ammonium Sulfate, MOPS, Glycerol, Tryptone, Yeast Extract, and Trace metals solution.

Come out of Phase 1 with the arc of your experiment, which components you want to use in which wells, and what stock concentrations you want.

## Step 1: Workcell Tutorial - Run a Workflow on the workcell using the Monomer MCP, to grow some cells in provided media
Monomer Staff will provide you with an empty 96-well plate, a Reagent Plate filled with Novel Media, and a 24-well Cell Culture Stock Plate with live Vibrio Natriegens in it.

You will follow the Tutorial below to use the MCP to run an experiment to determine the correct seeding density to get the most growth.

## Step 2: Design your stock plate

You'll get a **24-well deep well plate** to use as your stock plate. Assign one component or component mixture per well (e.g. A1 = Glucose 1M, B1 = KCl 2M, C1 = MOPS 500mM). The robot will pipette directly from these wells into your experiment plate, so stock concentration determines your working concentration range.

**Transfer limit: max 40 transfers per iteration.** With 8 experimental wells and 4 components, that's 32 transfers — leaving room for a control well of pure base media. Plan your well layout and dilutions before you start. This is to make sure the workcell is not locked up for extended periods of time

Fill out [`REAGENT_PLATE.md`](./REAGENT_PLATE.md) and hand it to a Monomer team member.
We'll prepare the stock solutions, load the plate, and give you a `reagent_type` tag to use in your workflow.

## Step 3: Run an Agentic Experiment Cycle

Your agent:
1. **Decides** — picks a media composition to test next (gradient descent, Bayesian optimization, etc.)
2. **Acts** — generates a transfer array and instantiates the workflow
3. **Waits** — the first few iterations require Monomer team approval (~a few minutes); later iterations may be pre-approved
4. **Observes** — reads OD600 growth data; platereader runs every 5–10 minutes during the ~90 min monitoring window
5. **Loops** — each iteration should be ~2 hours end-to-end; you get ~6–8 iterations over the hackathon

# Setting up your MCP Servers

There are two servers you will use during the hackathon:

1. **Monomer Cloud MCP** - this MCP is what you will use to interact with your plate data.
2. **Monomer Automation MCP** - this MCP is what you will use to interact with the automated workcell.

## Step 1: Onboarding to Monomer Bio
If you haven't already been onboarded to the Monomer culture monitor, we will need your email. Monomer staff will stop by after Phase 1 to collect your information and get you set up. You will receive an email invitation to the Monomer Culture Monitor, accept the invite and then navigate to the [settings page](https://cloud-staging.monomerbio.com/settings). From here, you will need to click the **Show Token** button to obtain the MCP token needed for the next step.

<img width="601" height="223" alt="image" src="https://github.com/user-attachments/assets/fa7ac205-624b-42e6-94dd-fb62bd90c66b" />

## Step 2: Connect to the MCP

NOTE(Turner): Outside of the hackathon we should use https://desktop-nrh3hvl.tapir-decibel.ts.net/mcp for remote/testing.

### Option A: Cursor

1. Download [Cursor](https://cursor.com/download)
2. Open Settings → MCP
3. Add server: `http://192.168.68.55:8080/mcp` (no auth needed on local network)
4. For Monitor MCP (read-only cloud data), add: `https://backend-staging.monomerbio.com/mcp` with `Authorization: Bearer <YOUR_TOKEN>`

### Option B: Claude Code (requires subscription)

1. Set up Claude Code using the instructions from their [Get Started page](https://code.claude.com/docs/en/overview#get-started).
2. In your terminal, run the following command to set up the **monomer cloud** MCP:
`claude mcp add --transport http monomer-cloud https://backend-staging.monomerbio.com/mcp --header "Authorization: Bearer <YOUR_TOKEN>"`
3. In your terminal, run the following command to set up the **monomer automation platform** MCP:
`claude mcp add --transport http monomer-autoplat http://192.168.68.55:8080/mcp --header "Authorization: Bearer <YOUR_TOKEN>"`

### Option C: Claude API

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

### Option D: Any MCP-compatible tool

The workcell speaks standard MCP (JSON-RPC 2.0 over HTTP POST). See `CLAUDE.md` for the full tool list and MCP Resources (DSL guides, schema references, and a working example workflow your AI can read directly).

---

## Workcell Tutorial

### Start: Explore the MCPs
Ask your tool (Claude Code, Cursor, etc.) to tell you about the cloud mcp and the autoplat mcp, and what they can be used for.

### Build: Generate a Simple Transfer Routine on your test plate
Ask Monomer Staff for the name of your `<Reagant Plate>` and `<Cell Culture Stock Plate>`.
Use those inputs to modify the following: 
```
Create a workflow to transfer different volumes of Novel Media from <Reagent Plate> and different percentages
of cell stock solution from well A1 of <Cell Culture Stock Plate> into different wells of the 96-well
experiment plate using the hackathon_transfer_samples Routine, make triplicates of percentages from 50% to 5%;
measure absorbance of the plate immediately before the transfer routine and then immediately
after the transfer routine, then every 10 minutes. Instantiate the workflow once it is validated.
```
And then paste this into cursor, claude code, or the MCP client of your choice.

### Analyze: Ask Monomer Cloud for data and poll it to build a graph of Delta OD600 in Streamlit that continuously updates
We are trying to optimize for the biggest change in growth for a given media, not just Max OD600, so it behooves us to capture this delta. Once your plate has finished the liquid handling step, your workflow will continuously take plate reads. You should be able to log in to our staging [Culture Monitor](https://cloud-staging.monomerbio.com/) to view your plate data.

## Workcell Constraints

- **Workflow approval:** Every workflow goes to `pending_approval` after instantiation. The first few iterations require manual approval from a Monomer team member (~a few minutes). `poll_workflow_completion()` blocks automatically; your agent just waits. If nothing happens after 10 minutes, flag a Monomer team member.

- **One workflow at a time:** The workcell runs workflows sequentially. Wait for the current one to complete before instantiating the next.

- **Tip and reagent tracking:** Handled internally by the workflow template. You don't need to count tips or reagent wells — the template computes consumption from your transfer array.

- **Workcell sharing:** Other teams may be using the workcell. If your workflow is queued but not starting, check with the Monomer team.

- **Volume limits:** P50 handles 1–50 µL, P200 handles 51–200 µL, P1000 handles 201–1000 µL. `apply_constraints()` enforces these in your transfer array.

- **Monitoring frequency:** Minimum 5 minutes between platereader reads. Default in the template is 10 minutes (`monitoring_interval_minutes=10`), which gives a 90-minute window with 9 reads. You can go down to 5 minutes for more granular data.

- **Reagent plate tag:** Your custom stock plate must be registered on the workcell with a specific `reagent_type` tag before you can use it. Coordinate with the Monomer team when you hand off your plate layout — they'll give you the tag string to use in your workflow.
