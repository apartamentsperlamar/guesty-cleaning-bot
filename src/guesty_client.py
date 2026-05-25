import json
import os
import logging
from datetime import datetime, timezone, timedelta

import pytz
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, before_log, after_log

logger = logging.getLogger(__name__)
MADRID = pytz.timezone("Europe/Madrid")


def _parse_utc_to_madrid(dt_str: str | None):
    """Convierte una cadena ISO 8601 UTC a datetime con zona horaria Europe/Madrid."""
    if not dt_str:
        return None
    try:
        dt_str = dt_str.replace("Z", "+00:00")
        dt_utc = datetime.fromisoformat(dt_str)
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        return dt_utc.astimezone(MADRID)
    except (ValueError, TypeError):
        return None


def _extract_guests(reservation: dict) -> tuple[int, int]:
    """Extrae (huéspedes, bebés) de una reserva."""
    guests_details = reservation.get("guestsDetails") or {}
    adults = reservation.get("adults", 0) or 0
    children = reservation.get("children", 0) or 0
    guests = adults + children

    infants = (
        reservation.get("infantsCount")
        or reservation.get("infants")
        or guests_details.get("infants")
        or 0
    )
    return int(guests), int(infants)


def _extract_notes(reservation: dict) -> str | None:
    """Extrae notas de la reserva, probando campos en orden de prioridad."""
    for field in ("notes", "guestNote"):
        value = reservation.get(field)
        if value and str(value).strip():
            return str(value).strip()

    custom_fields = reservation.get("customFields")
    if isinstance(custom_fields, list):
        for field in custom_fields:
            val = field.get("fieldValue") or field.get("value") or field.get("text")
            if val and str(val).strip():
                return str(val).strip()
    elif isinstance(custom_fields, dict):
        for val in custom_fields.values():
            if val and str(val).strip():
                return str(val).strip()

    return None


class GuestyClient:
    BASE_URL = "https://open-api.guesty.com"

    def __init__(self):
        self.client_id = os.environ["GUESTY_CLIENT_ID"]
        self.client_secret = os.environ["GUESTY_CLIENT_SECRET"]
        self.token = None
        self.token_expiry = None

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        before=before_log(logger, logging.DEBUG),
        after=after_log(logger, logging.DEBUG),
    )
    def authenticate(self):
        """Obtiene un token de acceso usando client_credentials."""
        logger.info("Autenticando con Guesty Open API...")
        response = requests.post(
            f"{self.BASE_URL}/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "open-api",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        self.token = data["access_token"]
        expires_in = data.get("expires_in", 3600)
        self.token_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)
        logger.info("Autenticación exitosa. Token válido hasta: %s", self.token_expiry)

    def _ensure_token(self):
        """Garantiza que el token es válido, renovándolo si es necesario."""
        if self.token is None or datetime.now(timezone.utc) >= self.token_expiry:
            self.authenticate()

    def _get_headers(self) -> dict:
        """Devuelve las cabeceras HTTP con el token Bearer."""
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _fetch_reservations_page(self, filters: list, limit: int, skip: int) -> dict:
        """Hace una única llamada paginada a /v1/reservations y devuelve el JSON."""
        self._ensure_token()
        params = {
            "filters": json.dumps(filters),
            "limit": limit,
            "skip": skip,
        }
        response = requests.get(
            f"{self.BASE_URL}/v1/reservations",
            headers=self._get_headers(),
            params=params,
            timeout=30,
        )
        logger.debug("GET /v1/reservations status=%s url=%s", response.status_code, response.url)
        response.raise_for_status()
        return response.json()

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        before=before_log(logger, logging.DEBUG),
    )
    def _get_reservations(self, filters: list) -> list:
        """Realiza llamadas paginadas a /v1/reservations con los filtros dados."""
        all_reservations = []
        skip = 0
        limit = 100

        while True:
            data = self._fetch_reservations_page(filters, limit, skip)

            # La API v1 devuelve los resultados en distintas claves según el endpoint
            results = (
                data.get("results")
                or (data.get("data") or {}).get("results")
                or data.get("reservations")
                or []
            )
            total_count = (
                data.get("count")
                or (data.get("data") or {}).get("count")
                or data.get("total")
                or 0
            )

            all_reservations.extend(results)
            skip += len(results)

            if not results or skip >= total_count:
                break

        return all_reservations

    def get_reservations_by_checkout(self, date_str: str) -> list:
        """Devuelve las reservas confirmadas con check-out en la fecha indicada."""
        logger.info("Buscando reservas con check-out el %s...", date_str)
        filters = [
            {"field": "checkOut", "operator": "$eq", "value": date_str},
            {"field": "status", "operator": "$in", "value": ["confirmed"]},
        ]
        reservations = self._get_reservations(filters)
        logger.info("Encontradas %d reservas con check-out el %s", len(reservations), date_str)
        return reservations

    def get_reservations_by_checkin(self, date_str: str) -> list:
        """Devuelve las reservas confirmadas con check-in en la fecha indicada."""
        logger.info("Buscando reservas con check-in el %s...", date_str)
        filters = [
            {"field": "checkIn", "operator": "$eq", "value": date_str},
            {"field": "status", "operator": "$in", "value": ["confirmed"]},
        ]
        reservations = self._get_reservations(filters)
        logger.info("Encontradas %d reservas con check-in el %s", len(reservations), date_str)
        return reservations

    def get_reservations_in_range(self, start_date: str, end_date: str) -> list:
        """Devuelve reservas con check-out entre start_date y end_date (inclusive)."""
        logger.info("Buscando reservas con check-out entre %s y %s...", start_date, end_date)
        filters = [
            {"field": "checkOut", "operator": "$gte", "value": start_date},
            {"field": "checkOut", "operator": "$lte", "value": end_date},
            {"field": "status", "operator": "$in", "value": ["confirmed"]},
        ]
        reservations = self._get_reservations(filters)
        logger.info("Encontradas %d reservas en el rango", len(reservations))
        return reservations

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        before=before_log(logger, logging.DEBUG),
    )
    def get_next_reservation(self, listing_id: str, after_date: str) -> dict | None:
        """Devuelve la siguiente reserva confirmada en el apartamento, a partir de after_date."""
        self._ensure_token()
        filters = [
            {"field": "listingId", "operator": "$eq", "value": listing_id},
            {"field": "checkIn", "operator": "$gte", "value": after_date},
            {"field": "status", "operator": "$in", "value": ["confirmed"]},
        ]
        params = {
            "filters": json.dumps(filters),
            "limit": 1,
            "skip": 0,
            "sort": "checkIn asc",
        }
        response = requests.get(
            f"{self.BASE_URL}/v1/reservations",
            headers=self._get_headers(),
            params=params,
            timeout=30,
        )
        logger.debug(
            "GET /v1/reservations (next) status=%s url=%s", response.status_code, response.url
        )
        response.raise_for_status()
        data = response.json()

        results = (
            data.get("results")
            or (data.get("data") or {}).get("results")
            or data.get("reservations")
            or []
        )
        return results[0] if results else None

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        before=before_log(logger, logging.DEBUG),
    )
    def get_listing(self, listing_id: str) -> dict | None:
        """Devuelve los datos de un apartamento por su ID."""
        self._ensure_token()
        try:
            response = requests.get(
                f"{self.BASE_URL}/v1/listings/{listing_id}",
                headers=self._get_headers(),
                timeout=30,
            )
            logger.debug(
                "GET /v1/listings/%s status=%s", listing_id, response.status_code
            )
            response.raise_for_status()
            data = response.json()
            # La respuesta puede ser el objeto directo o estar anidada bajo "data"
            if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
                return data["data"]
            return data
        except Exception as e:
            logger.warning("No se pudo obtener el listing %s: %s", listing_id, e)
            return None

    def build_cleaning_slots(self, date_str: str) -> list:
        """
        Construye los turnos de limpieza para la fecha indicada.
        Combina la reserva saliente con la siguiente reserva entrante en el mismo apartamento.
        """
        logger.info("Construyendo turnos de limpieza para el %s...", date_str)
        checkouts = self.get_reservations_by_checkout(date_str)

        if not checkouts:
            logger.info("No hay check-outs el %s. Sin limpiezas.", date_str)
            return []

        slots = []
        for reservation in checkouts:
            listing_id = (
                reservation.get("listingId")
                or (reservation.get("listing") or {}).get("_id")
                or ""
            )

            # Nombre del apartamento
            listing = self.get_listing(listing_id) if listing_id else None
            if listing:
                listing_name = (
                    listing.get("nickname")
                    or listing.get("name")
                    or listing.get("title")
                    or listing_id
                )
            else:
                listing_name = listing_id or "Apartamento desconocido"

            # Datos de la reserva SALIENTE
            checkout_dt = _parse_utc_to_madrid(
                reservation.get("checkOut") or reservation.get("plannedArrival")
            )
            checkout_time = checkout_dt.strftime("%H:%M") if checkout_dt else None
            checkout_date = checkout_dt.strftime("%Y-%m-%d") if checkout_dt else date_str

            out_guests, out_infants = _extract_guests(reservation)

            # Siguiente reserva en el mismo apartamento
            next_res = self.get_next_reservation(listing_id, date_str) if listing_id else None

            if next_res:
                checkin_dt = _parse_utc_to_madrid(
                    next_res.get("checkIn") or next_res.get("plannedDeparture")
                )
                checkin_time = checkin_dt.strftime("%H:%M") if checkin_dt else None
                checkin_date = checkin_dt.strftime("%Y-%m-%d") if checkin_dt else None
                checkin_is_today = checkin_date == date_str if checkin_date else False
                in_guests, in_infants = _extract_guests(next_res)
                incoming_notes = _extract_notes(next_res)

                slot = {
                    "listing_id": listing_id,
                    "listing_name": listing_name,
                    "checkout_reservation_id": reservation.get("_id", ""),
                    "checkout_time": checkout_time,
                    "checkout_date": checkout_date,
                    "outgoing_guests": out_guests,
                    "outgoing_infants": out_infants,
                    "has_next_reservation": True,
                    "checkin_reservation_id": next_res.get("_id"),
                    "checkin_time": checkin_time,
                    "checkin_date": checkin_date,
                    "checkin_is_today": checkin_is_today,
                    "incoming_guests": in_guests,
                    "incoming_infants": in_infants,
                    "incoming_notes": incoming_notes,
                    "high_priority": checkin_is_today,
                }
            else:
                slot = {
                    "listing_id": listing_id,
                    "listing_name": listing_name,
                    "checkout_reservation_id": reservation.get("_id", ""),
                    "checkout_time": checkout_time,
                    "checkout_date": checkout_date,
                    "outgoing_guests": out_guests,
                    "outgoing_infants": out_infants,
                    "has_next_reservation": False,
                    "checkin_reservation_id": None,
                    "checkin_time": None,
                    "checkin_date": None,
                    "checkin_is_today": False,
                    "incoming_guests": None,
                    "incoming_infants": None,
                    "incoming_notes": None,
                    "high_priority": False,
                }

            logger.info(
                "Slot creado: %s | checkout %s | siguiente entrada: %s",
                listing_name,
                checkout_time,
                slot["checkin_time"] or "ninguna",
            )
            slots.append(slot)

        logger.info("Total de turnos de limpieza construidos: %d", len(slots))
        return slots
