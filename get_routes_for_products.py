from __future__ import annotations

from pathlib import Path
from typing import TypedDict

import pandas as pd

EXCEL_PATH = (
    Path(__file__).resolve().parent
    / "SMT2020"
    / "SMT_2020 - Final"
    / "General Data"
    / "dataset 4"
    / "SMT_2020_Model_Data_-_LVHM_E.xlsx"
)

PROCESSING_UNIT_MULTIPLIERS = {
    "wafer": 1,
    "lot": 1,
    "batch": 1,
}

NUM_PRODUCTS = 10


class RouteStep(TypedDict):
    product: str
    step: int
    machine: str
    processing_unit: str
    processing_time: tuple[float, float]


class ScaledRouteStep(TypedDict):
    product: str
    step: int
    machine: str
    processing_time: tuple[float, float]


def _closed_interval(mean: float, offset: float) -> tuple[float, float]:
    return (mean - offset, mean + offset)


def _scale_interval(
    interval: tuple[float, float], multiplier: float
) -> tuple[float, float]:
    low, high = interval
    return (low * multiplier, high * multiplier)


def _processing_unit_multiplier(processing_unit: str) -> float:
    key = processing_unit.strip().lower()
    if key not in PROCESSING_UNIT_MULTIPLIERS:
        raise ValueError(f"Unknown processing unit: {processing_unit!r}")
    return PROCESSING_UNIT_MULTIPLIERS[key]


def get_routes_for_product(
    product_number: int,
    excel_path: Path | str = EXCEL_PATH,
) -> list[RouteStep]:
    if not 1 <= product_number <= NUM_PRODUCTS:
        raise ValueError(f"product_number must be between 1 and {NUM_PRODUCTS}")

    sheet_name = f"Route_Product_{product_number}"
    df = pd.read_excel(excel_path, sheet_name=sheet_name)

    product_name = f"Route_Product_{product_number}"
    routes: list[RouteStep] = []

    for _, row in df.iterrows():
        mean = row["MEAN"]
        offset = row["OFFSET"]
        if pd.isna(mean) or pd.isna(offset):
            continue

        routes.append(
            {
                "product": product_name,
                "step": int(row["STEP"]),
                "machine": str(row["TOOLGROUP"]),
                "processing_unit": str(row["PROCESSING UNIT"]),
                "processing_time": _closed_interval(float(mean), float(offset)),
            }
        )

    return routes


def get_routes_for_products(
    excel_path: Path | str = EXCEL_PATH,
) -> dict[int, list[RouteStep]]:
    return {
        product_number: get_routes_for_product(product_number, excel_path)
        for product_number in range(1, NUM_PRODUCTS + 1)
    }


def scale_processing_times(
    routes: list[RouteStep],
) -> list[ScaledRouteStep]:
    scaled_routes: list[ScaledRouteStep] = []

    for step in routes:
        multiplier = _processing_unit_multiplier(step["processing_unit"])
        scaled_routes.append(
            {
                "product": step["product"],
                "step": step["step"],
                "machine": step["machine"],
                "processing_time": _scale_interval(
                    step["processing_time"], multiplier
                ),
            }
        )

    return scaled_routes


def scale_all_product_routes(
    all_routes: dict[int, list[RouteStep]],
) -> dict[int, list[ScaledRouteStep]]:
    return {
        product_number: scale_processing_times(routes)
        for product_number, routes in all_routes.items()
    }


if __name__ == "__main__":
    routes_by_product = get_routes_for_products()
    scaled_routes_by_product = scale_all_product_routes(routes_by_product)

    for product_number in range(1, NUM_PRODUCTS + 1):
        routes = routes_by_product[product_number]
        scaled_routes = scaled_routes_by_product[product_number]
        print(f"Product {product_number}: {len(routes)} steps")
        print("  First step (raw):", routes[0])
        print("  First step (scaled):", scaled_routes[0])
