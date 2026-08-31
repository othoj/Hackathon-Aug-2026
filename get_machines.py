# Discrete event simulation based on dataset 4 (LVHM_E).

from pathlib import Path

import pandas as pd

DEFAULT_XLSX_PATH = (
    Path(__file__).resolve().parent
    / "SMT2020"
    / "SMT_2020 - Final"
    / "General Data"
    / "dataset 4"
    / "SMT_2020_Model_Data_-_LVHM_E.xlsx"
)

LOADING_UNLOADING_TIME_MINUTES = 1


def _empty_to_no(value) -> str:
    if pd.isna(value) or str(value).strip() == "":
        return "NO"
    return str(value).strip()


def load_toolgroups(xlsx_path: Path | str | None = None) -> dict[str, dict]:
    """Load machine definitions from the Toolgroups sheet in the LVHM_E workbook."""
    path = Path(xlsx_path) if xlsx_path is not None else DEFAULT_XLSX_PATH
    df = pd.read_excel(path, sheet_name="Toolgroups", header=0)

    machines: dict[str, dict] = {}
    for _, row in df.iterrows():
        toolgroup = str(row["TOOLGROUP"]).strip()
        machines[toolgroup] = {
            "category": str(row["AREA"]).strip(),
            "number_of_tools": int(row["NUMBER OF TOOLS"]),
            "cascading_tool": _empty_to_no(row["CASCADINGTOOL"]),
            "batching_tool": _empty_to_no(row["BACTHINGTOOL"]),
            "batching_criterion": _empty_to_no(row["BATCHCRITERION"]),
            "loading_time": LOADING_UNLOADING_TIME_MINUTES,
            "unloading_time": LOADING_UNLOADING_TIME_MINUTES,
        }

    return machines

