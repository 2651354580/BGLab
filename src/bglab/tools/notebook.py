""

from __future__ import annotations
import json
import os
from bglab.tools.base import Tool, ToolRegistry


NOTEBOOK_EDIT_PROMPT = """Completely replaces the contents of a specific cell in a Jupyter notebook (.ipynb file) with new source. Jupyter notebooks are interactive documents that combine code, text, and visualizations, commonly used for data analysis and scientific computing. The notebook_path parameter must be an absolute path, not a relative path. The cell_number is 0-indexed. Use edit_mode=insert to add a new cell at the index specified by cell_number. Use edit_mode=delete to delete the cell at the index specified by cell_number."""


def _parse_cell_id(raw: str | None) -> int | None:
    """Parse cell_id: can be numeric string index or UUID-style string."""
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return raw  # UUID string


def _notebook_edit_call(args: dict) -> str:
    notebook_path = str(args.get("notebook_path", ""))
    if not notebook_path:
        return "Error: notebook_path is required (must be absolute path)"

    if not os.path.isabs(notebook_path):
        return "Error: notebook_path must be an absolute path"

    new_source = str(args.get("new_source", ""))
    cell_type = args.get("cell_type", "code")
    if cell_type not in ("code", "markdown"):
        cell_type = "code"
    edit_mode = args.get("edit_mode", "replace")
    if edit_mode not in ("replace", "insert", "delete"):
        edit_mode = "replace"

    if not os.path.exists(notebook_path):
        return f"Error: file not found: {notebook_path}"

    # Read notebook
    try:
        with open(notebook_path, encoding="utf-8") as f:
            nb = json.load(f)
    except json.JSONDecodeError as e:
        return f"Error: invalid notebook JSON: {e}"

    cells = nb.get("cells", [])
    if not isinstance(cells, list):
        return "Error: notebook has no cells array"

    # Resolve cell_id to index
    cell_id = args.get("cell_id")
    idx = None
    if cell_id is not None:
        parsed = _parse_cell_id(str(cell_id))
        if isinstance(parsed, int):
            if 0 <= parsed < len(cells):
                idx = parsed
        else:
            # Search by cell id (UUID in metadata)
            for i, c in enumerate(cells):
                if isinstance(c, dict) and c.get("id") == parsed:
                    idx = i
                    break
    else:
        # Default to last cell if no cell_id given for insert
        if edit_mode == "insert":
            idx = len(cells) - 1 if cells else -1
        else:
            idx = len(cells) - 1 if cells else None

    if edit_mode == "delete":
        if idx is None or idx < 0 or idx >= len(cells):
            return f"Error: cell_id out of range (0-{len(cells) - 1})"
        deleted = cells.pop(idx)
        nb["cells"] = cells
        with open(notebook_path, "w", encoding="utf-8") as f:
            json.dump(nb, f, ensure_ascii=False, indent=1)
        return f"Deleted cell {idx} from {notebook_path}"

    elif edit_mode == "insert":
        new_idx = idx + 1 if idx is not None and idx >= 0 else 0
        import uuid as _uuid
        new_cell = {
            "cell_type": cell_type,
            "metadata": {},
            "source": new_source.splitlines(True) if new_source else [""],
            "id": str(_uuid.uuid4())[:8],
        }
        if new_source and cell_type == "code":
            new_cell["outputs"] = []
            new_cell["execution_count"] = None
        cells.insert(new_idx, new_cell)
        nb["cells"] = cells
        with open(notebook_path, "w", encoding="utf-8") as f:
            json.dump(nb, f, ensure_ascii=False, indent=1)
        return f"Inserted new {cell_type} cell at index {new_idx} in {notebook_path}"

    else:  # replace
        if idx is None or idx < 0 or idx >= len(cells):
            return f"Error: cell_id out of range (0-{len(cells) - 1})"
        old_cell = cells[idx]
        new_cell_type = cell_type or old_cell.get("cell_type", "code")
        old_cell["cell_type"] = new_cell_type
        old_cell["source"] = new_source.splitlines(True) if new_source else [""]
        if new_cell_type == "code" and "outputs" not in old_cell:
            old_cell["outputs"] = []
        nb["cells"] = cells
        with open(notebook_path, "w", encoding="utf-8") as f:
            json.dump(nb, f, ensure_ascii=False, indent=1)
        return f"Replaced cell {idx} ({new_cell_type}) in {notebook_path}"


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="NotebookEdit",
        searchHint="edit jupyter notebook ipynb cells",
        description="Edit a Jupyter notebook cell (replace/insert/delete)",
        prompt=NOTEBOOK_EDIT_PROMPT,
        parameters={
            "type": "object",
            "properties": {
                "notebook_path": {
                    "type": "string",
                    "description": "Absolute path to the .ipynb file to edit",
                },
                "cell_id": {
                    "type": "string",
                    "description": "ID (index or UUID) of the cell to edit. For insert, inserts after this cell.",
                },
                "new_source": {
                    "type": "string",
                    "description": "The new source for the cell",
                },
                "cell_type": {
                    "type": "string",
                    "enum": ["code", "markdown"],
                    "description": "Cell type (code or markdown). Required for insert mode.",
                },
                "edit_mode": {
                    "type": "string",
                    "enum": ["replace", "insert", "delete"],
                    "description": "Edit mode: replace (default), insert, or delete",
                },
            },
            "required": ["notebook_path", "new_source"],
        },
        call=_notebook_edit_call,
        is_read_only=False,
    ))
