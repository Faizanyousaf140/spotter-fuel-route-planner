import uuid

from django.db import models


class FuelStation(models.Model):
    """
    One truck stop from the supplied OPIS price file, after cleaning,
    de-duplication and offline geocoding (see ``build_station_dataset``).

    Coordinates are *city-level* (Census place internal point or similar):
    the source file has no coordinates and its addresses are highway/exit
    descriptions such as ``"I-44, EXIT 283 & US-69"``.
    """

    opis_id = models.PositiveIntegerField(unique=True, help_text="OPIS Truckstop ID")
    name = models.CharField(max_length=128)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2, db_index=True)
    rack_ids = models.CharField(max_length=64, help_text="Pipe-separated OPIS rack IDs")

    price = models.DecimalField(
        max_digits=8, decimal_places=5, help_text="Effective retail price, USD/gallon"
    )
    price_min = models.DecimalField(max_digits=8, decimal_places=5)
    price_max = models.DecimalField(max_digits=8, decimal_places=5)
    price_observations = models.PositiveSmallIntegerField(default=1)

    latitude = models.FloatField()
    longitude = models.FloatField()
    geocode_source = models.CharField(max_length=48)
    geocode_match = models.CharField(max_length=128, blank=True)

    class Meta:
        ordering = ["opis_id"]
        indexes = [models.Index(fields=["latitude", "longitude"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.price}"


class RoutePlan(models.Model):
    """
    A computed route + fuel plan, stored so that ``/api/v1/route/<id>/`` and the
    saved map page keep working across restarts and worker processes.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    start_query = models.CharField(max_length=255)
    finish_query = models.CharField(max_length=255)
    route_distance_miles = models.FloatField()
    total_fuel_cost = models.DecimalField(max_digits=10, decimal_places=2)
    response = models.JSONField(help_text="The full API response for this plan")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.start_query} -> {self.finish_query} (${self.total_fuel_cost})"
