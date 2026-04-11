"""
tools/ors_tools.py
OpenRouteService tools — geocoding (Pelias) + VROOM route optimization.

n8n equivalents:
  geocode_address()              →  GPS Pickup / GPS Delivery nodes
  optimize_route()               →  Request Open Route API → Extract Job → Merge Sequence
  optimize_route_with_retry()    →  Retry wrapper — 3 attempts, exponential backoff (spec §5.3)
"""
from dotenv import load_dotenv
load_dotenv()

import os
import time
import logging
from typing import List

log = logging.getLogger(__name__)

import requests
from langchain_core.tools import tool

ORS_BASE = "https://api.openrouteservice.org"
ORS_KEY = os.getenv("ORS_API_KEY", "")


def _ors_headers() -> dict:
    return {
        "Authorization": ORS_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json, application/geo+json",
    }


@tool
def geocode_address(address: str) -> dict:
    """
    Convert a human-readable address to GPS coordinates using ORS Geocoder (Pelias).

    Endpoint: GET https://api.openrouteservice.org/geocode/search
    Params:
        api_key  - your ORS key (query param)
        text     - address string
        size     - number of results (we use 1)

    Args:
        address: Full street address to geocode.

    Returns:
        dict with keys: address (str), latitude (float), longitude (float), confidence (float).
    """
    resp = requests.get(
        f"{ORS_BASE}/geocode/search",
        params={
            "api_key": ORS_KEY,
            "text": address,
            "size": 1,
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    if not data.get("features"):
        raise ValueError(f"No geocoding result for address: {address!r}")

    feature = data["features"][0]
    lon, lat = feature["geometry"]["coordinates"]
    label = feature["properties"].get("label", address)
    confidence = feature["properties"].get("confidence", 0.0)

    return {
        "address": label,
        "latitude": lat,
        "longitude": lon,
        "confidence": confidence,
    }
# print(geocode_address("1600 Amphitheatre Parkway, Mountain View, CA"))
# output:
#  {                                                                                      
#     "address": "1600 Amphitheatre Parkway, Mountain View, CA, USA",                      
#     "latitude": 37.422288,                                                               
#     "longitude": -122.085652,                                                            
#     "confidence": 1                                                                      
#   }        

@tool
def elevation_point(latitude: float, longitude: float) -> dict:
    """
    Get elevation (height above sea level in meters) for a single GPS coordinate
    using ORS Elevation API.

    Endpoint: POST https://api.openrouteservice.org/elevation/point
    Body:
        {
          "geometry": {
            "type": "Point",
            "coordinates": [lon, lat]
          }
        }

    Response format:
        {
          "geometry": { "coordinates": [lon, lat, elevation] },
          "attribution": "...",
          "timestamp": ...,
          "version": "..."
        }

    Args:
        latitude: Latitude coordinate.
        longitude: Longitude coordinate.

    Returns:
        dict with keys: elevation (float in meters), latitude (float), longitude (float).
    """
    resp = requests.post(
        f"{ORS_BASE}/elevation/point",
        headers=_ors_headers(),
        json={
            "format_in": "geojson",
            "geometry": {
                "type": "Point",
                "coordinates": [longitude, latitude]
            }
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    coords = data.get("geometry", {}).get("coordinates", [])
    elevation = coords[2] if len(coords) >= 3 else 0.0

    return {
        "elevation": elevation,
        "latitude": latitude,
        "longitude": longitude,
    }
# print(elevation_point(37.422288, -122.085652))
# output :{'elevation': 7, 'latitude': 37.422288, 'longitude': -122.085652}

@tool
def optimize_route(stops: List[dict]) -> dict:
    """
    Send geocoded stops to ORS /optimization (VROOM) to get the optimal
    truck pickup sequence with driving duration and distance.

    Endpoint: POST https://api.openrouteservice.org/optimization
    Body schema (VROOM):
      {
        "jobs": [
          { "id": <int>, "location": [lon, lat], "service": 300 }
        ],
        "vehicles": [
          {
            "id": 1,
            "profile": "driving-hgv",
            "start": [depot_lon, depot_lat],
            "end":   [depot_lon, depot_lat],   ← circular return
            "time_window": [28800, 64800]       ← 08:00–18:00 in seconds from midnight
          }
        ]
      }

    Args:
        stops: List of dicts, each must have:
               { stop_index, store_name, address, longitude, latitude }

    Returns:
        dict matching RouteResult schema:
          {
            total_duration_seconds: int,
            total_distance_meters: int,
            ordered_stops: [ { job_id, store_name, address, longitude, latitude,
                               arrival_time_seconds, service_duration_seconds } ]
          }
    """
    if not stops:
        raise ValueError("No stops provided to optimize_route")

    # Depot = first stop for circular routing
    depot_lon = stops[0]["longitude"]
    depot_lat = stops[0]["latitude"]

    jobs = [
        {
            "id": s["stop_index"],
            "location": [s["longitude"], s["latitude"]],
            "service": 300,          # 5 min service time per stop
            "description": s["store_name"],
            "amount": [1],           # required for VROOM capacity constraints to be enforced
        }
        for s in stops
    ]

    vehicles = [
        {
            "id": 1,
            "profile": "driving-hgv",  # heavy goods vehicle (truck)
            "start": [depot_lon, depot_lat],
            "end": [depot_lon, depot_lat],   # circular — returns to depot
            "time_window": [28800, 64800],   # 08:00–18:00
            "capacity": [100],               # arbitrary capacity units
        }
    ]

    payload = {"jobs": jobs, "vehicles": vehicles}

    resp = requests.post(
        f"{ORS_BASE}/optimization",
        headers=_ors_headers(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    # print("----------")
    # print(data)
    # print("----------")


    log.info("ORS optimization response: %s", data)

    if "error" in data:
        raise ValueError(f"ORS API error: {data['error']}")
    if not data.get("routes"):
        raise ValueError("ORS optimization returned no routes - check vehicle/job configuration")

    route = data["routes"][0]

    if "summary" not in route:
        if "duration" in route or "distance" in route:
            summary = {"duration": route.get("duration", 0), "distance": route.get("distance", 0)}
        else:
            raise ValueError(f"Route missing summary. Full response: {data}")
    else:
        summary = route["summary"]

    # Build a lookup for stop metadata
    stop_lookup = {s["stop_index"]: s for s in stops}

    ordered_stops = []
    for step in route["steps"]:
        if step["type"] != "job":
            continue
        job_id = step["job"]
        s = stop_lookup[job_id]
        ordered_stops.append({
            "job_id": job_id,
            "store_name": s["store_name"],
            "address": s["address"],
            "longitude": s["longitude"],
            "latitude": s["latitude"],
            "arrival_time_seconds": step.get("arrival", 0),
            "service_duration_seconds": step.get("service", 300),
        })

    return {
        "total_duration_seconds": summary["duration"],
        "total_distance_meters": summary["distance"],
        "ordered_stops": ordered_stops,
    }

# print(optimize_route(
#     [
#         {'stop_index': 1, 'store_name': 'Store A', 'address': '1600 Amphitheatre Parkway, Mountain View, CA', 'latitude': 37.422288, 'longitude': -122.085652},
#         {'stop_index': 2, 'store_name': 'Store B', 'address': '1 Hacker Way, Menlo Park, CA', 'latitude': 37.4847, 'longitude': -122.1477},
#         {'stop_index': 3, 'store_name': 'Store C', 'address': '2300 Traverwood Dr, Ann Arbor, MI', 'latitude': 42.3037, 'longitude': -83.7108},
#     ]
# ))
# output:
# {'total_duration_seconds': 1962, 'total_distance_meters': 0, 'ordered_stops': 
# [{'job_id': 2, 'store_name': 'Store B', 'address': '1 Hacker Way, Menlo Park, CA', 'longitude':-122.1477, 'latitude': 37.4847, 'arrival_time_seconds': 29745,'service_duration_seconds': 300},
#  {'job_id': 1, 'store_name': 'Store A', 'address': '1600 Amphitheatre Parkway, Mountain View, CA', 'longitude': -122.085652, 'latitude':37.422288, 'arrival_time_seconds': 31062, 'service_duration_seconds': 300}
#]}

# --- /pois (Points of Interest search) not req of my project.
# It returns nearby:
# fuel stations
# restaurants
# parking
# hospitals
# ATMs
# warehouses (depending on tags)
# )

# {profile} inside the url endpoints means the mode of transport:
# 1.driving-car
# 2.driving-hgv(trucks)
# 3.cycling
# 4.foot-walking


@tool
def distance_matrix(locations: List[dict], profile: str = "driving-hgv") -> dict:
    """
    Get duration and distance matrix between all pairs of locations.
    Use this AFTER optimize_route to get accurate distances for the optimized sequence.

    Endpoint: POST https://api.openrouteservice.org/v2/matrix/{profile}
    Body:
        {
          "locations": [[lon1, lat1], [lon2, lat2], ...],
          "sources": [0, 1, 2],  # optional - which locations are origins
          "destinations": [0, 1, 2]  # optional - which locations are destinations
        }

    Args:
        locations: List of dicts with 'longitude' and 'latitude' keys.
        profile: Routing profile (default: 'driving-hgv' for trucks).
                 Options: 'driving-car', 'driving-hgv', 'cycling-regular', 'foot-walking'

    Returns:
        dict with keys:
          durations: NxN matrix (seconds between each pair)
          distances: NxN matrix (meters between each pair)
    """

    if len(locations) < 2:
        return {
            "legs": [],
            "total_distance_km": 0.0,
            "total_duration_min": 0.0,
        }
    
    loc_coords = [[loc["longitude"], loc["latitude"]] for loc in locations]

    resp = requests.post(
        f"{ORS_BASE}/v2/matrix/{profile}",
        headers=_ors_headers(),
        json={
            "locations": loc_coords,
            "metrics": ["duration", "distance"],
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    durations = data.get("durations", [])
    distances = data.get("distances", [])

    legs = []
    for i in range(len(locations) - 1):
        legs.append({
            "from": locations[i].get("store_name", f"Stop {i+1}"),
            "to": locations[i + 1].get("store_name", f"Stop {i+2}"),
            "distance_km": round(distances[i][i + 1] / 1000, 2),
            "duration_min": round(durations[i][i + 1] / 60, 2),
        })

    total_distance_km = round(sum(leg["distance_km"] for leg in legs), 2)
    total_duration_min = round(sum(leg["duration_min"] for leg in legs), 2)

    return {
        "legs": legs,
        "total_distance_km": total_distance_km,
        "total_duration_min": total_duration_min,
    }

# print(distance_matrix(
#     [
#         {'store_name': 'Store A', 'address': '1600 Amphitheatre Parkway, Mountain View, CA', 'latitude': 37.422288, 'longitude': -122.085652},
#         {'store_name': 'Store B', 'address': '1 Hacker Way, Menlo Park, CA', 'latitude': 37.4847, 'longitude': -122.1477},
#         {'store_name': 'Store C', 'address': '2300 Traverwood Dr, Ann Arbor, MI', 'latitude': 42.3037, 'longitude': -83.7108},
#     ]
# ))
# output:
# {'legs': [{'from': 'Store A', 'to': 'Store B', 'distance_km': 11.37, 'duration_min': 15.75}, {'from': 'Store B', 'to': 'Store C', 'distance_km': 3838.34, 'duration_min': 3244.26}], 'total_distance_km': 3849.71, 'total_duration_min': 3260.01}

def optimize_route_with_retry(stops: List[dict], max_retries: int = 3) -> dict:
    """
    Calls optimize_route with exponential backoff on failure (spec §5.3).

    Retry schedule: 1s → 3s → 9s. After max_retries exhausted, raises
    RuntimeError("ORS_OPTIMIZATION_FAILED").

    Args:
        stops:       List of stop dicts (must have stop_index, store_name,
                     address, latitude, longitude).
        max_retries: Maximum number of attempts (default 3).

    Returns:
        RouteResult dict from optimize_route on success.

    Raises:
        RuntimeError: After all retries are exhausted.
    """
    delays = [1, 3, 9]
    last_exc: Exception = RuntimeError("No attempts made")

    for attempt, delay in enumerate(delays[:max_retries], 1):
        try:
            return optimize_route.invoke({"stops": stops})
        except Exception as exc:
            last_exc = exc
            log.warning(
                "ORS optimization attempt %d/%d failed: %s.%s",
                attempt, max_retries, exc,
                f" Retrying in {delay}s..." if attempt < max_retries else " No more retries.",
            )
            if attempt < max_retries:
                time.sleep(delay)

    raise RuntimeError(
        f"ORS_OPTIMIZATION_FAILED after {max_retries} attempts: {last_exc}"
    )

