import json
import os
import logging
from datetime import datetime, timezone, timedelta

import pytz
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, before_log

logger = logging.getLogger(__name__)
MADRID = pytz.timezone("Europe/Madrid")

# Estados válidos para reservas activas (confirmed = futura, checked_in = huésped dentro)
ACTIVE_STATUSES = ["confirmed", "checked_in"]


def _date_to_utc_range(date_str: str) -> tuple[str, str]:
    """
    Convierte una fecha local Madrid (YYYY-MM-DD) a rango UTC para filtrar datetimes en la API.
    Devuelve (utc_start_iso, utc_end_iso) cubriendo todo el día en hora Madrid.
    """
    d = datetime.strptime(date_str, "%Y-%m-%d")
    madrid_start = MADRID.localize(d.replace(hour=0, minute=0, second=0, microsecond=0))
    madrid_end = MADRID.localize(d.replace(hour=23, minute=59, second=59, microsecond=999999))
    utc_start = madrid_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    utc_end = madrid_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.999Z")
    return utc_start, utc_end


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

    # Guesty v1 puede devolver el total en "guests" o desglosado en adults+children
    total_direct = reservation.get("guests") or reservation.get("guestsCount") or 0
    adults = reservation.get("adults") or reservation.get("adultsCount") or guests_details.get("adults") or 0
    children = reservation.get("children") or reservation.get("childrenCount") or guests_details.get("children") or 0
    guests = int(total_direct) if total_direct else int(adults) + int(children)

    infants = (
        reservation.get("infantsCount")
        or reservation.get("infants")
        or guests_details.get("infantsCount")
        or guests_details.get("infants")
        or 0
    )
    return int(guests), int(infants)


def _is_url(value: str) -> bool:
    """Devuelve True si el valor parece una URL."""
    return value.startswith("http://") or value.startswith("https://")


def _text_from_value(val) -> str | None:
    """
    Extrae texto legible de un valor de campo de Guesty.
    Maneja strings, dicts como {'other': 'CUNA'} y otros tipos.
    """
    if not val:
        return None
    if isinstance(val, str):
        text = val.strip()
        return None if not text or _is_url(text) else text
    if isinstance(val, dict):
        # Intentar claves comunes de texto libre
        for key in ("other", "text", "value", "content", "body"):
            candidate = val.get(key)
            if candidate and isinstance(candidate, str) and candidate.strip():
                text = candidate.strip()
                return None if _is_url(text) else text
        # Fallback: unir todos los valores de string no vacíos y no URL
        parts = [
            str(v).strip()
            for v in val.values()
            if v and isinstance(v, str) and not _is_url(str(v).strip())
        ]
        return " | ".join(parts) if parts else None
    return None


def _extract_notes(reservation: dict) -> str | None:
    """
    Extrae notas libres de la reserva entrante.
    Prioridad: customFields con contenido de texto → guestNote → notes.
    Se descartan valores que sean URLs.
    """
    # customFields: array de objetos con fieldValue
    custom_fields = reservation.get("customFields")
    if isinstance(custom_fields, list):
        for field in custom_fields:
            raw = field.get("fieldValue") or field.get("value") or field.get("text")
            text = _text_from_value(raw)
            if text:
                return text

    # Campos de texto directos, filtrando URLs
    for key in ("guestNote", "notes"):
        text = _text_from_value(reservation.get(key))
        if text:
            return text

    return None


class GuestyClient:
    BASE_URL = "https://open-api.guesty.com"

    def __init__(self):
        self.client_id = os.environ["GUESTY_CLIENT_ID"]
        self.client_secret = os.environ["GUESTY_CLIENT_SECRET"]
        self.token = None
        self.token_expiry = None
        # Autenticar una sola vez al inicializar el cliente
        self.authenticate()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=5, max=60),
        before=before_log(logger, logging.DEBUG),
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
        """Renueva el token solo si ha expirado. No usar dentro de métodos con @retry."""
        if self.token is None or datetime.now(timezone.utc) >= self.token_expiry:
            self.authenticate()

    def _get_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=3, max=30),
        before=before_log(logger, logging.DEBUG),
    )
    def _fetch_page(self, filters: list, limit: int, skip: int) -> dict:
        """
        Hace una sola llamada GET a /v1/reservations.
        NO llama a _ensure_token — el token ya fue obtenido antes de entrar al bucle.
        Si recibe 401 renueva el token y reintenta.
        """
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
        if response.status_code == 401:
            logger.warning("Token expirado (401). Renovando...")
            self.authenticate()
            raise requests.HTTPError("Token renovado, reintentando", response=response)
        logger.debug(
            "GET /v1/reservations skip=%d status=%s", skip, response.status_code
        )
        response.raise_for_status()
        return response.json()

    def _get_reservations(self, filters: list) -> list:
        """Obtiene todas las páginas de reservas para los filtros dados."""
        # El token ya está garantizado antes de llamar a este método
        all_reservations = []
        skip = 0
        limit = 100

        while True:
            data = self._fetch_page(filters, limit, skip)

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
        """
        Devuelve las reservas activas con check-out en la fecha indicada (hora Madrid).
        Incluye estado confirmed y checked_in porque el huésped ya estará dentro.
        """
        logger.info("Buscando reservas con check-out el %s...", date_str)
        utc_start, utc_end = _date_to_utc_range(date_str)
        logger.info("Rango UTC: %s → %s", utc_start, utc_end)

        self._ensure_token()
        filters = [
            {"field": "checkOut", "operator": "$gte", "value": utc_start},
            {"field": "checkOut", "operator": "$lte", "value": utc_end},
            {"field": "status", "operator": "$in", "value": ACTIVE_STATUSES},
        ]
        reservations = self._get_reservations(filters)
        logger.info("Encontradas %d reservas con check-out el %s", len(reservations), date_str)
        return reservations

    def get_reservations_by_checkin(self, date_str: str) -> list:
        """Devuelve las reservas con check-in en la fecha indicada (hora Madrid)."""
        logger.info("Buscando reservas con check-in el %s...", date_str)
        utc_start, utc_end = _date_to_utc_range(date_str)

        self._ensure_token()
        filters = [
            {"field": "checkIn", "operator": "$gte", "value": utc_start},
            {"field": "checkIn", "operator": "$lte", "value": utc_end},
            {"field": "status", "operator": "$in", "value": ACTIVE_STATUSES},
        ]
        reservations = self._get_reservations(filters)
        logger.info("Encontradas %d reservas con check-in el %s", len(reservations), date_str)
        return reservations

    def get_reservations_in_range(self, start_date: str, end_date: str) -> list:
        """Devuelve reservas con check-out entre start_date y end_date (inclusive, hora Madrid)."""
        logger.info("Buscando reservas con check-out entre %s y %s...", start_date, end_date)
        utc_start, _ = _date_to_utc_range(start_date)
        _, utc_end = _date_to_utc_range(end_date)

        self._ensure_token()
        filters = [
            {"field": "checkOut", "operator": "$gte", "value": utc_start},
            {"field": "checkOut", "operator": "$lte", "value": utc_end},
            {"field": "status", "operator": "$in", "value": ACTIVE_STATUSES},
        ]
        reservations = self._get_reservations(filters)
        logger.info("Encontradas %d reservas en el rango", len(reservations))
        return reservations

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=3, max=30),
        before=before_log(logger, logging.DEBUG),
    )
    def get_next_reservation(self, listing_id: str, after_date: str) -> dict | None:
        """
        Devuelve la siguiente reserva activa en el apartamento, a partir de after_date.
        NO llama a _ensure_token — el token ya fue obtenido antes del bucle principal.
        """
        utc_start, _ = _date_to_utc_range(after_date)

        filters = [
            {"field": "listingId", "operator": "$eq", "value": listing_id},
            {"field": "checkIn", "operator": "$gte", "value": utc_start},
            {"field": "status", "operator": "$in", "value": ACTIVE_STATUSES},
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
        if response.status_code == 401:
            logger.warning("Token expirado (401) en get_next_reservation. Renovando...")
            self.authenticate()
            raise requests.HTTPError("Token renovado, reintentando", response=response)
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
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=3, max=30),
        before=before_log(logger, logging.DEBUG),
    )
    def get_listing(self, listing_id: str) -> dict | None:
        """Devuelve los datos de un apartamento por su ID. NO llama a _ensure_token."""
        try:
            response = requests.get(
                f"{self.BASE_URL}/v1/listings/{listing_id}",
                headers=self._get_headers(),
                timeout=30,
            )
            if response.status_code == 401:
                logger.warning("Token expirado (401) en get_listing. Renovando...")
                self.authenticate()
                raise requests.HTTPError("Token renovado, reintentando", response=response)
            logger.debug("GET /v1/listings/%s status=%s", listing_id, response.status_code)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
                return data["data"]
            return data
        except requests.HTTPError:
            raise
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

        # LOG DE DIAGNÓSTICO — se eliminará una vez confirmados los campos correctos
        if checkouts:
            sample = checkouts[0]
            logger.info("=== DIAGNÓSTICO RESERVA SALIENTE ===")
            logger.info("Campos disponibles: %s", sorted(sample.keys()))
            for key in ("guests", "guestsCount", "adults", "adultsCount", "children", "childrenCount", "infants", "infantsCount", "guestsDetails"):
                if key in sample:
                    logger.info("  %s = %s", key, sample[key])

        slots = []
        for reservation in checkouts:
            listing_id = (
                reservation.get("listingId")
                or (reservation.get("listing") or {}).get("_id")
                or ""
            )

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

            checkout_dt = _parse_utc_to_madrid(
                reservation.get("checkOut") or reservation.get("plannedArrival")
            )
            checkout_time = checkout_dt.strftime("%H:%M") if checkout_dt else None
            checkout_date = checkout_dt.strftime("%Y-%m-%d") if checkout_dt else date_str

            out_guests, out_infants = _extract_guests(reservation)

            next_res = self.get_next_reservation(listing_id, date_str) if listing_id else None

            if next_res:
                # LOG DE DIAGNÓSTICO — se eliminará una vez confirmados los campos correctos
                logger.info("=== DIAGNÓSTICO RESERVA ENTRANTE (%s) ===", listing_name)
                logger.info("Campos disponibles: %s", sorted(next_res.keys()))
                for key in ("guests", "guestsCount", "adults", "adultsCount", "children", "childrenCount", "infants", "infantsCount", "guestsDetails", "notes", "guestNote", "customFields"):
                    if key in next_res:
                        logger.info("  %s = %s", key, next_res[key])

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
