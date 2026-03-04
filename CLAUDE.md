# Monomer Bio Hackathon — AI Agent Context

This file gives AI coding assistants (Claude Code, Cursor, etc.) the technical context needed to help a contestant build a Track 2A closed-loop agent on the Monomer workcell.

## Platform Primitives

### Hierarchy
```
WorkflowDefinition  →  contains ordered RoutineReferences
WorkflowInstance    →  a running execution of a definition
Routine             →  atomic instrument action (incubate, pipette, read)
CulturePlate        →  tracked plate with barcode, history of readings
```

### Available Routines (Track 2A)

| Routine Name | Purpose | Key Parameters |
|---|---|---|
| **Hackathon Transfer Samples** | General-purpose liquid handling across reagent / experiment / cell_culture_stock plates | `reagent_name`, `experiment_plate_barcode`, `cell_culture_stock_plate_barcode`, `transfer_array` |
| **Measure Absorbance** | Read OD600 from a set of wells | `culture_plate_barcode`, `method_name` (`96wp_od600`), `wells_to_process` |

> **`reagent_name` is a restricted field** — it must match the tag registered on the workcell for your stock plate. Coordinate with the Monomer team when you hand off your plate layout to get the correct string.

These routines are wired up inside `workflow_definition_template.py`. Don't call them directly — use `instantiate_workflow()` which handles upload and scheduling.

---

## MCP Connection

### Autoplat MCP (Workcell — workflow control)
- **URL:** `http://192.168.68.55:8080/mcp`
- **Auth:** None (local network)
- **Transport:** JSON-RPC 2.0 over HTTP POST with SSE response

#### Available Tools

**Workflow Definitions**
```
list_workflow_definitions         # All registered workflow definitions
get_workflow_definition           # Detailed info about a specific definition
get_workflow_definition_schedule  # Scheduled nodes with relative execution times
get_workflow_definition_dag       # DAG structure showing nodes and dependencies
list_workflow_definition_files    # Workflow definition files on disk
get_workflow_dsl_schemas          # Simplified schemas for DSL classes
create_workflow_definition_file   # Upload a workflow .py file to workcell
validate_workflow_definition_file # Validate definition before registration ← use this
register_workflow_definition      # Register validated file as a named definition
```

**Workflow Instances**
```
list_workflow_instances           # All instances and their statuses
get_workflow_instance_details     # Poll instance status
list_workflow_routines            # Scheduled steps for a specific instance
list_pending_workflows            # Workflows awaiting operator approval
instantiate_workflow              # Launch a workflow (returns instance UUID)
check_workflow_cancellable        # Check if workflow can be safely cancelled
cancel_workflow_instance          # Cancel a running or pending instance
```

**Routines**
```
list_available_routines           # All available routines and their signatures
get_routine_details               # Detailed signature for one routine
list_future_routines              # Upcoming scheduled routines
get_future_routine_details        # Complete future routine details
get_workflow_routine_with_children# WorkflowRoutine with child FutureRoutines
trace_future_routine_to_workflow  # Trace a FutureRoutine back to its workflow
check_consumables_for_timeframe   # Consumables needed for upcoming routines
```

**Plates**
```
list_culture_plates               # All culture plates on the workcell
check_plate_availability          # Check if a plate barcode is available
unlink_culture_plate_from_workflow# Unlink a plate from its current workflow
list_reagent_plates               # Reagent plates and their media/well state
```

#### MCP Resources
Read these directly to understand the workflow DSL without guessing:
```
guide://workflows/dsl             # Complete DSL reference with examples ← start here
guide://workflows/creation        # Quick start guide for creating workflows
guide://workflows/concepts        # Workflow concepts and execution flow
example://workflows/ipsc-maintenance # Complete working example workflow file
schema://workflows/dsl-api        # Auto-generated API reference for DSL classes
schema://workflows/models         # Database schema models
guide://future-routines/monitoring# Monitoring guide for AI agents
schema://routines/parameters      # Routine parameter types reference
guide://cultures-and-plates/concepts # Domain concepts explanation
doc://cultures-and-plates/api-usage  # API usage guide with examples
```

### Monitor MCP (Cloud — read-only observation)
- **URL:** `https://backend-staging.monomerbio.com/mcp`
- **Auth:** `Authorization: Bearer YOUR_TOKEN` (get token from cloud-staging.monomerbio.com)
- **Transport:** Same JSON-RPC 2.0

#### Available Tools
```
list_cultures                     # All culture plates being tracked
get_culture_details               # Plate metadata + latest readings
list_culture_statuses             # Status summary of all cultures
update_culture_status             # Update status for one or more wells
list_plates                       # All plates with observation summaries
get_plate_observations            # Time-series OD600 readings for a plate
export_plate_observations         # Export observations as structured data
```

```python
# Connect to the Monitor MCP (cloud) — use this to read OD600 observations
monitor = McpClient("https://backend-staging.monomerbio.com")
monitor.session_id = "dummy"  # set auth header instead of session handshake

import requests
resp = requests.post(
    "https://backend-staging.monomerbio.com/mcp",
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer YOUR_TOKEN_HERE",
    },
    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "get_plate_observations",
                     "arguments": {"plate_name": "TEAM-R1-20260314"}}},
    timeout=30,
)
# Or use fetch_absorbance_results() from datasets.py which handles this via
# the Autoplat local REST API — simpler if you're already connected to Autoplat.
```

### Install in Cursor
```json
{
  "mcpServers": {
    "monomer-autoplat": {
      "url": "http://192.168.68.55:8080/mcp"
    },
    "monomer-monitor": {
      "url": "https://backend-staging.monomerbio.com/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN_HERE"
      }
    }
  }
}
```

---

### Transfer array format (all strategies)

Your agent produces a **list of dicts** — one dict per transfer step. No library needed.
The routine supports three named plate types: `"reagent"`, `"experiment"`, `"cell_culture_stock"`.

```python
transfers = [
    # Base media — new_tip "once" reuses one tip for all transfers from D1
    {"src_plate": "reagent", "src_well": "D1", "dst_plate": "experiment", "dst_well": "A2",
     "volume": 180, "new_tip": "once", "blow_out": True},
    {"src_plate": "reagent", "src_well": "D1", "dst_plate": "experiment", "dst_well": "B2",
     "volume": 160, "new_tip": "once", "blow_out": True},
    # Supplement — one tip reused for all transfers from A1
    {"src_plate": "reagent", "src_well": "A1", "dst_plate": "experiment", "dst_well": "B2",
     "volume":  20, "new_tip": "once", "blow_out": True},
    # Seed cells — always fresh tip, mix after dispensing
    {"src_plate": "cell_culture_stock", "src_well": "A1", "dst_plate": "experiment",
     "dst_well": "A2", "volume": 20, "post_mix_volume": 10, "post_mix_reps": 5,
     "new_tip": "always", "blow_out": False},
]
```

**Tip policies:** `"once"` = 1 tip per unique `(src_plate, src_well)` pair (most efficient for reagents);
`"always"` = fresh tip every transfer; `"never"` = keep current tip.

**Volumes:** P50: 1–50 µL | P200: 51–200 µL | P1000: 201–1000 µL

See `track-2a-closed-loop/REAGENT_PLATE.md` for the full field reference.

---

## Workcell Constraints

| Constraint | Value |
|-----------|-------|
| Max concurrent workflows | 1 (sequential scheduling) |
| Max transfers per iteration | 40 (`_MAX_TRANSFERS` in template) |
| Platereader minimum interval | 5 minutes (default in template: 10 min) |
| Well volume | 180 µL |
| Incubation temperature | 37°C |
| Tip reuse policy | Base media well (D1) reuses 1 tip; all other source wells use fresh tips |
| P50 range | 1–50 µL |
| P200 range | 51–200 µL |
| P1000 range | 201–1000 µL |
| Reagent plate storage | 4°C (Liconic STX-110) — pull 30 min before use to reduce cold-lag |

Workflows go to `pending_approval` after instantiation. The first few iterations require manual Monomer team approval; later iterations may be pre-approved.

---

## Useful REST Endpoints

The workcell also exposes a REST API alongside MCP:

```
GET  /api/datasets/?verbose=1&ordering=-createdAt   # All datasets (OD600 readings)
GET  /api/culture-plates/                            # All plates
```

Headers required: `X-Monomer-Client: desktop-frontend`

See `monomer/datasets.py` for a working example.
