"""
Load the cleaned, geocoded station file into the database (idempotent).

    python manage.py load_stations [--path data/processed/fuel_stations.csv]
"""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from route_api.models import FuelStation
from route_api.services.geo import is_valid_coordinate


class Command(BaseCommand):
    help = "Replace FuelStation rows with the contents of the processed station CSV."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path", type=Path,
            default=Path(settings.BASE_DIR) / "data" / "processed" / "fuel_stations.csv",
        )

    def handle(self, *args, **opts):
        path: Path = opts["path"]
        if not path.exists():
            raise CommandError(f"{path} not found. Run `python manage.py build_station_dataset` first.")
        stations, skipped = [], 0
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if not is_valid_coordinate(row["latitude"], row["longitude"]):
                    skipped += 1
                    continue
                stations.append(
                    FuelStation(
                        opis_id=int(row["opis_id"]),
                        name=row["name"],
                        address=row["address"],
                        city=row["city"],
                        state=row["state"],
                        rack_ids=row["rack_ids"],
                        price=Decimal(row["price"]),
                        price_min=Decimal(row["price_min"]),
                        price_max=Decimal(row["price_max"]),
                        price_observations=int(row["price_observations"]),
                        latitude=float(row["latitude"]),
                        longitude=float(row["longitude"]),
                        geocode_source=row["geocode_source"],
                        geocode_match=row["geocode_match"][:128],
                    )
                )
        if not stations:
            raise CommandError(f"{path} contains no valid stations")
        with transaction.atomic():
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(stations, batch_size=1000)
        self.stdout.write(self.style.SUCCESS(f"Loaded {len(stations)} stations ({skipped} skipped)"))
